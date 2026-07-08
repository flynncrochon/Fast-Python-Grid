@echo off
REM Launch the fastgrid Tk or Qt demo using the project's .venv (Python 3.10).
REM Usage:  demo.bat            -> prompts for tk or qt
REM         demo.bat qt         -> runs the Qt (PySide6) demo
REM         demo.bat tk         -> runs the Tk demo
REM         demo.bat qt --rows 500000   -> extra args pass through to the demo

setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [demo] .venv not found. Create it with:  py -3.10 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

set "TARGET=%~1"
if /i "%TARGET%"=="qt" goto run
if /i "%TARGET%"=="tk" goto run

set /p TARGET=Which demo? [tk/qt]:
if /i not "%TARGET%"=="qt" if /i not "%TARGET%"=="tk" (
    echo [demo] Unknown demo "%TARGET%". Choose tk or qt.
    exit /b 1
)
set "REST="
goto launch

:run
REM Drop the first arg (tk/qt); keep the rest to pass through.
shift
set "REST="
:collect
if "%~1"=="" goto launch
set "REST=%REST% %1"
shift
goto collect

:launch
set "PYTHONPATH=%ROOT%;%ROOT%scripts"
echo [demo] Launching %TARGET% demo...
"%PY%" "%ROOT%scripts\demo_%TARGET%.py"%REST%
exit /b %errorlevel%
