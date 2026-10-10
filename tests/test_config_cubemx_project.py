"""libxr stm32 setup（libxr.config_cubemx_project）：写入 libxr_config.yaml 的终端设备、LibXR
子模块的检出策略、改动前的工程检查和完整的 setup 流程。
libxr stm32 setup (libxr.config_cubemx_project): the terminal device written to libxr_config.yaml,
the checkout policy of the LibXR submodule, the project check before any change, and the whole
setup run.
"""

import contextlib
import importlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixtures import (
    IOC,
    MULTICORE_IOC,
    MULTICORE_MXPROJECT,
    GeneratorTestCase,
    TestCase,
)

from libxr import config_cubemx_project as cubemx_cfg
from libxr import generator_code_stm32 as generator
from libxr import generator_stm32_cmake as stm32_cmake


class TerminalOption(TestCase):
    """--terminal 记录的 terminal_source 被生成器使用。
    The terminal_source that --terminal records is the one the generator uses.
    """

    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.user = Path(self.temporary.name)
        self.path = self.user / "libxr_config.yaml"

    def effective_terminal(self):
        """生成器读取 libxr_config.yaml 后使用的 terminal_source。
        The terminal_source the generator uses after reading libxr_config.yaml.
        """
        importlib.reload(generator)
        generator.load_libxr_config(str(self.user), "")
        return generator.libxr_settings["terminal_source"]

    def test_terminal_is_recorded_for_a_new_project(self):
        # 新建的文件和 libxr gen 新建的一样先固定 generator 的版本；以前 setup -t 新建的文件没有
        # 这一项，BSP CI 因缺少版本固定而失败。
        # A new file pins the generator first, like one libxr gen creates; a file that setup -t
        # created used to lack it, and BSP CI failed for want of the pin.
        with mock.patch("libxr.update_notice.installed_version", return_value="6.0.0"):
            cubemx_cfg.set_terminal_source(str(self.user), "usart1")
        self.assertEqual(
            self.path.read_text(encoding="utf-8"), "generator: 6.0.0\nterminal_source: usart1\n"
        )
        self.assertEqual(self.effective_terminal(), "usart1")
        self.path.unlink()
        with mock.patch("libxr.update_notice.installed_version", return_value=None):
            cubemx_cfg.set_terminal_source(str(self.user), "usart1")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "terminal_source: usart1\n")

    def test_terminal_replaces_the_configured_one_and_keeps_comments(self):
        self.path.write_text(
            "# pinned\ngenerator: 6.0.0\n# console\nterminal_source: usart1  # debug port\n"
            "SYSTEM: None\n",
            encoding="utf-8",
        )
        cubemx_cfg.set_terminal_source(str(self.user), "usb_fs_cdc")
        self.assertRegex(
            self.path.read_text(encoding="utf-8"),
            r"\A# pinned\ngenerator: 6\.0\.0\n# console\nterminal_source: usb_fs_cdc +# debug port\n"
            r"SYSTEM: None\n\Z",
        )
        self.assertEqual(self.effective_terminal(), "usb_fs_cdc")

    def test_unparsable_config_stops_without_writing(self):
        self.path.write_text("terminal_source: [\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cubemx_cfg.set_terminal_source(str(self.user), "usart1")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "terminal_source: [\n")


def git(*args, cwd=None):
    """以固定身份在 cwd 中运行 git，返回去掉首尾空白的标准输出。
    Run git in cwd with a fixed identity and return its stripped stdout.
    """
    identity = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]
    result = subprocess.run(
        ["git", *identity, "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def commit_file(repo, name, text):
    """把 text 写入 repo 中的 name 并提交，返回新的 commit。
    Write text to name in repo, commit it and return the new commit.
    """
    (repo / name).write_text(text, encoding="utf-8")
    git("add", name, cwd=repo)
    git("commit", "-q", "-m", text, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


class LibXRRemote:
    """测试类的混入：setUpClass 建一个代替 LibXR 的本地裸仓库 remote，setUp 建临时目录 tmp
    并允许 git 使用 file 协议。
    A test class mixin: setUpClass creates a local bare repository, remote, standing in for
    LibXR, and setUp creates the temporary directory tmp and lets git use the file protocol.
    """

    @classmethod
    def setUpClass(cls):
        """创建代替 LibXR 的裸仓库：master 上依次是 old、default、newer，另有从 old 分出的
        divergent。
        Create a bare repository standing in for LibXR: old, default and newer in order on
        master, and divergent branched from old.
        """
        super().setUpClass()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        source = cls.root / "libxr-source"
        source.mkdir()
        git("init", "-q", "-b", "master", cwd=source)
        cls.old = commit_file(source, "version.txt", "old")
        cls.default = commit_file(source, "version.txt", "default")
        cls.newer = commit_file(source, "version.txt", "newer")
        git("checkout", "-q", "-b", "divergent", cls.old, cwd=source)
        cls.divergent = commit_file(source, "divergent.txt", "divergent")
        cls.remote = cls.root / "libxr.git"
        git("clone", "-q", "--bare", str(source), str(cls.remote), cwd=cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        patcher = mock.patch.dict(os.environ, GIT_ALLOW_PROTOCOL="file")
        patcher.start()
        self.addCleanup(patcher.stop)


class LibXRSubmodule(LibXRRemote, TestCase):
    """add_libxr 保留已有的 LibXR 检出，只在明确要求或目录为空时检出指定 commit。
    add_libxr keeps an existing LibXR checkout and checks out a commit only when asked or when
    the directory is empty.
    """

    def project(self, recorded, checked_out):
        """以 LibXR 为子模块的工程：gitlink 记录 recorded，检出停在 checked_out；返回
        (工程目录, 检出目录)。
        A project with LibXR as a submodule whose gitlink records recorded and whose checkout
        is left at checked_out; return (project directory, checkout directory).
        """
        project = Path(tempfile.mkdtemp(dir=self.tmp)) / "project"
        project.mkdir()
        git("init", "-q", "-b", "master", cwd=project)
        git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(self.remote),
            "Middlewares/Third_Party/LibXR",
            cwd=project,
        )
        checkout = project / "Middlewares" / "Third_Party" / "LibXR"
        git("checkout", "-q", recorded, cwd=checkout)
        git("add", ".gitmodules", "Middlewares/Third_Party/LibXR", cwd=project)
        git("commit", "-q", "-m", "add LibXR", cwd=project)
        git("checkout", "-q", checked_out, cwd=checkout)
        return project, checkout

    def add_libxr(self, project, **options):
        """以本生成器的默认 commit 为 default 运行 add_libxr，屏蔽它的标准输出。
        Run add_libxr with default as the generator's default commit, its stdout suppressed.
        """
        with contextlib.redirect_stdout(io.StringIO()):
            cubemx_cfg.add_libxr(project, default_libxr_commit=self.default, **options)

    def head(self, checkout):
        """检出的 HEAD。
        HEAD of the checkout.
        """
        return git("rev-parse", "HEAD", cwd=checkout)

    def test_existing_checkouts_stay_where_they_are(self):
        def warning(relation):
            return (
                f"WARNING:root:LibXR checkout {checked_out[:12]} is {relation} this generator's "
                f"default {self.default[:12]}; it was left unchanged. To switch, run "
                f"`libxr stm32 setup` with --commit {self.default} (or check out the commit in "
                "Middlewares/Third_Party/LibXR) and commit the gitlink."
            )

        # 已有的检出不需要下载源。
        # An existing checkout needs no source to download from.
        patcher = mock.patch.object(cubemx_cfg, "pick_git_base", side_effect=AssertionError)
        patcher.start()
        self.addCleanup(patcher.stop)
        for recorded, checked_out, relation in (
            (self.default, self.old, "older than"),
            (self.old, self.old, "older than"),
            (self.default, self.default, None),
            (self.default, self.newer, None),
            (self.default, self.divergent, "different from"),
        ):
            with self.subTest(recorded=recorded, checked_out=checked_out):
                project, checkout = self.project(recorded, checked_out)
                if relation is None:
                    with self.assertNoLogs(level="WARNING"):
                        self.add_libxr(project)
                else:
                    with self.assertLogs(level="WARNING") as logs:
                        self.add_libxr(project)
                    self.assertEqual(logs.output, [warning(relation)])
                self.assertEqual(self.head(checkout), checked_out)

    def test_local_changes_are_kept(self):
        project, checkout = self.project(self.old, self.old)
        (checkout / "version.txt").write_text("local changes", encoding="utf-8")
        self.add_libxr(project)
        self.assertEqual(self.head(checkout), self.old)
        self.assertEqual(git("status", "--porcelain", cwd=checkout), "M version.txt")

    def test_an_explicit_commit_moves_the_checkout(self):
        project, checkout = self.project(self.old, self.newer)
        self.add_libxr(project, libxr_commit=self.old)
        self.assertEqual(self.head(checkout), self.old)

    def test_an_empty_directory_is_initialized_to_its_gitlink(self):
        project, checkout = self.project(self.old, self.old)
        git("submodule", "deinit", "-q", "-f", "--", "Middlewares/Third_Party/LibXR", cwd=project)
        self.assertEqual(list(checkout.iterdir()), [])
        self.add_libxr(project)
        self.assertEqual(self.head(checkout), self.old)

    def test_a_successful_command_goes_to_the_debug_log_only(self):
        # 以前成功的 git 命令记为 INFO，setup 的输出满是完整的命令行。
        # Successful git commands used to be logged at INFO, filling the setup output with
        # whole command lines.
        with self.assertLogs(level="DEBUG") as logs:
            output = cubemx_cfg.run_command(["git", "--version"])
        self.assertTrue(output.startswith("git version "), output)
        self.assertEqual(logs.output, ["DEBUG:root:[OK] git --version"])

    def test_a_directory_with_user_files_is_refused_untouched(self):
        project, checkout = self.project(self.old, self.old)
        git("submodule", "deinit", "-q", "-f", "--", "Middlewares/Third_Party/LibXR", cwd=project)
        (checkout / "user_sources.cpp").write_text("keep", encoding="utf-8")
        with self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit) as exit:
            self.add_libxr(project)
        self.assertEqual(exit.exception.code, 1)
        self.assertEqual(
            logs.output,
            [
                f"ERROR:root:{checkout} exists but is not a valid Git checkout; it was left "
                "untouched. Move it away or turn it into a LibXR checkout, then run again."
            ],
        )
        self.assertEqual([p.name for p in checkout.iterdir()], ["user_sources.cpp"])

    def test_an_existing_clone_is_adopted(self):
        project = self.tmp / "project"
        project.mkdir()
        git("init", "-q", "-b", "master", cwd=project)
        checkout = project / "Middlewares" / "Third_Party" / "LibXR"
        git("clone", "-q", str(self.remote), str(checkout))
        git("checkout", "-q", self.newer, cwd=checkout)
        self.add_libxr(project, source=cubemx_cfg.LibXRSource(str(self.remote)))
        self.assertEqual(self.head(checkout), self.newer)

    def test_a_new_submodule_records_github_and_stages_the_default(self):
        project = self.tmp / "project"
        project.mkdir()
        git("init", "-q", "-b", "master", cwd=project)
        self.add_libxr(project, source=cubemx_cfg.LibXRSource(str(self.remote)))
        checkout = project / "Middlewares" / "Third_Party" / "LibXR"
        self.assertEqual(
            git(
                "config",
                "-f",
                ".gitmodules",
                f"submodule.{cubemx_cfg.SUBMODULE_PATH}.url",
                cwd=project,
            ),
            cubemx_cfg.LIBXR_URL,
        )
        self.assertEqual(self.head(checkout), self.default)
        staged = git("ls-files", "-s", "--", cubemx_cfg.SUBMODULE_PATH, cwd=project).split()[1]
        self.assertEqual(staged, self.default)

    def test_a_local_source_is_cloned_without_a_global_file_permission(self):
        # git 2.38 起子模块默认不能从本地路径克隆；这里不设 GIT_ALLOW_PROTOCOL。
        # Since git 2.38 a submodule cannot be cloned from a local path by default; no
        # GIT_ALLOW_PROTOCOL is set here.
        project = self.tmp / "project"
        project.mkdir()
        git("init", "-q", "-b", "master", cwd=project)
        with mock.patch.dict(os.environ):
            del os.environ["GIT_ALLOW_PROTOCOL"]
            self.add_libxr(project, source=cubemx_cfg.LibXRSource(str(self.remote)))
        checkout = project / "Middlewares" / "Third_Party" / "LibXR"
        self.assertEqual(self.head(checkout), self.default)
        self.assertEqual(
            cubemx_cfg.LibXRSource(str(self.remote)).config_for(cubemx_cfg.LIBXR_URL),
            [
                "-c",
                f"url.{self.remote}.insteadOf={cubemx_cfg.LIBXR_URL}",
                "-c",
                "protocol.file.allow=always",
            ],
        )

    def test_a_mirror_only_stands_in_for_the_github_url(self):
        source = cubemx_cfg.LibXRSource("https://gitee.com/jiu-xiao/libxr")
        self.assertEqual(
            source.config_for("https://github.com/Jiu-Xiao/libxr"),
            [
                "-c",
                "url.https://gitee.com/jiu-xiao/libxr.insteadOf=https://github.com/Jiu-Xiao/libxr",
            ],
        )
        self.assertEqual(source.config_for("https://github.com/someone/libxr-fork.git"), [])


class SetupProject(GeneratorTestCase):
    """setup_project：改动之前先检查工程，XRobot 模式默认沿用工程现在的选择，没有 git 时报错。
    setup_project: the project is checked before anything changes, the XRobot mode keeps the
    project's choice by default, and a missing git is an error.
    """

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "Core").mkdir()
        (self.root / "User").mkdir()
        (self.root / "demo.ioc").write_text("", encoding="utf-8")
        (self.root / "CMakeLists.txt").write_text("", encoding="utf-8")

    def test_the_project_is_checked_before_anything_changes(self):
        cases = (
            (
                "missing Core",
                lambda root: shutil.rmtree(root / "Core"),
                # 只有 .ioc 的目录以前只报缺少 Core/，没有说明怎样生成代码。
                # A directory with only the .ioc file used to report the missing Core/ without
                # saying how to generate the code.
                "{} is not a valid STM32CubeMX project: missing Core/ directory; generate the "
                "code with STM32CubeMX, or run `libxr stm32 cubemx-gen`",
            ),
            ("no .ioc", lambda root: (root / "demo.ioc").unlink(), "{} holds no .ioc file"),
            (
                "two .ioc",
                lambda root: (root / "other.ioc").write_text("", encoding="utf-8"),
                "{} holds several .ioc files (demo.ioc, other.ioc); a directory holds one CubeMX "
                "project",
            ),
            (
                "not CMake",
                lambda root: (root / "CMakeLists.txt").unlink(),
                "{} has no CMakeLists.txt; set Toolchain / IDE to CMake in the Project Manager of "
                "STM32CubeMX and generate the project again",
            ),
        )
        for case, prepare, message in cases:
            with self.subTest(case=case):
                self.setUp()
                prepare(self.root)
                before = sorted(path.name for path in self.root.iterdir())
                with (
                    mock.patch.object(cubemx_cfg, "add_libxr") as add_libxr,
                    self.assertLogs(level="ERROR") as logs,
                    self.assertRaises(SystemExit) as exit,
                ):
                    cubemx_cfg.setup_project(str(self.root))
                self.assertEqual(exit.exception.code, 1)
                self.assertEqual(logs.output, ["ERROR:root:" + message.format(self.root.name)])
                add_libxr.assert_not_called()
                self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)

    def xrobot_mode(self, existing, option):
        """在 app_main 由 existing 模式生成的工程上运行 setup_project（选项为 option），返回生成时
        的 XRobot 模式。
        Run setup_project with option on a project whose app_main was generated in the existing
        mode; return the XRobot mode it generates with.
        """
        code = self.generate(use_xrobot=existing)
        (self.root / "User" / "app_main.cpp").write_text(code, encoding="utf-8")
        # XRobot 工程需要的 LibXR 文件（见 test_xrobot_needs_a_libxr_with_its_cmake_file）。
        # The LibXR file an XRobot project needs (see
        # test_xrobot_needs_a_libxr_with_its_cmake_file).
        cmake = self.root / "Middlewares" / "Third_Party" / "LibXR" / "cmake"
        cmake.mkdir(parents=True, exist_ok=True)
        (cmake / "XRobot.cmake").write_text("", encoding="utf-8")
        with (
            mock.patch.object(cubemx_cfg, "add_libxr"),
            mock.patch("libxr.peripheral_analyzer_stm32.parse_project"),
            mock.patch("libxr.generator_code_stm32.generate") as generate,
            mock.patch("libxr.generator_stm32_cmake.integrate"),
        ):
            cubemx_cfg.setup_project(str(self.root), xrobot_enable=option)
        return generate.call_args.args[2]

    def test_the_xrobot_mode_follows_the_project_unless_given(self):
        for existing, option, mode in (
            (True, None, True),
            (False, None, False),
            (True, False, False),
            (False, True, True),
        ):
            with self.subTest(existing=existing, option=option):
                self.assertIs(self.xrobot_mode(existing, option), mode)

    def test_xrobot_needs_a_libxr_with_its_cmake_file(self):
        # 以前缺少 cmake/XRobot.cmake 的 LibXR 检出照常生成，构建时才报找不到模块头文件。
        # A LibXR checkout without cmake/XRobot.cmake used to generate as usual and fail only at
        # build time with missing Module headers.
        checkout = self.root / "Middlewares" / "Third_Party" / "LibXR"
        checkout.mkdir(parents=True)
        missing = (
            "ERROR:root:Middlewares/Third_Party/LibXR has no cmake/XRobot.cmake, which a --xrobot "
            "project needs to build its XRobot Modules; check out a LibXR commit that has it with "
            "`libxr stm32 setup --xrobot --commit {}`"
        )
        for option, generated in ((True, False), (False, True)):
            with self.subTest(option=option):
                with (
                    mock.patch.object(cubemx_cfg, "add_libxr"),
                    mock.patch.object(cubemx_cfg, "_report_next_steps"),
                    mock.patch("libxr.peripheral_analyzer_stm32.parse_project") as parse,
                    mock.patch("libxr.generator_code_stm32.generate"),
                    mock.patch("libxr.generator_stm32_cmake.integrate"),
                    contextlib.ExitStack() as stack,
                ):
                    if not generated:
                        logs = stack.enter_context(self.assertLogs(level="ERROR"))
                        exit = stack.enter_context(self.assertRaises(SystemExit))
                    cubemx_cfg.setup_project(str(self.root), xrobot_enable=option, commit="abc")
                self.assertIs(parse.called, generated)
                if not generated:
                    self.assertEqual(exit.exception.code, 1)
                    self.assertEqual(logs.output, [missing.format("<commit>")])
                    self.assertFalse((self.root / ".gitignore").exists())
        # 有默认提交时提示它。
        # The default commit is named when there is one.
        default = "0123456789abcdef0123456789abcdef01234567"
        with self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit):
            cubemx_cfg.check_xrobot_support(str(self.root), default)
        self.assertEqual(logs.output, [missing.format(default)])
        (checkout / "cmake").mkdir()
        (checkout / "cmake" / "XRobot.cmake").write_text("", encoding="utf-8")
        cubemx_cfg.check_xrobot_support(str(self.root), default)

    def test_a_missing_git_is_an_error(self):
        with (
            mock.patch("shutil.which", return_value=None),
            self.assertLogs(level="ERROR") as logs,
            self.assertRaises(SystemExit) as exit,
        ):
            cubemx_cfg.setup_project(str(self.root))
        self.assertEqual(exit.exception.code, 1)
        self.assertEqual(
            logs.output,
            [
                "ERROR:root:git was not found on PATH; LibXR is added to the project as a Git "
                "submodule"
            ],
        )

    def test_an_existing_gitignore_is_kept(self):
        (self.root / ".gitignore").write_text("*.bak\n", encoding="utf-8")
        cubemx_cfg.create_gitignore_file(str(self.root))
        self.assertEqual((self.root / ".gitignore").read_text(encoding="utf-8"), "*.bak\n")

    def test_gitattributes_keeps_text_files_lf_in_the_repository(self):
        cubemx_cfg.create_gitattributes_file(str(self.root))
        self.assertEqual(
            (self.root / ".gitattributes").read_text(encoding="utf-8"),
            "# Text files are LF in the repository; the working tree follows the platform.\n"
            "* text=auto\n*.sh text eol=lf\n*.bat text eol=crlf\n",
        )

    def test_an_existing_gitattributes_only_gets_the_missing_lines(self):
        path = self.root / ".gitattributes"
        for before, after in (
            ("* text=auto\n", "* text=auto\n*.sh text eol=lf\n*.bat text eol=crlf\n"),
            (
                "*.png binary\n* text=auto",
                "*.png binary\n* text=auto\n*.sh text eol=lf\n*.bat text eol=crlf\n",
            ),
            (
                "* text=auto\r\n*.png binary\r\n",
                "* text=auto\r\n*.png binary\r\n*.sh text eol=lf\r\n*.bat text eol=crlf\r\n",
            ),
            (
                "# mine\n*.bat text eol=crlf\n*.sh text eol=lf\n* text=auto\n",
                "# mine\n*.bat text eol=crlf\n*.sh text eol=lf\n* text=auto\n",
            ),
        ):
            with self.subTest(before=before):
                path.write_bytes(before.encode("utf-8"))
                cubemx_cfg.create_gitattributes_file(str(self.root))
                self.assertEqual(path.read_bytes().decode("utf-8"), after)

    def test_the_next_steps_name_the_call_site_and_the_build(self):
        # 以前 setup 只报告完成，没有调用 app_main() 的固件编译通过后什么都不运行。
        # setup used to report only that it finished; firmware without the app_main() call
        # built and then ran nothing.
        sources = self.root / "Core" / "Src"
        sources.mkdir()
        (sources / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        (sources / "freertos.c").write_text(
            "void StartDefaultTask(void *argument) {}\n", encoding="utf-8"
        )
        # adc.c 排在 freertos.c 前面但没有默认任务；只扫描 .c 文件，notes.txt 中的调用不算。
        # adc.c sorts before freertos.c but has no default task; only .c files are scanned,
        # so the call in notes.txt does not count.
        (sources / "adc.c").write_text("void MX_ADC1_Init(void) {}\n", encoding="utf-8")
        (sources / "notes.txt").write_text("app_main();\n", encoding="utf-8")
        (self.root / "CMakePresets.json").write_text(
            '{"configurePresets": [{"name": "default", "hidden": true}, {"name": "Debug"}],'
            ' "buildPresets": [{"name": "Debug", "configurePreset": "Debug"}]}',
            encoding="utf-8",
        )
        build = "INFO:root:Build: cmake --preset Debug && cmake --build --preset Debug"
        with self.assertLogs(level="INFO") as logs:
            cubemx_cfg._report_next_steps(str(self.root))
        self.assertEqual(
            logs.output,
            [
                'INFO:root:Next: #include "app_main.h" and call app_main() in the default task '
                "StartDefaultTask (Core/Src/freertos.c), inside USER CODE sections, which CubeMX "
                "keeps when it regenerates the code.",
                build,
            ],
        )
        (sources / "freertos.c").write_text(
            "void StartDefaultTask(void *argument) {\n  app_main();\n}\n", encoding="utf-8"
        )
        with self.assertLogs(level="INFO") as logs:
            cubemx_cfg._report_next_steps(str(self.root))
        self.assertEqual(logs.output, [build])

    def test_a_threadx_project_is_told_to_call_app_main_from_a_thread(self):
        # 以前 ThreadX 工程也被告知在 main() 中调用 app_main()，而 main() 在启动调度器后不再返回。
        # A ThreadX project used to be told to call app_main() in main(), which does not
        # return once it starts the scheduler.
        sources = self.root / "Core" / "Src"
        sources.mkdir()
        (sources / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        (sources / "app_threadx.c").write_text(
            "UINT App_ThreadX_Init(VOID *memory_ptr) { return TX_SUCCESS; }\n", encoding="utf-8"
        )
        with self.assertLogs(level="INFO") as logs:
            cubemx_cfg._report_next_steps(str(self.root))
        self.assertEqual(
            logs.output,
            [
                "INFO:root:Next: in App_ThreadX_Init() (Core/Src/app_threadx.c), create a thread "
                'whose entry function includes "app_main.h" and calls app_main(), inside USER '
                "CODE sections, which CubeMX keeps when it regenerates the code. "
                "App_ThreadX_Init() runs before the scheduler starts, so it cannot call "
                "app_main() directly."
            ],
        )

    def test_an_xrobot_project_without_modules_gets_the_xrobot_steps(self):
        # 以前只给出构建命令，用户要连续失败两次才找到 xrobot init。
        # Only the build command used to follow, and it took two failures to find xrobot init.
        sources = self.root / "Core" / "Src"
        sources.mkdir()
        (sources / "main.c").write_text("int main(void) { app_main(); }\n", encoding="utf-8")
        steps = [
            "INFO:root:Next: Modules/modules.yaml does not exist yet; set up XRobot in this order:",
            "INFO:root:  xrobot init                           create Modules/modules.yaml, "
            "Modules/sources.yaml and User/xrobot.yaml",
            "INFO:root:  xrobot module add <owner>/<Module>    add a Module",
            "INFO:root:  xrobot setup                          fetch the Modules and generate "
            "User/xrobot_main.hpp",
            "INFO:root:  xrobot instance add <owner>/<Module>  add an instance of the Module",
        ]
        with self.assertLogs(level="INFO") as logs:
            cubemx_cfg._report_next_steps(str(self.root), xrobot=True)
        self.assertEqual(logs.output, steps)
        # 已有 Modules/modules.yaml：libxr 的 setup 不更新 xrobot_main.hpp，提醒运行 xrobot gen
        # （用户 2026-10-07 决定，各平台统一）。
        # With Modules/modules.yaml: the libxr setup does not update xrobot_main.hpp, so it
        # reminds to run xrobot gen (the user's decision of 2026-10-07, on every platform).
        (self.root / "Modules").mkdir()
        (self.root / "Modules" / "modules.yaml").write_text("modules: []\n", encoding="utf-8")
        with self.assertLogs(level="INFO") as logs:
            cubemx_cfg._report_next_steps(str(self.root), xrobot=True)
        self.assertEqual(
            logs.output,
            [
                "INFO:root:Next: run `xrobot gen` to bring User/xrobot_main.hpp up to date; "
                "libxr setup does not update it"
            ],
        )
        (self.root / "Modules" / "modules.yaml").unlink()
        with self.assertNoLogs(level="INFO"):
            cubemx_cfg._report_next_steps(str(self.root), xrobot=False)


class SetupRun(LibXRRemote, GeneratorTestCase):
    """setup_project 依次加入 LibXR、写 .gitignore、.gitattributes 和终端设备、解析 .ioc、生成代码
    并接入 CMake。
    setup_project adds LibXR, writes .gitignore, .gitattributes and the terminal device, parses
    the .ioc file, generates the code and integrates CMake, in that order.
    """

    def test_a_cubemx_project_is_set_up(self):
        project = self.tmp / "project"
        (project / "Core").mkdir(parents=True)
        (project / "demo.ioc").write_text(IOC, encoding="utf-8")
        (project / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
        git("init", "-q", "-b", "master", cwd=project)
        with contextlib.redirect_stdout(io.StringIO()), self.assertLogs(level="INFO") as logs:
            cubemx_cfg.setup_project(
                str(project),
                terminal_source="usart1",
                xrobot_enable=False,
                commit=self.default,
                git_source=str(self.remote),
            )
        self.assertEqual(
            logs.output[-2:],
            [
                "INFO:root:[Pass] All tasks completed.",
                'INFO:root:Next: #include "app_main.h" and call app_main() in main() of '
                "Core/Src/main.c, inside USER CODE sections, which CubeMX keeps when it "
                "regenerates the code.",
            ],
        )
        checkout = project / "Middlewares" / "Third_Party" / "LibXR"
        self.assertEqual(git("rev-parse", "HEAD", cwd=checkout), self.default)
        self.assertEqual(
            (project / ".gitignore").read_text(encoding="utf-8"),
            "build/**\n.history/**\n.cache/**\n.config.yaml\nCMakeFiles/**\n",
        )
        self.assertIn(
            "* text=auto\n*.sh text eol=lf\n*.bat text eol=crlf\n",
            (project / ".gitattributes").read_text(encoding="utf-8"),
        )
        self.assertTrue((project / ".config.yaml").is_file())
        user = project / "User"
        self.assertIn(
            "terminal_source: usart1\n", (user / "libxr_config.yaml").read_text(encoding="utf-8")
        )
        self.assertIn(
            "  STDIO::read_ = usart1.read_port_;\n",
            (user / "app_main.cpp").read_text(encoding="utf-8"),
        )
        self.assertTrue(
            (project / "CMakeLists.txt")
            .read_text(encoding="utf-8")
            .endswith(stm32_cmake.include_cmake_cmd)
        )


class MulticoreSetup(GeneratorTestCase):
    """多核工程的 setup：每个核各自解析硬件、生成代码和接入 CMake，XRobot 选择逐核判定。
    The setup of a multicore project: every core parses its hardware, generates its
    code and integrates CMake on its own, and the XRobot choice is made per core.
    """

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # 工程根目录只有 .ioc 和 .mxproject，Core/ 在每个核的子工程里（多核布局）。
        # The project root holds only the .ioc and .mxproject, with Core/ in every
        # core's subproject (the multicore layout).
        (self.root / "demo.ioc").write_text(MULTICORE_IOC, encoding="utf-8")
        (self.root / ".mxproject").write_text(MULTICORE_MXPROJECT, encoding="utf-8")
        for core in ("CM7", "CM4"):
            (self.root / core / "Core" / "Src").mkdir(parents=True)
            (self.root / core / "Core" / "Inc").mkdir(parents=True)
            (self.root / core / "CMakeLists.txt").write_text("", encoding="utf-8")
        # LibXR 子模块在根目录，核通过 LIBXR_SOURCE_DIR 用它。
        # The LibXR submodule sits in the root, and a core uses it through
        # LIBXR_SOURCE_DIR.
        checkout = self.root / "Middlewares" / "Third_Party" / "LibXR"
        (checkout / "cmake").mkdir(parents=True)
        (checkout / "cmake" / "XRobot.cmake").write_text("", encoding="utf-8")

    def run_setup(self):
        """带 mock 的 add_libxr 运行 setup_project，其余步骤真实执行。
        Run setup_project with add_libxr mocked and every other step real.
        """
        with mock.patch.object(cubemx_cfg, "add_libxr"):
            cubemx_cfg.setup_project(str(self.root))

    def test_every_core_gets_its_own_configuration_and_code(self):
        # 两个核各自解析出的硬件不同（USART3 只归 CM7，TIM6 的时基只归 CM4），各自生成
        # 入口源文件和 CMake 接入。
        # The two cores parse different hardware (USART3 belongs to CM7 only, and the
        # TIM6 timebase to CM4 only) and each generates its entry source and CMake
        # integration.
        self.run_setup()
        cm7 = (self.root / "CM7" / ".config.yaml").read_text(encoding="utf-8")
        cm4 = (self.root / "CM4" / ".config.yaml").read_text(encoding="utf-8")
        self.assertIn("Source: SysTick", cm7)
        self.assertIn("Source: TIM6", cm4)
        self.assertIn("USART3:", cm7)
        self.assertNotIn("USART3:", cm4)
        for core in ("CM7", "CM4"):
            self.assertTrue((self.root / core / "User" / "app_main.cpp").exists())
            self.assertTrue((self.root / core / "cmake" / "LibXR.CMake").exists())
            self.assertIn(
                "include(${CMAKE_CURRENT_LIST_DIR}/cmake/LibXR.CMake)",
                (self.root / core / "CMakeLists.txt").read_text(encoding="utf-8"),
            )
        # CM7 的入口源文件引用 huart3，CM4 的没有：CM4 的 main.c 从不定义它，以前照抄
        # 会在链接时失败。
        # CM7's entry source references huart3 and CM4's does not: CM4's main.c never
        # defines it, and copying it over used to fail the link.
        self.assertIn("huart3", (self.root / "CM7" / "User" / "app_main.cpp").read_text())
        self.assertNotIn("huart3", (self.root / "CM4" / "User" / "app_main.cpp").read_text())

    def test_the_xrobot_choice_is_made_per_core(self):
        # CM7 的入口源文件已按 --xrobot 生成、CM4 的是普通 LibXR：setup 后 CM7 仍是
        # XRobot，CM4 不跟着变成 XRobot。
        # CM7's entry source was generated with --xrobot and CM4's is plain LibXR:
        # after setup CM7 keeps XRobot and CM4 does not follow it.
        (self.root / "CM7" / "User").mkdir()
        (self.root / "CM7" / "User" / "app_main.cpp").write_text(
            self.generate(use_xrobot=True), encoding="utf-8"
        )
        self.run_setup()
        self.assertIn(
            "XR_REGISTER", (self.root / "CM7" / "User" / "app_main.cpp").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "XR_REGISTER", (self.root / "CM4" / "User" / "app_main.cpp").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
