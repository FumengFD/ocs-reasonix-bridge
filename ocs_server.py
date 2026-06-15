# OCS HTTP Server — 纯 HTTP API，专供 OCS 自动答题调用
# 独立于 FastMCP，不依赖 MCP 协议，消除所有兼容性问题

import json
import os
import sys
import re
import time
import asyncio
import base64
import shutil
import subprocess
import tempfile
from io import BytesIO
from typing import Optional
from collections import OrderedDict

# Windows: 消除 ConnectionResetError 噪音（必须最顶部、在任何 asyncio 调用前）
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Windows: 强制 UTF-8 输出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import requests as http_requests
from PIL import Image
from openai import AsyncOpenAI, APIError, APIConnectionError, RateLimitError, APITimeoutError
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.middleware.cors import CORSMiddleware
import uvicorn

# -- 配置 -------------------------------------------------------

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8865"))
ANSWER_TIMEOUT = int(os.getenv("ANSWER_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
CACHE_SIZE = int(os.getenv("CACHE_SIZE", "500"))
MAX_QUESTION_LEN = int(os.getenv("MAX_QUESTION_LEN", "2000"))  # 题目最大字符数

# -- 统计 -------------------------------------------------------

stats = {
    "total_requests": 0, "cache_hits": 0, "ai_success": 0,
    "ai_errors": 0, "start_time": time.time(),
    "last_error": None, "last_error_time": None,
}

# -- LRU 缓存 -------------------------------------------------

class AnswerCache:
    def __init__(self, maxsize=500):
        self._cache: OrderedDict[str, list[str]] = OrderedDict()
        self.maxsize = maxsize

    def _key(self, q: str, t: str) -> str:
        return f"{t}:{q.strip()}"

    def get(self, q: str, t: str) -> Optional[list[str]]:
        k = self._key(q, t)
        if k in self._cache:
            self._cache.move_to_end(k)
            return self._cache[k]
        return None

    def set(self, q: str, t: str, a: list[str]):
        k = self._key(q, t)
        if k in self._cache:
            self._cache.move_to_end(k)
        else:
            self._cache[k] = a
            while len(self._cache) > self.maxsize:
                self._cache.popitem(last=False)

    def __len__(self): return len(self._cache)


cache = AnswerCache(maxsize=CACHE_SIZE)

# -- AI -------------------------------------------------------

ai: Optional[AsyncOpenAI] = None

def get_ai():
    global ai
    if ai is None:
        if not DEEPSEEK_API_KEY:
            raise RuntimeError("DEEPSEEK_API_KEY not set")
        ai = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=ANSWER_TIMEOUT, max_retries=0)
    return ai


SYSTEM_PROMPT = """你是一个专业的网课答题助手。请根据题目和选项，给出正确答案。

规则：
1. 单选题: 只返回一个正确选项的字母编号，如 "A"。不要返回选项文字，只返回字母！
2. 多选题: 返回所有正确选项的字母编号连在一起，如 "ABD"。不要返回选项文字，只返回字母！
3. 判断题: 只返回 "正确" 或 "错误"。
4. 填空题: 如果空后有括号内含选项如（早/晚），从中选一个填入。如果题目有 A. B. C. 选项标记，返回字母编号。否则返回文字。多个空用 # 分隔。
5. 连线题: 左边每一项匹配右边一项。返回右边项的字母编号（如 A#C#B），多个用 # 分隔。不要返回数字或文字！
6. 图片题: 根据图片OCR文字推断答案。
7. 填空题绝不允许返回空答案。无法确定时，根据上下文推断最合理的答案填入。"""


def build_prompt(question: str, options: list[str], qtype: str) -> str:
    labels = [chr(65 + i) for i in range(len(options))] if options else []
    if options and qtype in ("single", "multiple"):
        opts_text = "\n".join(f"{labels[i]}. {options[i]}" for i in range(len(options)))
        hint = {"single": "单选题，只返回一个字母（如 A）。",
                "multiple": "多选题，返回所有正确选项的字母连在一起（如 ABD）。"}.get(qtype, "")
        return f"题目：{question}\n\n选项：\n{opts_text}\n\n{hint}"
    elif qtype == "judgement":
        return f"题目：{question}\n\n判断题，只返回'正确'或'错误'。"
    elif qtype == "completion":
        # 检测空位
        blank_count = len(re.findall(r'_{2,}|（）|\(\)', question))
        if blank_count == 0:
            blank_count = 1
        
        # 检测内嵌选项: (A)xxx (B)xxx 或 （较晚/同一/较早）
        has_letter_opts = bool(re.search(r'[（(][A-Ha-h][）)]', question))
        embedded_opts = re.findall(r'[（(]([^）)]+)[）)]', question)
        
        if has_letter_opts:
            hint = (
                f"填空题（共{blank_count}个空）。题目包含 A-H 选项。\n"
                f"请从选项中为每个空选择正确字母。返回格式如 C#F#D#G。\n"
                f"只返回字母，不要解释。"
            )
        elif embedded_opts:
            opt_texts = [o for o in embedded_opts if '/' in o and len(o) < 20]
            hint = (
                f"填空题（共{blank_count}个空）。\n"
                f"每个空从对应括号选项中选择：{', '.join(opt_texts[:4])}\n"
                f"多个空用 # 分隔（如 较早#下游）。只返回答案。"
            )
        else:
            hint = (
                f"填空题（共{blank_count}个空）。\n"
                f"如果有 A. B. C. 选项标记，返回字母编号（如 A#B#C）。\n"
                f"普通填空返回文字，多个空用 # 分隔。只返回答案。"
            )
        return f"题目：{question}\n\n{hint}"
    elif qtype == "line":
        return f"题目：{question}\n\n连线题。左边每一项匹配右边一项。\n返回右边项的字母编号（A、B、C...），多个用 # 分隔。\n如左边三项分别匹配右边第 C、A、B 项，返回：C#A#B\n只返回字母，不要数字或文字！"
    return f"题目：{question}\n\n请直接给出答案。"


def parse_answer(raw: str, qtype: str, n_opts: int) -> list[str]:
    """解析 AI 返回的答案"""
    raw = raw.strip()

    if qtype == "single":
        # 提取字母
        m = re.search(r'[A-Za-z]', raw)
        if m:
            letter = m.group().upper()
            # 验证字母在选项范围内
            idx = ord(letter) - 65
            if 0 <= idx < max(n_opts, 1):
                return [letter]
        # 无有效字母 → 默认选 A
        return ["A"] if n_opts > 0 else [raw or "A"]

    elif qtype == "multiple":
        # 提取所有字母
        letters = re.findall(r'[A-Za-z]', raw)
        if letters:
            # 去重并排序，限制在选项范围内
            seen = set()
            result = []
            for l in letters:
                ul = l.upper()
                idx = ord(ul) - 65
                if 0 <= idx < n_opts and ul not in seen:
                    seen.add(ul)
                    result.append(ul)
            if result:
                return [''.join(sorted(result))]
        # 无有效字母 → 全选
        if n_opts > 0:
            return [''.join(chr(65+i) for i in range(n_opts))]
        return [raw or "A"]

    elif qtype == "judgement":
        for w in ['正确','对','是','T','t','True','true','1','√']:
            if w in raw: return ['正确']
        for w in ['错误','错','否','F','f','False','false','0','×','X']:
            if w in raw: return ['错误']
        return ['正确']  # 默认选正确

    elif qtype == "completion":
        parts = [p.strip() for p in re.split(r'#|\n', raw) if p.strip()]
        return parts if parts else ["未知"]

    elif qtype == "line":
        # 连线题：提取字母
        letters = re.findall(r'[A-Za-z]', raw)
        if letters:
            return [l.upper() for l in letters]
        return ["A"]

    return [raw or "A"]


async def ai_answer(question: str, options: list[str], qtype: str, images: list[bytes] | None = None) -> list[str]:
    client = get_ai()
    prompt = build_prompt(question, options, qtype)

    # 多模态模型 + 有图片 → 直接让 AI 看图
    if VISION_CAPABLE and images:
        return await _vision_answer(client, prompt, images, qtype, len(options) if options else 0)

    # 文本模型 → 纯文字 prompt
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":prompt}],
                    temperature=0.1, max_tokens=1024,
                ), timeout=ANSWER_TIMEOUT)
            return parse_answer(resp.choices[0].message.content.strip(), qtype, len(options) if options else 0)
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep((attempt + 1) * 2)
            else:
                raise RuntimeError(f"API连接失败(重试{MAX_RETRIES}次): {e}")
        except APIError as e:
            raise RuntimeError(f"API错误: {e}")
        except asyncio.TimeoutError:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(3)
            else:
                raise RuntimeError(f"答题超时({ANSWER_TIMEOUT}s)")


async def _vision_answer(client, prompt: str, images: list[bytes], qtype: str, n_opts: int) -> list[str]:
    """多模态模型直接看图答题"""
    content = [{"type": "text", "text": prompt}]
    for img_bytes in images[:4]:  # 最多4张
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=VISION_MODEL,
                    messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":content}],
                    temperature=0.1, max_tokens=1024,
                ), timeout=ANSWER_TIMEOUT)
            return parse_answer(resp.choices[0].message.content.strip(), qtype, n_opts)
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            if attempt < MAX_RETRIES:
                await asyncio.sleep((attempt + 1) * 2)
            else:
                raise RuntimeError(f"Vision API连接失败: {e}")
        except APIError as e:
            if attempt == 0 and "vision" in str(e).lower():
                print(f"[{time.strftime('%H:%M:%S')}] Vision model not supported, fallback to text", file=sys.stderr, flush=True)
                # 不支持视觉 → 回退到文本模型
                return await ai_answer(prompt, [], qtype, None)
            raise RuntimeError(f"Vision API错误: {e}")


# -- 图片处理 -----------------------------------------------

VISION_MODEL = os.getenv("VISION_MODEL", "")
VISION_CAPABLE = False  # 自动检测结果，启动时确定

# 1x1 透明 PNG，用于测试模型是否支持视觉
_TEST_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


async def detect_vision_capability():
    """启动时自动检测模型是否支持多模态"""
    global VISION_CAPABLE, VISION_MODEL
    if not DEEPSEEK_API_KEY:
        return

    # 已知多模态模型 → 直接跳过检测
    KNOWN_VISION = {"gpt-4o", "gpt-4-turbo", "gpt-4.1", "claude-3", "claude-3.5",
                    "claude-3.7", "claude-4", "gemini", "qwen-plus", "qwen-max",
                    "qwen-vl", "qwq", "glm-4v", "glm-4-plus", "glm-4.5", "moonshot-v1-auto"}
    if any(k in DEEPSEEK_MODEL.lower() for k in KNOWN_VISION):
        VISION_CAPABLE = True
        VISION_MODEL = VISION_MODEL or DEEPSEEK_MODEL
        print(f"[init] {DEEPSEEK_MODEL} is vision-capable (known multimodal model)", file=sys.stderr, flush=True)
        return

    # 用户已手动指定 → 直接启用
    if VISION_MODEL:
        VISION_CAPABLE = True
        print(f"[init] Vision model: {VISION_MODEL} (user configured)", file=sys.stderr, flush=True)
        return
    print(f"[init] Testing if {DEEPSEEK_MODEL} supports vision...", file=sys.stderr, flush=True)
    try:
        client = get_ai()
        b64 = base64.b64encode(_TEST_IMAGE).decode("utf-8")
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "Say 'yes' if you can see this image."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                ]}],
                max_tokens=5, temperature=0.0,
            ), timeout=10
        )
        VISION_CAPABLE = True
        VISION_MODEL = DEEPSEEK_MODEL
        print(f"[init] {DEEPSEEK_MODEL} IS vision-capable — images will be sent directly", file=sys.stderr, flush=True)
    except Exception as e:
        VISION_CAPABLE = False
        print(f"[init] {DEEPSEEK_MODEL} is text-only (will use MinerU/Vision API for images)", file=sys.stderr, flush=True)
MINERU_PATH = os.getenv("MINERU_PATH", "mineru")
MINERU_API_URL = os.getenv("MINERU_API_URL", "")  # 由桥接自动管理
MINERU_API_PROC = None  # mineru-api 子进程句柄


async def ensure_mineru_api():
    """确保 mineru-api 服务在运行，没有则启动"""
    global MINERU_API_URL, MINERU_API_PROC
    if MINERU_API_URL:
        # 检查是否还活着
        try:
            resp = await asyncio.to_thread(http_requests.get, f"{MINERU_API_URL}/docs", timeout=2)
            if resp.status_code == 200:
                return
        except Exception:
            pass

    # 尝试启动 mineru-api
    try:
        MINERU_API_PROC = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mineru.cli.fast_api",
            "--host", "127.0.0.1", "--port", "8888",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # 等待启动
        for _ in range(15):
            await asyncio.sleep(1)
            try:
                resp = await asyncio.to_thread(http_requests.get, "http://127.0.0.1:8888/docs", timeout=2)
                if resp.status_code == 200:
                    MINERU_API_URL = "http://127.0.0.1:8888"
                    print(f"[{time.strftime('%H:%M:%S')}] MinerU API started on :8888", file=sys.stderr, flush=True)
                    return
            except Exception:
                continue
    except Exception:
        pass
    # 启动失败，保持空——后续用 CLI 模式（较慢）


MAX_IMAGE_SIZE = int(os.getenv("MAX_IMAGE_SIZE", str(5 * 1024 * 1024)))  # 5MB 默认

async def extract_text_from_image(image_url: str) -> str:
    """从图片 URL 提取文字。优先 MinerU，失败则 vision API。处理完自动释放内存。"""
    if image_url.startswith("data:"):
        return await _extract_from_data_url(image_url)

    img_data = None
    try:
        resp = await asyncio.to_thread(
            http_requests.get, image_url, timeout=10,
            headers={"User-Agent": "OCS-Bridge/1.0"}
        )
        if resp.status_code != 200:
            return f"[图片下载失败: HTTP {resp.status_code}]"
        img_data = resp.content
        if len(img_data) > MAX_IMAGE_SIZE:
            return f"[图片过大: {len(img_data)} bytes]"
    except Exception as e:
        return f"[图片下载失败: {e}]"

    if not img_data:
        return ""

    # 优先 MinerU
    text = await _try_mineru(img_data)
    if text and len(text.strip()) > 3:
        return text
    # MinerU 失败 → 尝试 Vision API
    text = await _try_vision(img_data)
    # 释放内存
    del img_data
    return text or "[图片文字无法识别]"


async def _extract_from_data_url(data_url: str) -> str:
    try:
        header, encoded = data_url.split(",", 1)
        img_data = base64.b64decode(encoded)
        text = await _try_mineru(img_data)
        if text and len(text.strip()) > 3:
            return text
        text = await _try_vision(img_data)
        return text or "[图片文字无法识别]"
    except Exception:
        return "[图片解析失败]"


async def _try_mineru(img_data: bytes) -> str:
    """通过 MinerU API 服务提取图片文字。自动清理临时文件。"""
    tmp_img = None
    tmp_out = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(img_data)
            tmp_img = f.name
        tmp_out = tempfile.mkdtemp(prefix="mineru_")

        # 优先使用持久 API 服务（快），否则启动临时实例
        cmd = [MINERU_PATH, "-p", tmp_img, "-o", tmp_out, "-m", "auto", "-l", "ch"]
        if MINERU_API_URL:
            cmd.extend(["--api-url", MINERU_API_URL])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

        text = ""
        if os.path.isdir(tmp_out):
            for root, dirs, files in os.walk(tmp_out):
                for fname in files:
                    if fname.endswith(".md"):
                        with open(os.path.join(root, fname), "r", encoding="utf-8") as mf:
                            text += mf.read() + "\n"
        return text.strip()[:3000]
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
    finally:
        # 清理临时文件
        if tmp_img and os.path.exists(tmp_img):
            try: os.unlink(tmp_img)
            except Exception: pass
        if tmp_out and os.path.isdir(tmp_out):
            try: shutil.rmtree(tmp_out, ignore_errors=True)
            except Exception: pass


async def _try_vision(img_data: bytes) -> str:
    if not VISION_MODEL or not DEEPSEEK_API_KEY:
        return ""
    try:
        img_base64 = base64.b64encode(img_data).decode("utf-8")
        client = get_ai()
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "请提取图片中的所有文字。只返回文字，不要解释。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_base64}"}}
                ]}],
                max_tokens=1024, temperature=0.0,
            ), timeout=20
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return ""


async def process_images(images: list[str]) -> str:
    texts = []
    for url in images:
        text = await extract_text_from_image(url)
        if text:
            texts.append(text)
    return "\n".join(texts)


async def get_answer_with_images(question: str, options: list[str], qtype: str, images: list[bytes]) -> tuple[list[str], bool]:
    """带图片的答题（多模态模型直接看图）"""
    stats["total_requests"] += 1
    try:
        ans = await ai_answer(question, options, qtype, images)
        stats["ai_success"] += 1
        cache.set(question, qtype, ans)
        return ans, False
    except Exception as e:
        stats["ai_errors"] += 1
        stats["last_error"] = str(e)
        raise


async def get_answer(question: str, options: list[str], qtype: str) -> tuple[list[str], bool]:
    cached = cache.get(question, qtype)
    if cached is not None:
        stats["total_requests"] += 1
        stats["cache_hits"] += 1
        return cached, True
    stats["total_requests"] += 1
    try:
        ans = await ai_answer(question, options, qtype)
        stats["ai_success"] += 1
        cache.set(question, qtype, ans)
        return ans, False
    except Exception as e:
        stats["ai_errors"] += 1
        stats["last_error"] = str(e)
        stats["last_error_time"] = time.time()
        raise


# -- 类型映射 -----------------------------------------------

TYPE_MAP = {
    "0": "single", "1": "multiple", "2": "completion",
    "3": "judgement", "4": "completion",
    "5": "completion", "6": "completion", "7": "completion",
    "8": "completion", "9": "completion", "10": "completion",
    "11": "line", "14": "completion", "15": "completion",
    # 字符串形式（自定义 config 直接传）
    "single": "single", "multiple": "multiple",
    "judgement": "judgement", "completion": "completion",
    "line": "line",
}

def map_type(raw) -> str:
    return TYPE_MAP.get(str(raw), str(raw) if raw else "single")


# -- HTTP Handlers ------------------------------------------

async def health(request):
    return JSONResponse({
        "status": "ok" if DEEPSEEK_API_KEY else "no_key",
        "model": DEEPSEEK_MODEL,
        "uptime": round(time.time() - stats["start_time"]),
        "cache": len(cache),
    })

async def stats_handler(request):
    total = stats["total_requests"]
    hr = (stats["cache_hits"] / total * 100) if total > 0 else 0
    return JSONResponse({
        "uptime": round(time.time() - stats["start_time"]),
        "requests": total, "cache_hits": stats["cache_hits"],
        "cache_size": len(cache), "hit_rate": f"{hr:.1f}%",
        "ai_ok": stats["ai_success"], "ai_err": stats["ai_errors"],
        "last_error": stats["last_error"],
    })

async def search(request):
    """TikuAdapter 兼容 + 简化接口"""
    # 解析请求
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"code": -1, "msg": "JSON解析失败"}, status_code=400)

    question = (body.get("question") or "").strip()
    # 截断过长题目，防止超时
    if len(question) > MAX_QUESTION_LEN:
        question = question[:MAX_QUESTION_LEN] + "..."
    options = body.get("options")
    qtype = map_type(body.get("type", "single"))

    # 规范化 options
    if isinstance(options, str):
        options = [o.strip() for o in options.split("\n") if o.strip()]
    if not options:
        options = []

    if not question:
        return JSONResponse({"code": -1, "msg": "question为空"}, status_code=400)

    # -- 图片处理：从题目和选项中提取 URL 并识别文字 --
    url_pattern = re.compile(r'https?://[^\s,;，；]+')
    all_urls = url_pattern.findall(question)
    for opt in options:
        all_urls.extend(url_pattern.findall(opt))

    if all_urls:
        if VISION_CAPABLE:
            # 多模态模型 → 直接下载图片发给 AI 看，跳过 MinerU
            print(f"[{time.strftime('%H:%M:%S')}] IMG: {len(all_urls)} image(s), sending to {VISION_MODEL} directly...", file=sys.stderr, flush=True)
            img_bytes_list = []
            for url in all_urls[:6]:
                try:
                    resp = await asyncio.to_thread(http_requests.get, url, timeout=10, headers={"User-Agent": "OCS-Bridge/1.0"})
                    if resp.status_code == 200 and len(resp.content) < MAX_IMAGE_SIZE:
                        img_bytes_list.append(resp.content)
                except Exception:
                    pass
            if img_bytes_list:
                answers, from_cache = await get_answer_with_images(question, options, qtype, img_bytes_list)
                print(f"[{time.strftime('%H:%M:%S')}] Q: {question[:50]} | A: {answers} | vision", file=sys.stderr, flush=True)
                return JSONResponse({"code": 1, "question": question, "answer": {"allAnswer": [answers]}, "msg": "success"})
        # 文本模型 → MinerU OCR 提取文字
        print(f"[{time.strftime('%H:%M:%S')}] IMG: {len(all_urls)} image(s) found, extracting...", file=sys.stderr, flush=True)
        img_texts = []
        for url in all_urls[:6]:
            try:
                txt = await extract_text_from_image(url)
                if txt and not txt.startswith("[图片") and len(txt.strip()) >= 1:
                    img_texts.append(f"[图片文字]: {txt}")
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] IMG: failed {url[:60]} - {e}", file=sys.stderr, flush=True)
        if img_texts:
            question = question + "\n\n" + "\n".join(img_texts)

    # 答题
    try:
        answers, from_cache = await get_answer(question, options, qtype)
        print(f"[{time.strftime('%H:%M:%S')}] Q: {question[:50]} | A: {answers} | cache={from_cache}", file=sys.stderr, flush=True)
        return JSONResponse({
            "code": 1,
            "question": question,
            "answer": {"allAnswer": [answers]},
            "msg": "success",
        })
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] ERROR: {e}", file=sys.stderr, flush=True)
        return JSONResponse({
            "code": -1,
            "question": question,
            "answer": {"allAnswer": []},
            "msg": str(e),
        })


async def save_config(request):
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
        m = body.get("model", "deepseek-v4-flash")
        u = body.get("base_url", "https://api.deepseek.com")
        k = body.get("key", "")
        v = body.get("vision", "")
        if not k:
            return JSONResponse({"ok": False, "error": "Key required"})
        lines = ["DEEPSEEK_API_KEY=" + k, "DEEPSEEK_BASE_URL=" + u, "DEEPSEEK_MODEL=" + m]
        if v:
            lines.append("VISION_MODEL=" + v)
        with open(".env", "w") as ef:
            ef.write("\n".join(lines) + "\n")
        os.environ["DEEPSEEK_API_KEY"] = k
        os.environ["DEEPSEEK_BASE_URL"] = u
        os.environ["DEEPSEEK_MODEL"] = m
        global DEEPSEEK_MODEL, DEEPSEEK_BASE_URL, DEEPSEEK_API_KEY, ai
        DEEPSEEK_MODEL = m
        DEEPSEEK_BASE_URL = u
        DEEPSEEK_API_KEY = k
        ai = None  # 重置客户端，下次使用新 key
        if v:
            os.environ["VISION_MODEL"] = v
            global VISION_MODEL
            VISION_MODEL = v
        # 生成证书（如没有）
        if not os.path.exists("cert.pem") or not os.path.exists("key.pem"):
            try:
                from cryptography import x509; from cryptography.x509.oid import NameOID
                from cryptography.hazmat.primitives import hashes, serialization
                from cryptography.hazmat.primitives.asymmetric import rsa
                import datetime, ipaddress
                key = rsa.generate_private_key(65537, 2048)
                subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
                cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj)
                    .public_key(key.public_key()).serial_number(1000)
                    .not_valid_before(datetime.datetime.utcnow())
                    .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
                    .add_extension(x509.SubjectAlternativeName([
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]), critical=False)
                    .sign(key, hashes.SHA256()))
                with open("cert.pem", "wb") as f: f.write(cert.public_bytes(serialization.Encoding.PEM))
                with open("key.pem", "wb") as f: f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
                subprocess.run(["certutil", "-user", "-addstore", "Root", "cert.pem"], capture_output=True, shell=True)
            except Exception: pass
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

async def api_status(request):
    from starlette.responses import JSONResponse
    return JSONResponse({
        "configured": bool(os.getenv("DEEPSEEK_API_KEY", "")),
        "model": os.getenv("DEEPSEEK_MODEL", ""),
        "uptime": round(time.time() - stats["start_time"]),
    })

CONFIG_PAGE = '<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><title>OCS-AI-Server</title>' + \
'<style>*{margin:0;padding:0}body{font-family:sans-serif;background:#f5f5f5;padding:20px;' + \
'max-width:600px;margin:0 auto}.card{background:#fff;border-radius:8px;padding:24px;margin-top:16px}' + \
'h1{font-size:20px;margin:0}h2{font-size:14px;color:#666;font-weight:400}' + \
'label{display:block;margin:12px 0 4px}select,input{width:100%;padding:8px;border:1px solid #ddd;' + \
'border-radius:6px;font-size:14px;box-sizing:border-box}button{background:#1677ff;color:#fff;' + \
'border:none;border-radius:6px;padding:10px 20px;font-size:14px;cursor:pointer;margin-top:16px}' + \
'.msg{padding:10px;border-radius:6px;margin-top:12px;display:none}.ok{background:#f6ffed;color:#389e0d}' + \
'.err{background:#fff2f0;color:#cf1322}</style></head><body><div class="card">' + \
'<h1>OCS-AI-Server</h1><h2>选择 AI 模型并输入 API Key</h2>' + \
'<label>AI 模型</label><select id="model">' + \
'<option value="deepseek-v4-flash|https://api.deepseek.com">DeepSeek V4 Flash</option>' + \
'<option value="deepseek-v4-pro|https://api.deepseek.com">DeepSeek V4 Pro</option>' + \
'<option value="gpt-4o|https://api.openai.com/v1">GPT-4o (OpenAI)</option>' + \
'<option value="qwen-plus|https://dashscope.aliyuncs.com/compatible-mode/v1">Qwen-Plus（通义千问）</option>' + \
'<option value="qwen-max|https://dashscope.aliyuncs.com/compatible-mode/v1">Qwen-Max</option>' + \
'<option value="llama-3.3-70b-versatile|https://api.groq.com/openai/v1">Groq Llama 3.3</option>' + \
'<option value="moonshot-v1-auto|https://api.moonshot.cn/v1">Moonshot V1（月之暗面）</option>' + \
'<option value="glm-4-flash|https://open.bigmodel.cn/api/paas/v4">GLM-4-Flash（智谱）</option>' + \
'<option value="glm-4-plus|https://open.bigmodel.cn/api/paas/v4">GLM-4-Plus（智谱）</option></select>' + \
'<label>API Key</label><input type="password" id="key" placeholder="粘贴 API Key">' + \
'<label>视觉模型（可选）</label><input type="text" id="vision" placeholder="如 gpt-4o，留空则不用">' + \
'<button onclick="save()">保存配置</button><div id="msg" class="msg"></div>' + \
'<script>async function save(){' + \
'var m=document.getElementById("model").value.split("|");' + \
'var k=document.getElementById("key").value.trim();' + \
'var v=document.getElementById("vision").value.trim();' + \
'if(!k){var e=document.getElementById("msg");e.innerHTML="请输入 API Key";' + \
'e.className="msg err";e.style.display="block";return}' + \
'try{var r=await fetch("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},' + \
'body:JSON.stringify({model:m[0],base_url:m[1],key:k,vision:v})});' + \
'var d=await r.json();var e=document.getElementById("msg");' + \
'if(d.ok){e.innerHTML="已保存！";e.className="msg ok";e.style.display="block";' + \
'setTimeout(function(){location.reload()},1000)}' + \
'else{e.innerHTML=d.error;e.className="msg err";e.style.display="block"}}' + \
'catch(ex){e.innerHTML="错误："+ex.message;e.className="msg err";e.style.display="block"}}' + \
'</script></body></html>'

CONFIG_HOME = '<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><title>OCS-AI-Server</title>' + \
'<style>*{margin:0;padding:0}body{font-family:sans-serif;background:#f5f5f5;padding:20px;max-width:700px;margin:0 auto}' + \
'.card{background:#fff;border-radius:8px;padding:24px;margin-top:16px}' + \
'h1{font-size:20px}h2{font-size:14px;color:#666;font-weight:400;margin:4px 0 16px}' + \
'h3{font-size:15px;margin:16px 0 8px}pre{background:#f6f8fa;padding:14px;border-radius:6px;font-size:13px;overflow-x:auto;margin:8px 0;white-space:pre-wrap}' + \
'.ok{background:#f6ffed;color:#389e0d;padding:12px 16px;border-radius:6px;margin:8px 0;border:1px solid #b7eb8f}' + \
'.step{background:#f0f5ff;border-left:3px solid #1677ff;padding:10px 14px;margin:8px 0;border-radius:0 6px 6px 0;font-size:14px}' + \
'a{color:#1677ff}</style></head><body><div class="card">' + \
'<h1>OCS-AI-Server</h1>' + \
'<h2>配置完成！在 OCS 中粘贴以下 JSON：</h2>' + \
'<div class="ok">当前模型：<strong>' + DEEPSEEK_MODEL + '</strong></div>' + \
'<pre>[{ "name": "OCS-AI", "url": "http://localhost:8865/search", "method": "post", "type": "GM_xmlhttpRequest", "contentType": "json", "data": { "question": "${title}", "options": "${options}", "type": "${type}" }, "handler": "return (res)=>res.answer.allAnswer.map(i=>([res.question,i.join(\'#\')]))" }]</pre>' + \
'<div class="step">1. 安装 <a href="https://docs.ocsjs.com/docs/script" target="_blank">ScriptCat + OCS 脚本</a></div>' + \
'<div class="step">2. OCS 面板 → 通用 → 全局设置 → 题库配置 → 粘贴上方 JSON</div>' + \
'<div class="step">3. 解析器选 <strong>默认</strong> → 保存 → 进入答题页面</div>' + \
'<div style="margin-top:16px"><a href="/setup">修改模型/Key</a></div>' + \
'</div></body></html>'

async def config_page(request):
    from starlette.responses import HTMLResponse
    return HTMLResponse(CONFIG_PAGE)

async def config_page_route(request):
    from starlette.responses import HTMLResponse
    if os.path.exists(".env") and os.getenv("DEEPSEEK_API_KEY", ""):
        return HTMLResponse(CONFIG_HOME)
    return HTMLResponse(CONFIG_PAGE)

# -- App ----------------------------------------------------

routes = [
    Route("/health", health, methods=["GET"]),
    Route("/stats", stats_handler, methods=["GET"]),
    Route("/adapter-service/search", search, methods=["POST", "OPTIONS"]),
    Route("/api/search", search, methods=["POST", "OPTIONS"]),
    Route("/search", search, methods=["POST", "OPTIONS"]),
    Route("/", config_page_route, methods=["GET"]),
    Route("/setup", config_page, methods=["GET"]),
    Route("/api/save", save_config, methods=["POST"]),
    Route("/api/status", api_status, methods=["GET"]),
]

app = Starlette(routes=routes)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# -- 交互式配置（首次运行） ---------------------




if __name__ == "__main__":
    # 每次启动打开浏览器
    import webbrowser
    try:
        webbrowser.open("http://127.0.0.1:" + os.getenv("BRIDGE_PORT", "8865") + "/")
    except Exception:
        pass

    import os as _os
    import sys as _sys
    # PyInstaller 打包后 __file__ 指向临时目录，用 executable 替代
    if getattr(_sys, 'frozen', False):
        cert_dir = _os.path.dirname(_os.path.abspath(_sys.executable))
    else:
        cert_dir = _os.path.dirname(_os.path.abspath(__file__))
    cert_file = _os.path.join(cert_dir, "cert.pem")
    key_file = _os.path.join(cert_dir, "key.pem")
    use_ssl = _os.path.exists(cert_file) and _os.path.exists(key_file)

    print(f"OCS Bridge HTTP Server", file=sys.stderr)
    if use_ssl:
        print(f"  https://localhost:{BRIDGE_PORT}/health", file=sys.stderr)
        print(f"  https://localhost:{BRIDGE_PORT}/adapter-service/search", file=sys.stderr)
        # 自动信任证书
        try:
            subprocess.run(["certutil", "-f", "-user", "-addstore", "Root", cert_file],
                          capture_output=True)
            print(f"  Cert trusted by system", file=sys.stderr)
        except Exception:
            pass
    else:
        print(f"  http://localhost:{BRIDGE_PORT}/health", file=sys.stderr)
        print(f"  http://localhost:{BRIDGE_PORT}/adapter-service/search", file=sys.stderr)
    print(f"  model: {DEEPSEEK_MODEL} | retries: {MAX_RETRIES} | timeout: {ANSWER_TIMEOUT}s", file=sys.stderr)

    # 自动检测模型多模态能力
    asyncio.run(detect_vision_capability())

    # 尝试后台启动 MinerU API
    asyncio.run(ensure_mineru_api())

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=BRIDGE_PORT,
        ssl_keyfile=key_file if use_ssl else None,
        ssl_certfile=cert_file if use_ssl else None,
        log_level="warning"
    )
