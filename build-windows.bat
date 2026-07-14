@echo off
setlocal
set "ROOT=%~dp0"
pushd "%ROOT%"

cmake -B build\native -DCMAKE_BUILD_TYPE=Release                || ( popd & echo [build-windows] cmake configure FAILED & pause & exit /b 1 )
cmake --build build\native --config Release                     || ( popd & echo [build-windows] cmake build FAILED & pause & exit /b 1 )
cmake --install build\native --config Release --prefix .        || ( popd & echo [build-windows] cmake install FAILED & pause & exit /b 1 )

py -m pip install --quiet --upgrade build || ( popd & echo [build-windows] could not install 'build'. Is Python on PATH? & pause & exit /b 1 )
py -m build --wheel "%ROOT%." || ( popd & exit /b 1 )

popd
echo [build-windows] DLLs -^> fastpygrid\core\ ; win_amd64 wheel -^> %ROOT%dist
exit /b 0
