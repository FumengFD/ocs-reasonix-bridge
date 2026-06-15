@echo off
cd /d "%~dp0"
title ocs-AI-bridge

echo ============================================
echo   ocs-AI-bridge
echo ============================================
echo.

REM Find Python (skip Windows Store stub)
set PYTHON=
for %%p in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
) do if exist "%%~p" set PYTHON=%%~p
if "%PYTHON%"=="" (
    where py >nul 2>&1 && set PYTHON=py -3
)
if "%PYTHON%"=="" (
    where python >nul 2>&1 && python --version >nul 2>&1 && set PYTHON=python
)
if not "%PYTHON%"=="" goto after_python

echo [FAIL] Python not found.
where winget >nul 2>&1 || goto no_python

echo [INFO] Installing Python via winget...
winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto no_python
echo [ OK ] Python installed
set PYTHON=
for %%p in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
) do if exist "%%~p" set PYTHON=%%~p
if "%PYTHON%"=="" where py >nul 2>&1 && set PYTHON=py -3
if not "%PYTHON%"=="" goto after_python

:no_python
echo [FAIL] Python not found.
echo Download from: https://www.python.org/downloads/
echo Then re-run start.bat
pause
exit /b 1

:after_python
echo [ OK ] Python

REM .env
if not exist ".env" goto ask_key
%PYTHON% -c "import os; from dotenv import load_dotenv; load_dotenv(); k=os.getenv('DEEPSEEK_API_KEY',''); exit(0 if k and len(k)>10 else 1)" >nul 2>&1
if errorlevel 1 goto ask_key
echo [ OK ] API key
goto after_key

:ask_key
echo.
echo Select model:
echo  1) DeepSeek V4 Flash
echo  2) DeepSeek V4 Pro
echo  3) GPT-4o (OpenAI)
echo  4) Qwen-Plus
echo  5) Qwen-Max
echo  6) Groq Llama 3.3
echo  7) Moonshot V1
echo  8) GLM-4-Flash
echo  9) GLM-4-Plus
echo Enter number (1-9, default=1):
set /p N="> "
if "%N%"=="" set N=1

if "%N%"=="1" set URL=https://api.deepseek.com&set MODEL=deepseek-v4-flash
if "%N%"=="2" set URL=https://api.deepseek.com&set MODEL=deepseek-v4-pro
if "%N%"=="3" set URL=https://api.openai.com/v1&set MODEL=gpt-4o
if "%N%"=="4" set URL=https://dashscope.aliyuncs.com/compatible-mode/v1&set MODEL=qwen-plus
if "%N%"=="5" set URL=https://dashscope.aliyuncs.com/compatible-mode/v1&set MODEL=qwen-max
if "%N%"=="6" set URL=https://api.groq.com/openai/v1&set MODEL=llama-3.3-70b-versatile
if "%N%"=="7" set URL=https://api.moonshot.cn/v1&set MODEL=moonshot-v1-auto
if "%N%"=="8" set URL=https://open.bigmodel.cn/api/paas/v4&set MODEL=glm-4-flash
if "%N%"=="9" set URL=https://open.bigmodel.cn/api/paas/v4&set MODEL=glm-4-plus

echo.
echo Selected: %MODEL%
echo Paste your API key:
set /p K="> "
if not "%K%"=="" (
    echo DEEPSEEK_API_KEY=%K%> .env
    echo DEEPSEEK_BASE_URL=%URL%>> .env
    echo DEEPSEEK_MODEL=%MODEL%>> .env
    echo [ OK ] API key saved
)
echo.

echo Install MinerU OCR for images? (y/n, ~2GB):
set /p M="> "
if /i "%M%"=="y" (
    echo [INFO] Installing MinerU OCR...
    %PYTHON% -m pip install "mineru[core]"
    if errorlevel 1 (
        echo [WARN] MinerU install failed. Run: pip install mineru[core]
    ) else (
        echo [ OK ] MinerU OCR installed
    )
)

echo.
echo Vision model (press Enter to skip):
echo Example: gpt-4o  (if same API as selected)
echo Example: deepseek-chat
set /p VM="> "
if not "%VM%"=="" (
    echo VISION_MODEL=%VM%>> .env
    echo [ OK ] Vision model set
)

echo.
:after_key

REM Cert - auto generate if missing
if exist "cert.pem" if exist "key.pem" goto certs_ok
echo [INFO] Generating HTTPS cert...
%PYTHON% -c "from cryptography import x509;from cryptography.x509.oid import NameOID;from cryptography.hazmat.primitives import hashes,serialization;from cryptography.hazmat.primitives.asymmetric import rsa;import datetime,ipaddress;key=rsa.generate_private_key(public_exponent=65537,key_size=2048);subj=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')]);cert=x509.CertificateBuilder().subject_name(subj).issuer_name(subj).public_key(key.public_key()).serial_number(1000).not_valid_before(datetime.datetime.utcnow()).not_valid_after(datetime.datetime.utcnow()+datetime.timedelta(days=365)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.IPv4Address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256());open('cert.pem','wb').write(cert.public_bytes(serialization.Encoding.PEM));open('key.pem','wb').write(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()));print('done')" >nul 2>&1
:certs_ok

if exist "cert.pem" if exist "key.pem" (
    echo [ OK ] HTTPS certs
    certutil -user -addstore Root cert.pem >nul 2>&1
    if not errorlevel 1 echo [ OK ] Cert trusted
) else (
    %PYTHON% -m pip install pyOpenSSL cryptography >nul 2>&1 || echo [WARN] Cert gen failed
)

REM Core deps
%PYTHON% -c "import starlette, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing deps...
    %PYTHON% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [FAIL] pip install failed. Try: pip install -r requirements.txt
        pause
        exit /b 1
    )
    echo [ OK ] Deps installed
) else (
    echo [ OK ] Core deps
)

REM MinerU check
%PYTHON% -c "import mineru" >nul 2>&1
if errorlevel 1 (
    echo [INFO] MinerU not installed (image OCR disabled)
) else (
    echo [ OK ] MinerU OCR
)

echo.
echo === Starting server ===
echo.

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8865.*LISTENING" 2^>nul') do (
    taskkill /PID %%a /F 2>nul
)
timeout /t 1 /nobreak >nul

%PYTHON% ocs_server.py
pause
