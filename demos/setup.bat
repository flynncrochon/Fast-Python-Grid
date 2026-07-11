@echo off
REM Create demos\.venv, install deps, and install the freshly built wheel into it
REM so the demos import fastpygrid from the venv. Run build.bat first (dist\*.whl).
setlocal
REM This bat lives in demos/, so ROOT is its parent (the repo root).
set "DEMOS=%~dp0"
for %%I in ("%~dp0..") do set "ROOT=%%~fI\"
set "VENV=%DEMOS%.venv"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PY%" (
    echo [setup] demos\.venv already exists. Reinstalling requirements...
) else (
    echo [setup] Creating demos\.venv...
    python -m venv "%VENV%" || py -3 -m venv "%VENV%" || (
        echo [setup] Could not create venv. Is Python installed and on PATH?
        exit /b 1
    )
)

"%PY%" -m pip install -r "%ROOT%requirements.txt" || exit /b 1

set "WHEEL="
for %%W in ("%ROOT%dist\*.whl") do set "WHEEL=%%W"
if defined WHEEL (
    "%PY%" -m pip install --force-reinstall "%WHEEL%" || exit /b 1
    echo [setup] Installed %WHEEL%
) else (
    echo [setup] NOTE: no wheel in dist\. Run build.bat first, then re-run setup.bat.
)

echo [setup] Done. Run demo.bat to launch.
exit /b 0
