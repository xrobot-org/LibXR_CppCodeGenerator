"""MSPM0 的解析与生成（libxr.peripheral_analyzer_mspm0、libxr.generator_code_mspm0）：工程
YAML 的内容、生成的 app_main 源文件，以及重新生成时保留的用户代码。
Parsing and generating an MSPM0 project (libxr.peripheral_analyzer_mspm0,
libxr.generator_code_mspm0): the project YAML, the generated app_main source, and the user
code kept on regeneration.

测试固件是两个 BSP 的真实工程文件：.syscfg 加上 SysConfig 生成的 ti_msp_dl_config.h/.c（CI 里
没有 SysConfig，输出随工程一起提交），以及一个覆盖 BSP 没用到的 SysConfig 写法的 variants 工程
（SPI 用 MFCLK 分频和 RX timeout DMA 触发、外设模式的 SPI、只有目标模式的 I2C、默认实例名且
引脚只有建议值的 PWM、端口加引脚号的 GPIO、默认名 PIN_0 的引脚、收发都有 DMA 的 UART），它的
输出同样由 SysConfig 1.28.1 生成。测试把固件复制进临时工程，使生成输出的修改时间比 .syscfg 新，
解析直接复用它；测试期间不设置 SYSCONFIG_TOOL 和 MSPM0_SDK_INSTALL_DIR。
The fixtures are the real project files of the two BSPs: the .syscfg plus the
ti_msp_dl_config.h/.c that SysConfig generated (CI has no SysConfig, so the output is committed
with the project), and a variants project with the SysConfig setups the BSPs do not use (an SPI
on MFCLK with a divider and an RX timeout DMA trigger, an SPI in peripheral mode, an I2C in
target mode only, a PWM with the default instance name and only suggested pins, GPIO pins given
as port plus pin number, a pin with the default name PIN_0, a UART with DMA on both sides),
whose output SysConfig 1.28.1 generated as well. A test copies the fixtures into a temporary
project, so the generated output is newer than the .syscfg and parsing reuses it;
SYSCONFIG_TOOL and MSPM0_SDK_INSTALL_DIR are unset during the tests.
"""

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import yaml
from fixtures import DATA, TestCase, run_libxr

from libxr import generator_code_mspm0 as generator
from libxr import peripheral_analyzer_mspm0 as analyzer
from libxr.peripheral_analyzer_mspm0 import parse_project

# 测试期间清掉的 SysConfig 环境变量：设置了它们时解析会运行 SysConfig，而不是复用固件。
# The SysConfig environment variables cleared during the tests: with them set, parsing runs
# SysConfig instead of reusing the fixtures.
SYSCONFIG_VARIABLES = ("SYSCONFIG_TOOL", "MSPM0_SDK_INSTALL_DIR")


def bsp(name: str) -> str:
    """g3507 这样的短名对应的固件 .syscfg 文件名。
    The fixture .syscfg file name of a short name such as g3507.
    """
    return {
        "g3507": "mspm0g3507_minidb48.syscfg",
        "g3519": "mspm0g3519_minidb48.syscfg",
        "variants": "mspm0_variants.syscfg",
    }[name]


def project_of(name: str, edit=None, build: str = "build") -> Path:
    """把 name 的固件复制进一个临时工程：根目录的 .syscfg 加上 <build>/test/syscfg 里的
    SysConfig 输出（修改时间更新）；返回工程目录。edit 给出时用它改写 .syscfg 和两个输出文件的
    文本（edit(文件名, 文本) -> 文本）。
    Copy the fixtures of name into a temporary project: the .syscfg in the root plus the
    SysConfig output in <build>/test/syscfg (with a newer modification time); the project
    directory is returned. With edit, the text of the .syscfg and of both output files is
    rewritten through it (edit(file name, text) -> text).
    """
    root = Path(tempfile.mkdtemp(prefix="libxr-mspm0-"))
    output = root / build / "test" / "syscfg"
    output.mkdir(parents=True)
    copies = {f"{name}.syscfg": root / bsp(name)}
    for suffix in ("ti_msp_dl_config.h", "ti_msp_dl_config.c"):
        copies[f"{name}_{suffix}"] = output / suffix
    # .syscfg 先写，输出后写，使输出更新。
    # The .syscfg is written first and the output after it, so the output is newer.
    for source, target in copies.items():
        text = (DATA / "mspm0" / source).read_bytes().decode("utf-8")
        if edit is not None:
            text = edit(target.name, text)
        target.write_bytes(text.encode("utf-8"))
    for target in list(copies.values())[1:]:
        os.utime(target)
    return root


class MSPM0TestCase(TestCase):
    """清掉 SysConfig 环境变量、并在测试结束时删除临时工程的基类。
    A base class that clears the SysConfig environment variables and removes the temporary
    projects at the end of the test.
    """

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        for variable in SYSCONFIG_VARIABLES:
            os.environ.pop(variable, None)

    def project(self, name: str, edit=None, build: str = "build") -> Path:
        """project_of() 的临时工程，测试结束时删除。
        The temporary project of project_of(), removed at the end of the test.
        """
        root = project_of(name, edit, build)
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def parse(self, name: str, edit=None) -> dict:
        """解析 name 的临时工程，返回写出的工程 YAML。
        Parse the temporary project of name and return the project YAML it wrote.
        """
        root = self.project(name, edit)
        parse_project(str(root), summary=False)
        return yaml.safe_load((root / ".config.yaml").read_text(encoding="utf-8"))


class Parsing(MSPM0TestCase):
    """libxr parse：工程 YAML 由 .syscfg 和 SysConfig 输出组成。
    libxr parse: the project YAML comes from the .syscfg and the SysConfig output.
    """

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


class ParsingVariants(MSPM0TestCase):
    """libxr parse 对 BSP 没用到的 SysConfig 写法（variants 工程，审查 2026-10-07 的 H1-H6、
    L2、L4-L6、L9 和 M4）。
    libxr parse on the SysConfig setups the BSPs do not use (the variants project; H1-H6, L2,
    L4-L6, L9 and M4 of the 2026-10-07 review).
    """

    def parse_logged(self, edit=None) -> tuple[dict, list[str]]:
        """解析 variants 工程，返回工程 YAML 和警告。
        Parse the variants project and return the project YAML and the warnings.
        """
        with self.assertLogs(level="WARNING") as logs:
            data = self.parse("variants", edit)
        return data, logs.output

    def test_the_spi_clock_configuration_is_read(self):
        # H1：SysConfig 把 SPI 的结构写成 gSPI_1_clockConfig，成员按列对齐。
        # H1: SysConfig writes the SPI struct as gSPI_1_clockConfig, members aligned in columns.
        data, _logs = self.parse_logged()
        self.assertEqual(
            data["Peripherals"]["SPI"]["SPI_1"]["SysConfigClock"],
            {"clockSel": "DL_SPI_CLOCK_MFCLK", "divideRatio": "DL_SPI_CLOCK_DIVIDE_RATIO_4"},
        )

    def test_an_rx_timeout_dma_trigger_is_the_rx_channel(self):
        # L2：RX timeout 触发的通道也认作 RX 通道并记下触发，gen 再说明它不能用（审查 B2）。
        # L2: a channel on the RX timeout trigger is the RX channel too and its trigger is
        # recorded; gen then says it cannot be used (review B2).
        data, _logs = self.parse_logged()
        spi = data["Peripherals"]["SPI"]["SPI_1"]
        self.assertEqual((spi["DMA_RX"], spi["DMA_TX"]), ("DMA_CH_SPI1_RX", "DMA_CH_SPI1_TX"))
        self.assertEqual(spi["DMA_RX_TRIGGER"], "DL_SPI_DMA_INTERRUPT_RX")

        def timeout(name, text):
            return text.replace('"DL_SPI_DMA_INTERRUPT_RX"', '"DL_SPI_DMA_INTERRUPT_RX_TIMEOUT"')

        data, _logs = self.parse_logged(timeout)
        spi = data["Peripherals"]["SPI"]["SPI_1"]
        self.assertEqual(spi["DMA_RX"], "DMA_CH_SPI1_RX")
        self.assertEqual(spi["DMA_RX_TRIGGER"], "DL_SPI_DMA_INTERRUPT_RX_TIMEOUT")

    def test_a_uart_with_dma_rx_records_extend_and_its_channel(self):
        data, _logs = self.parse_logged()
        uart = data["Peripherals"]["UART"]["UART_0"]
        self.assertEqual((uart["Extend"], uart["DMA_RX_ID"]), (True, 0))
        self.assertEqual(data["DMAFullChannels"], 3)

    def test_pwm_channels_come_from_the_output_when_the_pins_are_only_suggested(self):
        # H3：引脚只有 $suggestSolution；去掉它们后通道仍来自 SysConfig 输出的 _IDX 宏。
        # H3: the pins are only $suggestSolution; without them the channels still come from
        # the _IDX macros of the SysConfig output.
        data, _logs = self.parse_logged()
        pwm = data["Peripherals"]["PWM"]["PWM_0"]
        self.assertEqual(pwm["Channels"], [0, 1])
        self.assertEqual(pwm["Pins"], {"ccp0Pin": "PA12", "ccp1Pin": "PA13"})

        def drop_pins(name, text):
            if name.endswith(".syscfg"):
                return "\n".join(line for line in text.splitlines() if "ccp" not in line)
            return text

        data, _logs = self.parse_logged(drop_pins)
        self.assertEqual(data["Peripherals"]["PWM"]["PWM_0"]["Channels"], [0, 1])

    def test_a_pwm_without_channels_is_a_warning(self):
        def drop_channels(name, text):
            return "\n".join(
                line for line in text.splitlines() if "ccp" not in line and "_C0_" not in line
            ).replace("_C1_IDX", "_C1_INDEX")

        data, logs = self.parse_logged(drop_channels)
        self.assertEqual(data["Peripherals"]["PWM"]["PWM_0"]["Channels"], [])
        self.assertTrue(any("the PWM PWM_0 has no channel" in line for line in logs))

    def test_gpio_pins_given_as_port_and_pin_number_are_kept(self):
        # H5：TI 例程的写法 port = "PORTA" 加 assignedPin = "26"。
        # H5: the form of TI's examples, port = "PORTA" plus assignedPin = "26".
        data, _logs = self.parse_logged()
        self.assertEqual(data["GPIO"]["USER_LED_1"]["Pin"], "PA26")
        self.assertEqual(data["GPIO"]["USER_LED_2"]["Port"], "GPIO_LEDS_PORT")

    def test_a_skipped_gpio_pin_is_a_warning(self):
        def drop_macro(name, text):
            return text.replace("GPIO_LEDS_USER_LED_2_IOMUX", "GPIO_LEDS_USER_LED_2_MUX")

        data, logs = self.parse_logged(drop_macro)
        self.assertNotIn("USER_LED_2", data["GPIO"])
        self.assertTrue(any("the GPIO USER_LED_2 is skipped" in line for line in logs))

    def test_a_default_pin_name_falls_back_to_the_group_and_the_pin(self):
        # M4：PIN_0 去掉前缀是 0，不是标识符。
        # M4: PIN_0 without its prefix is 0, not an identifier.
        data, logs = self.parse_logged()
        self.assertEqual(data["GPIO"]["gpio_btn_pin_0"]["PinMacro"], "GPIO_BTN_PIN_0_PIN")
        self.assertTrue(
            any("the object is named gpio_btn_pin_0. Give the pin a name" in line for line in logs)
        )

    def test_a_pin_named_like_a_device_macro_is_an_error(self):
        # M4：PIN_SPI1 会成为 SPI1，与器件头文件的外设宏同名。
        # M4: PIN_SPI1 would become SPI1, the peripheral macro of the device header.
        def rename(name, text):
            return text.replace("PIN_0", "PIN_SPI1")

        with self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit):
            self.parse("variants", rename)
        self.assertIn("the GPIO object name SPI1 (pin PIN_SPI1 of GPIO_BTN)", logs.output[-1])

    def test_an_interrupt_without_a_polarity_follows_the_sysconfig_default(self):
        # L4：SysConfig 的 polarity 默认是 DISABLE。
        # L4: the polarity of SysConfig defaults to DISABLE.
        def enable(name, text):
            if name.endswith(".syscfg"):
                return text.replace(
                    'GPIO2.associatedPins[0].direction        = "INPUT";',
                    'GPIO2.associatedPins[0].direction        = "INPUT";\n'
                    "GPIO2.associatedPins[0].interruptEn      = true;",
                )
            return text

        data, _logs = self.parse_logged(enable)
        self.assertEqual(data["GPIO"]["gpio_btn_pin_0"]["Interrupt"], "DISABLE")

    def test_target_i2c_and_peripheral_spi_are_listed_but_not_generated(self):
        # H6
        data, logs = self.parse_logged()
        peripherals = data["Peripherals"]
        self.assertNotIn("I2C_TARGET", peripherals["I2C"])
        self.assertNotIn("SPI_PERIPH", peripherals["SPI"])
        self.assertEqual(
            peripherals["Other"]["I2C_TARGET"], {"Module": "I2C", "Peripheral": "I2C1"}
        )
        self.assertEqual(
            peripherals["Other"]["SPI_PERIPH"], {"Module": "SPI", "Peripheral": "SPI0"}
        )
        self.assertTrue(any("I2C_TARGET is not in controller mode" in line for line in logs))
        self.assertTrue(any("SPI_PERIPH is in PERIPHERAL mode" in line for line in logs))

    def test_a_dma_channel_without_a_name_in_the_syscfg_comes_from_the_output(self):
        # L6：.syscfg 没写通道名时，SysConfig 输出里的 <通道>_CHAN_ID 和触发宏给出它。
        # L6: without a channel name in the .syscfg, the <channel>_CHAN_ID and the trigger
        # macros of the SysConfig output give it.
        def unnamed(name, text):
            if name.endswith(".syscfg"):
                return text.replace("UART1.DMA_CHANNEL_TX.$name", "// UART1.DMA_CHANNEL_TX.$name")
            return text

        data, _logs = self.parse_logged(unnamed)
        self.assertEqual(data["Peripherals"]["UART"]["UART_0"]["DMA_TX"], "DMA_CH_UART0_TX")

    def test_an_unknown_device_is_a_formatted_error(self):
        # L5：没有调用栈，只有一条错误。
        # L5: no traceback, one error.
        def unknown(name, text):
            return text.replace('"MSPM0G3507"', '"MSPM0Z9999"')

        with self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit):
            self.parse("variants", unknown)
        self.assertEqual(
            logs.output,
            ["ERROR:root:mspm0_variants.syscfg: Unknown MSPM0 model: MSPM0Z9999"],
        )

    def test_the_summary_lines_are_translated(self):
        # L9
        root = self.project("variants")
        with (
            mock.patch.dict(os.environ, XR_LANG="zh"),
            contextlib.redirect_stdout(io.StringIO()) as out,
            self.assertLogs(level="WARNING"),
        ):
            parse_project(str(root))
        self.assertIn("  USER_LED_1：PA26，OUTPUT", out.getvalue().splitlines())


class SysConfigRun(MSPM0TestCase):
    """SysConfig 的运行和复用（M3、M7）。
    Running and reusing SysConfig (M3, M7).
    """

    def environment(self, root: Path) -> None:
        """为 root 设置一个能用的 SysConfig 环境：存在的工具文件和带 product.json 的 SDK。
        Set a usable SysConfig environment for root: a tool file that exists and an SDK with
        product.json.
        """
        tool = root / "sysconfig_cli.bat"
        tool.write_text("", encoding="utf-8")
        sdk = root / "sdk"
        (sdk / ".metadata").mkdir(parents=True)
        (sdk / ".metadata" / "product.json").write_text("{}", encoding="utf-8")
        os.environ["SYSCONFIG_TOOL"] = str(tool)
        os.environ["MSPM0_SDK_INSTALL_DIR"] = str(sdk)

    def test_a_configured_sysconfig_runs_even_when_a_build_output_exists(self):
        root = self.project("variants")
        self.environment(root)

        def sysconfig(command, **_options):
            output = Path(command[command.index("--output") + 1])
            for suffix in ("ti_msp_dl_config.h", "ti_msp_dl_config.c"):
                shutil.copy(DATA / "mspm0" / f"variants_{suffix}", output / suffix)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch("subprocess.run", side_effect=sysconfig) as run,
            self.assertLogs(level="INFO") as logs,
        ):
            parse_project(str(root), summary=False)
        self.assertEqual(run.call_count, 1)
        self.assertIn("INFO:root:Running SysConfig on mspm0_variants.syscfg", logs.output)
        self.assertFalse(any("Reusing" in line for line in logs.output))

    def test_a_sysconfig_that_cannot_start_is_a_formatted_error(self):
        root = self.project("variants")
        self.environment(root)
        with (
            mock.patch("subprocess.run", side_effect=OSError("not a program")),
            self.assertLogs(level="ERROR") as logs,
            self.assertRaises(SystemExit),
        ):
            parse_project(str(root), summary=False)
        self.assertTrue(logs.output[-1].startswith("ERROR:root:Cannot run SysConfig ("))
        self.assertTrue(logs.output[-1].endswith("): not a program"))

    def test_a_sysconfig_timeout_is_a_formatted_error(self):
        root = self.project("variants")
        self.environment(root)
        with (
            mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 300)),
            self.assertLogs(level="ERROR") as logs,
            self.assertRaises(SystemExit),
        ):
            parse_project(str(root), summary=False)
        self.assertIn("SysConfig did not finish on", logs.output[-1])

    def test_a_wrong_path_is_named_instead_of_not_set(self):
        root = self.project("variants")
        shutil.rmtree(root / "build")
        os.environ["SYSCONFIG_TOOL"] = str(root / "missing_cli.bat")
        with self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit):
            parse_project(str(root), summary=False)
        self.assertIn(
            f"SYSCONFIG_TOOL={root / 'missing_cli.bat'} is not a file (it names "
            "sysconfig_cli.bat or sysconfig_cli.sh); MSPM0_SDK_INSTALL_DIR is not set",
            logs.output[-1],
        )

    def test_a_wrong_path_is_reported_when_the_build_output_is_reused(self):
        root = self.project("variants")
        os.environ["SYSCONFIG_TOOL"] = str(root / "missing_cli.bat")
        with self.assertLogs(level="WARNING") as logs:
            parse_project(str(root), summary=False)
        self.assertTrue(
            logs.output[0].startswith(
                f"WARNING:root:SysConfig cannot run: SYSCONFIG_TOOL={root / 'missing_cli.bat'}"
            )
        )

    def test_a_cmake_build_tree_is_reused(self):
        root = self.project("variants", build="cmake-build-debug")
        with self.assertLogs(level="INFO") as logs:
            parse_project(str(root), summary=False)
        self.assertIn(
            f"INFO:root:Reusing the SysConfig output in {Path('cmake-build-debug') / 'test'}",
            logs.output,
        )


class Generation(MSPM0TestCase):
    """libxr gen：生成的 app_main 源文件与设置，以及重新生成时保留的用户代码。
    libxr gen: the generated app_main source and settings, and the user code kept on
    regeneration.
    """

    def setUp(self):
        super().setUp()
        generator.initialize_registry(False)
        generator.reset_settings()

    def generate(self, name: str, use_xrobot: bool = True, edit=None) -> tuple[Path, str]:
        """解析 name 的临时工程并生成 app_main 源文件；BSP 工程的 User/app_main.cpp 先放上审查
        过的输出（重新生成的起点）。返回工程目录和重新生成的文本。
        Parse the temporary project of name and generate the app_main source; for a BSP
        project the reviewed output is placed at User/app_main.cpp first, as the starting point
        of the regeneration. The project directory and the regenerated text are returned.
        """
        root = self.project(name, edit)
        with contextlib.ExitStack() as stack:
            if name == "variants":
                stack.enter_context(self.assertLogs(level="WARNING"))
            parse_project(str(root), summary=False)
            output = root / "User" / "app_main.cpp"
            output.parent.mkdir()
            reviewed = DATA / "mspm0" / f"{name}_expected_app_main.cpp"
            if use_xrobot and name != "variants":
                shutil.copy(reviewed, output)
            self.regenerate(root, use_xrobot)
        return root, output.read_text(encoding="utf-8")

    def regenerate(self, root: Path, use_xrobot: bool = True) -> str:
        """再生成一次 root 的 app_main 源文件并返回它的文本。
        Generate the app_main source of root once more and return its text.
        """
        output = root / "User" / "app_main.cpp"
        generator.generate(str(root / ".config.yaml"), str(output), use_xrobot, "")
        return output.read_text(encoding="utf-8")

    def settings(self, root: Path, change=None) -> dict:
        """root 的 libxr_config.yaml；给出 change 时用它修改设置并写回。
        The libxr_config.yaml of root; with change, the settings are modified through it and
        written back.
        """
        config = root / "User" / "libxr_config.yaml"
        settings = yaml.safe_load(config.read_text(encoding="utf-8"))
        if change is not None:
            change(settings)
            config.write_text(yaml.dump(settings), encoding="utf-8")
        return settings

    def project_yaml(self, root: Path, change) -> None:
        """用 change 修改 root 的工程 YAML 并写回。
        Modify the project YAML of root through change and write it back.
        """
        path = root / ".config.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")

    def failure(self, root: Path) -> str:
        """重新生成 root 并断言失败，返回最后一条错误。
        Regenerate root, assert that it fails and return the last error.
        """
        with self.assertLogs(level="WARNING") as logs, self.assertRaises(SystemExit):
            self.regenerate(root)
        self.assertTrue(logs.output[-1].startswith("ERROR:"))
        return logs.output[-1]

    def test_the_generated_source_matches_the_reviewed_output(self):
        for name in ("g3507", "g3519", "variants"):
            with self.subTest(name=name):
                generator.initialize_registry(False)
                generator.reset_settings()
                _root, code = self.generate(name)
                expected = (DATA / "mspm0" / f"{name}_expected_app_main.cpp").read_text(
                    encoding="utf-8"
                )
                self.assertEqual(code, expected)

    def test_the_written_settings_hold_the_defaults_the_generated_code_uses(self):
        root, code = self.generate("g3507")
        settings = self.settings(root)
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

        def change(settings):
            settings["PWM"]["pwm_tima1_c0"]["frequency"] = 1000
            settings["UART"]["uart1"]["tx_buffer_size"] = 64

        self.settings(root, change)
        code = self.regenerate(root)
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
        self.assertIn("// A user note that must survive regeneration.", self.regenerate(root))

    def test_without_xrobot_the_entry_sleeps_and_registers_nothing(self):
        _root, code = self.generate("g3507", use_xrobot=False)
        self.assertNotIn("XR_REGISTER", code)
        self.assertNotIn("xrobot_main.hpp", code)
        self.assertIn("Thread::Sleep(UINT32_MAX);", code)
        self.assertNotIn("XROBOT_MAIN();", code)

    def test_the_spi_clock_constant_is_defined_whenever_it_is_used(self):
        # H2：MFCLK 不分频和 LFCLK 时也定义 SPI_1_CLK_FREQ。
        # H2: SPI_1_CLK_FREQ is defined for MFCLK without a divider and for LFCLK as well.
        root, code = self.generate("variants")
        self.assertIn("static constexpr uint32_t SPI_1_CLK_FREQ = MFCLK_FREQ / 4;", code)
        for sel, div, line in (
            ("MFCLK", 1, "SPI_1_CLK_FREQ = MFCLK_FREQ;"),
            ("LFCLK", 2, "SPI_1_CLK_FREQ = LFCLK_FREQ / 2;"),
            ("BUSCLK", 2, "SPI_1_CLK_FREQ = PD1_BUSCLK_FREQ / 2;"),
        ):
            with self.subTest(sel=sel, div=div):

                def change(data, sel=sel, div=div):
                    data["Peripherals"]["SPI"]["SPI_1"]["SysConfigClock"] = {
                        "clockSel": f"DL_SPI_CLOCK_{sel}",
                        "divideRatio": f"DL_SPI_CLOCK_DIVIDE_RATIO_{div}",
                    }

                self.project_yaml(root, change)
                code = self.regenerate(root)
                self.assertIn(f"static constexpr uint32_t {line}", code)
                self.assertIn("MSPM0_SPI_INIT(SPI_1, SPI_1_CLK_FREQ,", code)
                source = line.split(" = ")[1].split(" ")[0].rstrip(";")
                self.assertIn(f"static constexpr uint32_t {source} = ", code)

    def test_an_unsupported_spi_clock_source_is_an_error(self):
        root, _code = self.generate("variants")

        def change(data):
            data["Peripherals"]["SPI"]["SPI_1"]["SysConfigClock"]["clockSel"] = "DL_SPI_CLOCK_X"

        self.project_yaml(root, change)
        self.assertIn(
            "SPI_1: the SysConfig clock source DL_SPI_CLOCK_X is not supported", self.failure(root)
        )

    def test_a_missing_power_domain_is_an_error_not_an_undefined_macro(self):
        # L3：SysConfig 不为 I2C 定义 I2C_0_INST_FREQUENCY。
        # L3: SysConfig defines no I2C_0_INST_FREQUENCY for an I2C.
        root, _code = self.generate("variants")
        self.project_yaml(root, lambda data: data["Peripherals"]["I2C"]["I2C_0"].pop("PowerDomain"))
        self.assertIn("I2C_0: the project YAML has no the power domain of I2C0", self.failure(root))

    def test_a_pwm_object_is_named_after_its_timer(self):
        # H4：实例名 PWM_0、定时器 TIMG0 -> pwm_timg0_c0，与 libxr pins -d 一致。
        # H4: instance PWM_0 on timer TIMG0 -> pwm_timg0_c0, as libxr pins -d names it.
        root, code = self.generate("variants")
        self.assertIn("static MSPM0PWM pwm_timg0_c0(MSPM0_PWM_CH(PWM_0, 0));", code)
        self.assertIn("pwm_timg0_c1", self.settings(root)["PWM"])

    def test_terminal_source_none_or_empty_stays_off(self):
        # M1：与 STM32 一样，空值不生成终端；第一次生成时才选第一个 UART。
        # M1: as on STM32 an empty value generates no terminal; only the first generation
        # picks the first UART.
        for value in ("none", ""):
            with self.subTest(value=value):
                generator.initialize_registry(False)
                generator.reset_settings()
                root, code = self.generate("variants")
                self.assertIn("// Terminal on uart0", code)
                self.settings(
                    root, lambda settings, value=value: settings.update(terminal_source=value)
                )
                for _ in range(2):
                    code = self.regenerate(root)
                    self.assertNotIn("Terminal on", code)
                    self.assertEqual(self.settings(root)["terminal_source"], value)

    def test_settings_are_validated(self):
        # M2
        root, _code = self.generate("variants")
        for section, name, key, value, message in (
            ("UART", "uart0", "tx_buffer_size", 30, "is not a multiple of 4 up to 65532"),
            ("UART", "uart0", "tx_buffer_size", 65536, "is not a multiple of 4 up to 65532"),
            ("UART", "uart0", "tx_queue_size", 0, "is not a positive integer"),
            ("SPI", "spi1", "dma_enable_min_size", "abc", "is not a non-negative integer"),
            ("I2C", "i2c0", "buffer_size", 1.5, "is not a positive integer"),
            ("PWM", "pwm_timg0_c0", "frequency", "fast", "is not a positive integer"),
            ("Terminal", None, "max_line_size", -1, "is not a positive integer"),
        ):
            with self.subTest(key=key, value=value):

                def change(settings, section=section, name=name, key=key, value=value):
                    (settings[section][name] if name else settings[section])[key] = value

                previous = self.settings(root)
                self.settings(root, change)
                error = self.failure(root)
                self.assertIn(f"{key} {value!r} {message}", error)
                (root / "User" / "libxr_config.yaml").write_text(
                    yaml.dump(previous), encoding="utf-8"
                )

    def test_a_null_setting_takes_the_default(self):
        # M2：null 不再写成 2 * None。
        # M2: a null no longer becomes 2 * None.
        root, _code = self.generate("variants")
        self.settings(root, lambda settings: settings["UART"]["uart0"].update(tx_buffer_size=None))
        code = self.regenerate(root)
        self.assertIn("static uint8_t uart0_tx_buf[2 * 128];", code)
        self.assertNotIn(".SetConfig", code)

    def test_a_settings_key_in_another_case_is_used_and_kept(self):
        root, _code = self.generate("variants")

        def change(settings):
            settings["UART"] = {"UART0": settings["UART"].pop("uart0")}
            settings["UART"]["UART0"]["tx_buffer_size"] = 64

        self.settings(root, change)
        code = self.regenerate(root)
        self.assertIn("static uint8_t uart0_tx_buf[2 * 64];", code)
        self.assertEqual(list(self.settings(root)["UART"]), ["UART0"])

    def test_a_uart_extend_with_dma_rx_receives_by_dma(self):
        # 用户 2026-10-07 决定：UART0（G3507 的 UART Extend）配了 FULL-DMA 通道上的 DMA RX，
        # 用 MSPM0_UART_EXTEND_INIT。
        # User decision 2026-10-07: UART0 (the UART Extend of a G3507) has DMA RX on a
        # FULL-DMA channel, so it takes MSPM0_UART_EXTEND_INIT.
        root, code = self.generate("variants")
        self.assertIn("MSPM0_UART_EXTEND_INIT(UART_0, DMA_CH_UART0_TX, DMA_CH_UART0_RX,", code)
        self.assertNotIn("MSPM0_UART_MAIN_INIT(", code)
        for line in (
            "#define DMA_CH_UART0_RX_LIBXR_UART_IRQN UART_0_INST_INT_IRQN",
            "#define DMA_CH_UART0_RX_LIBXR_UART_RX 1",
            "#define DMA_CH_UART0_RX_LIBXR_FULL_CHANNEL 1",
            "#define DMA_CH_UART0_RX_LIBXR_HALF_INTERRUPT 1",
            "#define UART_0_LIBXR_EXTEND_CAPABLE 1",
            "alignas(4) static uint8_t uart0_rx_dma_buf[128];",
        ):
            self.assertIn(line, code)
        self.assertEqual(self.settings(root)["UART"]["uart0"]["rx_dma_buffer_size"], 128)

    def test_a_gpio_named_like_a_generated_object_is_an_error(self):
        # 审查 C1：timebase 是生成代码里的对象，GPIO 不能再叫它。
        # Review C1: timebase is an object of the generated code, so a GPIO cannot take it.
        root, _code = self.generate("variants")
        self.project_yaml(
            root, lambda data: data["GPIO"].update(timebase=data["GPIO"].pop("USER_LED_1"))
        )
        self.assertIn(
            "The generated code uses these GPIO names for something else: timebase",
            self.failure(root),
        )

    def test_power_manager_is_generated_always_and_registered_only_with_xrobot(self):
        # power_manager 对象总是生成，登记只在使用 XRobot 时；GPIO 不能再叫 power_manager。
        # The power_manager object is always generated; it is registered only with XRobot, and
        # a GPIO cannot take the name power_manager.
        root, code = self.generate("g3507", use_xrobot=False)
        self.assertIn('#include "mspm0_power.hpp"', code)
        self.assertIn("static MSPM0PowerManager power_manager;", code)
        self.assertNotIn("XR_REGISTER", code)
        code = self.regenerate(root)
        self.assertIn("XR_REGISTER(power_manager, LibXR::PowerManager);", code)
        self.project_yaml(
            root, lambda data: data["GPIO"].update(power_manager=data["GPIO"].pop("LED1"))
        )
        self.assertIn(
            "GPIO name power_manager is already used by the PowerManager object",
            self.failure(root),
        )

    def test_dma_rx_on_a_uart_main_is_a_warning(self):
        root, _code = self.generate("variants")
        self.project_yaml(
            root, lambda data: data["Peripherals"]["UART"]["UART_0"].update(Extend=False)
        )
        with self.assertLogs(level="WARNING") as logs:
            code = self.regenerate(root)
        self.assertIn(
            "WARNING:root:UART_0: the DMA RX channel DMA_CH_UART0_RX is not used; UART0 is a "
            "UART Main instance, which receives on byte interrupts (MSPM0_UART_MAIN_INIT); DMA "
            "reception needs a UART Extend instance",
            logs.output,
        )
        self.assertIn("MSPM0_UART_MAIN_INIT(UART_0, DMA_CH_UART0_TX,", code)
        self.assertNotIn("rx_dma_buf", code)

    def test_dma_rx_on_a_channel_that_is_not_full_is_an_error(self):
        root, _code = self.generate("variants")
        self.project_yaml(
            root, lambda data: data["Peripherals"]["UART"]["UART_0"].update(DMA_RX_ID=4)
        )
        self.assertIn(
            "UART_0: the DMA RX channel DMA_CH_UART0_RX is channel 4, but "
            "MSPM0_UART_EXTEND_INIT receives on a FULL-DMA channel (0 to 2)",
            self.failure(root),
        )

    def test_the_receive_ring_must_be_even(self):
        root, _code = self.generate("variants")
        self.settings(
            root, lambda settings: settings["UART"]["uart0"].update(rx_dma_buffer_size=63)
        )
        self.assertIn("UART.uart0.rx_dma_buffer_size", self.failure(root))

    def test_an_spi_rx_timeout_trigger_is_an_error(self):
        # 审查 B2：驱动按 SysConfig 的触发搬运每个字节，RX timeout 触发的传输等不到结束。
        # Review B2: the driver moves every byte on SysConfig's trigger, and a transfer on the
        # RX timeout trigger never completes.
        root, _code = self.generate("variants")
        self.project_yaml(
            root,
            lambda data: data["Peripherals"]["SPI"]["SPI_1"].update(
                DMA_RX_TRIGGER="DL_SPI_DMA_INTERRUPT_RX_TIMEOUT"
            ),
        )
        self.assertIn(
            "SPI_1: the DMA RX channel DMA_CH_SPI1_RX is triggered by RX timeout",
            self.failure(root),
        )

    def test_a_uart_without_dma_tx_names_the_sysconfig_option(self):
        # L6
        root, _code = self.generate("variants")
        self.project_yaml(root, lambda data: data["Peripherals"]["UART"]["UART_0"].pop("DMA_TX"))
        self.assertIn(
            "UART_0: no DMA TX channel in the SysConfig project (UART > DMA Configuration: "
            "Enable DMA TX",
            self.failure(root),
        )

    def test_different_frequencies_of_one_timer_are_a_warning(self):
        # L7
        root, _code = self.generate("variants")

        def change(settings):
            settings["PWM"]["pwm_timg0_c0"]["frequency"] = 1000
            settings["PWM"]["pwm_timg0_c1"]["frequency"] = 2000

        self.settings(root, change)
        with self.assertLogs(level="WARNING") as logs:
            self.regenerate(root)
        self.assertIn(
            "WARNING:root:libxr_config.yaml: the PWM channels of PWM_0 share one period but set "
            "different frequencies (pwm_timg0_c0 1000, pwm_timg0_c1 2000); the last SetConfig "
            "(pwm_timg0_c1) wins",
            logs.output,
        )

    def test_a_hand_written_file_lists_the_lines_that_are_replaced(self):
        # M6：已有文件没有生成说明时警告，照常生成。
        # M6: an existing file without the generated-file notice is warned about and generated
        # as usual.
        root, _code = self.generate("g3507")
        output = root / "User" / "app_main.cpp"
        output.write_text(
            '#include "app_main.h"\n'
            "static int hand_written = 1;\n"
            "/* User Code Begin 1 */\nstatic int kept = 2;\n/* User Code End 1 */\n"
            'extern "C" void app_main(void)\n{\n'
            "  /* User Code Begin 2 */\n  /* User Code End 2 */\n"
            "  KEY1.EnableInterrupt();\n"
            "  /* User Code Begin 3 */\n  /* User Code End 3 */\n"
            "  XROBOT_MAIN();\n}\n",
            encoding="utf-8",
        )
        with self.assertLogs(level="WARNING") as logs:
            code = self.regenerate(root)
        warning = next(line for line in logs.output if "was not generated by libxr gen" in line)
        self.assertIn("these 2 line(s) of it are replaced", warning)
        self.assertTrue(
            warning.endswith("\n  static int hand_written = 1;\n  KEY1.EnableInterrupt();")
        )
        self.assertIn("static int kept = 2;", code)
        # 生成过的文件不再警告。
        # A generated file is not warned about any more.
        with self.assertNoLogs(level="WARNING"):
            self.regenerate(root)


class Setup(MSPM0TestCase):
    """libxr mspm0 setup 和 libxr hpm setup（M5、HPM 17）：XRobot 的选择、.gitignore 和下一步。
    libxr mspm0 setup and libxr hpm setup (M5, HPM 17): the XRobot choice, .gitignore and the
    next steps.
    """

    def setUp(self):
        super().setUp()
        generator.initialize_registry(False)
        generator.reset_settings()

    def setup(self, root: Path, *options) -> tuple[int, str]:
        """在 root 上运行 libxr mspm0 setup，返回退出码和生成的 app_main 源文件。
        Run libxr mspm0 setup on root and return the exit code and the generated app_main.
        """
        with self.assertLogs(level="INFO") as logs:
            code, _out, _err = run_libxr("mspm0", "setup", "-d", str(root), *options)
        self.logs = logs.output
        return code, (root / "User" / "app_main.cpp").read_text(encoding="utf-8")

    def test_a_new_project_uses_no_xrobot_and_an_existing_one_keeps_its_choice(self):
        root = self.project("variants")
        code, app_main = self.setup(root)
        self.assertEqual(code, 0)
        self.assertNotIn("xrobot_main.hpp", app_main)
        self.assertIn(".config.yaml", (root / ".gitignore").read_text(encoding="utf-8"))
        _code, app_main = self.setup(root, "--xrobot")
        self.assertIn('#include "xrobot_main.hpp"', app_main)
        _code, app_main = self.setup(root)
        self.assertIn('#include "xrobot_main.hpp"', app_main)
        self.assertIn(
            f"INFO:root:{root / 'User' / 'app_main.cpp'} uses XRobot; generating with --xrobot "
            "(--no-xrobot turns it off).",
            self.logs,
        )
        # HPM 17：与 stm32 setup 一样，还没有 Modules/modules.yaml 时给出 XRobot 的设置步骤，
        # 其中 xrobot setup 生成 User/xrobot_main.hpp。
        # HPM 17: as stm32 setup does, the XRobot setup steps are given while there is no
        # Modules/modules.yaml; xrobot setup among them generates User/xrobot_main.hpp.
        self.assertIn(
            "INFO:root:Next: Modules/modules.yaml does not exist yet; set up XRobot in this order:",
            self.logs,
        )
        # 有了 Modules/modules.yaml 之后提醒运行 xrobot gen：setup 不更新 xrobot_main.hpp。
        # Once Modules/modules.yaml exists, the reminder to run xrobot gen: setup does not
        # update xrobot_main.hpp.
        (root / "Modules").mkdir(exist_ok=True)
        (root / "Modules" / "modules.yaml").write_text("modules: []\n", encoding="utf-8")
        self.setup(root, "--xrobot")
        self.assertIn(
            "INFO:root:Next: run `xrobot gen` to bring User/xrobot_main.hpp up to date; libxr "
            "setup does not update it",
            self.logs,
        )
        _code, app_main = self.setup(root, "--no-xrobot")
        self.assertNotIn("xrobot_main.hpp", app_main)
        self.assertFalse(any("xrobot gen" in line for line in self.logs))
        _code, app_main = self.setup(root)
        self.assertNotIn("xrobot_main.hpp", app_main)

    def test_an_existing_gitignore_is_kept(self):
        root = self.project("variants")
        (root / ".gitignore").write_text("mine\n", encoding="utf-8")
        self.setup(root)
        self.assertEqual((root / ".gitignore").read_text(encoding="utf-8"), "mine\n")

    def test_hpm_setup_keeps_the_choice_of_the_output_file_as_well(self):
        root = Path(tempfile.mkdtemp(prefix="libxr-hpm-setup-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "User").mkdir()
        (root / "User" / "app_main.cpp").write_text("int x;\n", encoding="utf-8")
        with (
            mock.patch("libxr.peripheral_analyzer_hpm.parse_project"),
            mock.patch("libxr.generator_code_hpm.generate") as generate,
            self.assertLogs(level="INFO"),
        ):
            self.assertEqual(run_libxr("hpm", "setup", "-d", str(root))[0], 0)
        self.assertIs(generate.call_args.args[2], False)
        self.assertTrue((root / ".gitignore").is_file())


class Analyzer(TestCase):
    """解析器的小函数。
    Small functions of the parser.
    """

    def test_the_clock_configurations_of_every_kind_are_read(self):
        source = (DATA / "mspm0" / "variants_ti_msp_dl_config.c").read_text(encoding="utf-8")
        clocks = analyzer._clock_configs(source)
        self.assertEqual(
            sorted(clocks), ["I2C_0", "I2C_TARGET", "PWM_0", "SPI_1", "SPI_PERIPH", "UART_0"]
        )
        self.assertEqual(clocks["UART_0"]["clockSel"], "DL_UART_MAIN_CLOCK_BUSCLK")
