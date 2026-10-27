@echo off
chcp 65001 >nul
title Biblioteca Pessoal
cd /d "%~dp0"

set PY=python
%PY% --version >nul 2>&1
if errorlevel 1 set PY=py

echo Verificando dependencias locais...
%PY% -c "import flask, openpyxl, waitress" >nul 2>&1
if errorlevel 1 (
  echo Instalando dependencias pela primeira vez...
  %PY% -m pip install -q -r requirements.txt
)
if errorlevel 1 (
  echo.
  echo ERRO: nao consegui instalar. Verifique se o Python esta instalado.
  echo Baixe em python.org e marque a opcao "Add python.exe to PATH".
  pause
  exit /b 1
)

rem Procura o Chrome nos locais padrao
set CHROME=
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

echo.
echo Abrindo a Biblioteca no Chrome...
echo Para encerrar, feche esta janela.
choice /m "Acesso: L = local neste computador; R = rede local (outros computadores). Padrao L" /c LR /d L /t 15
if errorlevel 2 (set "BIBLIO_REDE=1") else (set "BIBLIO_REDE=")
if defined CHROME start "" powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%CHROME%' 'http://localhost:5000'"
if not defined CHROME start "" powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://localhost:5000'"
%PY% app.py
pause

