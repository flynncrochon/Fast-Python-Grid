cmake_minimum_required(VERSION 3.15)
project(fastpygrid_native CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# The two native DLLs, sources in fastpygrid/csrc. Their .cpp #pragma comment(lib,
# ...) the Windows libs, so no target_link_libraries needed. No "lib" prefix ->
# ctypes loads them by name.
add_library(surface  SHARED fastpygrid/csrc/surface.cpp)
add_library(gridcore SHARED fastpygrid/csrc/gridcore.cpp)
set_target_properties(surface gridcore PROPERTIES PREFIX "")

# Install lays out the `fastpygrid` package: both .dll drop into core/ beside the
# .py that loads them (csrc/ sources are not shipped). Destinations are under
# fastpygrid/ so this works both for build.bat (--prefix dist -> dist/fastpygrid)
# and the scikit-build-core wheel (prefix = wheel root -> fastpygrid/ in site-packages).
install(TARGETS surface gridcore RUNTIME DESTINATION fastpygrid/core LIBRARY DESTINATION fastpygrid/core)
install(DIRECTORY fastpygrid/ DESTINATION fastpygrid FILES_MATCHING PATTERN "*.py")
