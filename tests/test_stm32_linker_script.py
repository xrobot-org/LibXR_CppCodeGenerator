"""STM32 链接脚本中 GOT 的位置（libxr.stm32_linker_script）：没有 .got 时插在 .fini_array 之后，
已有的 .got 不紧跟 .fini_array 时给出警告，重复运行不再改动。
The place of the GOT in an STM32 linker script (libxr.stm32_linker_script): without .got one is
inserted after .fini_array, an existing .got that does not directly follow .fini_array gets a
warning, and running again changes nothing.
"""

import tempfile
import textwrap
from pathlib import Path

from fixtures import TestCase, logging_marker

from libxr import generator_stm32_cmake as stm32_cmake
from libxr import stm32_linker_script as linker

# CubeMX 6.18.1 为 STM32F407 写出的链接脚本中 .fini_array 前后的部分（省略号处有删节）。
# The part around .fini_array of the linker script CubeMX 6.18.1 writes for an STM32F407 (cut
# where the ellipses are).
CUBEMX_SCRIPT = textwrap.dedent("""\
    /* Memories definition */
    MEMORY
    {
      CCMRAM    (xrw)    : ORIGIN = 0x10000000,   LENGTH = 64K
      RAM    (xrw)    : ORIGIN = 0x20000000,   LENGTH = 128K
      FLASH    (rx)    : ORIGIN = 0x8000000,   LENGTH = 1024K
    }

    /* Sections */
    SECTIONS
    {
      /* ... */
      .init_array (READONLY) : /* The "READONLY" keyword is only supported in GCC11 and later, remove it if using GCC10 or earlier. */
      {
        . = ALIGN(4);
        PROVIDE_HIDDEN (__init_array_start = .);
        KEEP (*(SORT(.init_array.*)))
        KEEP (*(.init_array*))
        PROVIDE_HIDDEN (__init_array_end = .);
        . = ALIGN(4);
      } >FLASH

      .fini_array (READONLY) : /* The "READONLY" keyword is only supported in GCC11 and later, remove it if using GCC10 or earlier. */
      {
        . = ALIGN(4);
        PROVIDE_HIDDEN (__fini_array_start = .);
        KEEP (*(SORT(.fini_array.*)))
        KEEP (*(.fini_array*))
        PROVIDE_HIDDEN (__fini_array_end = .);
        . = ALIGN(4);
      } >FLASH

      _siccmram = LOADADDR(.ccmram);

      /* CCM-RAM section */
      .ccmram :
      {
        . = ALIGN(4);
        _sccmram = .;       /* create a global symbol at ccmram start */
        *(.ccmram)
        *(.ccmram*)
        . = ALIGN(4);
        _eccmram = .;       /* create a global symbol at ccmram end */
      } >CCMRAM AT> FLASH

      /* used by the startup to initialize data */
      _sidata = LOADADDR(.data);

      /* Initialized data sections goes into RAM, load LMA copy after code */
      .data :
      {
        . = ALIGN(4);
        _sdata = .;        /* create a global symbol at data start */
        *(.data)           /* .data sections */
        *(.data*)          /* .data* sections */
        . = ALIGN(4);
      } >RAM AT> FLASH

      /* Initialized TLS data section */
      .tdata : ALIGN(4)
      {
        *(.tdata .tdata.* .gnu.linkonce.td.*)
        . = ALIGN(4);
        _edata = .;        /* define a global symbol at data end */
      } >RAM AT> FLASH
      /* ... */
    }
    """)

# 插入后的 .got 输出段。
# The .got output section after the insertion.
GOT_BLOCK = """\

  /* GOT entries are link-time constants in this static image. ld.lld puts the init/fini
     arrays and the GOT in one RELRO range, so .got must directly follow .fini_array. */
  .got :
  {
    . = ALIGN(4);
    *(.got .got.*)
    . = ALIGN(4);
  } >FLASH
"""
FINI_END = "    PROVIDE_HIDDEN (__fini_array_end = .);\n    . = ALIGN(4);\n  } >FLASH\n"
PLACED_SCRIPT = CUBEMX_SCRIPT.replace(FINI_END, FINI_END + GOT_BLOCK)
# .got 在 CCM 段之后的脚本：ST Arm Clang 的 NEWLIB 配置链接失败的写法。
# A script with .got after the CCM section: the layout an ST Arm Clang NEWLIB link fails on.
GOT_AFTER_CCM = CUBEMX_SCRIPT.replace(
    "  /* used by the startup", GOT_BLOCK.lstrip("\n") + "\n  /* used by the startup"
)


class PlacedGot(TestCase):
    """单个链接脚本中 .got 的插入、保留和警告。
    The insertion, keeping and warnings of .got in a single linker script.
    """

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "STM32F407XX_FLASH.ld"

    def place(self, text):
        """写入链接脚本并放置 GOT，返回 (是否写回, 结果)。
        Write the linker script, place the GOT and return (whether it was written, the result).
        """
        self.path.write_bytes(text.encode("utf-8"))
        changed = linker.place_got(self.path)
        return changed, self.path.read_bytes().decode("utf-8")

    def test_a_cubemx_script_gets_got_directly_after_fini_array(self):
        self.assertEqual(self.place(CUBEMX_SCRIPT), (True, PLACED_SCRIPT))

    def test_running_again_changes_nothing(self):
        self.place(CUBEMX_SCRIPT)
        with self.assertLogs(level="INFO") as logs:
            logging_marker()
            self.assertEqual(self.place(PLACED_SCRIPT), (False, PLACED_SCRIPT))
        self.assertEqual(logs.output, ["WARNING:root:marker"])

    def test_line_endings_stay_as_the_file_has_them(self):
        crlf = CUBEMX_SCRIPT.replace("\n", "\r\n")
        self.assertEqual(self.place(crlf), (True, PLACED_SCRIPT.replace("\n", "\r\n")))

    def test_the_got_takes_the_memory_region_of_fini_array(self):
        rom = CUBEMX_SCRIPT.replace("FLASH", "ROM")
        self.assertEqual(self.place(rom), (True, PLACED_SCRIPT.replace("FLASH", "ROM")))

    def test_got_in_comments_or_input_rules_does_not_count(self):
        # 更早的 BSP 在 .data 中加载 GOT；插入的 .got 在前，先取走这些输入段。
        # Earlier BSPs loaded the GOT with .data; the inserted .got comes first and takes those
        # input sections.
        with_rule = CUBEMX_SCRIPT.replace(
            "    *(.data*)          /* .data* sections */\n",
            "    *(.data*)          /* .data* sections */\n"
            "    *(.got .got.*)     /* .got : loaded with .data */\n",
        )
        changed, text = self.place(with_rule)
        self.assertTrue(changed)
        self.assertEqual(text, with_rule.replace(FINI_END, FINI_END + GOT_BLOCK))

    def test_a_got_elsewhere_is_left_alone_with_a_warning(self):
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(self.place(GOT_AFTER_CCM), (False, GOT_AFTER_CCM))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("does not directly follow .fini_array", logs.output[0])

    def test_a_script_without_fini_array_is_left_alone_with_a_warning(self):
        script = "SECTIONS\n{\n  .text :\n  {\n    *(.text*)\n  } >FLASH\n}\n"
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(self.place(script), (False, script))
        self.assertIn("has no .fini_array output section", logs.output[0])


class ProjectScripts(TestCase):
    """工程根目录中的每份链接脚本，以及 libxr stm32 cmake 中的这一步。
    Every linker script in the root of a project, and this step of libxr stm32 cmake.
    """

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_every_linker_script_in_the_root_is_placed(self):
        (self.root / "STM32F407XX_FLASH.ld").write_text(CUBEMX_SCRIPT, encoding="utf-8")
        (self.root / "STM32F407XX_RAM.ld").write_text(PLACED_SCRIPT, encoding="utf-8")
        (self.root / "notes.txt").write_text(CUBEMX_SCRIPT, encoding="utf-8")
        written = linker.place_got_in_project(self.root)
        self.assertEqual(written, [self.root / "STM32F407XX_FLASH.ld"])
        for name in ("STM32F407XX_FLASH.ld", "STM32F407XX_RAM.ld"):
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), PLACED_SCRIPT)
        self.assertEqual((self.root / "notes.txt").read_text(encoding="utf-8"), CUBEMX_SCRIPT)

    def test_integrating_cmake_places_the_got(self):
        (self.root / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
        (self.root / "STM32F407XX_FLASH.ld").write_text(CUBEMX_SCRIPT, encoding="utf-8")
        stm32_cmake.integrate(str(self.root))
        self.assertEqual(
            (self.root / "STM32F407XX_FLASH.ld").read_text(encoding="utf-8"), PLACED_SCRIPT
        )
