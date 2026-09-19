"""Tests for xr-syntax-backed STM32 user-code preservation."""

import re

from libxr.GeneratorCodeSTM32 import preserve_user_blocks


def _legacy(existing_code: str, section: int) -> str:
    patterns = {
        1: (r'/\* User Code Begin 1 \*/(.*?)/\* User Code End 1 \*/', ''),
        2: (r'/\* User Code Begin 2 \*/(.*?)/\* User Code End 2 \*/', ''),
        3: (r'/\* User Code Begin 3 \*/(.*?)/\* User Code End 3 \*/', ''),
    }
    if section not in patterns:
        return ''
    pattern, default = patterns[section]
    match = re.search(pattern, existing_code, re.DOTALL)
    if section != 1:
        return '  ' + match.group(1).strip() if match else default
    return match.group(1).strip() if match else default


def _assert_parity(source: str) -> None:
    for section in (1, 2, 3, 4):
        assert preserve_user_blocks(source, section) == _legacy(source, section)


def test_numbered_user_blocks_match_legacy_output() -> None:
    source = """#include "app_main.h"
/* User Code Begin 1 */
static int user_value = 1;
/* User Code End 1 */

extern "C" void app_main(void) {
  /* User Code Begin 2 */
  setup_user();
  /* User Code End 2 */

  /* User Code Begin 3 */
  loop_user();
  /* User Code End 3 */
}
"""
    _assert_parity(source)


def test_nested_numbered_regions_match_legacy_output() -> None:
    source = """/* User Code Begin 1 */
outer_a();
/* User Code Begin 2 */
inner();
/* User Code End 2 */
outer_b();
/* User Code End 1 */
"""
    _assert_parity(source)


def test_mismatched_end_marker_does_not_destroy_later_match() -> None:
    source = """/* User Code Begin 1 */
keep_a();
/* User Code End 2 */
keep_b();
/* User Code End 1 */
"""
    assert preserve_user_blocks(source, 1) == _legacy(source, 1)
    assert "User Code End 2" in preserve_user_blocks(source, 1)


def test_crlf_blocks_match_legacy_output() -> None:
    source = (
        "/* User Code Begin 1 */\r\n"
        "alpha();\r\n"
        "/* User Code End 1 */\r\n"
        "/* User Code Begin 2 */\r\n"
        "beta();\r\n"
        "/* User Code End 2 */\r\n"
    )
    _assert_parity(source)


def test_unclosed_and_orphan_markers_return_empty() -> None:
    assert preserve_user_blocks("/* User Code Begin 1 */\nvalue();\n", 1) == ""
    assert preserve_user_blocks("/* User Code End 1 */\n", 1) == ""


def test_non_utf8_source_bytes_can_be_preserved_by_document_path() -> None:
    source = (
        b"/* User Code Begin 1 */\n"
        b"const char data[] = \"\xff\";\n"
        b"/* User Code End 1 */\n"
    ).decode("utf-8", errors="surrogateescape")
    result = preserve_user_blocks(source, 1)
    assert result.encode("utf-8", errors="surrogateescape") == b'const char data[] = "\xff";'
