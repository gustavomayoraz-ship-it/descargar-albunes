@echo off
cd /d "%~dp0"
py -3.14 -c "import sys" >nul 2>nul
if not errorlevel 1 goto abrir
echo Python 3.14 no esta instalado. Instalandolo, espera unos minutos...
winget install -e --id Python.Python.3.14 --silent --accept-package-agreements --accept-source-agreements
py -3.14 -c "import sys" >nul 2>nul
if not errorlevel 1 goto abrir
:: Sin winget o sin el launcher en el PATH: el propio programa sabe instalar y relanzar con la 3.14.
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw descargar_albunes.py
    exit /b 0
)
echo No se pudo instalar Python 3.14 automaticamente.
echo Instalalo desde https://www.python.org/downloads/ y volve a abrir este archivo.
start https://www.python.org/downloads/
pause
exit /b 1
:abrir
start "" pyw -3.14 descargar_albunes.py
