"""libxr stm32 cubemx-gen（libxr.cubemx_generator）：命令组装，用 Python 写的假 STM32CubeMX
运行（成功、失败的脚本命令、缺少输出、非零退出码、超时），以及对话框的回答规则。
libxr stm32 cubemx-gen (libxr.cubemx_generator): command building, runs with a fake STM32CubeMX
written in Python (success, a failed script command, missing output, a non-zero exit code, a
timeout), and the rules that answer dialogs.
"""

import os
import stat
import sys
import tempfile
import textwrap
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fixtures import MULTICORE_IOC, MULTICORE_MXPROJECT, TestCase

from libxr.cubemx_generator import (
    ACCESS_BRIDGE_OPTION,
    DEFAULT_EXPECT_PATHS,
    FIRMWARE_TITLE,
    LICENSE_AGREE,
    WAIT,
    Dialog,
    DialogAnswer,
    DialogStopped,
    _default_expect_paths,
    _x11_dialog_titles,
    answer_dialog,
    build_cubemx_command,
    generate_cubemx_project,
)

# 假 CubeMX 像真的一样回显每条脚本命令并打印 OK（exit 打印 Bye bye），按 FAKE_CUBEMX_MODE 行为：
# ok（默认）为 project generate 脚本创建 Core/Inc 和 Drivers，ko 对 project generate 打印 KO，
# fail 以 23 退出，no-output 什么都不生成，timeout 启动一个持续写心跳文件的子进程后等待。
# Like the real one, the fake CubeMX echoes each script command and prints OK (Bye bye for exit),
# and acts on FAKE_CUBEMX_MODE: ok, the default, creates Core/Inc and Drivers for a project
# generate script, ko prints KO for project generate, fail exits with 23, no-output creates
# nothing, and timeout starts a child that keeps writing a heartbeat file, then waits.
FAKE_CUBEMX = r"""
import os
import subprocess
import sys
import time


def main() -> int:
    if "-q" not in sys.argv:
        print("missing -q", file=sys.stderr)
        return 2

    script_path = sys.argv[sys.argv.index("-q") + 1]
    with open(script_path, "r", encoding="utf-8") as script_file:
        script_text = script_file.read()

    mode = os.environ.get("FAKE_CUBEMX_MODE", "ok")
    if mode == "fail":
        print("fake CubeMX failure", file=sys.stderr)
        return 23

    if mode == "timeout":
        heartbeat_file = os.environ["FAKE_CUBEMX_HEARTBEAT_FILE"]
        child_code = (
            "import pathlib, sys, time\n"
            "heartbeat = pathlib.Path(sys.argv[1])\n"
            "while True:\n"
            "    heartbeat.write_text(str(time.time()), encoding='utf-8')\n"
            "    time.sleep(0.1)\n"
        )
        subprocess.Popen([sys.executable, "-c", child_code, heartbeat_file])
        for _ in range(50):
            if os.path.exists(heartbeat_file):
                break
            time.sleep(0.02)
        time.sleep(30)
        return 0

    if "project generate" not in script_text:
        print("unexpected script body", file=sys.stderr)
        return 3
    for line in script_text.splitlines():
        print(line)
        if line == "exit":
            print("Bye bye")
        elif line == "project generate" and mode == "ko":
            print("KO")
        else:
            print("OK")
    if mode in ("ok", "ko"):
        os.makedirs(os.path.join(os.getcwd(), "Core", "Inc"), exist_ok=True)
        os.makedirs(os.path.join(os.getcwd(), "Drivers"), exist_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


class CubeMXTestCase(TestCase):
    """临时目录中的假 CubeMX 和一个只有 demo.ioc 的工程。
    A fake CubeMX in a temporary directory, and a project holding only demo.ioc.
    """

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.fake = self.tmp / "fake_cubemx.py"
        with self.fake.open("w", encoding="utf-8", newline="\n") as fake:
            fake.write(f"#!{sys.executable}\n" + textwrap.dedent(FAKE_CUBEMX).lstrip())
        if os.name != "nt":
            self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.project = self.tmp / "project"
        self.project.mkdir()
        (self.project / "demo.ioc").write_text(
            "ProjectManager.ProjectName=demo\n", encoding="utf-8"
        )

    def generate(self, mode="ok", **options):
        """以 FAKE_CUBEMX_MODE=mode 用假 CubeMX 生成工程。
        Generate the project with the fake CubeMX under FAKE_CUBEMX_MODE=mode.
        """
        options.setdefault("cubemx_cmd", str(self.fake))
        options.setdefault("launch_mode", "direct")
        options.setdefault("timeout", 5)
        with mock.patch.dict(os.environ, FAKE_CUBEMX_MODE=mode):
            return generate_cubemx_project(project_dir=str(self.project), **options)


class CommandLine(CubeMXTestCase):
    """启动 CubeMX 的命令行。
    The command line that starts CubeMX.
    """

    def test_launch_modes(self):
        script = str(self.tmp / "cubemx script.txt")
        exe = str(self.tmp / "STM32CubeMX.exe")
        jar = str(self.tmp / "STM32CubeMX.jar")
        Path(exe).write_text("", encoding="utf-8")
        Path(jar).write_text("", encoding="utf-8")
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(
                build_cubemx_command(exe, script, launch_mode="auto"), [exe, "-q", script]
            )
        self.assertEqual(
            logs.output,
            [
                "WARNING:libxr.cubemx_generator:STM32CubeMX.exe starts CubeMX and returns at "
                "once, so the generation is not awaited; use --launch-mode auto"
            ],
        )
        java = build_cubemx_command(jar, script, launch_mode="auto", java_cmd=sys.executable)
        index = java.index("-jar")
        self.assertEqual(java[index + 1 : index + 4], [jar, "-q", script])
        self.assertEqual(
            build_cubemx_command(str(self.fake), script, launch_mode="direct", silent=True),
            [sys.executable, str(self.fake), "-q", script, "-s"],
        )
        with self.assertRaisesMessage(
            ValueError,
            "Java launch mode requires an STM32CubeMX .jar path, or an installation with its "
            "jre folder and STM32CubeMX.jar (or a jar embedded in the executable).",
        ):
            build_cubemx_command(exe, script, launch_mode="java", java_cmd=sys.executable)

    def test_an_installation_is_started_through_its_own_java(self):
        install = self.tmp / "STM32CubeMX"
        java = install / "jre" / "bin" / ("java.exe" if os.name == "nt" else "java")
        java.parent.mkdir(parents=True)
        java.write_text("", encoding="utf-8")
        exe = install / "STM32CubeMX.exe"
        exe.write_text("", encoding="utf-8")
        (install / "STM32CubeMX.jar").write_text("", encoding="utf-8")
        (install / "STM32CubeMX.l4j.ini").write_text(
            "-Dfile.encoding=UTF8\n--add-opens java.desktop/java.awt=ALL-UNNAMED\n",
            encoding="utf-8",
        )
        command = build_cubemx_command(str(exe), "script.txt")
        self.assertEqual(command[0], str(java))
        jvm = command[command.index("-Dfile.encoding=UTF8") : command.index("-jar")]
        expected = ["-Dfile.encoding=UTF8", "--add-opens", "java.desktop/java.awt=ALL-UNNAMED"]
        if os.name == "nt":
            expected.append(ACCESS_BRIDGE_OPTION)
        self.assertEqual(jvm, expected)
        self.assertEqual(
            command[command.index("-jar") :],
            ["-jar", str(install / "STM32CubeMX.jar"), "-q", "script.txt"],
        )

        # CubeMX 6.18 起没有单独的 STM32CubeMX.jar，jar 嵌在可执行文件中。
        # From CubeMX 6.18 there is no separate STM32CubeMX.jar; the jar is in the executable.
        (install / "STM32CubeMX.jar").unlink()
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(build_cubemx_command(str(exe), "script.txt")[0], str(exe))
        self.assertEqual(
            logs.output,
            [
                "WARNING:libxr.cubemx_generator:STM32CubeMX.exe starts CubeMX and returns at "
                "once, so the generation is not awaited; use --launch-mode auto"
            ],
        )
        with zipfile.ZipFile(exe, "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Main-Class: Demo\n")
        command = build_cubemx_command(str(exe), "script.txt")
        self.assertEqual(command[0], str(java))
        self.assertEqual(command[command.index("-jar") :], ["-jar", str(exe), "-q", "script.txt"])

    def test_several_ioc_files_need_an_explicit_one(self):
        (self.project / "other.ioc").write_text("", encoding="utf-8")
        with self.assertRaisesMessage(
            ValueError,
            f"{self.project} holds several .ioc files (demo.ioc, other.ioc); pass --ioc",
        ):
            self.generate()
        self.assertEqual(self.generate(ioc_file=str(self.project / "demo.ioc")).returncode, 0)

    def test_a_command_that_cannot_be_built_leaves_no_script(self):
        exe = self.tmp / "STM32CubeMX.exe"
        exe.write_text("", encoding="utf-8")
        with self.assertRaisesMessage(
            ValueError,
            "Java launch mode requires an STM32CubeMX .jar path, or an installation with its "
            "jre folder and STM32CubeMX.jar (or a jar embedded in the executable).",
        ):
            self.generate(cubemx_cmd=str(exe), launch_mode="java", java_cmd=sys.executable)
        self.assertEqual([p.name for p in self.project.iterdir()], ["demo.ioc"])

    def test_a_missing_cubemx_is_named(self):
        absent = str(self.tmp / "absent" / "STM32CubeMX.exe")
        with self.assertRaisesMessage(
            FileNotFoundError, f"{absent} does not exist and is not a command on PATH"
        ):
            self.generate(cubemx_cmd=absent)


class DefaultExpectPaths(TestCase):
    """默认的生成结果检查路径：多核工程按每个核子工程的 Core/Inc 检查，单核按根目录。
    The default paths generation must produce: every core's Core/Inc for a multicore
    project, and the root paths for a single-core one.
    """

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)

    def write_multicore(self):
        """写一个能解析出 CM7/CM4 子工程的多核 .ioc 和 .mxproject，返回 .ioc 路径。
        Write a multicore .ioc and .mxproject that resolve the CM7/CM4 subprojects and
        return the .ioc path.
        """
        (self.project / "demo.ioc").write_text(MULTICORE_IOC, encoding="utf-8")
        (self.project / ".mxproject").write_text(MULTICORE_MXPROJECT, encoding="utf-8")
        for core in ("CM7", "CM4"):
            (self.project / core / "Core" / "Inc").mkdir(parents=True)
        return str(self.project / "demo.ioc")

    def test_a_multicore_project_expects_every_cores_core_inc(self):
        # 多核工程把 Core/ 生成在子工程里，检查路径跟着变；否则成功的生成被误报成失败。
        # 子工程目录按真实路径（realpath）给出：Windows 的用户目录会解析成 8.3 短名。
        # A multicore project generates Core/ in the subprojects, and the checked paths
        # follow; otherwise a successful generation is reported as a failure. The
        # subproject directories come as real paths: on Windows the user directory
        # resolves to its 8.3 short name.
        expected = [
            os.path.join(os.path.realpath(self.project / core), "Core", "Inc")
            for core in ("CM7", "CM4")
        ]
        self.assertEqual(_default_expect_paths(self.write_multicore()), expected)

    def test_a_single_core_project_expects_the_root_paths(self):
        (self.project / "demo.ioc").write_text("Mcu.Name=STM32F103\n", encoding="utf-8")
        self.assertEqual(
            _default_expect_paths(str(self.project / "demo.ioc")), DEFAULT_EXPECT_PATHS
        )

    def test_an_unreadable_layout_falls_back_to_the_root_paths(self):
        # 布局判断失败（如 .mxproject 缺失）时退回单核的检查路径，不让检查本身报错。
        # When the layout cannot be told (a missing .mxproject for one), the check falls
        # back to the single-core paths instead of failing on its own.
        (self.project / "demo.ioc").write_text(MULTICORE_IOC, encoding="utf-8")
        self.assertEqual(
            _default_expect_paths(str(self.project / "demo.ioc")), DEFAULT_EXPECT_PATHS
        )


class Generation(CubeMXTestCase):
    """运行 CubeMX 并检查结果。
    Running CubeMX and checking the result.
    """

    def test_a_successful_generation_is_logged_and_removes_its_script(self):
        logs = self.tmp / "logs"
        result = self.generate(log_dir=str(logs))
        self.assertEqual(result.returncode, 0)
        self.assertFalse(Path(result.script_path).exists())
        script = (logs / "cubemx_generate.txt").read_text(encoding="utf-8")
        self.assertIn("config load", script)
        self.assertIn("project generate", script)
        self.assertIn(str(self.fake), (logs / "cubemx_command.txt").read_text(encoding="utf-8"))
        self.assertTrue((self.project / "Core" / "Inc").is_dir())
        self.assertTrue((self.project / "Drivers").is_dir())

    def test_a_failed_script_command_fails_the_run(self):
        with self.assertRaisesMessage(
            RuntimeError,
            "STM32CubeMX did not complete the script command: project generate\n"
            f"config load {(self.project / 'demo.ioc').as_posix()}\n"
            "OK\nproject generate\nKO\nexit\nBye bye",
        ):
            self.generate("ko")

    def test_missing_expected_paths_fail_the_run(self):
        missing = ", ".join(
            os.path.abspath(os.path.join(str(self.project), p)) for p in ("Core/Inc", "Drivers")
        )
        with self.assertRaisesMessage(
            RuntimeError, f"STM32CubeMX finished but expected paths are still missing: {missing}"
        ):
            self.generate("no-output")

    def test_a_non_zero_exit_reports_the_code_and_output(self):
        with self.assertRaisesMessage(
            RuntimeError,
            "STM32CubeMX generation failed with exit code 23\nSTDOUT tail:\n\n"
            "STDERR tail:\nfake CubeMX failure",
        ):
            self.generate("fail")

    def test_a_timeout_stops_cubemx_and_its_children(self):
        heartbeat = self.tmp / "child.heartbeat"
        with (
            mock.patch.dict(os.environ, FAKE_CUBEMX_HEARTBEAT_FILE=str(heartbeat)),
            self.assertRaisesMessage(TimeoutError, "STM32CubeMX timed out after 3 seconds"),
        ):
            self.generate("timeout", timeout=3)
        # 心跳文件在 0.5 秒内不再变化时，子进程已停止。
        # The child has stopped once the heartbeat file no longer changes within 0.5 seconds.
        deadline = time.time() + 3
        while time.time() < deadline:
            before = heartbeat.read_text(encoding="utf-8")
            time.sleep(0.5)
            if heartbeat.read_text(encoding="utf-8") == before:
                return
        self.fail("the child of the fake CubeMX kept running after the timeout")


class DialogAnswers(TestCase):
    """CubeMX 对话框按 --firmware 和 --download 回答，其他对话框停止运行。
    CubeMX dialogs are answered by --firmware and --download; any other dialog stops the run.
    """

    MIGRATION = Dialog(
        FIRMWARE_TITLE,
        "This project was setup with STM32CubeMX V6.16.0 using STM32Cube FW_F1 V1.8.6.\n"
        "There are three options to proceed: ",
        ("Continue", "Migrate", "Cancel"),
    )
    NEED_MIGRATION = Dialog(
        "Project Manager Settings",
        "You need to migrate the project and work with the latest version of the Firmware Package.",
        ("Migrate", "Cancel"),
    )
    DOWNLOAD_OR_MIGRATE = Dialog(
        FIRMWARE_TITLE, "Download now or Migrate Project ?", ("Download", "Migrate", "Cancel")
    )
    CONFIRM = Dialog("Project Manager Settings", "Do you confirm this migration ?", ("Yes", "No"))
    DOWNLOAD = Dialog(
        "Project Manager Settings", "Download now or Continue ?", ("Download", "Continue")
    )
    LICENSE = Dialog(
        "License Agreement",
        "Please read and accept the following agreement",
        ("Finish", "Cancel"),
        (LICENSE_AGREE, "I do not accept the terms of this license agreement"),
    )
    LOGIN = Dialog(
        "User Login",
        "Downloading software components from st.com requires myST login information.",
        ("OK",),
    )

    def answer(self, dialog, **options):
        """回答 dialog；停止时返回 DialogStopped 的信息。
        Answer dialog; when it stops, return the DialogStopped message.
        """
        try:
            return answer_dialog(dialog, **options)
        except DialogStopped as error:
            return str(error)

    def test_the_firmware_dialog_follows_the_firmware_option(self):
        self.assertEqual(self.answer(self.MIGRATION, firmware="keep"), DialogAnswer("Continue"))
        self.assertEqual(self.answer(self.MIGRATION, firmware="migrate"), DialogAnswer("Migrate"))
        stopped = self.answer(self.MIGRATION, download=True)
        self.assertIn("Pass --firmware keep to stay on its firmware package", stopped)
        self.assertIn("Buttons: Continue / Migrate / Cancel", stopped)

    def test_keeping_a_missing_package_needs_download(self):
        self.assertIn(
            "Keeping the project's firmware package needs it installed",
            self.answer(self.DOWNLOAD_OR_MIGRATE, firmware="keep"),
        )
        self.assertEqual(
            self.answer(self.DOWNLOAD_OR_MIGRATE, firmware="keep", download=True),
            DialogAnswer("Download"),
        )
        self.assertEqual(
            self.answer(self.DOWNLOAD_OR_MIGRATE, firmware="migrate"), DialogAnswer("Migrate")
        )
        self.assertIn("needs it installed", self.answer(self.NEED_MIGRATION, firmware="keep"))
        self.assertEqual(
            self.answer(self.NEED_MIGRATION, firmware="migrate"), DialogAnswer("Migrate")
        )

    def test_only_a_requested_migration_is_confirmed(self):
        self.assertEqual(self.answer(self.CONFIRM, firmware="migrate"), DialogAnswer("Yes"))
        self.assertIn("--firmware migrate", self.answer(self.CONFIRM, firmware="keep"))

    def test_downloads_and_licenses_need_download(self):
        self.assertEqual(self.answer(self.DOWNLOAD, download=True), DialogAnswer("Download"))
        self.assertIn("Pass --download to download it", self.answer(self.DOWNLOAD, firmware="keep"))
        self.assertEqual(
            self.answer(self.LICENSE, download=True), DialogAnswer("Finish", select=LICENSE_AGREE)
        )
        self.assertIn("Pass --download to accept it", self.answer(self.LICENSE))

    def test_a_progress_dialog_is_waited_on(self):
        progress = Dialog(
            "Downloading selected software packages",
            "Connection to HTTP Server ...\nDownload and Unzip selected Files",
            ("OK", "Cancel", "Pause"),
            progress=True,
        )
        self.assertEqual(self.answer(progress), WAIT)
        self.assertEqual(self.answer(progress, firmware="keep", download=True), WAIT)

    def test_a_login_or_an_unknown_dialog_always_stops(self):
        self.assertIn(
            "Sign in once in STM32CubeMX",
            self.answer(self.LOGIN, firmware="keep", download=True),
        )
        stopped = self.answer(Dialog("Warning", "Something else", ("OK",)), download=True)
        self.assertEqual(
            stopped,
            "STM32CubeMX shows a dialog that libxr does not answer:\n"
            '"Warning"\nSomething else\nButtons: OK',
        )


class X11Dialogs(TestCase):
    """Linux 上从 X11 窗口树中找出 CubeMX 的对话框。
    On Linux, the dialogs of CubeMX are found in the X11 window tree.
    """

    def test_only_x_errors_skip_a_window(self):
        # 遍历期间关闭的窗口使 X 服务器返回 BadWindow，这样的窗口跳过；以前任何异常都被吞掉，
        # 代码错误也看不出来。
        # A window closed during the walk makes the X server return BadWindow, and such a
        # window is skipped; any exception used to be swallowed, hiding errors in the code too.
        class XError(Exception):
            pass

        class BadWindow(XError):
            pass

        x = types.SimpleNamespace(IsViewable=2, AnyPropertyType=0)
        xlib = types.ModuleType("Xlib")
        xlib.X = x
        error = types.ModuleType("Xlib.error")
        error.XError = XError
        xlib.error = error

        class Window:
            """X11 窗口的替身：query_tree() 抛出 fail（不为 None 时）。
            A stand-in for an X11 window: query_tree() raises fail when it is not None.
            """

            def __init__(self, children=(), pid=None, title="", fail=None):
                self.children, self.pid, self.title, self.fail = children, pid, title, fail

            def query_tree(self):
                if self.fail is not None:
                    raise self.fail
                return types.SimpleNamespace(children=list(self.children))

            def get_attributes(self):
                return types.SimpleNamespace(map_state=x.IsViewable)

            def get_full_property(self, atom, _type):
                if atom == "_NET_WM_PID":
                    return types.SimpleNamespace(value=[self.pid]) if self.pid else None
                return types.SimpleNamespace(value=self.title.encode("utf-8"))

            def get_wm_transient_for(self):
                return object()

            def get_wm_name(self):
                return self.title

        display = types.SimpleNamespace(intern_atom=lambda name: name)
        dialog = Window(pid=42, title="Missing pack")
        with mock.patch.dict(sys.modules, {"Xlib": xlib, "Xlib.error": error}):
            root = Window(children=[Window(fail=BadWindow()), dialog])
            self.assertEqual(_x11_dialog_titles(display, root, {42}), ["Missing pack"])
            root = Window(children=[Window(fail=RuntimeError("bug")), dialog])
            with self.assertRaises(RuntimeError):
                _x11_dialog_titles(display, root, {42})


if __name__ == "__main__":
    unittest.main()
