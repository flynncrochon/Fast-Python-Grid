cmake_minimum_required(VERSION 3.15)
project(fastgrid_native CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# The two native DLLs. Their .cpp #pragma comment(lib, ...) the Windows libs, so
# no target_link_libraries needed. No "lib" prefix -> ctypes loads them by name.
add_library(surface  SHARED src/core/_gpu/surface.cpp)
add_library(gridcore SHARED src/core/_gridstore/gridcore.cpp)
set_target_properties(surface gridcore PROPERTIES PREFIX "")

# Install lays out the `fastgrid` package: each .dll beside its module, plus the
# .py sources (no .cpp). Destinations are under fastgrid/ so this works both for
# build.bat (--prefix dist -> dist/fastgrid) and the scikit-build-core wheel
# (prefix = wheel root -> fastgrid/ in site-packages).
install(TARGETS surface  RUNTIME DESTINATION fastgrid/core/_gpu       LIBRARY DESTINATION fastgrid/core/_gpu)
install(TARGETS gridcore RUNTIME DESTINATION fastgrid/core/_gridstore LIBRARY DESTINATION fastgrid/core/_gridstore)
install(DIRECTORY src/ DESTINATION fastgrid FILES_MATCHING PATTERN "*.py")
