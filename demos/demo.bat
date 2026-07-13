@echo off
REM Launch the LIVE OpenGL demo grid (tk or qt host). Double-click, the window
REM stays open until you close it (unlike test_gl.bat, which runs the self-test
REM and exits).
REM
REM   demo.bat                     -> prompts for tk or qt
REM   demo.bat tk                  -> tkinter host, 100k rows
REM   demo.bat qt                  -> Qt (PySide6) host, same data
REM   demo.bat tk --rows 500000    -> extra args pass straight through
REM
REM Self-contained: copies the built DLLs out of dist\*.whl into fastpygrid\core\
REM on first run. Build them first with:  python -m fastpygrid.core.gpu --build
REM Qt needs PySide6; demos\setup.bat puts it in demos\.venv (used automatically).
setlocal
REM This bat lives in demos/, so ROOT is its parent (the repo root).
for %%I in ("%~dp0..") do set "ROOT=%%~fI\"
pushd "%ROOT%"

REM Prefer demos\.venv (has PySide6 for the qt host); fall back to system python.
set "PY=python"
where python >nul 2>nul || set "PY=py"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

REM Pick the host: tk or qt (first arg, else prompt). Keep any remaining args in REST.
set "REST="
set "TARGET=%~1"
if /i "%TARGET%"=="tk" goto collect
if /i "%TARGET%"=="qt" goto collect
set /p TARGET=Which host? [tk/qt]:
if /i "%TARGET%"=="tk" goto extract
if /i "%TARGET%"=="qt" goto extract
echo [demo] Unknown host "%TARGET%". Choose tk or qt.
popd & exit /b 1

:collect
shift
:collect_loop
if "%~1"=="" goto extract
set "REST=%REST% %1"
shift
goto collect_loop

:extract
if not exist "%ROOT%fastpygrid\core\glsurface.dll" (
    echo [demo] Extracting DLLs from dist\*.whl...
    if not exist "%ROOT%dist\*win_amd64.whl" (
        echo [demo] No Windows wheel in dist\. Build first:  build-windows.bat
        echo.
        pause
        popd & exit /b 1
    )
    %PY% -c "import zipfile,glob,os;w=sorted(glob.glob('dist/*win_amd64.whl'))[-1];z=zipfile.ZipFile(w);[open('fastpygrid/core/'+os.path.basename(n),'wb').write(z.read(n)) for n in z.namelist() if n.endswith('.dll')];print('[demo] extracted DLLs from',w)" || ( echo. & pause & popd & exit /b 1 )
)

set "PYTHONPATH=%ROOT%"
echo [demo] Launching %TARGET% demo, close the grid window to exit...
%PY% "%ROOT%demos\demo_gpu_%TARGET%.py"%REST%

REM Keep the console open if the demo errored out, so you can read the traceback
REM instead of the window flashing shut.
if errorlevel 1 (
    echo.
    echo [demo] The demo exited with an error ^(see above^).
    pause
)
popd
exit /b %errorlevel%
