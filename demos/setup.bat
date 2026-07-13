@echo off
REM Create demos\.venv, install deps, and install the freshly built wheel into it
REM so the demos import fastpygrid from the venv. Run build-all.bat first (dist\*.whl).
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
REM Pick the Windows wheel specifically -- dist\ may also hold the linux_x86_64 wheel.
for %%W in ("%ROOT%dist\*win_amd64.whl") do set "WHEEL=%%W"
if defined WHEEL (
    REM Force-reinstall fastpygrid so a rebuilt same-version wheel updates. Then pull
    REM the qt extra (PySide6) for the Qt demo host; idempotent, downloads only once.
    "%PY%" -m pip install --force-reinstall --no-deps "%WHEEL%" || exit /b 1
    "%PY%" -m pip install "%WHEEL%[qt]" || exit /b 1
    echo [setup] Installed %WHEEL% (with qt extra: PySide6)
) else (
    echo [setup] NOTE: no wheel in dist\. Run build-all.bat first, then re-run setup.bat.
)

echo [setup] Done. Run demo.bat to launch.
exit /b 0
