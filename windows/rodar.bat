@echo off
rem ===========================================================================
rem  AutoApply AI - executa um script do projeto com a venv, gravando log.
rem  Feito para o Agendador de Tarefas do Windows (funciona de qualquer pasta).
rem
rem  Uso:  windows\rodar.bat main.py
rem        windows\rodar.bat briefing_diario.py
rem        windows\rodar.bat main.py --modulo alertas
rem
rem  Log:  logs\autoapply.log (anexa; abre certo no VS Code / Bloco de Notas)
rem ===========================================================================
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs
rem UTF-8 na saida: emoji no log nao derruba o processo
set PYTHONUTF8=1
rem Sem navegador em tarefa agendada: se o token.json vencer, falha rapido
rem (rode "python google_auth.py" no terminal para renovar) em vez de travar.
set AUTOAPPLY_HEADLESS=true
if not exist "venv\Scripts\python.exe" (
  echo [%date% %time%] venv nao encontrada em %cd%\venv - crie com: python -m venv venv>> "logs\autoapply.log"
  exit /b 1
)
echo.>> "logs\autoapply.log"
echo ===== [%date% %time%] %* =====>> "logs\autoapply.log"
"venv\Scripts\python.exe" %* >> "logs\autoapply.log" 2>&1
exit /b %errorlevel%
