"""STM32 链接脚本中 GOT 的位置：.got 输出段放在 Flash 中，紧跟 .fini_array。
The place of the GOT in an STM32 linker script: a .got output section in Flash, directly after
.fini_array.

CubeMX 写出的链接脚本没有 .got 输出段，而 CubeMX 生成的 syscalls.c 和 sysmem.c 写 errno；ST Arm
Clang 的 picolibc 中 errno 是线程局部变量，访问它要用一个 GOT 条目。ld.lld 把这个孤立的段放进 RAM，
加载地址等于运行地址：.bin 从 Flash 一直覆盖到 RAM（约 384 MiB），启动代码复制 .data 时写进这个字的
也不是链接时的值。静态镜像中的 GOT 条目是链接时就确定的常量，所以放在 Flash 中。ld.lld 把 init/fini
数组和 GOT 归入同一段 RELRO 范围，要求它们连续，所以 .got 紧跟 .fini_array；中间隔着别的输出段时，
STARM_NEWLIB 的链接报 ".got is not contiguous with other relro sections"。
The linker script CubeMX writes has no .got output section, while the syscalls.c and sysmem.c
CubeMX generates write errno; in the picolibc of ST Arm Clang errno is thread-local, and reaching
it takes a GOT entry. ld.lld puts that orphan section in RAM with its load address equal to its
run address: the .bin spans Flash up to RAM (about 384 MiB), and the startup copy of .data does
not write the linked value into that word. GOT entries of a static image are constants fixed at
link time, so they go to Flash. ld.lld puts the init/fini arrays and the GOT in one RELRO range
that must be contiguous, so .got directly follows .fini_array; with another output section in
between, an STARM_NEWLIB link fails with ".got is not contiguous with other relro sections".
"""

import logging
import re
from pathlib import Path

from xr_syntax.i18n import tr

# 插入的 .got 输出段，按 .fini_array 的缩进和内存区域写出。
# The inserted .got output section, written with the indentation and memory region of .fini_array.
GOT_SECTION = """\
{indent}/* GOT entries are link-time constants in this static image. ld.lld puts the init/fini
{indent}   arrays and the GOT in one RELRO range, so .got must directly follow .fini_array. */
{indent}.got :
{indent}{{
{indent}  . = ALIGN(4);
{indent}  *(.got .got.*)
{indent}  . = ALIGN(4);
{indent}}}{region_suffix}
"""

# 一行开头的输出段定义：段名，可选的括号（如 (READONLY)、(NOLOAD)），其他属性，然后是冒号。
# An output section definition at the start of a line: the name, an optional parenthesis such as
# (READONLY) or (NOLOAD), other attributes, then the colon.
_SECTION_HEADER = re.compile(
    r"^(?P<indent>[ \t]*)(?P<name>\.[\w.$]+|/DISCARD/)[ \t]*(?:\([^)\n]*\))?[^:;=\n{}]*:(?![=:])",
    re.MULTILINE,
)
# 输出段右花括号后的可选子句（>REGION、AT>LMA_REGION、:PHDR、=FILL 及末尾逗号），允许跨行。
# Optional clauses after the closing brace of an output section (>REGION, AT>LMA_REGION,
# :PHDR, =FILL and an optional trailing comma), which may span lines.
_SECTION_TRAILER = re.compile(
    r"(?:\s*(?:\bAT\s*>\s*[^\s:;={}/,]+|>\s*[^\s:;={}/,]+|:\s*[^\s:;={}/,]+|=\s*(?:\([^)]*\)|[^\s;{},]+)))*"
    r"(?:\s*,)?"
)
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _without_comments(text: str) -> str:
    """把 text 中的注释换成等长的空格（换行与回车保留），使偏移量不变。
    Replace the comments of text with as many spaces (keeping the line breaks and carriage returns),
    so that offsets stay the same.
    """
    return _COMMENT.sub(lambda m: re.sub(r"[^\r\n]", " ", m.group()), text)


def _sections(code: str) -> list[re.Match]:
    """code（已去掉注释）中 SECTIONS 块顶层的输出段定义，按出现顺序排列。
    The output section definitions at the top level of the SECTIONS block of code (with the
    comments removed), in their order.
    """
    found = []
    for match in _SECTION_HEADER.finditer(code):
        start = match.start("name")
        if code.count("{", 0, start) - code.count("}", 0, start) == 1:
            found.append(match)
    return found


def _closing_line_end(code: str, header: re.Match) -> tuple[int, str] | None:
    """header 所定义的输出段结束的位置：(包含跨行区域子句的结束行末尾偏移量, 内存区域等后缀)。
    Where the output section that header defines ends: (the end-of-line offset after its closing
    brace and any following region clause across lines, the region/trailer suffix).

    找不到配对的花括号时为 None。
    None when the braces do not pair up.
    """
    opening = code.find("{", header.end())
    if opening < 0:
        return None
    depth = 0
    for index in range(opening, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                trailer = _SECTION_TRAILER.match(code, index + 1)
                trailer_end = trailer.end() if trailer else index + 1
                line_end = code.find("\n", trailer_end)
                line_end = len(code) if line_end < 0 else line_end
                region = " ".join(trailer.group().split()).rstrip(",").strip() if trailer else ""
                return line_end, region
    return None


def place_got(file_path: str | Path) -> bool:
    """确保链接脚本 file_path 中 .got 输出段紧跟 .fini_array；写回时返回 True。
    Make sure the .got output section of the linker script file_path directly follows
    .fini_array; True when the file was written back.

    没有 .got 输出段时，在 .fini_array 之后插入一个，内存区域与 .fini_array 相同。已有的 .got
    不紧跟 .fini_array 时只给出警告，用户写的段不动；没有 .fini_array 时也只警告。未改动行的换行符
    和编码保持原样；仅新插入块使用 .fini_array 结束行的换行符。
    Without a .got output section, one is inserted after .fini_array, in the memory region of
    .fini_array. An existing .got that does not directly follow .fini_array only gets a warning,
    and the user's section stays; a script without .fini_array also only gets a warning. Untouched
    lines keep their original line endings and encoding; only the inserted block uses the line
    ending of .fini_array's closing line.
    """
    path = Path(file_path)
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="surrogateescape")
    code = _without_comments(text)
    sections = _sections(code)
    names = [match.group("name") for match in sections]
    if ".fini_array" not in names:
        logging.warning(
            tr(
                f"{path} has no .fini_array output section; the GOT placement is left unchanged",
                f"{path} 中没有 .fini_array 输出段，GOT 的位置保持不变",
            )
        )
        return False
    fini = names.index(".fini_array")
    if ".got" in names:
        if fini + 1 < len(names) and names[fini + 1] == ".got":
            return False
        logging.warning(
            tr(
                f"{path}: the .got output section does not directly follow .fini_array, so an "
                'ST Arm Clang (ld.lld) link can fail with ".got is not contiguous with other '
                'relro sections"; move it directly after .fini_array',
                f"{path} 中的 .got 输出段没有紧跟 .fini_array，ST Arm Clang（ld.lld）链接可能报"
                "“.got is not contiguous with other relro sections”；请把它移到 .fini_array 之后",
            )
        )
        return False
    closing = _closing_line_end(code, sections[fini])
    if closing is None:
        logging.warning(
            tr(
                f"{path}: the braces of .fini_array do not pair up; the GOT placement is left "
                "unchanged",
                f"{path} 中 .fini_array 的花括号不配对，GOT 的位置保持不变",
            )
        )
        return False
    line_end, region = closing
    if line_end < len(text) and line_end > 0 and text[line_end - 1] == "\r":
        insert_at = line_end - 1
        eol = "\r\n"
    elif line_end < len(text):
        insert_at = line_end
        eol = "\n"
    else:
        insert_at = line_end
        eol = "\r\n" if "\r\n" in text else "\n"
    region_suffix = f" {region}" if region else ""
    block = GOT_SECTION.format(
        indent=sections[fini].group("indent"),
        region_suffix=region_suffix,
    )
    inserted = block.rstrip("\n").replace("\n", eol)
    placed = text[:insert_at] + eol + eol + inserted + text[insert_at:]
    path.write_bytes(placed.encode("utf-8", errors="surrogateescape"))
    if region:
        logging.info(
            tr(
                f"Placed the GOT in {region.lstrip('>').strip()} after .fini_array in {path}",
                f"已在 {path} 中把 GOT 放进 {region.lstrip('>').strip()}，紧跟 .fini_array",
            )
        )
    else:
        logging.info(
            tr(
                f"Placed the GOT after .fini_array in {path}",
                f"已在 {path} 中把 GOT 紧跟 .fini_array 放置",
            )
        )
    return True


def place_got_in_project(input_directory: str | Path) -> list[Path]:
    """对 input_directory 根目录中的每个链接脚本（*.ld）调用 place_got()，返回写回的文件。
    Call place_got() on each linker script (*.ld) in the root of input_directory; return the
    files that were written back.

    CubeMX 的 CMake 工程把链接脚本放在根目录；一个工程可以有多份（如 Flash 和 RAM 各一份）。
    A CubeMX CMake project keeps its linker scripts in the root; a project can have several
    (one for Flash and one for RAM, for example).
    """
    scripts = sorted(
        entry
        for entry in Path(input_directory).iterdir()
        if entry.is_file() and entry.suffix.lower() == ".ld"
    )
    return [script for script in scripts if place_got(script)]
