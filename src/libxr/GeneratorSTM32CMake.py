#!/usr/bin/env python
import argparse
import os
import logging
import shutil
import re
from pathlib import Path
from typing import Union

from xr_syntax.cmake import CMakeDocument
from xr_syntax.core import decode_source, encode_source

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


def _command_args(command) -> list:
    return [argument.text for argument in command.arguments]


def _apply_byte_edits(content: str, edits: list) -> str:
    data = encode_source(content)
    previous_start = len(data)
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        if start < 0 or end < start or end > len(data):
            raise ValueError("invalid CMake source edit span")
        if end > previous_start:
            raise ValueError("overlapping CMake source edits")
        data = data[:start] + encode_source(replacement) + data[end:]
        previous_start = start
    return decode_source(data)


def _line_start(data: bytes, offset: int) -> int:
    newline = data.rfind(b"\n", 0, offset)
    return newline + 1


def _trailing_whitespace_end(data: bytes, offset: int) -> int:
    whitespace = b" \t\r\n\f\v"
    while offset < len(data) and data[offset] in whitespace:
        offset += 1
    return offset


def _remove_cmake_set(content: str, variable: str, value_check) -> str:
    document = CMakeDocument.parse(content)
    data = document.render_bytes()
    edits = []
    for command in document.command_views("set"):
        args = _command_args(command)
        if len(args) != 2 or args[0] != variable or not value_check(args[1]):
            continue
        start = _line_start(data, command.node.span.start)
        if data[start:command.node.span.start].strip():
            continue
        end = _trailing_whitespace_end(data, command.node.span.end)
        edits.append((start, end, ""))
    return _apply_byte_edits(content, edits)


def _replace_argument(content: str, command, argument_index: int, value: str) -> str:
    span = command.arguments[argument_index].node.span
    return _apply_byte_edits(content, [(span.start, span.end, value)])


def _replace_command(content: str, command, source: str) -> str:
    span = command.node.span
    return _apply_byte_edits(content, [(span.start, span.end, source)])


def normalize_libxr_cmake(content: str, system: str) -> str:
    content = _remove_cmake_set(
        content,
        "CMAKE_CXX_STANDARD",
        lambda value: value.isdigit(),
    )
    content = _remove_cmake_set(
        content,
        "CMAKE_CXX_STANDARD_REQUIRED",
        lambda value: bool(value) and not any(ch.isspace() for ch in value),
    )
    content = (
        "set(CMAKE_CXX_STANDARD 20)\n"
        "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n\n"
        + content.lstrip("\n")
    )

    document = CMakeDocument.parse(content)
    system_command = next(
        (
            command
            for command in document.command_views("set")
            if len(_command_args(command)) == 2
            and _command_args(command)[0] == "LIBXR_SYSTEM"
        ),
        None,
    )
    if system_command is not None:
        content = _replace_argument(content, system_command, 1, system)
    else:
        document = CMakeDocument.parse(content)
        driver_command = next(
            (
                command
                for command in document.command_views("set")
                if len(_command_args(command)) == 2
                and _command_args(command)[0] == "LIBXR_DRIVER"
            ),
            None,
        )
        if driver_command is not None:
            data = document.render_bytes()
            insert_at = _line_start(data, driver_command.node.span.start)
            content = _apply_byte_edits(
                content,
                [(insert_at, insert_at, f"set(LIBXR_SYSTEM {system})\n")],
            )

    document = CMakeDocument.parse(content)
    compile_feature = next(
        (
            command
            for command in document.command_views("target_compile_features")
            if command.name == "target_compile_features"
            and len(_command_args(command)) == 3
            and _command_args(command)[0] == "xr"
            and _command_args(command)[1] == "PUBLIC"
            and re.fullmatch(r"cxx_std_\d+", _command_args(command)[2])
        ),
        None,
    )
    if compile_feature is not None:
        content = _replace_command(
            content,
            compile_feature,
            "target_compile_features(xr PUBLIC cxx_std_20)",
        )

    canonical_feature = "target_compile_features(xr PUBLIC cxx_std_20)"
    if canonical_feature not in content:
        document = CMakeDocument.parse(content)
        link_command = next(
            (
                command
                for command in document.command_views("target_link_libraries")
                if command.name == "target_link_libraries"
                and _command_args(command)
                and _command_args(command)[0] == "xr"
            ),
            None,
        )
        if link_command is not None:
            data = document.render_bytes()
            insert_at = _trailing_whitespace_end(data, link_command.node.span.end)
            content = _apply_byte_edits(
                content,
                [(insert_at, insert_at, f"\n{canonical_feature}\n")],
            )

    target_properties = "set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES"
    if target_properties not in content:
        document = CMakeDocument.parse(content)
        include_command = next(
            (
                command
                for command in document.command_views("target_include_directories")
                if command.name == "target_include_directories"
                and len(_command_args(command)) >= 2
                and _command_args(command)[0] == "${CMAKE_PROJECT_NAME}"
                and _command_args(command)[1] == "PRIVATE"
            ),
            None,
        )
        if include_command is not None:
            data = document.render_bytes()
            insert_at = _line_start(data, include_command.node.span.start)
            block = (
                "set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES\n"
                "    CXX_STANDARD 20\n"
                "    CXX_STANDARD_REQUIRED ON\n"
                ")\n\n"
            )
            content = _apply_byte_edits(
                content,
                [(insert_at, insert_at, block)],
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
