"""STM32CubeMX 生成后步骤（libxr.stm32_cubemx_sync）：.ioc 的修改、生成后脚本的登记、.ioc 摘要和
LibXR.CMake 中的摘要检查，以及 sync 失败时的日志。
The STM32CubeMX post-generation step (libxr.stm32_cubemx_sync): the changes to the .ioc file,
the registration of the post-generation script, the .ioc digest and its check in LibXR.CMake,
and the log of a failed sync.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fixtures import GeneratorTestCase, TestCase

from libxr import generator_code_stm32 as generator
from libxr import generator_stm32_cmake as stm32_cmake
from libxr import stm32_cubemx_sync as cubemx_sync

# CubeMX 默认设置下 OTG_FS 的相关行：main() 调用 MX_USB_OTG_FS_PCD_Init（static），NVIC 生成
# OTG_FS_IRQHandler。CubeMX 在 Windows 上写 CRLF。
# The OTG_FS lines with the CubeMX defaults: main() calls MX_USB_OTG_FS_PCD_Init (static) and
# NVIC generates OTG_FS_IRQHandler. CubeMX writes CRLF on Windows.
IOC = (
    "#MicroXplorer Configuration settings - do not modify\r\n"
    "NVIC.OTG_FS_IRQn=true\\:5\\:0\\:false\\:false\\:true\\:true\\:true\\:true\\:true\r\n"
    "NVIC.TIM6_DAC_IRQn=true\\:0\\:0\\:false\\:false\\:true\\:true\\:true\\:true\\:true\r\n"
    "ProjectManager.UAScriptAfterPath=\r\n"
    "ProjectManager.UAScriptBeforePath=\r\n"
    "ProjectManager.functionlistsort=1-SystemClock_Config-RCC-false-HAL-false,"
    "2-MX_GPIO_Init-GPIO-false-HAL-true,3-MX_USB_OTG_FS_PCD_Init-USB_OTG_FS-false-HAL-true\r\n"
)


class IocChanges(TestCase):
    """.ioc 的修改只动目标字段，换行保持原样。
    The changes to the .ioc file touch only the target fields and keep the line endings.
    """

    def test_the_otg_instance_is_neither_called_nor_handled_by_cubemx(self):
        text, changes = cubemx_sync.otg_ioc_edits(IOC, ["USB_OTG_FS"])
        self.assertEqual(len(changes), 2)
        self.assertIn("3-MX_USB_OTG_FS_PCD_Init-USB_OTG_FS-true-HAL-false\r\n", text)
        self.assertIn(
            "NVIC.OTG_FS_IRQn=true\\:5\\:0\\:false\\:false\\:false\\:true\\:true\\:true\\:true\r\n",
            text,
        )
        # Other entries and interrupts stay as they were.
        self.assertIn("2-MX_GPIO_Init-GPIO-false-HAL-true", text)
        self.assertIn("NVIC.TIM6_DAC_IRQn=true\\:0\\:0\\:false\\:false\\:true\\:", text)
        self.assertEqual(text.count("\r\n"), IOC.count("\r\n"))
        # A second run changes nothing.
        self.assertEqual(cubemx_sync.otg_ioc_edits(text, ["USB_OTG_FS"]), (text, []))

    def test_the_post_generation_script_is_registered_once(self):
        text, other = cubemx_sync.set_after_script(IOC, cubemx_sync.HOOK_CMD)
        self.assertIsNone(other)
        self.assertIn("ProjectManager.UAScriptAfterPath=cmake/libxr_after_cubemx.cmd\r\n", text)
        self.assertEqual(cubemx_sync.set_after_script(text, cubemx_sync.HOOK_CMD), (text, None))

    def test_a_script_of_the_user_is_kept(self):
        mine = IOC.replace("UAScriptAfterPath=", "UAScriptAfterPath=tools/mine.cmd")
        self.assertEqual(
            cubemx_sync.set_after_script(mine, cubemx_sync.HOOK_CMD), (mine, "tools/mine.cmd")
        )

    def test_a_missing_key_is_inserted_in_key_order(self):
        text = IOC.replace("ProjectManager.UAScriptAfterPath=\r\n", "")
        text, _ = cubemx_sync.set_after_script(text, cubemx_sync.HOOK_CMD)
        self.assertIn(
            "ProjectManager.UAScriptAfterPath=cmake/libxr_after_cubemx.cmd\r\n"
            "ProjectManager.UAScriptBeforePath=\r\n",
            text,
        )

    def test_the_digest_ignores_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            crlf = Path(directory, "crlf.ioc")
            lf = Path(directory, "lf.ioc")
            crlf.write_bytes(IOC.encode())
            lf.write_bytes(IOC.replace("\r\n", "\n").encode())
            self.assertEqual(cubemx_sync.ioc_digest(crlf), cubemx_sync.ioc_digest(lf))
            self.assertRegex(cubemx_sync.ioc_digest(crlf), "^[0-9a-f]{16}$")


class HookScripts(TestCase):
    """生成后脚本：同一个文件对 cmd 和 sh 都成立。
    The post-generation script: one file valid for both cmd and sh.
    """

    def test_the_first_line_hands_over_to_sh_and_is_a_label_for_cmd(self):
        first = cubemx_sync.HOOK_CMD_TEXT.split("\n")[0]
        # A colon starts a label in cmd; in sh it is the empty command.
        self.assertTrue(first.startswith(":;"))
        # The comment at the end absorbs the carriage return of a CRLF checkout.
        self.assertTrue(first.endswith(" #"))

    @unittest.skipUnless(shutil.which("sh"), "sh is not available")
    def test_sh_reports_a_missing_libxr_in_the_log(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "cmake"))
            for relative, text in (
                (cubemx_sync.HOOK_CMD, cubemx_sync.HOOK_CMD_TEXT),
                (cubemx_sync.HOOK_SH, cubemx_sync.HOOK_SH_TEXT),
            ):
                Path(directory, relative).write_text(text, encoding="utf-8", newline="\n")
            # A PATH without any python: the script must say so instead of failing silently.
            result = subprocess.run(
                ["sh", cubemx_sync.HOOK_CMD],
                cwd=directory,
                capture_output=True,
                text=True,
                env={"PATH": os.path.dirname(shutil.which("sh")), "LIBXR_PYTHON": ""},
            )
            self.assertEqual(result.returncode, 1)
            log = Path(directory, cubemx_sync.SYNC_LOG).read_text(encoding="utf-8")
            self.assertIn("[LibXR] FAILED: no Python with a libxr that has", log)
            self.assertIn("pip install -U libxr", log)
            self.assertIn(log.strip(), result.stdout)


class GeneratedRecords(GeneratorTestCase):
    """生成器记下 .ioc 摘要，并按 Core/Src 中实际生成的中断函数决定是否补向量函数。
    The generator records the .ioc digest and writes a vector function according to the
    interrupt handlers actually generated in Core/Src.
    """

    def test_the_notice_names_the_ioc_digest(self):
        project = self.project()
        project["Ioc"] = "Board.ioc"
        project["IocDigest"] = "0123456789abcdef"
        code = self.generate(project)
        self.assertIn("// Board.ioc digest: 0123456789abcdef\n", code)

    def test_the_generated_sources_decide_the_vector(self):
        settings = {
            "Role": "Device",
            "Vectors": {"OTG_HS_IRQn": {"Enabled": True, "Handler": False}},
        }
        generator.libxr_settings.setdefault("USB", {})["usb_otg_hs"] = {"enable": True}
        project = self.project(peripherals={"USB": {"USB_OTG_HS": {"enable": True, **settings}}})
        # The .ioc file already turns the handler off, but Core/Src still defines it: no second
        # definition.
        generator.cubemx_irq_handlers = {"OTG_HS_IRQHandler"}
        try:
            self.assertNotIn("void OTG_HS_IRQHandler", self.generate(project))
            generator.cubemx_irq_handlers = set()
            self.assertIn('extern "C" void OTG_HS_IRQHandler(void)', self.generate(project))
        finally:
            generator.cubemx_irq_handlers = None
        self.assertEqual(generator.otg_devices, ["USB_OTG_HS"])


@unittest.skipUnless(shutil.which("cmake"), "cmake is not available")
class CMakeDigestCheck(TestCase):
    """LibXR.CMake 的 CubeMX 块：摘要一致时配置通过，不一致时失败并附上生成后步骤的日志。
    The CubeMX block of LibXR.CMake: configuring passes with a matching digest and fails with the
    log of the post-generation step otherwise.
    """

    def configure(self, directory, digest, log=None):
        """在 directory 中写出 Board.ioc、记下 digest 的 app_main.cpp、可选的日志和只含 CubeMX 块的
        CMakeLists.txt，运行 CMake 配置并返回结果。
        Write Board.ioc, an app_main.cpp recording digest, the optional log and a CMakeLists.txt
        with only the CubeMX block into directory, run the CMake configuration and return the
        result.
        """
        Path(directory, "Board.ioc").write_bytes(IOC.encode())
        Path(directory, "User").mkdir(exist_ok=True)
        Path(directory, "User", "app_main.cpp").write_text(
            f"// Board.ioc digest: {digest}\n", encoding="utf-8"
        )
        if log is not None:
            Path(directory, ".libxr").mkdir(exist_ok=True)
            Path(directory, cubemx_sync.SYNC_LOG).write_text(log, encoding="utf-8")
        Path(directory, "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.22)\nproject(t NONE)\n"
            + stm32_cmake.CUBEMX_BLOCK
            + "\n",
            encoding="utf-8",
        )
        return subprocess.run(
            ["cmake", "-S", directory, "-B", os.path.join(directory, "build")],
            capture_output=True,
            text=True,
        )

    def test_a_matching_digest_configures(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "Board.ioc").write_bytes(IOC.encode())
            digest = cubemx_sync.ioc_digest(os.path.join(directory, "Board.ioc"))
            result = self.configure(directory, digest)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_another_digest_fails_with_the_log(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.configure(
                directory, "0000000000000000", log="[ERROR] something; with a semicolon\n"
            )
            self.assertNotEqual(result.returncode, 0)
            # CMake wraps the message; compare the words.
            message = " ".join(result.stderr.split())
            self.assertIn("changed after User/app_main.cpp was generated", message)
            self.assertIn("python -m libxr stm32 sync", message)
            self.assertIn("[ERROR] something; with a semicolon", message)


class FailedSync(TestCase):
    """sync 失败时，日志的最后一行说明怎样手动重试。
    When sync fails, the last line of the log tells how to retry by hand.
    """

    def test_the_log_ends_with_the_command_to_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs(level="ERROR"):
                self.assertEqual(cubemx_sync.sync(directory, after_cubemx=True), 1)
            log = Path(directory, cubemx_sync.SYNC_LOG).read_text(encoding="utf-8")
            self.assertIn("missing Core/ directory", log)
            self.assertIn("python -m libxr stm32 sync", log.strip().splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
