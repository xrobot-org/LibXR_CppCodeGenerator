"""多核 CubeMX 布局的识别（libxr.config_cubemx_project.select_cube_contexts 及其辅助）：
上下文到子工程目录的映射、被拒绝的布局及其原因、非 UTF-8 文件的提示。
The multicore CubeMX layout detection (libxr.config_cubemx_project.select_cube_contexts and
its helpers): the context-to-subproject mapping, the rejected layouts with their reasons, and
the hint a .ioc that is not UTF-8 gets.

这些检查原先在 scripts/cubemx_context_smoke.py 里，只被 CI 的 ruff 检查、从不运行；搬进
tests/ 之后 `python -m unittest discover -s tests` 每次都跑它们，识别逻辑被改坏时 CI 会失败。
These checks used to live in scripts/cubemx_context_smoke.py, which CI only linted with ruff
but never ran; moved into tests/, `python -m unittest discover -s tests` runs them every time
and CI fails when the detection breaks.
"""

import io
import shutil
import tempfile
from pathlib import Path

from fixtures import TestCase

from libxr import peripheral_analyzer_stm32
from libxr.config_cubemx_project import (
    LayoutAmbiguityError,
    detect_cube_contexts,
    select_cube_contexts,
)

# 映射到 CM7/CM4 子工程的 .mxproject，CubeMX 写出的分节格式。
# A .mxproject mapping the CM7/CM4 subprojects, in the section format CubeMX writes.
MXPROJECT = """\
[CortexM7:PreviousGenFiles]
SourcePath#0=..\\CM7\\Core\\Src
HeaderPath#0=..\\CM7\\Core\\Inc

[CortexM4:PreviousGenFiles]
SourcePath#0=..\\CM4\\Core\\Src
HeaderPath#0=..\\CM4\\Core\\Inc
"""


class CubeContextDetection(TestCase):
    """多核布局的识别、拒绝和它们的原因。
    The detection of multicore layouts, their rejection and the reasons given.
    """

    def write_project(self, ioc, mxproject=None, directories=()):
        """写一个含 .ioc、可选 .mxproject 和若干核子工程的最小布局，返回 .ioc 路径。
        Write a minimal layout with an .ioc, an optional .mxproject and the given core
        subprojects; return the .ioc path.
        """
        root = Path(tempfile.mkdtemp(prefix="cubemx_context_"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        (root / "demo.ioc").write_text(ioc, encoding="utf-8")
        if mxproject is not None:
            (root / ".mxproject").write_text(mxproject, encoding="utf-8")
        for directory in directories:
            (root / directory / "Core" / "Src").mkdir(parents=True, exist_ok=True)
        return root / "demo.ioc"

    def test_every_context_maps_to_its_subproject(self):
        # Mcu.Context0=CortexM7 配 .mxproject 的 CM7 路径，解析出 CM7、CM4 两个目录。
        # Mcu.Context0=CortexM7 with the CM7 path of .mxproject resolves the CM7 and CM4
        # directories.
        ioc_file = self.write_project(
            "Mcu.Context0=CortexM7\n"
            "Mcu.Context1=CortexM4\n"
            "Mcu.ContextNb=2\n"
            "CortexM7.IPs=RCC,GPIO\n"
            "CortexM4.IPs=RCC,GPIO\n",
            MXPROJECT,
            ("CM7", "CM4"),
        )
        contexts = select_cube_contexts(str(ioc_file))
        self.assertEqual(
            [Path(context["project_dir"]).name for context in contexts], ["CM7", "CM4"]
        )
        self.assertEqual([context["name"] for context in contexts], ["CortexM7", "CortexM4"])

    def test_stale_mxproject_paths_are_recovered(self):
        # .mxproject 里写的是旧机器的绝对路径时，按目录名在工程树里找到已生成的子工程。
        # With absolute paths of an old machine in .mxproject, the generated subprojects
        # are found by directory name in the project tree.
        ioc_file = self.write_project(
            "Mcu.Context0=CortexM7\n"
            "Mcu.Context1=CortexM4\n"
            "Mcu.ContextNb=2\n"
            "CortexM7.IPs=RCC\n"
            "CortexM4.IPs=RCC\n",
            "[CortexM7:PreviousGenFiles]\n"
            "SourcePath#0=C:\\old_workspace\\demo\\CM7\\Core\\Src\n"
            "\n"
            "[CortexM4:PreviousGenFiles]\n"
            "SourcePath#0=C:\\old_workspace\\demo\\CM4\\Core\\Src\n",
            ("CM7", "CM4"),
        )
        contexts = select_cube_contexts(str(ioc_file))
        self.assertEqual(
            [Path(context["project_dir"]).name for context in contexts], ["CM7", "CM4"]
        )

    def test_a_trustzone_layout_is_refused_with_its_context(self):
        # H5 TrustZone（CortexM33S/CortexM33NS）不是“简单多核”布局，拒绝信息要点名这个
        # 上下文，而不是一句没有原因的英文。
        # A TrustZone layout (CortexM33S/CortexM33NS) is not a "simple multicore" one, and
        # the rejection must name the context instead of a bare English sentence.
        ioc_file = self.write_project(
            "Mcu.Context0=CortexM33S\n"
            "Mcu.Context1=CortexM33NS\n"
            "Mcu.ContextNb=2\n"
            "Mcu.ContextProject=TrustZoneEnabled\n"
            "CortexM33S.IPs=RCC\n"
            "CortexM33NS.IPs=RCC\n",
            "[CortexM33S:PreviousGenFiles]\n"
            "SourcePath#0=..\\Secure\\Core\\Src\n"
            "\n"
            "[CortexM33NS:PreviousGenFiles]\n"
            "SourcePath#0=..\\NonSecure\\Core\\Src\n",
            ("Secure", "NonSecure"),
        )
        with self.assertRaises(LayoutAmbiguityError) as caught:
            select_cube_contexts(str(ioc_file))
        self.assertIn("Cannot identify an unambiguous simple multi-core", str(caught.exception))
        self.assertIn("CortexM33S", str(caught.exception))

    def test_a_wrong_context_count_is_refused_with_the_numbers(self):
        # Mcu.ContextNb 与条目不一致时给出两个数。
        # When Mcu.ContextNb disagrees with the entries, both numbers are given.
        ioc_file = self.write_project(
            "Mcu.Context0=CortexM7\n"
            "Mcu.Context1=CortexM4\n"
            "Mcu.ContextNb=3\n"
            "CortexM7.IPs=RCC\n"
            "CortexM4.IPs=RCC\n",
            MXPROJECT,
            ("CM7", "CM4"),
        )
        with self.assertRaises(LayoutAmbiguityError) as caught:
            select_cube_contexts(str(ioc_file))
        self.assertIn("Mcu.ContextNb=3 but the .ioc lists 2 contexts", str(caught.exception))

    def test_an_ambiguous_subproject_is_refused_with_the_context(self):
        # 一个上下文在 .mxproject 里对应两个已生成子工程时点名的它和个数。
        # When one context maps to two generated subprojects in .mxproject, it and the
        # count are named.
        ioc_file = self.write_project(
            "Mcu.Context0=CortexM7\n"
            "Mcu.Context1=CortexM4\n"
            "Mcu.ContextNb=2\n"
            "CortexM7.IPs=RCC\n"
            "CortexM4.IPs=RCC\n",
            "[CortexM7:PreviousGenFiles]\n"
            "SourcePath#0=..\\CM7\\Core\\Src\n"
            "SourcePath#1=..\\CM7-copy\\Core\\Src\n"
            "\n"
            "[CortexM4:PreviousGenFiles]\n"
            "SourcePath#0=..\\CM4\\Core\\Src\n",
            ("CM7", "CM4", "CM7-copy"),
        )
        with self.assertRaises(LayoutAmbiguityError) as caught:
            select_cube_contexts(str(ioc_file))
        self.assertIn("context CortexM7 maps to 2 generated subprojects", str(caught.exception))

    def test_a_missing_mxproject_is_refused(self):
        # 没有 .mxproject 时无法把上下文映射到子工程，按多核以外的布局处理（单核工程）。
        # Without .mxproject the contexts cannot be mapped to subprojects, so the
        # project is not handled as a single-core one.
        ioc_file = self.write_project(
            "Mcu.Context0=CortexM7\nMcu.Context1=CortexM4\nMcu.ContextNb=2\n",
            None,
            ("CM7", "CM4"),
        )
        with self.assertRaises(LayoutAmbiguityError):
            select_cube_contexts(str(ioc_file))

    def test_a_single_context_project_uses_the_project_root(self):
        # 只有一个上下文时不按多核处理，setup 按单核工程走。
        # With one context the project is not handled as multicore and setup runs the
        # single-core path.
        ioc_file = self.write_project(
            "Mcu.Context0=CortexM33\nMcu.ContextNb=1\nCortexM33.IPs=RCC\n", None, ("",)
        )
        self.assertEqual(select_cube_contexts(str(ioc_file)), [])

    def test_detection_keeps_non_cortex_context_metadata(self):
        # detect_cube_contexts 只报元数据，不猜布局：TrustZone 的两个上下文照常读出。
        # detect_cube_contexts reports metadata only and never guesses a layout: the
        # two TrustZone contexts are read as they are.
        ioc_file = self.write_project(
            "Mcu.Context0=CortexM33S\n"
            "Mcu.Context1=CortexM33NS\n"
            "Mcu.ContextNb=2\n"
            "Mcu.ContextProject=TrustZoneEnabled\n"
            "CortexM33S.IPs=RCC\n"
            "CortexM33NS.IPs=RCC\n",
            None,
            (),
        )
        self.assertEqual(
            detect_cube_contexts(str(ioc_file)),
            [
                {"name": "CortexM33S", "normalized": "CORTEXM33S", "ips": ["RCC"]},
                {"name": "CortexM33NS", "normalized": "CORTEXM33NS", "ips": ["RCC"]},
            ],
        )

    def test_a_multicore_ioc_that_is_not_utf8_keeps_the_save_hint(self):
        # 多核检测先于解析读 .ioc：非 UTF-8 的文件要得到和解析器一样的保存提示，而不是
        # 原始的 UnicodeDecodeError。
        # Multicore detection reads the .ioc before parsing: a .ioc that is not UTF-8
        # gets the same save hint as the parser, not a raw UnicodeDecodeError.
        root = Path(tempfile.mkdtemp(prefix="cubemx_context_"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        ioc_file = root / "demo.ioc"
        ioc_file.write_bytes(b"Mcu.Context0=CortexM7\nMcu.ContextNb=2\n\xff\xfe\n")
        with self.assertRaises(ValueError) as caught:
            select_cube_contexts(str(ioc_file))
        self.assertIn("is not UTF-8 text", str(caught.exception))
        self.assertIn("save it as UTF-8", str(caught.exception))

    def test_the_context_filtering_assigns_entries_to_the_owning_core(self):
        # 按核过滤：虚拟引脚和外设条目归拥有它们的核（冒烟脚本搬过来的核心检查）。
        # The context filtering assigns entries to the core owning them (the core check
        # moved over from the smoke script).
        raw_map = peripheral_analyzer_stm32._extract_key_value_pairs(
            io.StringIO(
                "Mcu.Context0=CortexM7\n"
                "Mcu.Context1=CortexM4\n"
                "Mcu.ContextNb=2\n"
                "CortexM7.IPs=RCC,NVIC1\\:I,SYS\\:I,USART3\\:I\n"
                "CortexM4.IPs=RCC,NVIC2\\:I,SYS_M4\\:I,USART1\\:I\n"
                "VP_SYS_VS_Systick.Mode=SysTick\n"
                "VP_SYS_M4_VS_Systick.Mode=SysTick\n"
                "NVIC1.SysTick_IRQn=true\n"
                "NVIC2.SysTick_IRQn=true\n"
                "USART3.BaudRate=115200\n"
                "USART1.BaudRate=115200\n"
            )
        )
        cm7 = peripheral_analyzer_stm32.filter_ioc_context(raw_map, "CM7")
        cm4 = peripheral_analyzer_stm32.filter_ioc_context(raw_map, "Cortex_M4")
        self.assertIn("VP_SYS_VS_Systick.Mode", cm7)
        self.assertNotIn("VP_SYS_M4_VS_Systick.Mode", cm7)
        self.assertIn("VP_SYS_M4_VS_Systick.Mode", cm4)
        self.assertNotIn("VP_SYS_VS_Systick.Mode", cm4)
        self.assertIn("USART3.BaudRate", cm7)
        self.assertNotIn("USART1.BaudRate", cm7)
        self.assertIn("USART1.BaudRate", cm4)
        self.assertNotIn("USART3.BaudRate", cm4)
        self.assertIn("NVIC1.SysTick_IRQn", cm7)
        self.assertNotIn("NVIC2.SysTick_IRQn", cm7)
        self.assertIn("NVIC2.SysTick_IRQn", cm4)
        self.assertNotIn("NVIC1.SysTick_IRQn", cm4)
