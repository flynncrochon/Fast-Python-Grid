@echo off
REM Create .venv and install fastgrid dependencies. Run once before demo.bat.
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"

if exist "%PY%" (
    echo [setup] .venv already exists. Reinstalling requirements...
) else (
    echo [setup] Creating .venv...
    python -m venv "%ROOT%.venv" || py -3 -m venv "%ROOT%.venv" || (
        echo [setup] Could not create venv. Is Python installed and on PATH?
        exit /b 1
    )
)

"%PY%" -m pip install -r "%ROOT%requirements.txt" || exit /b 1
echo [setup] Done. Run demo.bat to launch.
exit /b 0
