cmake_minimum_required(VERSION 3.20)
project(gnumarus VERSION 0.10.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(gnumarus
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
target_link_libraries(gnumarus PRIVATE OpenSSL::SSL OpenSSL::Crypto)

target_include_directories(gnumarus PRIVATE
    src
    third_party
    third_party/sqlite
)

# Silence warnings from the vendored amalgamation; keep ours strict.
set_source_files_properties(third_party/sqlite/sqlite3.c PROPERTIES
    COMPILE_OPTIONS "-w;-DSQLITE_THREADSAFE=1"
)
target_compile_options(gnumarus PRIVATE -Wall -Wextra -Wpedantic)

find_package(Threads REQUIRED)
target_link_libraries(gnumarus PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
