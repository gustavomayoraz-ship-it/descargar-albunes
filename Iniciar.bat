@echo off
cd /d "%~dp0"
py -3.14 -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo No se encontro Python 3.14. Instalalo desde python.org y volve a abrir este archivo.
    pause
    exit /b 1
)
start "" pyw -3.14 descargar_albunes.py
