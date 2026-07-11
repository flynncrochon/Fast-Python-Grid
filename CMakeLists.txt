cmake_minimum_required(VERSION 3.15)
project(fastpygrid_native CXX)

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

# Install lays out the `fastpygrid` package: each .dll beside its module, plus the
# .py sources (no .cpp). Destinations are under fastpygrid/ so this works both for
# build.bat (--prefix dist -> dist/fastpygrid) and the scikit-build-core wheel
# (prefix = wheel root -> fastpygrid/ in site-packages).
install(TARGETS surface  RUNTIME DESTINATION fastpygrid/core/_gpu       LIBRARY DESTINATION fastpygrid/core/_gpu)
install(TARGETS gridcore RUNTIME DESTINATION fastpygrid/core/_gridstore LIBRARY DESTINATION fastpygrid/core/_gridstore)
install(DIRECTORY src/ DESTINATION fastpygrid FILES_MATCHING PATTERN "*.py")
