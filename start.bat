@echo off
cd /d "%~dp0"
title ocs-AI-bridge

echo ============================================
echo   ocs-AI-bridge
echo ============================================
echo.

REM Find Python
set PYTHON=
where python >nul 2>&1 && set PYTHON=python
where py >nul 2>&1 && set PYTHON=py -3
if "%PYTHON%"=="" (
    echo [FAIL] Python not found. Install Python and add to PATH.
    pause
    exit /b 1
)
echo [ OK ] Python

REM .env
if not exist ".env" goto ask_key
%PYTHON% -c "import os; from dotenv import load_dotenv; load_dotenv(); k=os.getenv('DEEPSEEK_API_KEY',''); exit(0 if k and len(k)>10 else 1)" >nul 2>&1
if errorlevel 1 goto ask_key
echo [ OK ] API key
goto after_key

:ask_key
echo.
echo Paste your API key (right-click or Ctrl+V, then Enter):
echo Example: sk-your-key-here
set /p K="> "
if not "%K%"=="" (
    echo DEEPSEEK_API_KEY=%K%> .env
    echo [ OK ] API key saved
) else (
    echo [WARN] No key entered
)
echo.
:after_key

REM Certs - auto generate if missing
if exist "cert.pem" if exist "key.pem" goto certs_ok
echo [INFO] Generating HTTPS cert...
%PYTHON% -c "from cryptography import x509;from cryptography.x509.oid import NameOID;from cryptography.hazmat.primitives import hashes,serialization;from cryptography.hazmat.primitives.asymmetric import rsa;import datetime,ipaddress;key=rsa.generate_private_key(public_exponent=65537,key_size=2048);subj=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')]);cert=x509.CertificateBuilder().subject_name(subj).issuer_name(subj).public_key(key.public_key()).serial_number(1000).not_valid_before(datetime.datetime.utcnow()).not_valid_after(datetime.datetime.utcnow()+datetime.timedelta(days=365)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.IPv4Address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256());open('cert.pem','wb').write(cert.public_bytes(serialization.Encoding.PEM));open('key.pem','wb').write(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()));print('done')" >nul 2>&1
:certs_ok

if exist "cert.pem" if exist "key.pem" (
    echo [ OK ] HTTPS certs
    certutil -user -addstore Root cert.pem >nul 2>&1
    if not errorlevel 1 echo [ OK ] Cert trusted by system
) else (
    echo [WARN] pyOpenSSL missing. Run: pip install pyOpenSSL
)

REM Core deps
%PYTHON% -c "import starlette, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Dependencies missing. Run: pip install -r requirements.txt
    pause
    exit /b 1
)
echo [ OK ] Core deps

REM MinerU
%PYTHON% -c "import mineru" >nul 2>&1
if errorlevel 1 (
    echo [INFO] MinerU not installed - image OCR disabled
) else (
    echo [ OK ] MinerU OCR
)

echo.
echo === Starting server ===
echo.

REM Kill old processes
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8865.*LISTENING" 2^>nul') do (
    taskkill /PID %%a /F 2>nul
)
timeout /t 1 /nobreak >nul

%PYTHON% ocs_server.py
pause
