@echo off
REM Build the distributable wheel + sdist into dist\ via `python -m build`, the
REM same path CI and PyPI use. scikit-build-core compiles the DLLs through
REM CMakeLists.txt. Run after changing any .py or .cpp, then demos\setup.bat to
REM install the fresh wheel into the demo venv.
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%dist" rmdir /s /q "%ROOT%dist"
py -m pip install --quiet --upgrade build || ( echo [build] could not install 'build'. Is Python on PATH? & exit /b 1 )
py -m build "%ROOT%." || exit /b 1

echo [build] wheel + sdist -^> %ROOT%dist
exit /b 0
