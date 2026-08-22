@echo off
REM ===================================================================
REM  PRESERVA-SCAN - abrir o painel de varredura (duplo-clique)
REM  Prefere o executavel; se nao houver, usa o Python instalado.
REM ===================================================================
cd /d "%~dp0"

REM 1) Se o executavel estiver por perto, abre ele (nao precisa de Python).
if exist "PreservaScan.exe" (
  start "" "PreservaScan.exe"
  exit /b
)
if exist "dist\PreservaScan.exe" (
  start "" "dist\PreservaScan.exe"
  exit /b
)

REM 2) Caminho alternativo: rodar pelo Python (o proprio programa abre o navegador).
where python >nul 2>nul
if errorlevel 1 (
  echo Python nao encontrado e PreservaScan.exe ausente.
  echo Instale o Python 3 ou coloque o PreservaScan.exe nesta pasta.
  pause
  exit /b 1
)
python "scanner\preservascan.py"
pause
