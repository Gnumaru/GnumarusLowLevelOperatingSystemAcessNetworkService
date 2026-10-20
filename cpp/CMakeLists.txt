cmake_minimum_required(VERSION 3.20)
project(GnumarusOSBridge VERSION 0.10.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(GnumarusOSBridge
    src/main.cpp
    src/config.cpp
    src/tags.cpp
    src/codec.cpp
    src/http.cpp
    src/api.cpp
    src/hosts.cpp
    src/timex.cpp
    third_party/sqlite/sqlite3.c
)

find_package(OpenSSL REQUIRED)
target_link_libraries(GnumarusOSBridge PRIVATE OpenSSL::SSL OpenSSL::Crypto)

target_include_directories(GnumarusOSBridge PRIVATE
    src
    third_party
    third_party/sqlite
)

# Silence warnings from the vendored amalgamation; keep ours strict.
set_source_files_properties(third_party/sqlite/sqlite3.c PROPERTIES
    COMPILE_OPTIONS "-w;-DSQLITE_THREADSAFE=1"
)
target_compile_options(GnumarusOSBridge PRIVATE -Wall -Wextra -Wpedantic)

find_package(Threads REQUIRED)
target_link_libraries(GnumarusOSBridge PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
