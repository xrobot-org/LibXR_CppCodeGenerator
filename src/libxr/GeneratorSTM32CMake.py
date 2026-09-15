#!/usr/bin/env python
import argparse
import os
import logging
import shutil
import re
from pathlib import Path
from typing import Union

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

LIBXR_CMAKE_TEMPLATE = (
'''set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# LibXR
set(LIBXR_SYSTEM _LIBXR_SYSTEM_)
set(LIBXR_DRIVER st)
set(XROBOT_MODULES_DIR ${CMAKE_CURRENT_SOURCE_DIR}/Modules)
add_subdirectory(Middlewares/Third_Party/LibXR)
target_link_libraries(xr
    PUBLIC stm32cubemx
)
target_compile_features(xr PUBLIC cxx_std_20)

set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES
    CXX_STANDARD 20
    CXX_STANDARD_REQUIRED ON
)

target_include_directories(xr
    PUBLIC $<TARGET_PROPERTY:stm32cubemx,INTERFACE_INCLUDE_DIRECTORIES>
    PUBLIC Core/Inc
    PUBLIC User
)

# Add include paths
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user defined include paths
    PUBLIC $<TARGET_PROPERTY:xr,INTERFACE_INCLUDE_DIRECTORIES>
    PUBLIC User
)

# Add linked libraries
target_link_libraries(${CMAKE_PROJECT_NAME}
    stm32cubemx

    # Add user defined libraries
    xr
)

file(
    GLOB LIBXR_USER_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/User/*.cpp")


target_sources(${CMAKE_PROJECT_NAME}
    PRIVATE ${LIBXR_USER_SOURCES}
)

if(CMAKE_BUILD_TYPE STREQUAL "Debug")
    target_compile_options(${CMAKE_PROJECT_NAME} PRIVATE -Og)
    target_compile_options(xr PRIVATE -O2)
    if(TARGET FreeRTOS)
        target_compile_options(FreeRTOS PRIVATE -O2)
    endif()

    if(TARGET STM32_Drivers)
        target_compile_options(STM32_Drivers PRIVATE -O2)
    endif()

    if(TARGET USB_Device_Library)
        target_compile_options(USB_Device_Library PRIVATE -O2)
    endif()
endif()
'''
)

include_cmake_cmd = "include(${CMAKE_CURRENT_LIST_DIR}/cmake/LibXR.CMake)\n"


def normalize_libxr_cmake(content: str, system: str) -> str:
    content = re.sub(
        r'^\s*set\s*\(\s*CMAKE_CXX_STANDARD\s+\d+\s*\)\s*\n?',
        '',
        content,
        flags=re.MULTILINE
    )
    content = re.sub(
        r'^\s*set\s*\(\s*CMAKE_CXX_STANDARD_REQUIRED\s+\S+\s*\)\s*\n?',
        '',
        content,
        flags=re.MULTILINE
    )
    content = "set(CMAKE_CXX_STANDARD 20)\nset(CMAKE_CXX_STANDARD_REQUIRED ON)\n\n" + content.lstrip('\n')

    system_pattern = re.compile(
        r'(^\s*set\s*\(\s*LIBXR_SYSTEM\s+)(\S+)(\s*\)\s*)',
        re.MULTILINE
    )
    if system_pattern.search(content):
        content = system_pattern.sub(rf'\1{system}\3', content, count=1)
    else:
        content = re.sub(
            r'(^\s*set\s*\(\s*LIBXR_DRIVER\s+\S+\s*\)\s*$)',
            f"set(LIBXR_SYSTEM {system})\n\\1",
            content,
            count=1,
            flags=re.MULTILINE
        )

    content = re.sub(
        r'target_compile_features\s*\(\s*xr\s+PUBLIC\s+cxx_std_\d+\s*\)',
        'target_compile_features(xr PUBLIC cxx_std_20)',
        content,
        count=1
    )
    if "target_compile_features(xr PUBLIC cxx_std_20)" not in content:
        content = re.sub(
            r'(target_link_libraries\s*\(\s*xr\b[\s\S]*?\)\s*)',
            r'\1\ntarget_compile_features(xr PUBLIC cxx_std_20)\n',
            content,
            count=1
        )

    if "set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES" not in content:
        content = re.sub(
            r'(^\s*target_include_directories\(\$\{CMAKE_PROJECT_NAME\}\s+PRIVATE\s*$)',
            "set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES\n"
            "    CXX_STANDARD 20\n"
            "    CXX_STANDARD_REQUIRED ON\n"
            ")\n\n"
            r'\1',
            content,
            count=1,
            flags=re.MULTILINE
        )

    return content


def update_or_create_libxr_cmake(file_path: str, system: str) -> None:
    cmake_path = Path(file_path)

    if cmake_path.exists():
        content = read_text_with_fallback(str(cmake_path))
        new_content = normalize_libxr_cmake(content, system)
        if new_content != content:
            cmake_path.write_text(new_content, encoding="utf-8")
            logging.info(f"Updated existing LibXR.CMake for system: {system}")
        else:
            logging.info("LibXR.CMake already up to date, no changes needed.")
    else:
        cmake_path.write_text(
            LIBXR_CMAKE_TEMPLATE.replace("_LIBXR_SYSTEM_", system),
            encoding="utf-8"
        )
        logging.info(f"Generated LibXR.CMake at: {cmake_path}")


def normalize_starm_clang_toolchain(file_path: Union[str, Path]) -> None:
    """Keep CubeMX's ST Arm Clang runtime profile selectable from the CMake cache."""
    path = Path(file_path)
    if not path.exists():
        return

    content = read_text_with_fallback(str(path))
    line_pattern = re.compile(
        r'^(\s*set\s*\(\s*STARM_TOOLCHAIN_CONFIG\s+")([^"]+)'
        r'("(?:\s+CACHE\s+STRING\s+"[^"]*")?\s*\)\s*)$',
        re.MULTILINE,
    )
    match = line_pattern.search(content)
    if match is None:
        logging.warning("STARM_TOOLCHAIN_CONFIG not found in %s", path)
        return

    default_config = match.group(2)
    replacement = (
        f'{match.group(1)}{default_config}" CACHE STRING '
        '"ST Arm Clang runtime profile")'
    )
    new_content = content[:match.start()] + replacement + content[match.end():]

    property_block = (
        'set_property(CACHE STARM_TOOLCHAIN_CONFIG PROPERTY STRINGS\n'
        '             STARM_HYBRID STARM_NEWLIB STARM_PICOLIBC)'
    )
    if property_block not in new_content:
        insert_at = match.start() + len(replacement)
        new_content = new_content[:insert_at] + '\n' + property_block + new_content[insert_at:]

    # CubeMX emits an if/elseif block that computes multilib flags. Initialize the
    # variable explicitly and reject misspelled runtime profiles.
    if 'set(TOOLCHAIN_MULTILIBS "")' not in new_content:
        marker = 'if(STARM_TOOLCHAIN_CONFIG STREQUAL "STARM_HYBRID")'
        if marker in new_content:
            new_content = new_content.replace(
                marker, 'set(TOOLCHAIN_MULTILIBS "")\n\n' + marker, 1
            )

    first_if = new_content.find('if(STARM_TOOLCHAIN_CONFIG STREQUAL "STARM_HYBRID")')
    if first_if >= 0:
        first_endif = new_content.find('endif()', first_if)
        if first_endif >= 0:
            block = new_content[first_if:first_endif]
            guard = (
                'elseif(NOT STARM_TOOLCHAIN_CONFIG STREQUAL "STARM_PICOLIBC")\n'
                '  message(FATAL_ERROR "Unknown STARM_TOOLCHAIN_CONFIG: '
                '${STARM_TOOLCHAIN_CONFIG}")\n'
            )
            if 'Unknown STARM_TOOLCHAIN_CONFIG' not in block:
                new_content = new_content[:first_endif] + guard + new_content[first_endif:]

    if new_content != content:
        path.write_text(new_content, encoding="utf-8", newline="\n")
        logging.info("Made STARM_TOOLCHAIN_CONFIG cache-selectable in %s", path)


def clean_cmake_build_dirs(input_directory: Union[str, Path]) -> None:
    input_directory = Path(input_directory)
    removed = False
    for d in input_directory.iterdir():
        if d.is_dir() and (d.name == "build" or d.name.startswith("cmake-build")):
            shutil.rmtree(d)
            logging.info(f"Removed {d}")
            removed = True
    if not removed:
        logging.info("No build or cmake-build* directory found, nothing to clean.")


def read_text_with_fallback(path: str) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return Path(path).read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return Path(path).read_text(encoding="utf-8")


def main():
    from libxr.PackageInfo import LibXRPackageInfo

    LibXRPackageInfo.check_and_print()

    parser = argparse.ArgumentParser(description="Generate CMake file for LibXR.")
    parser.add_argument("input_dir", type=str, help="CubeMX CMake Project Directory")

    args = parser.parse_args()
    input_directory = args.input_dir

    if not os.path.isdir(input_directory):
        logging.error("Input directory does not exist.")
        exit(1)

    clean_cmake_build_dirs(input_directory)

    cmake_dir = os.path.join(input_directory, "cmake")
    os.makedirs(cmake_dir, exist_ok=True)

    file_path = os.path.join(cmake_dir, "LibXR.CMake")

    freertos_enable = os.path.exists(os.path.join(input_directory, "Core", "Inc", "FreeRTOSConfig.h"))
    threadx_enable = os.path.exists(os.path.join(input_directory, "Core", "Inc", "app_threadx.h"))

    if freertos_enable:
        system = "FreeRTOS"
    elif threadx_enable:
        system = "ThreadX"
    else:
        system = "None"

    update_or_create_libxr_cmake(file_path, system)
    logging.info("LibXR.CMake generated/updated successfully.")

    normalize_starm_clang_toolchain(os.path.join(cmake_dir, "starm-clang.cmake"))

    main_cmake_path = os.path.join(input_directory, "CMakeLists.txt")
    if os.path.exists(main_cmake_path):
        cmake_content = read_text_with_fallback(main_cmake_path)

        if include_cmake_cmd not in cmake_content:
            with open(main_cmake_path, "a", encoding="utf-8", newline="\n") as f:
                f.write('\n# Add LibXR\n' + include_cmake_cmd)
            logging.info("LibXR.CMake included in CMakeLists.txt.")
        else:
            logging.info("LibXR.CMake already included in CMakeLists.txt.")
    else:
        logging.error("CMakeLists.txt not found.")
        exit(1)
