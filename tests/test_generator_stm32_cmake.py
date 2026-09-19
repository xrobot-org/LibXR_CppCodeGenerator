"""Golden tests for structured LibXR CMake normalization."""

import re

from libxr.GeneratorSTM32CMake import (
    LIBXR_CMAKE_TEMPLATE,
    has_libxr_cmake_include,
    normalize_libxr_cmake,
)


def _legacy_normalize(content: str, system: str) -> str:
    content = re.sub(
        r'^\s*set\s*\(\s*CMAKE_CXX_STANDARD\s+\d+\s*\)\s*\n?',
        '',
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(
        r'^\s*set\s*\(\s*CMAKE_CXX_STANDARD_REQUIRED\s+\S+\s*\)\s*\n?',
        '',
        content,
        flags=re.MULTILINE,
    )
    content = (
        "set(CMAKE_CXX_STANDARD 20)\n"
        "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n\n"
        + content.lstrip('\n')
    )

    system_pattern = re.compile(
        r'(^\s*set\s*\(\s*LIBXR_SYSTEM\s+)(\S+)(\s*\)\s*)',
        re.MULTILINE,
    )
    if system_pattern.search(content):
        content = system_pattern.sub(rf'\1{system}\3', content, count=1)
    else:
        content = re.sub(
            r'(^\s*set\s*\(\s*LIBXR_DRIVER\s+\S+\s*\)\s*$)',
            f"set(LIBXR_SYSTEM {system})\n\\1",
            content,
            count=1,
            flags=re.MULTILINE,
        )

    content = re.sub(
        r'target_compile_features\s*\(\s*xr\s+PUBLIC\s+cxx_std_\d+\s*\)',
        'target_compile_features(xr PUBLIC cxx_std_20)',
        content,
        count=1,
    )
    if "target_compile_features(xr PUBLIC cxx_std_20)" not in content:
        content = re.sub(
            r'(target_link_libraries\s*\(\s*xr\b[\s\S]*?\)\s*)',
            r'\1\ntarget_compile_features(xr PUBLIC cxx_std_20)\n',
            content,
            count=1,
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
            flags=re.MULTILINE,
        )
    return content


def _assert_parity(source: str, system: str) -> None:
    assert normalize_libxr_cmake(source, system) == _legacy_normalize(source, system)


def test_template_parity() -> None:
    _assert_parity(LIBXR_CMAKE_TEMPLATE.replace("_LIBXR_SYSTEM_", "None"), "FreeRTOS")


def test_existing_settings_with_spacing_parity() -> None:
    source = """  set ( CMAKE_CXX_STANDARD 17 )

set(CMAKE_CXX_STANDARD_REQUIRED OFF)
# keep me
set ( LIBXR_SYSTEM None )
set(LIBXR_DRIVER st)

target_link_libraries(
  xr
  PUBLIC something
)

target_compile_features ( xr PUBLIC cxx_std_17 )

target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
  User
)
"""
    _assert_parity(source, "ThreadX")


def test_missing_system_and_compile_features_parity() -> None:
    source = """set(LIBXR_DRIVER st)

target_link_libraries(xr
    PUBLIC stm32cubemx
)

target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
    User
)
"""
    _assert_parity(source, "FreeRTOS")


def test_already_normalized_is_idempotent_and_matches_legacy() -> None:
    source = LIBXR_CMAKE_TEMPLATE.replace("_LIBXR_SYSTEM_", "ThreadX")
    first = normalize_libxr_cmake(source, "ThreadX")
    second = normalize_libxr_cmake(first, "ThreadX")
    assert first == _legacy_normalize(source, "ThreadX")
    assert second == first


def test_crlf_input_matches_legacy() -> None:
    source = (
        "set(CMAKE_CXX_STANDARD 17)\r\n"
        "set(CMAKE_CXX_STANDARD_REQUIRED OFF)\r\n"
        "set(LIBXR_DRIVER st)\r\n"
        "target_link_libraries(xr PUBLIC stm32cubemx)\r\n"
        "target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE User)\r\n"
    )
    _assert_parity(source, "None")


def test_libxr_include_detection_accepts_existing_legacy_line() -> None:
    source = "project(Demo)\ninclude(${CMAKE_CURRENT_LIST_DIR}/cmake/LibXR.CMake)\n"
    assert has_libxr_cmake_include(source)


def test_libxr_include_detection_is_structural_not_textual() -> None:
    source = (
        "project(Demo)\n"
        "include (  \"${CMAKE_CURRENT_LIST_DIR}/cmake/LibXR.CMake\"  )\n"
    )
    assert has_libxr_cmake_include(source)


def test_libxr_include_detection_ignores_comment_text() -> None:
    source = (
        "# include(${CMAKE_CURRENT_LIST_DIR}/cmake/LibXR.CMake)\n"
        "project(Demo)\n"
    )
    assert not has_libxr_cmake_include(source)
