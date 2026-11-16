@echo off
cd /d "%~dp0"
echo Iniciando LSEE Alocacao de Recursos (Web)...
python server.py --host 127.0.0.1 --port 8080 --restart --open-browser
pause
