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

# `cmake --install --prefix dist/fastgrid` lays out the runnable package:
# the .dll beside its module, and the .py sources (no .cpp).
install(TARGETS surface  RUNTIME DESTINATION core/_gpu       LIBRARY DESTINATION core/_gpu)
install(TARGETS gridcore RUNTIME DESTINATION core/_gridstore LIBRARY DESTINATION core/_gridstore)
install(DIRECTORY src/ DESTINATION . FILES_MATCHING PATTERN "*.py")
