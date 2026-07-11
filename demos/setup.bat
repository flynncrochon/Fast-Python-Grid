@echo off
REM Create demos\.venv, install deps, and stage the built library into demos\fastpygrid
REM so the demos import it directly. Run build.bat first (it produces dist\fastpygrid).
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

if exist "%ROOT%dist\fastpygrid" (
    robocopy "%ROOT%dist\fastpygrid" "%DEMOS%fastpygrid" /MIR >nul
    if errorlevel 8 ( echo [setup] Copy of fastpygrid FAILED & exit /b 1 )
    echo [setup] Staged fastpygrid -^> demos\fastpygrid
) else (
    echo [setup] NOTE: dist\fastpygrid not found. Run build.bat, then re-run setup.bat.
)

echo [setup] Done. Run demo.bat to launch.
exit /b 0
