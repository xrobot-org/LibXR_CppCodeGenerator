#!/usr/bin/env python

"""以脚本模式运行 STM32CubeMX 生成工程代码，按命令行参数回答 CubeMX 弹出的对话框。
Run STM32CubeMX in script mode to generate project code, answering the dialogs CubeMX shows as the
command line says.

Windows 上 CubeMX 以 Java Access Bridge 启动，从外部读出对话框的标题、正文和按钮，按名称点击
按钮；认不出或参数没有覆盖的对话框使运行停止并报出它的内容。Linux 上只认出对话框并停止。
On Windows CubeMX is started with the Java Access Bridge, so the title, text and buttons of a
dialog are read from outside and a button is clicked by name; a dialog that is not recognized or
not covered by the options stops the run and reports its content. On Linux a dialog is only
recognized and stops the run.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from xr_syntax.i18n import tr

LOGGER = logging.getLogger(__name__)

DEFAULT_EXPECT_PATHS = ("Core/Inc", "Drivers")

# 让 CubeMX 的 JVM 加载 Java Access Bridge，对话框的内容才能从外部读取。
# Makes CubeMX's JVM load the Java Access Bridge, so dialogs can be read from outside.
ACCESS_BRIDGE_OPTION = (
    "-Djavax.accessibility.assistive_technologies=com.sun.java.accessibility.AccessBridge"
)

# CubeMX 6.17 和 6.18 中对话框的文字（plugins 中的 projectmanager.jar、filemanager.jar 和
# updater.jar）。
# Dialog texts of CubeMX 6.17 and 6.18 (projectmanager.jar, filemanager.jar and updater.jar in
# plugins).
FIRMWARE_TITLE = "New STM32Cube firmware version available"
FIRMWARE_TEXTS = (
    "Continue now or Migrate Project ?",
    "Download now or Migrate Project ?",
    "You need to migrate the project",
    "Set this connection (and download) now or Migrate Project ?",
    "You can continue with this Firmware Package",
)
CONFIRM_MIGRATION_TEXT = "Do you confirm this migration ?"
DOWNLOAD_TEXTS = ("Download now or Continue ?", "Do you want to download this now ?")
LICENSE_TITLES = ("License Agreement", "Licensing Agreement")
LICENSE_AGREE = "I have read, and I agree to the terms of this license agreement"
# myST 登录（6.17 的 userauth.jar；6.18 下载不再要求登录）和代理认证（User Login）。
# The myST login (userauth.jar of 6.17; 6.18 downloads without a login) and proxy
# authentication (User Login).
LOGIN_TEXTS = ("myST login", "User Login", "User Authentication Manager")


@dataclass(frozen=True)
class Dialog:
    """从 CubeMX 读出的一个对话框：标题、正文（各行文字）、按钮和单选框/复选框的名称，以及
    是否带进度条。
    A dialog read from CubeMX: its title, its text (one line per label), the names of its
    buttons and of its radio buttons and check boxes, and whether it shows a progress bar.
    """

    title: str
    text: str = ""
    buttons: tuple[str, ...] = ()
    choices: tuple[str, ...] = ()
    progress: bool = False

    def describe(self) -> str:
        """对话框的标题、正文和按钮，用于报错。
        The title, text and buttons of the dialog, for error messages.
        """
        lines = [f'"{self.title}"', *self.text.splitlines()[:12]]
        if self.buttons:
            lines.append(tr("Buttons: ", "按钮：") + " / ".join(self.buttons))
        return "\n".join(line for line in lines if line.strip())


@dataclass(frozen=True)
class DialogAnswer:
    """对一个对话框的回答：先选中 select（单选框或复选框，可为空），再点击 click 按钮；click
    为空表示等待对话框自行关闭（WAIT）。
    The answer to a dialog: select (a radio button or check box, may be empty), then click; an
    empty click means waiting for the dialog to close by itself (WAIT).
    """

    click: str
    select: str = ""


WAIT = DialogAnswer(click="")


class DialogStopped(RuntimeError):
    """CubeMX 显示了不按参数回答的对话框；运行停止，信息说明对话框内容和处理办法。
    CubeMX showed a dialog that the options do not answer; the run stops and the message gives
    the dialog content and what to do.
    """


def _first(buttons: Sequence[str], *names: str) -> str:
    """names 中第一个出现在 buttons 里的名称；都没有时为空字符串。
    The first of names that is among buttons; an empty string when none is.
    """
    return next((name for name in names if name in buttons), "")


def answer_dialog(
    dialog: Dialog, firmware: str | None = None, download: bool = False
) -> DialogAnswer:
    """按命令行参数回答一个 CubeMX 对话框。
    Answer a CubeMX dialog as the command line options say.

    固件版本对话框（工程由另一版本的 CubeMX 保存）：firmware 为 keep 时选 Continue（缺少原固件包
    时，download 为真则选 Download），为 migrate 时选 Migrate 并确认迁移。缺少固件包的下载对话框：
    download 为真时选 Download。许可协议：download 为真时勾选同意并完成。ST 账号登录和其他对话框
    一律停止。
    The firmware version dialog (a project saved by another CubeMX version): with firmware keep,
    Continue (Download instead when the original package is missing and download is set); with
    firmware migrate, Migrate and the migration confirmation. The download dialog of a missing
    firmware package: Download when download is set. A license agreement: agree and finish when
    download is set. An ST account login and any other dialog always stop.

    带进度条的对话框（下载、解压、生成）只是显示进度，回答 WAIT，等它自行关闭。
    A dialog with a progress bar (download, unpacking, generation) only shows progress; the
    answer is WAIT until it closes by itself.

    Raises:
        DialogStopped: 这个对话框不按参数回答。
            The options do not answer this dialog.
    """
    content = f"{dialog.title}\n{dialog.text}"
    if any(text in content for text in LOGIN_TEXTS):
        raise DialogStopped(
            tr(
                "STM32CubeMX asks for a myST login to download software. Sign in once in "
                "STM32CubeMX, then run again.",
                "STM32CubeMX 要求登录 myST 账号才能下载软件。请先在 STM32CubeMX 中登录一次，"
                "然后重新运行。",
            )
            + "\n"
            + dialog.describe()
        )

    if dialog.progress:
        return WAIT

    if dialog.title in LICENSE_TITLES or LICENSE_AGREE in dialog.choices:
        button = _first(dialog.buttons, "Finish", "OK", "Accept", "Next", "Install")
        if download and LICENSE_AGREE in dialog.choices and button:
            return DialogAnswer(click=button, select=LICENSE_AGREE)
        raise DialogStopped(
            tr(
                "STM32CubeMX asks to accept the license of a package it downloads. Pass "
                "--download to accept it, or install the package in STM32CubeMX first.",
                "STM32CubeMX 要求接受它下载的软件包的许可协议。传入 --download 表示接受，"
                "或先在 STM32CubeMX 中安装该软件包。",
            )
            + "\n"
            + dialog.describe()
        )

    if CONFIRM_MIGRATION_TEXT in dialog.text:
        button = _first(dialog.buttons, "Yes", "OK", "Migrate")
        if firmware == "migrate" and button:
            return DialogAnswer(click=button)
        raise DialogStopped(_firmware_message(dialog))

    if dialog.title == FIRMWARE_TITLE or any(text in dialog.text for text in FIRMWARE_TEXTS):
        if firmware == "keep":
            if "Continue" in dialog.buttons:
                return DialogAnswer(click="Continue")
            if download and "Download" in dialog.buttons:
                return DialogAnswer(click="Download")
            raise DialogStopped(
                tr(
                    "Keeping the project's firmware package needs it installed. Pass --download "
                    "to download it, or --firmware migrate.",
                    "沿用工程原来的固件包需要先安装它。传入 --download 下载它，"
                    "或改用 --firmware migrate。",
                )
                + "\n"
                + dialog.describe()
            )
        if firmware == "migrate" and "Migrate" in dialog.buttons:
            return DialogAnswer(click="Migrate")
        raise DialogStopped(_firmware_message(dialog))

    if any(text in dialog.text for text in DOWNLOAD_TEXTS):
        button = _first(dialog.buttons, "Download", "Yes")
        if download and button:
            return DialogAnswer(click=button)
        raise DialogStopped(
            tr(
                "STM32CubeMX needs a firmware package that is not installed. Pass --download to "
                "download it.",
                "STM32CubeMX 需要一个尚未安装的固件包。传入 --download 下载它。",
            )
            + "\n"
            + dialog.describe()
        )

    raise DialogStopped(
        tr(
            "STM32CubeMX shows a dialog that libxr does not answer:",
            "STM32CubeMX 显示了 libxr 不回答的对话框：",
        )
        + "\n"
        + dialog.describe()
    )


def _firmware_message(dialog: Dialog) -> str:
    """固件版本对话框没有对应参数时的报错：说明 --firmware 的两种选择。
    The error for a firmware version dialog without a matching option: names both --firmware
    choices.
    """
    return (
        tr(
            "The project was saved by another STM32CubeMX version. Pass --firmware keep to stay "
            "on its firmware package, or --firmware migrate to migrate it.",
            "工程由另一版本的 STM32CubeMX 保存。传入 --firmware keep 沿用它的固件包，"
            "或 --firmware migrate 迁移工程。",
        )
        + "\n"
        + dialog.describe()
    )


class _AccessibleContextInfo(ctypes.Structure):
    """Java Access Bridge 的 AccessibleContextInfo 结构。
    The AccessibleContextInfo structure of the Java Access Bridge.
    """

    _fields_ = [
        ("name", ctypes.c_wchar * 1024),
        ("description", ctypes.c_wchar * 1024),
        ("role", ctypes.c_wchar * 256),
        ("role_en_US", ctypes.c_wchar * 256),
        ("states", ctypes.c_wchar * 256),
        ("states_en_US", ctypes.c_wchar * 256),
        ("indexInParent", ctypes.c_int32),
        ("childrenCount", ctypes.c_int32),
        ("x", ctypes.c_int32),
        ("y", ctypes.c_int32),
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
        ("accessibleComponent", ctypes.c_int),
        ("accessibleAction", ctypes.c_int),
        ("accessibleSelection", ctypes.c_int),
        ("accessibleText", ctypes.c_int),
        ("accessibleInterfaces", ctypes.c_int),
    ]


class _AccessibleActionInfo(ctypes.Structure):
    """Java Access Bridge 的 AccessibleActionInfo 结构（动作名称）。
    The AccessibleActionInfo structure of the Java Access Bridge, an action name.
    """

    _fields_ = [("name", ctypes.c_wchar * 256)]


class _AccessibleActionsToDo(ctypes.Structure):
    """Java Access Bridge 的 AccessibleActionsToDo 结构（要执行的动作）。
    The AccessibleActionsToDo structure of the Java Access Bridge, the actions to perform.
    """

    _fields_ = [("actionsCount", ctypes.c_int32), ("actions", _AccessibleActionInfo * 32)]


class _AccessBridge:
    """windowsaccessbridge-64.dll 的封装：读出 Java 对话框的内容，按名称执行动作。
    A wrapper of windowsaccessbridge-64.dll: read the content of a Java dialog and perform an
    action by name.

    必须在创建它的线程中持续处理 Windows 消息（pump），桥才能与 JVM 通信；CubeMX 退出时也要
    继续处理，否则 JVM 会等待桥而不结束。
    The thread that creates it must keep pumping Windows messages so that the bridge can talk to
    the JVM; that includes CubeMX's exit, or the JVM waits for the bridge and never ends.
    """

    def __init__(self, dll_path: str):
        """加载 dll 并启动桥。
        Load the dll and start the bridge.

        Raises:
            OSError: dll 无法加载。
                The dll cannot be loaded.
        """
        from ctypes import wintypes

        self.wintypes = wintypes
        self.user32 = ctypes.windll.user32
        bridge = ctypes.CDLL(dll_path)
        context = ctypes.c_int64
        bridge.Windows_run.restype = None
        bridge.isJavaWindow.argtypes = [wintypes.HWND]
        bridge.isJavaWindow.restype = wintypes.BOOL
        bridge.getAccessibleContextFromHWND.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(context),
        ]
        bridge.getAccessibleContextFromHWND.restype = wintypes.BOOL
        bridge.getAccessibleContextInfo.argtypes = [
            ctypes.c_int32,
            context,
            ctypes.POINTER(_AccessibleContextInfo),
        ]
        bridge.getAccessibleContextInfo.restype = wintypes.BOOL
        bridge.getAccessibleChildFromContext.argtypes = [ctypes.c_int32, context, ctypes.c_int32]
        bridge.getAccessibleChildFromContext.restype = context
        bridge.doAccessibleActions.argtypes = [
            ctypes.c_int32,
            context,
            ctypes.POINTER(_AccessibleActionsToDo),
            ctypes.POINTER(ctypes.c_int32),
        ]
        bridge.doAccessibleActions.restype = wintypes.BOOL
        bridge.releaseJavaObject.argtypes = [ctypes.c_int32, context]
        bridge.releaseJavaObject.restype = None
        self.bridge = bridge
        # read 取得的 Java 对象引用，由 release 释放。
        # The Java object references taken by read, freed by release.
        self.held: list[tuple[int, int]] = []
        bridge.Windows_run()

    def pump(self, seconds: float) -> None:
        """在 seconds 秒内处理本线程的 Windows 消息。
        Pump the Windows messages of this thread for seconds.
        """
        message = self.wintypes.MSG()
        end = time.monotonic() + seconds
        while True:
            while self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                self.user32.TranslateMessage(ctypes.byref(message))
                self.user32.DispatchMessageW(ctypes.byref(message))
            if time.monotonic() >= end:
                return
            time.sleep(0.02)

    def read(self, hwnd: int) -> tuple[Dialog, dict[str, tuple[int, int]]] | None:
        """读出 Java 窗口 hwnd 中的对话框，以及各按钮和选择项的 (vm, context)；不是 Java 对话框
        或读不到时为 None。
        Read the dialog of the Java window hwnd, with the (vm, context) of each button and choice;
        None when it is not a Java dialog or cannot be read.
        """
        if not self.bridge.isJavaWindow(hwnd):
            return None
        vm = ctypes.c_int32()
        root = ctypes.c_int64()
        if not self.bridge.getAccessibleContextFromHWND(hwnd, ctypes.byref(vm), ctypes.byref(root)):
            return None
        self.held.append((vm.value, root.value))
        nodes: list[tuple[str, str, int]] = []
        self._walk(vm.value, root.value, nodes, depth=0)
        if not nodes or nodes[0][0] != "dialog":
            return None
        labels = [name for role, name, _ in nodes if role in ("label", "text") and name]
        buttons = {
            name: (vm.value, ctx) for role, name, ctx in nodes if role == "push button" and name
        }
        choices = {
            name: (vm.value, ctx)
            for role, name, ctx in nodes
            if role in ("radio button", "check box") and name
        }
        dialog = Dialog(
            title=nodes[0][1],
            text="\n".join(labels),
            buttons=tuple(buttons),
            choices=tuple(choices),
            progress=any(role == "progress bar" for role, _, _ in nodes),
        )
        return dialog, {**choices, **buttons}

    def release(self) -> None:
        """释放 read 取得的全部 Java 对象引用；之后不能再使用 read 给出的目标。
        Free every Java object reference taken by read; the targets read gave are no longer
        usable afterwards.
        """
        for vm, context in self.held:
            self.bridge.releaseJavaObject(vm, context)
        self.held.clear()

    def _walk(self, vm: int, context: int, nodes: list, depth: int) -> None:
        """深度优先收集 (角色, 名称, context)，最多 12 层、每层 64 个子项。
        Collect (role, name, context) depth-first, at most 12 levels and 64 children each.
        """
        info = _AccessibleContextInfo()
        if not self.bridge.getAccessibleContextInfo(vm, context, ctypes.byref(info)):
            return
        nodes.append((info.role_en_US, info.name.strip(), context))
        if depth >= 12:
            return
        for index in range(min(info.childrenCount, 64)):
            child = self.bridge.getAccessibleChildFromContext(vm, context, index)
            if child:
                self.held.append((vm, child))
                self._walk(vm, child, nodes, depth + 1)

    def click(self, target: tuple[int, int]) -> bool:
        """对 (vm, context) 执行 click 动作（按钮、单选框和复选框都适用）。
        Perform the click action on (vm, context); it works for buttons, radio buttons and check
        boxes.
        """
        todo = _AccessibleActionsToDo()
        todo.actionsCount = 1
        todo.actions[0].name = "click"
        failure = ctypes.c_int32()
        return bool(
            self.bridge.doAccessibleActions(
                target[0], target[1], ctypes.byref(todo), ctypes.byref(failure)
            )
        )


def _windows_process_tree(process_id: int) -> set[int]:
    """Windows 上 process_id 及其全部子孙进程的进程号；快照失败时只含 process_id。
    On Windows, the ids of process_id and all its descendants; only process_id when the process
    snapshot fails.
    """
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        """Toolhelp32 的 PROCESSENTRY32W 结构。
        The Toolhelp32 PROCESSENTRY32W structure.
        """

        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    ids = {process_id}
    if snapshot in (-1, wintypes.HANDLE(-1).value):
        return ids
    parents: dict[int, int] = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    queue = [process_id]
    while queue:
        parent = queue.pop()
        for pid, ppid in parents.items():
            if ppid == parent and pid not in ids:
                ids.add(pid)
                queue.append(pid)
    return ids


def _windows_dialog_windows(process_id: int) -> list[tuple[int, str]]:
    """Windows 上 CubeMX 进程树中可见的 Java 对话框窗口（类名 SunAwtDialog）及其标题。
    On Windows, the visible Java dialog windows (class SunAwtDialog) of the CubeMX process tree,
    with their titles.
    """
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    process_ids = _windows_process_tree(process_id)
    found: list[tuple[int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        """EnumWindows 回调：收集属于这些进程的可见对话框窗口。
        EnumWindows callback: collect the visible dialog windows of these processes.
        """
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in process_ids and user32.IsWindowVisible(hwnd):
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value == "SunAwtDialog":
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                found.append((hwnd, title.value))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return found


def _x11_dialog_titles(display, root, process_ids: set[int]) -> list[str]:
    """X11 上 CubeMX 进程树中已映射的对话框窗口（带 WM_TRANSIENT_FOR）的标题。
    On X11, the titles of the mapped dialog windows (those with WM_TRANSIENT_FOR) of the CubeMX
    process tree.

    遍历期间关闭的窗口使 X 服务器返回错误（例如 BadWindow），这样的窗口跳过。
    A window closed during the walk makes the X server return an error, such as BadWindow; such a
    window is skipped.
    """
    from Xlib import X  # type: ignore
    from Xlib.error import XError  # type: ignore

    pid_atom = display.intern_atom("_NET_WM_PID")
    name_atom = display.intern_atom("_NET_WM_NAME")
    utf8_atom = display.intern_atom("UTF8_STRING")
    titles = []
    stack = [root]
    while stack:
        window = stack.pop()
        try:
            stack.extend(window.query_tree().children)
            if window.get_attributes().map_state != X.IsViewable:
                continue
            pid = window.get_full_property(pid_atom, X.AnyPropertyType)
            if not pid or int(pid.value[0]) not in process_ids:
                continue
            if window.get_wm_transient_for() is None:
                continue
            name = window.get_full_property(name_atom, utf8_atom)
            title = name.value.decode("utf-8", "replace") if name else window.get_wm_name()
            titles.append(str(title or ""))
        except XError:
            continue
    return titles


def _linux_process_tree(process_id: int) -> set[int]:
    """Linux 上 process_id 及其全部子孙进程的进程号，父子关系从 /proc/<pid>/stat 读取。
    On Linux, the ids of process_id and all its descendants, with the parent ids read from
    /proc/<pid>/stat.
    """
    parents: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join("/proc", entry, "stat"), encoding="utf-8") as stream:
                fields = stream.read().rsplit(")", 1)[1].split()
            parents[int(entry)] = int(fields[1])
        except (OSError, IndexError, ValueError):
            continue
    ids = {process_id}
    queue = [process_id]
    while queue:
        parent = queue.pop()
        for pid, ppid in parents.items():
            if ppid == parent and pid not in ids:
                ids.add(pid)
                queue.append(pid)
    return ids


class _DialogWatchThread(threading.Thread):
    """CubeMX 运行期间监视它的对话框：按 answer 回答，回答不了时记录 error 并置位 stop_event。
    Watch CubeMX's dialogs while it runs: answer them with answer, and when that is not possible
    record error and set stop_event.

    Windows 上用 bridge_dll（Java Access Bridge）读取和点击；没有它时只按窗口标题停止。Linux 上
    需要 DISPLAY 和 python-xlib，只认出对话框并停止。线程一直运行到 done 置位（CubeMX 已退出）。
    On Windows, bridge_dll (the Java Access Bridge) reads and clicks; without it the run stops on
    the window title alone. On Linux, DISPLAY and python-xlib are needed, and a dialog is only
    recognized and stops the run. The thread runs until done is set (CubeMX has exited).
    """

    def __init__(
        self,
        process_id: int,
        answer: Callable[[Dialog], DialogAnswer],
        bridge_dll: str,
        stop_event: threading.Event,
        done: threading.Event,
    ):
        """记录要监视的进程、回答方式和 Access Bridge 的 dll 路径（可为空）。
        Record the process to watch, how to answer and the Access Bridge dll path (may be empty).
        """
        super().__init__(daemon=True)
        self.process_id = process_id
        self.answer = answer
        self.bridge_dll = bridge_dll
        self.stop_event = stop_event
        self.done = done
        self.error: DialogStopped | None = None
        # 已回答的窗口及回答时刻；正在等待的进度窗口及下次查看的时刻。
        # Answered windows with the time of the answer; progress windows being waited on with
        # the time of the next look.
        self.answered: dict[int, float] = {}
        self.waiting: dict[int, float] = {}

    def run(self) -> None:
        """按平台监视对话框，直到 done 置位。
        Watch the dialogs for the platform until done is set.
        """
        try:
            if os.name == "nt":
                self._watch_windows()
            else:
                self._watch_x11()
        except DialogStopped as error:
            self.error = error
            self.stop_event.set()

    def _fail(self, error: DialogStopped) -> None:
        """记录 error，通知主线程结束 CubeMX。
        Record error and tell the main thread to end CubeMX.
        """
        self.error = error
        self.stop_event.set()

    def _watch_windows(self) -> None:
        """Windows：读出 CubeMX 的对话框，按名称点击 answer 选中的按钮。
        Windows: read CubeMX's dialogs and click, by name, the button that answer chooses.
        """
        bridge = None
        if self.bridge_dll:
            try:
                bridge = _AccessBridge(self.bridge_dll)
            except OSError as error:
                LOGGER.warning(
                    tr(
                        f"Java Access Bridge unavailable ({error}); dialogs stop the run",
                        f"Java Access Bridge 不可用（{error}）；出现对话框时停止运行",
                    )
                )
        while not self.done.is_set():
            if bridge is not None:
                bridge.pump(0.25)
            else:
                self.done.wait(0.25)
            if self.error is not None:
                continue
            windows = _windows_dialog_windows(self.process_id)
            # 已关闭窗口的句柄可能被新窗口重用，不再记着它们。
            # The handle of a closed window may be reused by a new one, so it is forgotten.
            present = {hwnd for hwnd, _ in windows}
            for seen in (self.answered, self.waiting):
                for hwnd in [hwnd for hwnd in seen if hwnd not in present]:
                    del seen[hwnd]
            for hwnd, title in windows:
                now = time.monotonic()
                if now - self.answered.get(hwnd, -10.0) < 3.0 or now < self.waiting.get(hwnd, 0.0):
                    continue
                if hwnd in self.answered:
                    self._fail(
                        DialogStopped(
                            tr(
                                f'STM32CubeMX dialog "{title}" stayed open after it was answered',
                                f"STM32CubeMX 对话框“{title}”在回答之后仍未关闭",
                            )
                        )
                    )
                    break
                if bridge is None:
                    self._fail(self._unreadable(title))
                    break
                try:
                    if not self._handle(bridge, hwnd, title):
                        break
                finally:
                    bridge.release()

    def _handle(self, bridge: _AccessBridge, hwnd: int, title: str) -> bool:
        """读出并回答窗口 hwnd 中的对话框；回答不了时记录错误并返回 False。
        Read and answer the dialog in the window hwnd; when it cannot be answered, record the
        error and return False.
        """
        read = bridge.read(hwnd)
        if read is None:
            self._fail(self._unreadable(title))
            return False
        dialog, targets = read
        LOGGER.debug(dialog.describe())
        try:
            answer = self.answer(dialog)
        except DialogStopped as error:
            self._fail(error)
            return False
        if not answer.click:
            # 进度对话框：过一会儿再看它是否已关闭或变成需要回答的对话框。
            # A progress dialog: look again later whether it has closed or now needs an answer.
            if hwnd not in self.waiting:
                LOGGER.info(
                    tr(
                        f'Waiting for STM32CubeMX: "{dialog.title}"',
                        f"等待 STM32CubeMX：“{dialog.title}”",
                    )
                )
            self.waiting[hwnd] = time.monotonic() + 2.0
            return True
        if answer.select:
            bridge.click(targets[answer.select])
        bridge.click(targets[answer.click])
        self.answered[hwnd] = time.monotonic()
        LOGGER.info(
            tr(
                f'Answered STM32CubeMX dialog "{dialog.title}": {answer.click}',
                f"已回答 STM32CubeMX 对话框“{dialog.title}”：{answer.click}",
            )
        )
        return True

    @staticmethod
    def _unreadable(title: str) -> DialogStopped:
        """无法读取内容的对话框造成的停止。
        The stop caused by a dialog whose content cannot be read.
        """
        return DialogStopped(
            tr(
                "STM32CubeMX shows a dialog that cannot be read:",
                "STM32CubeMX 显示了无法读取的对话框：",
            )
            + f' "{title}"'
        )

    def _watch_x11(self) -> None:
        """Linux：发现 CubeMX 的对话框就停止，报出它的标题。
        Linux: stop on a CubeMX dialog and report its title.
        """
        if not os.environ.get("DISPLAY"):
            return
        try:
            from Xlib import display as xdisplay  # type: ignore
        except ImportError:
            LOGGER.warning(
                tr(
                    "python-xlib is not installed; CubeMX dialogs are not detected on Linux",
                    "没有安装 python-xlib；Linux 上不会发现 CubeMX 的对话框",
                )
            )
            return
        display = xdisplay.Display()
        root = display.screen().root
        while not self.done.wait(0.5):
            titles = _x11_dialog_titles(display, root, _linux_process_tree(self.process_id))
            if titles:
                raise DialogStopped(
                    tr(
                        "STM32CubeMX shows a dialog, which libxr answers only on Windows; answer it "
                        "in STM32CubeMX, then run again:",
                        "STM32CubeMX 显示了对话框，libxr 只在 Windows 上回答对话框；"
                        "请在 STM32CubeMX 中处理后重新运行：",
                    )
                    + f' "{titles[0]}"'
                )


def _friendly_path_name(path: str) -> str:
    """路径的末级名称，用于提示信息（'.' 显示为当前目录名）；没有末级名称（如根目录）时为绝对路径。
    The last component of a path for messages, so '.' shows the current folder name; the
    absolute path when there is none, as for a root directory.
    """
    abs_path = os.path.abspath(path)
    base = os.path.basename(abs_path.rstrip(os.sep))
    return base or abs_path


def _resolve_existing_path(path_or_cmd: str) -> str:
    """把路径或命令名解析为已存在路径的绝对路径：先展开 ~ 和环境变量，再查文件系统和 PATH。
    Resolve a path or command name to the absolute path of an existing path: ~ and environment
    variables are expanded, then the file system and PATH are searched.

    Raises:
        FileNotFoundError: 路径不存在，PATH 中也找不到。
            The path does not exist and is not found on PATH.
    """
    expanded = os.path.expandvars(os.path.expanduser(path_or_cmd))
    if os.path.exists(expanded):
        return os.path.abspath(expanded)
    found = shutil.which(expanded)
    if found:
        return os.path.abspath(found)
    raise FileNotFoundError(
        tr(
            f"{path_or_cmd} does not exist and is not a command on PATH",
            f"{path_or_cmd} 不存在，PATH 中也没有这个命令",
        )
    )


def _iter_cubemx_candidates() -> Iterable[str]:
    """按顺序给出 STM32CubeMX 的候选位置：先是环境变量 STM32CUBEMX_CMD、CUBEMX_CMD、STM32CUBEMX
    中的非空值，再是当前平台的默认安装位置。
    Yield candidate STM32CubeMX locations in order: the non-empty values of the environment
    variables STM32CUBEMX_CMD, CUBEMX_CMD and STM32CUBEMX, then the platform's default install
    locations.
    """
    env_candidates = (
        os.environ.get("STM32CUBEMX_CMD", ""),
        os.environ.get("CUBEMX_CMD", ""),
        os.environ.get("STM32CUBEMX", ""),
    )
    for value in env_candidates:
        if value:
            yield value

    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", "")
        candidates = [
            "STM32CubeMX.exe",
            os.path.join(local_appdata, "Programs", "STM32CubeMX", "STM32CubeMX.exe"),
            os.path.join(
                program_files, "STMicroelectronics", "STM32Cube", "STM32CubeMX", "STM32CubeMX.exe"
            ),
        ]
    else:
        home = os.path.expanduser("~")
        candidates = [
            "STM32CubeMX",
            os.path.join(home, "STM32CubeMX", "STM32CubeMX"),
            "/opt/st/stm32cubemx/STM32CubeMX",
            "/usr/local/bin/STM32CubeMX",
        ]

    yield from candidates


def resolve_cubemx_command(explicit_cmd: str = "") -> str:
    """STM32CubeMX 的绝对路径：给出 explicit_cmd 时只解析它，否则取第一个存在的候选位置。
    The absolute path of STM32CubeMX: explicit_cmd alone when given, else the first candidate
    location that exists.

    Raises:
        FileNotFoundError: 找不到 STM32CubeMX。
            STM32CubeMX was not found.
    """
    if explicit_cmd:
        return _resolve_existing_path(explicit_cmd)

    for candidate in _iter_cubemx_candidates():
        try:
            return _resolve_existing_path(candidate)
        except FileNotFoundError:
            continue

    raise FileNotFoundError(
        tr(
            "Unable to locate STM32CubeMX. Pass --cubemx-cmd or set STM32CUBEMX_CMD.",
            "找不到 STM32CubeMX。请传入 --cubemx-cmd 或设置 STM32CUBEMX_CMD。",
        )
    )


def _iter_java_candidates(cubemx_cmd: str) -> Iterable[str]:
    """按顺序给出 Java 的候选位置：环境变量 STM32CUBEMX_JAVA、JAVA_CMD 中的非空值，JAVA_HOME 下的
    java，CubeMX 同目录 jre 中的 java，最后是命令名 java。
    Yield candidate Java locations in order: the non-empty values of the environment variables
    STM32CUBEMX_JAVA and JAVA_CMD, java under JAVA_HOME, java in the jre next to CubeMX, and
    finally the command name java.
    """
    env_candidates = (
        os.environ.get("STM32CUBEMX_JAVA", ""),
        os.environ.get("JAVA_CMD", ""),
    )
    for value in env_candidates:
        if value:
            yield value

    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        yield os.path.join(java_home, "bin", "java.exe" if os.name == "nt" else "java")

    cubemx_dir = os.path.dirname(os.path.abspath(cubemx_cmd))
    bundled_java = os.path.join(cubemx_dir, "jre", "bin", "java.exe" if os.name == "nt" else "java")
    yield bundled_java
    yield "java"


def resolve_java_command(cubemx_cmd: str, java_cmd: str = "") -> str:
    """运行 CubeMX .jar 所用 Java 的绝对路径：给出 java_cmd 时只解析它，否则取第一个存在的候选位置。
    The absolute path of the Java that runs a CubeMX .jar: java_cmd alone when given, else the
    first candidate location that exists.

    Raises:
        FileNotFoundError: 找不到 Java。
            No Java runtime was found.
    """
    if java_cmd:
        return _resolve_existing_path(java_cmd)

    for candidate in _iter_java_candidates(cubemx_cmd):
        try:
            return _resolve_existing_path(candidate)
        except FileNotFoundError:
            continue

    raise FileNotFoundError(
        tr(
            "Unable to locate Java runtime for STM32CubeMX. "
            "Pass --java-cmd or use --launch-mode direct.",
            "找不到运行 STM32CubeMX 所需的 Java。请传入 --java-cmd 或使用 --launch-mode direct。",
        )
    )


def _is_java_archive(cubemx_cmd: str) -> bool:
    """路径的扩展名为 .jar 时为 True，不区分大小写。
    True when the path has the .jar extension, ignoring case.
    """
    return os.path.splitext(cubemx_cmd)[1].lower() == ".jar"


def _format_script_path(path: str) -> str:
    """把路径写成 CubeMX 脚本中的形式：绝对路径，Windows 上用正斜杠，含空白时加双引号。
    Format a path for a CubeMX script: absolute, with forward slashes on Windows, and in double
    quotes when it contains whitespace.
    """
    normalized = os.path.abspath(path)
    if os.name == "nt":
        normalized = normalized.replace("\\", "/")
    if any(ch.isspace() for ch in normalized):
        return f'"{normalized}"'
    return normalized


def build_cubemx_script(ioc_path: str, generate_code_dir: str = "") -> str:
    """CubeMX 脚本文本：加载 .ioc，执行 project generate（给出 generate_code_dir 时改为
    generate code <目录>），最后 exit。
    The CubeMX script text: load the .ioc, run project generate (generate code <dir> when
    generate_code_dir is given), then exit.
    """
    script_lines = [f"config load {_format_script_path(ioc_path)}"]
    if generate_code_dir:
        script_lines.append(f"generate code {_format_script_path(generate_code_dir)}")
    else:
        script_lines.append("project generate")
    script_lines.append("exit")
    return "\n".join(script_lines) + "\n"


def _shell_join(args: Sequence[str]) -> str:
    """把参数拼成一行按 shell 规则引用的命令，用于日志。
    Join arguments into one shell-quoted command line for logs.
    """
    return shlex.join(args)


def _java_user_state_options() -> list[str]:
    """以 java -jar 启动 CubeMX 时附加的 JVM 参数：user.home 设为当前用户主目录，Java 首选项
    根目录设为其中的 .java。
    JVM options added when CubeMX is started through java -jar: user.home set to the current
    user's home directory and the Java preferences root to .java inside it.
    """
    java_home = os.path.abspath(os.path.expanduser("~"))
    prefs_root = os.path.join(java_home, ".java")
    return [
        f"-Duser.home={java_home}",
        f"-Djava.util.prefs.userRoot={prefs_root}",
    ]


def _bundled_cubemx(cubemx_cmd: str) -> tuple[str, str] | None:
    """CubeMX 安装中的 (jar, 自带 JRE 的 java)：jar 是可执行文件旁边的 STM32CubeMX.jar，没有时是
    可执行文件本身（CubeMX 6.18 起 jar 嵌在 STM32CubeMX.exe 中）；找不到 jar 或 JRE 时为 None。
    The (jar, java of the bundled JRE) of a CubeMX installation: the jar is STM32CubeMX.jar next
    to the executable, or else the executable itself (from CubeMX 6.18 the jar is embedded in
    STM32CubeMX.exe); None when the jar or the JRE is missing.
    """
    if _is_java_archive(cubemx_cmd):
        return None
    folder = os.path.dirname(os.path.abspath(cubemx_cmd))
    java = os.path.join(folder, "jre", "bin", "java.exe" if os.name == "nt" else "java")
    if not os.path.isfile(java):
        return None
    jar = os.path.join(folder, "STM32CubeMX.jar")
    if os.path.isfile(jar):
        return jar, java
    if zipfile.is_zipfile(cubemx_cmd):
        return os.path.abspath(cubemx_cmd), java
    return None


def _launcher_options(cubemx_cmd: str) -> list[str]:
    """CubeMX 启动器配置（与可执行文件同名的 .l4j.ini）中的 JVM 参数；没有该文件时为空。
    The JVM options in the CubeMX launcher configuration (the .l4j.ini named like the
    executable); empty when there is no such file.
    """
    ini_path = os.path.splitext(os.path.abspath(cubemx_cmd))[0] + ".l4j.ini"
    try:
        with open(ini_path, encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except OSError:
        return []
    options = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            options.extend(line.split())
    return options


def build_cubemx_command(
    cubemx_cmd: str,
    script_path: str,
    launch_mode: str = "auto",
    java_cmd: str = "",
    silent: bool = False,
) -> list[str]:
    """组成让 STM32CubeMX 执行 script_path 脚本的命令行。
    Build the command line that makes STM32CubeMX run the script at script_path.

    launch_mode 为 java，或为 auto 且能找到 .jar（cubemx_cmd 本身，或带自带 JRE 的安装中的 jar，
    见 _bundled_cubemx）时，用 java -jar 启动，并带上启动器配置中的 JVM 参数；Windows 上同时打开
    Java Access Bridge，以便读取和回答对话框。否则直接启动，其中 .py 文件用当前 Python 解释器
    运行。silent 为 True 时追加 -s。
    With launch_mode java, or auto when a .jar is found (cubemx_cmd itself, or the jar of an
    installation with its bundled JRE, see _bundled_cubemx), CubeMX is started through java -jar
    with the JVM options of its launcher configuration; on Windows the Java Access Bridge is
    enabled too, so dialogs can be read and answered. Otherwise CubeMX is started directly, a .py
    file with the current Python interpreter. silent appends -s.

    Windows 上的 STM32CubeMX.exe 是启动器：它启动 Java 后立即返回，不等待生成结束。
    On Windows, STM32CubeMX.exe is a launcher: it starts Java and returns at once, without
    waiting for the generation.

    Raises:
        ValueError: launch_mode 不是 auto、direct、java 之一，或 java 模式下找不到 CubeMX 的 .jar。
            launch_mode is not auto, direct or java, or java mode finds no CubeMX .jar.
        FileNotFoundError: 需要 java -jar 启动但找不到 Java。
            java -jar is needed but no Java runtime was found.
    """
    launch_mode = launch_mode.lower()
    if launch_mode not in {"auto", "direct", "java"}:
        raise ValueError(
            tr(f"Unsupported launch mode: {launch_mode}", f"不支持的启动方式：{launch_mode}")
        )

    is_jar = _is_java_archive(cubemx_cmd)
    bundled = _bundled_cubemx(cubemx_cmd)
    use_java = launch_mode == "java" or (launch_mode == "auto" and (is_jar or bundled is not None))

    if use_java and not is_jar and bundled is None:
        raise ValueError(
            tr(
                "Java launch mode requires an STM32CubeMX .jar path, or an installation with its "
                "jre folder and STM32CubeMX.jar (or a jar embedded in the executable).",
                "java 启动方式需要 STM32CubeMX 的 .jar 路径，或带 jre 目录和 STM32CubeMX.jar"
                "（或可执行文件内嵌 jar）的安装。",
            )
        )

    command: list[str]
    if use_java:
        if is_jar:
            jar = cubemx_cmd
            java = resolve_java_command(cubemx_cmd, java_cmd)
            options = []
        else:
            jar, bundled_java = bundled
            java = _resolve_existing_path(java_cmd) if java_cmd else bundled_java
            options = _launcher_options(cubemx_cmd)
        if os.name == "nt":
            options.append(ACCESS_BRIDGE_OPTION)
        command = [java, *_java_user_state_options(), *options, "-jar", jar, "-q", script_path]
    elif cubemx_cmd.lower().endswith(".py"):
        command = [sys.executable, cubemx_cmd, "-q", script_path]
    else:
        if os.path.basename(cubemx_cmd).lower() == "stm32cubemx.exe":
            LOGGER.warning(
                tr(
                    "STM32CubeMX.exe starts CubeMX and returns at once, so the generation is not "
                    "awaited; use --launch-mode auto",
                    "STM32CubeMX.exe 启动 CubeMX 后立即返回，不会等待生成结束；"
                    "请使用 --launch-mode auto",
                )
            )
        command = [cubemx_cmd, "-q", script_path]

    if silent:
        command.append("-s")
    return command


def find_ioc_file(directory: str) -> str | None:
    """目录中唯一的 .ioc 文件的路径；没有时为 None。
    The path of the only .ioc file in the directory; None when there is none.

    Raises:
        ValueError: 目录中有多个 .ioc 文件。
            The directory holds several .ioc files.
    """
    ioc_files = sorted(name for name in os.listdir(directory) if name.endswith(".ioc"))
    if len(ioc_files) > 1:
        raise ValueError(
            tr(
                f"{directory} holds several .ioc files ({', '.join(ioc_files)}); pass --ioc",
                f"{directory} 中有多个 .ioc 文件（{'、'.join(ioc_files)}）；请用 --ioc 指定",
            )
        )
    return os.path.join(directory, ioc_files[0]) if ioc_files else None


@dataclass
class CubeMXRunResult:
    """一次 STM32CubeMX 运行的命令、脚本路径、标准输出、标准错误、退出码和日志目录。
    The command, script path, stdout, stderr, exit code and log directory of one STM32CubeMX run.

    没有写日志时 log_dir 为空字符串。临时脚本在返回前已删除，此时 script_path 只记录其原位置。
    log_dir is an empty string when no logs were written. A temporary script is deleted before
    the result is returned; script_path then only records where it was.
    """

    command: list[str]
    script_path: str
    stdout: str
    stderr: str
    returncode: int
    log_dir: str = ""


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """结束进程及其子进程：Windows 上执行 taskkill /T /F；其他平台向进程组发送 SIGTERM，3 秒后仍未
    退出则发送 SIGKILL。
    Terminate a process and its children: taskkill /T /F on Windows; elsewhere SIGTERM to the
    process group, then SIGKILL when it has not exited after 3 seconds.

    上述操作无法执行时只结束该进程本身；进程已退出时不做任何事。
    When that cannot be done, only the process itself is killed; an exited process is left alone.
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        except Exception:
            pass
    elif hasattr(os, "killpg"):
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return
            except Exception:
                pass
        except Exception:
            pass
    process.kill()


def _tail_text(text: str, lines: int = 40) -> str:
    """文本的最后 lines 行（默认 40 行），用于错误信息。
    The last lines of a text, 40 by default, for error messages.
    """
    text_lines = text.splitlines()
    return "\n".join(text_lines[-lines:])


def _write_text_file(path: str, content: str) -> None:
    """以 UTF-8 和 LF 换行写入文本文件，覆盖已有内容。
    Write a text file as UTF-8 with LF line endings, replacing any existing content.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        file.write(content)


def _prepare_script_path(project_dir: str, script_path: str, keep_script: bool) -> tuple[str, bool]:
    """决定 CubeMX 脚本的写入位置。
    Choose where the CubeMX script is written.

    Returns:
        (脚本路径, 运行后是否删除)：给出 script_path 时用它；keep_script 时为工程目录中的
        cubemx_generate.txt；否则为在工程目录中新建的临时文件，运行后删除。
        (script path, whether to delete it after the run): script_path when given;
        cubemx_generate.txt in the project directory with keep_script; otherwise a new
        temporary file in the project directory, deleted after the run.
    """
    if script_path:
        return os.path.abspath(script_path), False
    if keep_script:
        return os.path.join(project_dir, "cubemx_generate.txt"), False

    fd, name = tempfile.mkstemp(prefix="cubemx_generate_", suffix=".txt", dir=project_dir)
    os.close(fd)
    return name, True


def _default_expect_paths(ioc_file: str) -> Sequence[str]:
    """默认的生成结果检查路径：单核工程为根目录的 Core/Inc 和 Drivers，多核工程为每个核
    子工程的 Core/Inc。
    The default paths generation must produce: Core/Inc and Drivers of the root for a
    single-core project, and the Core/Inc of every core's subproject for a multicore one.

    多核工程把 Core/ 生成在每个核的子工程里，根目录没有 Core/Inc 和 Drivers；仍按单核
    的路径检查会把成功的生成误报成失败。
    A multicore project generates Core/ in every core's subproject and the root has no
    Core/Inc or Drivers; checking the single-core paths would report a successful
    generation as a failure.
    """
    from libxr.config_cubemx_project import select_cube_contexts

    try:
        contexts = select_cube_contexts(ioc_file)
    except (OSError, ValueError):
        contexts = []
    if contexts:
        return [os.path.join(context["project_dir"], "Core", "Inc") for context in contexts]
    return DEFAULT_EXPECT_PATHS


def _normalize_expect_paths(project_dir: str, expect_paths: Sequence[str]) -> list[str]:
    """把期望路径转为绝对路径，相对路径以工程目录为基准。
    Make the expected paths absolute, resolving relative ones against the project directory.
    """
    resolved = []
    for path in expect_paths:
        if os.path.isabs(path):
            resolved.append(os.path.abspath(path))
        else:
            resolved.append(os.path.abspath(os.path.join(project_dir, path)))
    return resolved


def _script_results(script_text: str, stdout_text: str) -> list[tuple[str, str]]:
    """把 CubeMX 对脚本各命令的 OK/KO 结果按顺序与命令配对；exit 不计在内，没有结果的命令为空串。
    Pair, in order, the OK/KO results CubeMX printed with the script commands; exit is not
    counted, and a command without a result gets an empty string.
    """
    commands = [line.strip() for line in script_text.splitlines() if line.strip()]
    commands = [command for command in commands if command != "exit"]
    results = [line.strip() for line in stdout_text.splitlines() if line.strip() in ("OK", "KO")]
    return [
        (command, results[index] if index < len(results) else "")
        for index, command in enumerate(commands)
    ]


def generate_cubemx_project(
    project_dir: str,
    ioc_file: str = "",
    cubemx_cmd: str = "",
    java_cmd: str = "",
    launch_mode: str = "auto",
    generate_code_dir: str = "",
    expect_paths: Sequence[str] | None = None,
    log_dir: str = "",
    script_path: str = "",
    keep_script: bool = False,
    silent: bool = False,
    firmware: str | None = None,
    download: bool = False,
    timeout: int = 1200,
) -> CubeMXRunResult:
    """以脚本模式运行 STM32CubeMX 生成工程，再检查每条脚本命令都成功、期望的输出路径都存在。
    Generate a project by running STM32CubeMX in script mode, then check that every script
    command succeeded and that the expected output paths exist.

    没有 ioc_file 时使用工程目录中唯一的 .ioc。给出 log_dir 时在其中写入脚本、命令行、标准输出和
    标准错误。CubeMX 弹出的对话框按 firmware 和 download 回答（见 answer_dialog），回答不了时停止
    运行。
    Without ioc_file, the only .ioc in the project directory is used. With log_dir, the script,
    the command line, stdout and stderr are written there. The dialogs CubeMX shows are answered
    by firmware and download (see answer_dialog); one that cannot be answered stops the run.

    Args:
        launch_mode: auto、direct 或 java，含义见 build_cubemx_command。
            auto, direct or java, as described in build_cubemx_command.
        generate_code_dir: 给出时脚本执行 generate code <目录>，而不是 project generate。
            When given, the script runs generate code <dir> instead of project generate.
        expect_paths: 生成后必须存在的路径，相对路径以工程目录为基准；None 时为 Core/Inc
            和 Drivers。
            Paths that must exist after generation, relative ones resolved against the project
            directory; Core/Inc and Drivers when None.
        script_path, keep_script: 脚本位置，见 _prepare_script_path；默认用运行后删除的临时文件。
            Where the script goes, see _prepare_script_path; a temporary file deleted after the
            run by default.
        silent: 为 True 时向 STM32CubeMX 传入 -s。
            Pass -s to STM32CubeMX when True.
        firmware: keep、migrate 或 None：工程由另一版本的 CubeMX 保存时的选择。
            keep, migrate or None: the choice when the project was saved by another CubeMX
            version.
        download: 允许 CubeMX 下载缺少的固件包并接受其许可协议。
            Let CubeMX download a missing firmware package and accept its license.
        timeout: CubeMX 运行的时限（秒），超时后结束整个进程树。
            The CubeMX time limit in seconds; the whole process tree is terminated when it expires.

    Raises:
        FileNotFoundError: 找不到工程目录、.ioc 文件、STM32CubeMX 或 Java。
            The project directory, the .ioc file, STM32CubeMX or Java was not found.
        ValueError: 目录中有多个 .ioc，launch_mode 无效，或 java 模式下找不到 CubeMX 的 .jar。
            The directory holds several .ioc files, launch_mode is invalid, or java mode finds
            no CubeMX .jar.
        TimeoutError: CubeMX 在 timeout 秒内没有结束。
            CubeMX did not finish within timeout seconds.
        RuntimeError: 对话框无法回答（DialogStopped），CubeMX 以非零退出码结束，某条脚本命令
            失败，或期望路径不存在。
            A dialog could not be answered (DialogStopped), CubeMX exited with a non-zero code, a
            script command failed, or an expected path is missing.
    """
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        raise FileNotFoundError(
            tr(f"Project directory not found: {project_dir}", f"找不到工程目录：{project_dir}")
        )

    ioc_path = os.path.abspath(ioc_file) if ioc_file else find_ioc_file(project_dir)
    if not ioc_path:
        project_name = _friendly_path_name(project_dir)
        raise FileNotFoundError(
            tr(
                f"No .ioc file found in {project_name}",
                f"{project_name} 中没有 .ioc 文件",
            )
        )

    resolved_cubemx_cmd = resolve_cubemx_command(cubemx_cmd)
    actual_script_path, should_cleanup_script = _prepare_script_path(
        project_dir, script_path, keep_script
    )
    # 组成命令行时会检查启动方式和 Java；失败时删除已建的临时脚本再抛出。
    # Building the command line checks the launch mode and Java; on failure the temporary
    # script already created is removed before the error propagates.
    try:
        command = build_cubemx_command(
            resolved_cubemx_cmd,
            actual_script_path,
            launch_mode=launch_mode,
            java_cmd=java_cmd,
            silent=silent,
        )
    except Exception:
        if should_cleanup_script:
            with contextlib.suppress(OSError):
                os.remove(actual_script_path)
        raise
    script_text = build_cubemx_script(ioc_path, generate_code_dir)
    _write_text_file(actual_script_path, script_text)

    # 以 Java 启动时，同一个 JRE 中的 Access Bridge 用来读取和回答对话框。
    # When started through Java, the Access Bridge of the same JRE reads and answers dialogs.
    bridge_dll = ""
    if os.name == "nt" and ACCESS_BRIDGE_OPTION in command:
        candidate = os.path.join(os.path.dirname(command[0]), "windowsaccessbridge-64.dll")
        bridge_dll = candidate if os.path.isfile(candidate) else ""

    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        _write_text_file(os.path.join(log_dir, "cubemx_generate.txt"), script_text)
    command_line = _shell_join(command)
    LOGGER.info(tr(f"Running CubeMX command: {command_line}", f"运行 CubeMX 命令：{command_line}"))

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    with contextlib.ExitStack() as logs:
        stdout_handle = None
        stderr_handle = None
        if log_dir:
            stdout_handle = logs.enter_context(
                open(
                    os.path.join(log_dir, "cubemx_stdout.log"), "w", encoding="utf-8", newline="\n"
                )
            )
            stderr_handle = logs.enter_context(
                open(
                    os.path.join(log_dir, "cubemx_stderr.log"), "w", encoding="utf-8", newline="\n"
                )
            )
            _write_text_file(
                os.path.join(log_dir, "cubemx_command.txt"), _shell_join(command) + "\n"
            )

        def consume_stream(stream, sink: list[str], handle) -> None:
            """逐行读取子进程的输出流存入 sink，给出 handle 时同时写入日志文件；读完后关闭流。
            Read a child process stream line by line into sink, also writing each line to handle
            when given; the stream is closed at the end.
            """
            try:
                for line in iter(stream.readline, ""):
                    sink.append(line)
                    if handle is not None:
                        handle.write(line)
                        handle.flush()
            finally:
                stream.close()

        try:
            # CubeMX 路径在此之前已解析，参数以列表传入且不经过 shell，工程路径不会被 shell 展开。
            # CubeMX path is resolved before this point and arguments are passed as
            # a list with shell disabled, so project paths cannot be shell-expanded.
            popen_kwargs = {}
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True

            process = subprocess.Popen(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                command,
                cwd=project_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,
                shell=False,
                **popen_kwargs,
            )
        except Exception:
            if should_cleanup_script:
                with contextlib.suppress(OSError):
                    os.remove(actual_script_path)
            raise

        stop_event = threading.Event()
        done = threading.Event()
        watch_thread = _DialogWatchThread(
            process.pid,
            lambda dialog: answer_dialog(dialog, firmware=firmware, download=download),
            bridge_dll,
            stop_event,
            done,
        )
        watch_thread.start()

        stdout_thread = threading.Thread(
            target=consume_stream, args=(process.stdout, stdout_lines, stdout_handle), daemon=True
        )
        stderr_thread = threading.Thread(
            target=consume_stream, args=(process.stderr, stderr_lines, stderr_handle), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        timeout_error: TimeoutError | None = None
        deadline = time.monotonic() + timeout
        try:
            while True:
                if stop_event.is_set():
                    _terminate_process_tree(process)
                    returncode = process.wait(timeout=5)
                    break
                returncode = process.poll()
                if returncode is not None:
                    break
                if time.monotonic() >= deadline:
                    _terminate_process_tree(process)
                    returncode = process.wait(timeout=5)
                    timeout_error = TimeoutError(
                        tr(
                            f"STM32CubeMX timed out after {timeout} seconds",
                            f"STM32CubeMX 运行超过 {timeout} 秒，已超时",
                        )
                    )
                    break
                time.sleep(0.2)
        finally:
            # 监视线程在 CubeMX 退出前一直处理消息（Access Bridge 需要它才能让 JVM 结束）。
            # The watcher pumps messages until CubeMX has exited (the Access Bridge needs that for
            # the JVM to end).
            done.set()
            watch_thread.join(timeout=2.0)

        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)

        stdout_text = "".join(stdout_lines)
        stderr_text = "".join(stderr_lines)

    result = CubeMXRunResult(
        command=command,
        script_path=actual_script_path,
        stdout=stdout_text,
        stderr=stderr_text,
        returncode=returncode,
        log_dir=os.path.abspath(log_dir) if log_dir else "",
    )

    if should_cleanup_script:
        with contextlib.suppress(OSError):
            os.remove(actual_script_path)

    if watch_thread.error is not None:
        raise watch_thread.error

    if timeout_error is not None:
        raise timeout_error

    if returncode != 0:
        stdout_tail = _tail_text(stdout_text)
        stderr_tail = _tail_text(stderr_text)
        raise RuntimeError(
            tr(
                "STM32CubeMX generation failed with exit code "
                f"{returncode}\nSTDOUT tail:\n{stdout_tail}\nSTDERR tail:\n{stderr_tail}",
                f"STM32CubeMX 生成失败，退出码 {returncode}\n"
                f"标准输出末尾：\n{stdout_tail}\n标准错误末尾：\n{stderr_tail}",
            )
        )

    failed = [
        command for command, status in _script_results(script_text, stdout_text) if status != "OK"
    ]
    if failed:
        raise RuntimeError(
            tr(
                f"STM32CubeMX did not complete the script command: {failed[0]}",
                f"STM32CubeMX 没有完成脚本命令：{failed[0]}",
            )
            + "\n"
            + _tail_text(stdout_text, 15)
        )

    effective_expect_paths = (
        _default_expect_paths(ioc_path) if expect_paths is None else expect_paths
    )
    missing_paths = [
        path
        for path in _normalize_expect_paths(project_dir, effective_expect_paths)
        if not os.path.exists(path)
    ]
    if missing_paths:
        raise RuntimeError(
            tr(
                "STM32CubeMX finished but expected paths are still missing: ",
                "STM32CubeMX 已结束，但仍缺少期望的路径：",
            )
            + ", ".join(missing_paths)
        )

    LOGGER.info(tr("STM32CubeMX generation finished successfully.", "STM32CubeMX 生成完成。"))
    return result


if __name__ == "__main__":
    from libxr.legacy import run

    raise SystemExit(run("xr_cubemx_generate"))
