@echo off
REM Package the distributable wheel + sdist into dist\ via `python -m build`, the
REM same path CI and PyPI use. scikit-build-core compiles the Windows DLLs through
REM CMakeLists.txt as part of this; the Linux .so must already be in fastpygrid\core\
REM (run build-linux.bat first) since CMake on Windows can't cross-compile them.
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%dist" rmdir /s /q "%ROOT%dist"
py -m pip install --quiet --upgrade build || ( echo [build-local] could not install 'build'. Is Python on PATH? & exit /b 1 )

REM --wheel builds straight from the source tree, NOT via an sdist. That matters:
REM the prebuilt Linux .so are gitignored, so an sdist would strip them and the
REM wheel would ship DLLs only. Direct-from-source keeps both platforms' binaries.
REM -C wheel.platlib=false forces the py3-none-any tag so this one dll+so wheel
REM installs on both OSes. (pyproject stays platform-tagged for correct PyPI wheels.)
py -m build --wheel -C wheel.platlib=false "%ROOT%." || exit /b 1
REM sdist is source-only (no built binaries by design) and safe to build separately.
py -m build --sdist "%ROOT%." || exit /b 1

echo [build-local] wheel + sdist -^> %ROOT%dist
exit /b 0
