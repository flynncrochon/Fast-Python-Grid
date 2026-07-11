@echo off
REM Build the runnable fastgrid library into dist\fastgrid : the .py sources plus
REM freshly compiled .dll (via CMake), nothing else. CMake finds MSVC itself.
REM Run after changing any .py or .cpp, then run the demos against dist\fastgrid.
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%dist" rmdir /s /q "%ROOT%dist"
cmake -S "%ROOT%." -B "%ROOT%build" -DCMAKE_BUILD_TYPE=Release || exit /b 1
cmake --build "%ROOT%build" --config Release || exit /b 1
cmake --install "%ROOT%build" --prefix "%ROOT%dist/fastgrid" --config Release || exit /b 1

echo [build] dist -^> %ROOT%dist\fastgrid
exit /b 0
