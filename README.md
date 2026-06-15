# OCS-AI-Server

双击 `ocs-server.exe`，浏览器自动弹出配置页面——选 AI 模型、粘贴 API Key、保存。然后把 OCS JSON 配置复制到 OCS 题库配置，进入答题页面即可自动答题。


## 支持的题型

- 单选题 / 多选题 / 判断题
- 填空题 / 连线题
- 图片题（MinerU OCR 提取公式和文字/多模态模型直接识别）

## 支持的 AI 模型

Web 配置页面可选全部 9 款模型：

DeepSeek V4 Flash/Pro · GPT-4o · Qwen-Plus/Max · Groq Llama 3.3 · Moonshot V1 · GLM-4-Flash/Plus

多模态模型自动跳过 MinerU。

## 使用

下载 [Release](https://github.com/FumengFD/OCS-AI-Server/releases) 里的 `ocs-server.exe`，双击运行。

然后：
1. 浏览器自动打开 `http://127.0.0.1:8865` → 选模型 → 粘贴 API Key → 点保存
2. 页面显示 OCS JSON 配置 → 复制
3. OCS 面板 → 通用 → 全局设置 → 题库配置 → 粘贴 JSON → 解析器选**默认** → 保存
4. 进入答题页面，自动答题开始

## OCS 配置 JSON

```json
[{
  "name": "OCS-AI",
  "url": "http://localhost:8865/search",
  "method": "post",
  "type": "GM_xmlhttpRequest",
  "contentType": "json",
  "data": {
    "question": "${title}",
    "options": "${options}",
    "type": "${type}"
  },
  "handler": "return (res)=>res.answer.allAnswer.map(i=>([res.question,i.join('#')]))"
}]
```

## 图片识别

安装 MinerU OCR 引擎（文本模型需要，多模态模型不需要）：

```bash
pip install "mineru[core]"
```

OCR 降级流程：`题目含图片URL → MinerU OCR → AI 答题`。多模态模型直接看图。

参考：[MinerU](https://github.com/opendatalab/MinerU) · [claude-code-vision-skill](https://github.com/xiincs/claude-code-vision-skill)

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `DEEPSEEK_API_KEY` | 必填 | API 密钥 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 地址 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 模型名 |
| `VISION_MODEL` | 空 | 视觉模型 |
| `ANSWER_TIMEOUT` | `60` | 超时秒数 |
| `BRIDGE_PORT` | `8865` | 端口 |

## 开发

```bash
git clone https://github.com/FumengFD/OCS-AI-Server.git
cd OCS-AI-Server
pip install -r requirements.txt
python ocs_server.py
```

打包 exe：
```bash
pip install pyinstaller
pyinstaller --onefile --name ocs-server ocs_server.py
```

Reasonix MCP 插件（可选，在 `reasonix.toml` 添加）：
```toml
[[plugins]]
name = "ocs-bridge"
type = "http"
url  = "http://localhost:8865/mcp"
```

## License

MIT
