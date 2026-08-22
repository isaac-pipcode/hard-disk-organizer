@echo off
REM Duplo-clique para abrir o painel de varredura no navegador (sem terminal).
REM Coloque este arquivo na MESMA pasta de panel.py e scan.py.
cd /d "%~dp0"
start "" http://localhost:8080
python -m uvicorn panel:app --host 127.0.0.1 --port 8080
pause
