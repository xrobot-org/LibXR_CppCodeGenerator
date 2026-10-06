"""MSPM0 的解析与生成（libxr.peripheral_analyzer_mspm0、libxr.generator_code_mspm0）：工程
YAML 的内容、生成的 app_main 源文件，以及重新生成时保留的用户代码。
Parsing and generating an MSPM0 project (libxr.peripheral_analyzer_mspm0,
libxr.generator_code_mspm0): the project YAML, the generated app_main source, and the user
code kept on regeneration.

测试固件是两个 BSP 的真实工程文件：.syscfg 加上 SysConfig 生成的 ti_msp_dl_config.h/.c（CI 里
没有 SysConfig，输出随工程一起提交）。测试把固件复制进临时工程，使生成输出的修改时间比 .syscfg
新，解析直接复用它。
The fixtures are the real project files of the two BSPs: the .syscfg plus the
ti_msp_dl_config.h/.c that SysConfig generated (CI has no SysConfig, so the output is committed
with the project). A test copies the fixtures into a temporary project, so the generated output
is newer than the .syscfg and parsing reuses it.
"""

import os
import shutil
import tempfile
from pathlib import Path

import yaml
from fixtures import DATA, TestCase

from libxr import generator_code_mspm0 as generator
from libxr.peripheral_analyzer_mspm0 import parse_project

# 与 CI 无关的本地 SysConfig 环境；设置了就顺便测试运行 SysConfig 的路径，否则只测复用路径。
# A local SysConfig environment, independent of CI; when set, the run-SysConfig path is
# exercised as well, otherwise only the reuse path.
SYSCONFIG_ENV = {
    name: os.environ.get(name, "") for name in ("SYSCONFIG_TOOL", "MSPM0_SDK_INSTALL_DIR")
}


def bsp(name: str) -> str:
    """g3507 这样的短名对应的固件 .syscfg 文件名。
    The fixture .syscfg file name of a short name such as g3507.
    """
    return {
        "g3507": "mspm0g3507_minidb48.syscfg",
        "g3519": "mspm0g3519_minidb48.syscfg",
    }[name]


def project_of(name: str) -> Path:
    """把 name 的固件复制进一个临时工程：根目录的 .syscfg 加上 build/test/syscfg 里的 SysConfig
    输出（修改时间更新）；返回工程目录。测试结束时删除。
    Copy the fixtures of name into a temporary project: the .syscfg in the root plus the
    SysConfig output in build/test/syscfg (with a newer modification time); the project
    directory is returned and removed at the end of the test.
    """
    root = Path(tempfile.mkdtemp(prefix="libxr-mspm0-"))
    shutil.copy(DATA / "mspm0" / f"{name}.syscfg", root / bsp(name))
    output = root / "build" / "test" / "syscfg"
    output.mkdir(parents=True)
    for suffix in ("ti_msp_dl_config.h", "ti_msp_dl_config.c"):
        target = output / suffix
        shutil.copy(DATA / "mspm0" / f"{name}_{suffix}", target)
        # 复制保留了源文件的旧修改时间；更新它，使解析认定输出比 .syscfg 新。
        # The copy keeps the old mtime of the source; touch it so parsing takes the output as
        # newer than the .syscfg.
        os.utime(target)
    return root


class Parsing(TestCase):
    """libxr parse：工程 YAML 由 .syscfg 和 SysConfig 输出组成。
    libxr parse: the project YAML comes from the .syscfg and the SysConfig output.
    """

    def parse(self, name: str) -> dict:
        """解析 name 的临时工程，返回写出的工程 YAML。
        Parse the temporary project of name and return the project YAML it wrote.
        """
        root = project_of(name)
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        parse_project(str(root), summary=False)
        return yaml.safe_load((root / ".config.yaml").read_text(encoding="utf-8"))

    def test_the_platform_the_device_and_the_clocks_come_from_the_project(self):
        data = self.parse("g3507")
        self.assertEqual(data["Platform"], "mspm0")
        self.assertEqual(data["Syscfg"], "mspm0g3507_minidb48.syscfg")
        self.assertEqual(data["Mcu"], {"Family": "MSPM0", "Type": "MSPM0G3507"})
        self.assertEqual(data["CPUCLK"], 80000000)
        self.assertEqual(data["ULPCLKDivider"], 2)

    def test_gpio_pins_carry_the_macros_the_output_actually_emits(self):
        gpio = self.parse("g3507")["GPIO"]
        self.assertEqual(
            gpio["LED1"],
            {
                "Group": "GPIO_LEDS",
                "Pin": "PB8",
                "Port": "GPIO_LEDS_PIN_LED1_PORT",
                "PinMacro": "GPIO_LEDS_PIN_LED1_PIN",
                "IOMUX": "GPIO_LEDS_PIN_LED1_IOMUX",
                "Direction": "OUTPUT",
                "Resistor": "NONE",
            },
        )
        self.assertEqual(gpio["KEY1"]["Port"], "GPIO_KEYS_PORT")
        self.assertEqual(gpio["KEY1"]["Direction"], "INPUT")
        self.assertEqual(gpio["KEY1"]["Interrupt"], "FALL")

    def test_peripherals_carry_the_dma_channels_the_power_domain_and_the_pwm_channels(self):
        peripherals = self.parse("g3507")["Peripherals"]
        self.assertEqual(
            peripherals["UART"]["UART_0"],
            {
                "Peripheral": "UART0",
                "DMA_TX": "DMA_CH_UART0_TX",
                "Pins": {"rxPin": "PA11", "txPin": "PA10"},
                "PowerDomain": 0,
            },
        )
        self.assertEqual(peripherals["SPI"]["SPI_1"]["PowerDomain"], 1)
        self.assertEqual(peripherals["SPI"]["SPI_1"]["DMA_RX"], "DMA_CH_SPI1_RX")
        self.assertEqual(peripherals["PWM"]["PWM_TIMA1"]["Channels"], [0, 1])
        self.assertNotIn("GPIO", peripherals)

    def test_ignored_peripherals_are_kept_for_display(self):
        peripherals = self.parse("g3507")["Peripherals"]
        self.assertEqual(peripherals["Other"]["MCAN_0"], {"Module": "MCAN", "Peripheral": "CANFD0"})

    def test_the_second_uart_of_the_other_board_is_parsed_as_well(self):
        peripherals = self.parse("g3519")["Peripherals"]
        self.assertEqual(peripherals["UART"]["UART_7"]["DMA_TX"], "DMA_CH_UART7_TX")
        self.assertEqual(peripherals["UART"]["UART_7"]["PowerDomain"], 0)


class Generation(TestCase):
    """libxr gen：生成的 app_main 源文件与设置，以及重新生成时保留的用户代码。
    libxr gen: the generated app_main source and settings, and the user code kept on
    regeneration.
    """

    def setUp(self):
        super().setUp()
        generator.initialize_registry()
        generator.reset_settings()

    def generate(self, name: str, use_xrobot: bool = True) -> tuple[Path, str]:
        """解析 name 的临时工程并生成 app_main 源文件；工程里的 User/app_main.cpp 先放上审查过
        的输出（重新生成的起点）。返回工程目录和重新生成的文本。
        Parse the temporary project of name and generate the app_main source; the reviewed
        output is placed at User/app_main.cpp first, as the starting point of the
        regeneration. The project directory and the regenerated text are returned.
        """
        root = project_of(name)
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        parse_project(str(root), summary=False)
        output = root / "User" / "app_main.cpp"
        output.parent.mkdir()
        reviewed = DATA / "mspm0" / f"{name}_expected_app_main.cpp"
        if use_xrobot:
            shutil.copy(reviewed, output)
        generator.generate(str(root / ".config.yaml"), str(output), use_xrobot, "")
        return root, output.read_text(encoding="utf-8")

    def test_the_generated_source_matches_the_reviewed_output(self):
        for name in ("g3507", "g3519"):
            with self.subTest(name=name):
                _root, code = self.generate(name)
                expected = (DATA / "mspm0" / f"{name}_expected_app_main.cpp").read_text(
                    encoding="utf-8"
                )
                self.assertEqual(code, expected)

    def test_the_written_settings_hold_the_defaults_the_generated_code_uses(self):
        root, code = self.generate("g3507")
        settings = yaml.safe_load((root / "User" / "libxr_config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(settings["terminal_source"], "uart0")
        self.assertEqual(settings["UART"]["uart0"]["tx_buffer_size"], 128)
        self.assertEqual(settings["I2C"]["i2c0"]["dma_enable_min_size"], 8)
        self.assertEqual(settings["SPI"]["spi1"]["tx_buffer_size"], 32)
        self.assertIn("frequency", settings["PWM"]["pwm_tima1_c0"])
        self.assertNotIn("USART", settings)
        self.assertNotIn("SYSTEM", settings)
        # 缓冲区按设置写出，重新生成只写有变化的文件之外，文本保持不变。
        # The buffers follow the settings; regenerating the same project writes the same text.
        self.assertIn("static uint8_t uart0_tx_buf[2 * 128];", code)

    def test_a_configured_frequency_and_changed_sizes_are_applied(self):
        root, _code = self.generate("g3507")
        config = root / "User" / "libxr_config.yaml"
        settings = yaml.safe_load(config.read_text(encoding="utf-8"))
        settings["PWM"]["pwm_tima1_c0"]["frequency"] = 1000
        settings["UART"]["uart1"]["tx_buffer_size"] = 64
        config.write_text(yaml.dump(settings), encoding="utf-8")
        generator.generate(
            str(root / ".config.yaml"), str(root / "User" / "app_main.cpp"), True, ""
        )
        code = (root / "User" / "app_main.cpp").read_text(encoding="utf-8")
        self.assertIn("pwm_tima1_c0.SetConfig({1000});", code)
        self.assertIn("static uint8_t uart1_tx_buf[2 * 64];", code)

    def test_the_user_code_of_an_existing_file_is_kept(self):
        root, _code = self.generate("g3507")
        output = root / "User" / "app_main.cpp"
        code = output.read_text(encoding="utf-8")
        code = code.replace(
            "/* User Code Begin 1 */",
            "/* User Code Begin 1 */\n// A user note that must survive regeneration.",
        )
        output.write_text(code, encoding="utf-8")
        generator.generate(str(root / ".config.yaml"), str(output), True, "")
        self.assertIn(
            "// A user note that must survive regeneration.",
            output.read_text(encoding="utf-8"),
        )

    def test_without_xrobot_the_entry_sleeps_and_registers_nothing(self):
        _root, code = self.generate("g3507", use_xrobot=False)
        self.assertNotIn("XR_REGISTER", code)
        self.assertNotIn("xrobot_main.hpp", code)
        self.assertIn("Thread::Sleep(UINT32_MAX);", code)
        self.assertNotIn("XROBOT_MAIN();", code)
