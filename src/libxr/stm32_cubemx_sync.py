"""libxr stm32 sync：STM32CubeMX 生成代码之后更新 LibXR 的生成结果。
libxr stm32 sync: bring the LibXR output up to date after STM32CubeMX generated the code.

sync 依次解析 .ioc、生成 User/app_main.cpp、在 .ioc 中关掉 LibXR 接管的 OTG 实例的初始化调用
和中断函数生成（见 otg_ioc_edits()）、更新 CMake 集成。setup 把 cmake/libxr_after_cubemx.cmd
登记为 CubeMX 的生成后脚本（Project Manager > Code Generator > User Actions > After Code
Generation），此后每次在 CubeMX 中生成代码都会自动运行 sync。
sync parses the .ioc file, generates User/app_main.cpp, turns off in the .ioc file the
initialization call and the interrupt handler of the OTG instances LibXR takes over (see
otg_ioc_edits()) and updates the CMake integration. setup registers
cmake/libxr_after_cubemx.cmd as the post-generation script of CubeMX (Project Manager > Code
Generator > User Actions > After Code Generation), so every code generation in CubeMX runs
sync.

CubeMX 只把脚本的输出显示在 Output 窗口，脚本失败时照常完成生成。所以 sync 的输出同时写进
.libxr/after_cubemx.log，生成的 User/app_main.cpp 记下 .ioc 的摘要；LibXR.CMake 在配置时比对
摘要，.ioc 改过而代码没有重新生成时让构建失败，并给出日志的内容和要运行的命令。
CubeMX shows the output of the script only in its Output window and completes the generation
when the script fails. So the output of sync also goes to .libxr/after_cubemx.log, and the
generated User/app_main.cpp records the digest of the .ioc file; LibXR.CMake compares the
digest when it configures and fails the build when the .ioc file changed without the code being
generated again, with the content of the log and the command to run.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterable

from xr_syntax.i18n import tr

from libxr.output import _Formatter

# 生成后脚本：同一个文件在 Windows 上由 cmd 执行，在 Linux 和 macOS 上由 sh 执行（第一行以 :
# 开头，对 cmd 是标号，对 sh 是空命令）。sh 部分交给 HOOK_SH。
# The post-generation script: the same file runs under cmd on Windows and under sh on Linux and
# macOS (its first line starts with :, a label for cmd and an empty command for sh). The sh part
# hands over to HOOK_SH.
HOOK_CMD = "cmake/libxr_after_cubemx.cmd"
HOOK_SH = "cmake/libxr_after_cubemx.sh"
# sync 的输出，LibXR.CMake 报错时附上它。
# The output of sync, which LibXR.CMake shows when it fails the build.
SYNC_LOG = ".libxr/after_cubemx.log"
# .ioc 中生成后脚本的键。
# The key of the post-generation script in the .ioc file.
AFTER_SCRIPT_KEY = "ProjectManager.UAScriptAfterPath"

# 第一行：sh 执行到这里为止（# 之后是注释，吞掉 Windows 签出时行尾的 \r）；cmd 把它当标号。
# The first line: sh stops here (what follows # is a comment and absorbs the \r of a Windows
# checkout); cmd takes it for a label.
HOOK_CMD_TEXT = """:; exec sh "$(dirname "$0")/libxr_after_cubemx.sh" "$@"; exit 1 #
@echo off
rem STM32CubeMX runs this after it generates the code (Project Manager > Code Generator >
rem User Actions > After Code Generation). It runs `libxr stm32 sync` for this project;
rem the output also goes to .libxr\\after_cubemx.log. Written by `libxr stm32 setup`.
setlocal
cd /d "%~dp0.."
if not exist .libxr mkdir .libxr
if defined LIBXR_PYTHON (
  "%LIBXR_PYTHON%" -c "import libxr.stm32_cubemx_sync" >nul 2>&1 && (
    "%LIBXR_PYTHON%" -m libxr stm32 sync --after-cubemx -d .
    exit /b
  )
)
python -c "import libxr.stm32_cubemx_sync" >nul 2>&1 && (
  python -m libxr stm32 sync --after-cubemx -d .
  exit /b
)
py -3 -c "import libxr.stm32_cubemx_sync" >nul 2>&1 && (
  py -3 -m libxr stm32 sync --after-cubemx -d .
  exit /b
)
> .libxr\\after_cubemx.log echo [LibXR] FAILED: no Python with a libxr that has `libxr stm32 sync` was found (tried LIBXR_PYTHON, python, py -3). Install or update it with `pip install -U libxr`, or set LIBXR_PYTHON to the python.exe that has it, then run `python -m libxr stm32 sync` in %CD%.
type .libxr\\after_cubemx.log
exit /b 1
"""

HOOK_SH_TEXT = """#!/bin/sh
# STM32CubeMX runs this through libxr_after_cubemx.cmd after it generates the code (Project
# Manager > Code Generator > User Actions > After Code Generation). It runs
# `libxr stm32 sync` for this project; the output also goes to .libxr/after_cubemx.log.
# Written by `libxr stm32 setup`.
cd "$(dirname "$0")/.." || exit 1
mkdir -p .libxr
for py in ${LIBXR_PYTHON:-} python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import libxr.stm32_cubemx_sync" >/dev/null 2>&1; then
    exec "$py" -m libxr stm32 sync --after-cubemx -d .
  fi
done
echo "[LibXR] FAILED: no Python with a libxr that has \\`libxr stm32 sync\\` was found (tried ${LIBXR_PYTHON:+$LIBXR_PYTHON, }python3, python). Install or update it with \\`pip install -U libxr\\`, or set LIBXR_PYTHON to the python that has it, then run \\`python3 -m libxr stm32 sync\\` in $(pwd)." > .libxr/after_cubemx.log
cat .libxr/after_cubemx.log
exit 1
"""


def ioc_digest(ioc_path: str) -> str:
    """.ioc 文件的摘要：去掉回车后的 SHA-256 的前 16 个十六进制字符，与签出时的换行方式无关；
    LibXR.CMake 用同样的方法计算。
    The digest of an .ioc file: the first 16 hex characters of the SHA-256 without carriage
    returns, independent of the line endings of the checkout; LibXR.CMake computes it the same
    way.
    """
    with open(ioc_path, "rb") as f:
        data = f.read().replace(b"\r", b"")
    return hashlib.sha256(data).hexdigest()[:16]


def _read_ioc(ioc_path: str) -> str:
    """原样读出 .ioc 文本，换行不转换。
    Read the .ioc text as it is, without newline translation.
    """
    with open(ioc_path, encoding="utf-8", newline="") as f:
        return f.read()


def _write_ioc(ioc_path: str, text: str) -> None:
    """原样写回 .ioc 文本。
    Write the .ioc text back as it is.
    """
    with open(ioc_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _line_pattern(key: str) -> re.Pattern:
    """匹配 .ioc 中 key=值 一行（值不含行尾的回车）。
    Match the key=value line of key in the .ioc text (the value without the trailing carriage
    return).
    """
    return re.compile(rf"^{re.escape(key)}=([^\r\n]*)", re.MULTILINE)


def otg_ioc_edits(text: str, instances: Iterable[str]) -> tuple[str, list[str]]:
    """在 .ioc 文本 text 中关掉 OTG 实例 instances（如 USB_OTG_FS）的初始化调用和中断函数生成，
    返回新文本和改动的说明。
    Turn off in the .ioc text text the initialization call and the interrupt handler of the OTG
    instances (such as USB_OTG_FS), returning the new text and descriptions of the changes.

    ProjectManager.functionlistsort 中 MX_<实例>_PCD_Init 一项的 <不调用> 设为 true、<静态> 设为
    false（没有调用的 static 函数会产生编译警告）；NVIC.<OTG_x>_IRQn 的第 6 项（Generate IRQ
    handler）设为 false，中断本身保持开启。之后 CubeMX 生成的代码不再引用 PCD 句柄，它和 HAL 的
    PCD 代码被链接器回收。
    In ProjectManager.functionlistsort the <no call> field of MX_<instance>_PCD_Init becomes true
    and its <static> field false (an uncalled static function draws a compiler warning); field 6
    of NVIC.<OTG_x>_IRQn (Generate IRQ handler) becomes false while the interrupt stays enabled.
    The code CubeMX generates afterwards no longer references the PCD handle, and the linker
    drops it with the PCD code of HAL.
    """
    changes = []
    for name in instances:
        function = f"MX_{name}_PCD_Init"
        pattern = _line_pattern("ProjectManager.functionlistsort")
        match = pattern.search(text)
        if match:
            entries = match.group(1).split(",")
            for index, entry in enumerate(entries):
                parts = entry.split("-")
                offset = 1 if parts and parts[0] in ("true", "false") else 0
                if len(parts) < offset + 6 or parts[offset + 1] != function:
                    continue
                wanted = parts[:]
                wanted[offset + 3] = "true"
                wanted[offset + 5] = "false"
                if wanted != parts:
                    entries[index] = "-".join(wanted)
                    changes.append(
                        tr(
                            f"{function}: not called from main(), not static",
                            f"{function}：main() 不调用，非 static",
                        )
                    )
            text = text[: match.start(1)] + ",".join(entries) + text[match.end(1) :]
        irq = f"{name[len('USB_') :]}_IRQn"
        nvic = _line_pattern(f"NVIC.{irq}").search(text)
        if nvic:
            fields = nvic.group(1).split("\\:")
            if len(fields) > 5 and fields[5] != "false":
                fields[5] = "false"
                text = text[: nvic.start(1)] + "\\:".join(fields) + text[nvic.end(1) :]
                changes.append(
                    tr(
                        f"{irq}: the IRQ handler is not generated",
                        f"{irq}：不生成中断函数",
                    )
                )
    return text, changes


def set_after_script(text: str, path: str) -> tuple[str, str | None]:
    """把 .ioc 文本 text 中的生成后脚本设为 path，返回新文本和原来的脚本（没有或为空时为 None）。
    原来已是别的脚本时不改动。
    Set the post-generation script in the .ioc text text to path, returning the new text and the
    earlier script (None when there was none or it was empty). An earlier, different script is
    left in place.
    """
    match = _line_pattern(AFTER_SCRIPT_KEY).search(text)
    if match:
        current = match.group(1).strip()
        if current and current != path:
            return text, current
        if current == path:
            return text, None
        return text[: match.start(1)] + path + text[match.end(1) :], None
    # 没有这个键时，按键名的顺序插入到 ProjectManager 段中。
    # Without the key, it goes into the ProjectManager section in key order.
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    line = f"{AFTER_SCRIPT_KEY}={path}"
    for index, existing in enumerate(lines):
        if existing.startswith("ProjectManager.") and existing > line:
            lines.insert(index, line)
            break
    else:
        lines.insert(len(lines) - 1 if lines and not lines[-1] else len(lines), line)
    return newline.join(lines), None


def _write_if_changed(path: str, text: str) -> bool:
    """内容不同时写出 path（LF 换行），返回是否写了。
    Write path (LF newlines) when its content differs, returning whether it was written.
    """
    try:
        with open(path, encoding="utf-8", newline="") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


def _make_executable(project_dir: str, relative: str) -> None:
    """给脚本加上可执行权限；它已由 Git 跟踪时，同时在索引中记下可执行位，使 Linux 上的签出也能
    直接执行它（Windows 的文件系统不保存这一位）。
    Make the script executable; when Git tracks it, also record the executable bit in the
    index, so that a Linux checkout can run it directly (Windows file systems do not keep that
    bit).
    """
    path = os.path.join(project_dir, relative)
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", relative],
            cwd=project_dir,
            capture_output=True,
        )
        if tracked.returncode == 0:
            subprocess.run(
                ["git", "update-index", "--chmod=+x", relative],
                cwd=project_dir,
                capture_output=True,
            )
    except OSError:
        pass


def _ignore_log(project_dir: str) -> None:
    """工程的 .gitignore 不忽略 .libxr/ 时加上这一行。
    Add .libxr/ to the .gitignore of the project when it does not ignore it yet.
    """
    path = os.path.join(project_dir, ".gitignore")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", newline="") as f:
        content = f.read()
    if any(line.strip() in (".libxr/", ".libxr", ".libxr/**") for line in content.splitlines()):
        return
    newline = "\r\n" if "\r\n" in content else "\n"
    if content and not content.endswith(("\n", "\r")):
        content += newline
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content + ".libxr/" + newline)


def install_hook(project_dir: str, ioc_path: str) -> None:
    """写出生成后脚本并在 .ioc 中登记它；.ioc 已登记别的脚本时保留它，记录警告说明怎样从那个脚本
    调用 HOOK_CMD。
    Write the post-generation script and register it in the .ioc file; an .ioc file that
    already registers another script keeps it, with a warning that tells how to call HOOK_CMD
    from that script.
    """
    for relative, text in ((HOOK_CMD, HOOK_CMD_TEXT), (HOOK_SH, HOOK_SH_TEXT)):
        if _write_if_changed(os.path.join(project_dir, relative), text):
            logging.info(tr(f"Wrote {relative}", f"已写入 {relative}"))
    _make_executable(project_dir, HOOK_CMD)
    _ignore_log(project_dir)
    text = _read_ioc(ioc_path)
    new_text, other = set_after_script(text, HOOK_CMD)
    name = os.path.basename(ioc_path)
    if other:
        logging.warning(
            tr(
                f"{name} already runs {other} after code generation; LibXR does not replace "
                f"it. Call {HOOK_CMD} at the end of that script so that every CubeMX code "
                "generation also runs `libxr stm32 sync`",
                f"{name} 已登记生成后脚本 {other}；LibXR 不替换它。请在该脚本末尾调用 "
                f"{HOOK_CMD}，使每次在 CubeMX 中生成代码时也运行 `libxr stm32 sync`",
            )
        )
    elif new_text != text:
        _write_ioc(ioc_path, new_text)
        logging.info(
            tr(
                f"{name}: STM32CubeMX now runs {HOOK_CMD} after each code generation",
                f"{name}：STM32CubeMX 此后每次生成代码都会运行 {HOOK_CMD}",
            )
        )


def apply_otg_settings(ioc_path: str, instances: Iterable[str]) -> bool:
    """在 .ioc 中关掉 instances 的初始化调用和中断函数生成（见 otg_ioc_edits()），返回是否改动。
    Turn off the initialization call and the interrupt handler of instances in the .ioc file
    (see otg_ioc_edits()), returning whether anything changed.
    """
    instances = list(instances)
    if not instances:
        return False
    text = _read_ioc(ioc_path)
    new_text, changes = otg_ioc_edits(text, instances)
    if new_text == text:
        return False
    _write_ioc(ioc_path, new_text)
    name = os.path.basename(ioc_path)
    logging.info(
        tr(
            f"{name}: {'; '.join(changes)}. They take effect when STM32CubeMX generates the "
            "code next time (reopen the .ioc file if it is open in STM32CubeMX); until then "
            "LibXR takes over the generated calls",
            f"{name}：{'；'.join(changes)}。下次在 STM32CubeMX 中生成代码时生效（.ioc 正在 "
            "STM32CubeMX 中打开时需重新打开）；在此之前由 LibXR 接管生成的调用",
        )
    )
    return True


def sync(project_dir: str, after_cubemx: bool = False) -> int:
    """更新 project_dir 中 LibXR 的生成结果，返回退出码（0 成功，1 失败）。
    Bring the LibXR output of project_dir up to date and return the exit code (0 on success, 1
    on failure).

    依次：检查工程（见 check_project()）；解析 .ioc 并生成 User/app_main.cpp（沿用工程是否使用
    XRobot）；在 .ioc 中关掉 LibXR 接管的 OTG 实例的初始化调用和中断函数生成，有改动时再解析和
    生成一次，使 app_main.cpp 记下改后 .ioc 的摘要；更新 CMake 集成；更新已安装的生成后脚本。
    输出同时写进 SYNC_LOG，最后一行是结果。after_cubemx 表示由 CubeMX 的生成后脚本调用，结果行
    这时说明怎样手动重试。
    In order: check the project (see check_project()); parse the .ioc file and generate
    User/app_main.cpp, keeping whether the project uses XRobot; turn off in the .ioc file the
    initialization call and the interrupt handler of the OTG instances LibXR takes over, and
    parse and generate once more after a change, so that app_main.cpp records the digest of the
    changed .ioc file; update the CMake integration; update an installed post-generation
    script. The output also goes to SYNC_LOG, whose last line is the result. after_cubemx means
    the post-generation script of CubeMX called it; the result line then tells how to retry by
    hand.
    """
    from libxr import generator_code_stm32
    from libxr.config_cubemx_project import check_project
    from libxr.generator_stm32_cmake import integrate, project_uses_xrobot
    from libxr.peripheral_analyzer_stm32 import parse_project

    project_dir = os.path.abspath(project_dir)
    log_path = os.path.join(project_dir, SYNC_LOG)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(_Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    ok = False
    try:
        if after_cubemx:
            logging.info(
                tr(
                    "LibXR: updating the generated code after STM32CubeMX",
                    "LibXR：在 STM32CubeMX 生成代码之后更新 LibXR 的生成结果",
                )
            )
        ioc_path = check_project(project_dir)
        yaml_path = os.path.join(project_dir, ".config.yaml")
        cpp_path = os.path.join(project_dir, "User", "app_main.cpp")
        use_xrobot = project_uses_xrobot(project_dir)
        for _ in range(2):
            parse_project(project_dir, yaml_path, summary=False)
            generator_code_stm32.generate(yaml_path, cpp_path, use_xrobot)
            if not apply_otg_settings(ioc_path, generator_code_stm32.otg_devices):
                break
        integrate(project_dir)
        if os.path.exists(os.path.join(project_dir, HOOK_CMD)):
            for relative, text in ((HOOK_CMD, HOOK_CMD_TEXT), (HOOK_SH, HOOK_SH_TEXT)):
                _write_if_changed(os.path.join(project_dir, relative), text)
            _make_executable(project_dir, HOOK_CMD)
        ok = True
    except SystemExit as error:
        # The steps log their own error before they exit.
        ok = not error.code
    except Exception as error:  # noqa: BLE001 - every failure must reach the log
        logging.error(str(error))
        logging.debug(tr("Traceback:", "调用栈："), exc_info=True)
    finally:
        if ok:
            logging.info(tr("[LibXR] OK", "[LibXR] 完成"))
        else:
            logging.error(
                tr(
                    f"[LibXR] FAILED. Fix the error above, then run `python -m libxr stm32 "
                    f"sync` in {project_dir}, or generate the code in STM32CubeMX again",
                    f"[LibXR] 失败。修正上面的错误后，在 {project_dir} 中运行 `python -m libxr "
                    "stm32 sync`，或在 STM32CubeMX 中重新生成代码",
                )
            )
        root.removeHandler(handler)
        handler.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(sync(os.curdir))
