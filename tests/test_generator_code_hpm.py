"""HPM 的解析与生成（libxr.peripheral_analyzer_hpm、libxr.generator_code_hpm）：工程 YAML 的
内容、生成的 app_main 源文件、重新生成时保留的用户代码，以及解析的两条提示（main.c 不调用
pinmux 函数、两个活动函数选中同一个 pad）。
Parsing and generating an HPM project (libxr.peripheral_analyzer_hpm,
libxr.generator_code_hpm): the project YAML, the generated app_main source, the user code kept
on regeneration, and the two parse notices (a main.c that calls no pinmux function, and two
active functions that select the same pad).

测试固件是两个 HPM BSP 的真实工程文件：app.yaml、根目录 main.c 和 boards/ 下 Pinmux Tool 导出的
.hpmpc。测试把固件复制进临时工程，在那里解析和生成。
The fixtures are the real project files of the two HPM BSPs: app.yaml, the root main.c and the
.hpmpc under boards/ that the Pinmux Tool exported. A test copies the fixtures into a temporary
project and parses and generates there.
"""

import json
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml
from fixtures import DATA, TestCase

from libxr import generator_code_hpm as generator
from libxr.peripheral_analyzer_hpm import parse_project


def project_of(name: str) -> Path:
    """把 name 的工程文件复制进一个临时工程：根目录的 app.yaml 和 main.c，boards/name/ 下的
    .hpmpc。返回工程目录，测试结束时删除。
    Copy the project files of name into a temporary project: the app.yaml and main.c in the
    root and the .hpmpc under boards/name/. The directory is returned and removed at the end
    of the test.
    """
    root = Path(tempfile.mkdtemp(prefix="libxr-hpm-"))
    shutil.copy(DATA / "hpm" / f"{name}_app.yaml", root / "app.yaml")
    shutil.copy(DATA / "hpm" / f"{name}_main.c", root / "main.c")
    board = root / "boards" / name
    board.mkdir(parents=True)
    shutil.copy(DATA / "hpm" / f"{name}.hpmpc", board / "tool_config.hpmpc")
    return root


class Parsing(TestCase):
    """libxr parse：工程 YAML 由 .hpmpc 和根目录 main.c 的调用组成。
    libxr parse: the project YAML comes from the .hpmpc and the calls of the root main.c.
    """

    def parse(self, name: str) -> dict:
        """解析 name 的临时工程，返回写出的工程 YAML。
        Parse the temporary project of name and return the project YAML it wrote.
        """
        root = project_of(name)
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        parse_project(str(root), summary=False)
        return yaml.safe_load((root / ".config.yaml").read_text(encoding="utf-8"))

    def test_the_soc_the_package_and_the_active_functions_come_from_the_project(self):
        data = self.parse("hpm5301evklite")
        self.assertEqual(data["Platform"], "hpm")
        self.assertEqual(data["Hpmpc"], "boards/hpm5301evklite/tool_config.hpmpc")
        self.assertEqual(data["Mcu"], {"Family": "HPM", "Type": "HPM5301"})
        self.assertEqual(data["Package"], "QFN48")
        self.assertEqual(data["MainFunctions"], ["init_bsp_pins"])

    def test_gpio_pins_carry_the_pad_the_direction_and_the_pull(self):
        gpio = self.parse("hpm5301evklite")["GPIO"]
        self.assertEqual(
            gpio["pa10"],
            {"Pad": "PA10", "Port": "A", "Line": 10, "PadCtls": {}, "Direction": "OUTPUT"},
        )
        self.assertEqual(gpio["pa3"]["Direction"], "INPUT")
        # KEY 引脚下拉：padCtls 使能下拉（PE=1，PS=0）。
        # The KEY pin pulls down: padCtls enables the pull-down (PE=1, PS=0).
        self.assertEqual(gpio["pa3"]["PadCtls"]["PE"], "1")
        self.assertEqual(gpio["pa3"]["PadCtls"]["PS"], "0")

    def test_peripherals_carry_the_i2c_the_pwm_channels_and_the_other_modules(self):
        peripherals = self.parse("hpm5301evklite")["Peripherals"]
        self.assertEqual(peripherals["I2C"]["I2C3"]["Pins"], {"SCL": "PB13", "SDA": "PB12"})
        # 5301 没有 PWM 外设（引脚数据里没有 PWM 信号）：GPTMR 的比较器生成 PWM 对象。
        # A HPM5301 has no PWM peripheral (no PWM signal in its pin data): the GPTMR
        # comparators generate PWM objects.
        self.assertEqual(peripherals["PWM"]["GPTMR0"]["Channels"], [{"Index": 1, "Pad": "PB08"}])
        # UART 和 SPI 被认出但驱动还压着，只作展示。
        # UART and SPI are recognized but their drivers are held back; display only.
        self.assertEqual(peripherals["Other"]["UART0"]["Module"], "UART")
        self.assertEqual(peripherals["Other"]["SPI1"]["Pins"]["SCLK"], "PA27")
        self.assertNotIn("GPIO", peripherals)

    def test_the_jtag_pins_of_a_conditional_function_stay_out(self):
        # O2：rmcs 的 init_bsp_jtag_shared_pins 在 #if 里，PA07（KEY）不出现。5361 有 PWM
        # 外设（引脚数据里有 PWM 信号），蜂鸣器和 IMU 加热器的 GPTMR 比较器归入 Other 只作
        # 展示。
        # O2: the init_bsp_jtag_shared_pins of rmcs sits in #if, so PA07 (the KEY) stays
        # out. A HPM5361 has a PWM peripheral (its pin data carries PWM signals), so the
        # GPTMR comparators of the buzzer and the IMU heater land in Other for display only.
        data = self.parse("rmcs_slave_lite")
        self.assertEqual(data["Mcu"], {"Family": "HPM", "Type": "HPM5361"})
        self.assertEqual(data["MainFunctions"], ["init_bsp_pins"])
        self.assertNotIn("pa7", data["GPIO"])
        self.assertEqual(data["GPIO"]["pb11"]["Direction"], "OUTPUT")
        self.assertNotIn("PWM", data["Peripherals"])
        self.assertEqual(data["Peripherals"]["Other"]["GPTMR2"]["Pins"]["COMP_2"], "PA26")


class Generation(TestCase):
    """libxr gen：生成的 app_main 源文件与设置，以及重新生成时保留的用户代码。
    libxr gen: the generated app_main source and settings, and the user code kept on
    regeneration.
    """

    def setUp(self):
        super().setUp()
        generator.initialize_registry(False)
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
        reviewed = DATA / "hpm" / f"{name}_expected_app_main.cpp"
        if use_xrobot:
            shutil.copy(reviewed, output)
        generator.generate(str(root / ".config.yaml"), str(output), use_xrobot, "")
        return root, output.read_text(encoding="utf-8")

    def test_the_generated_source_matches_the_reviewed_output(self):
        for name in ("hpm5301evklite", "rmcs_slave_lite"):
            with self.subTest(name=name):
                _root, code = self.generate(name)
                expected = (DATA / "hpm" / f"{name}_expected_app_main.cpp").read_text(
                    encoding="utf-8"
                )
                self.assertEqual(code, expected)

    def test_the_written_settings_hold_the_defaults_the_generated_code_uses(self):
        root, code = self.generate("hpm5301evklite")
        settings = yaml.safe_load((root / "User" / "libxr_config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(settings["I2C"]["i2c3"]["speed"], 100000)
        self.assertIn("frequency", settings["PWM"]["pwm_gptmr0_ch1"])
        self.assertNotIn("USART", settings)
        self.assertNotIn("SYSTEM", settings)
        # 速率按设置写出，重新生成只写有变化的文件之外，文本保持不变。
        # The speed follows the setting; regenerating the same project writes the same text.
        self.assertIn("static HPMI2C i2c3(HPM_I2C3, clock_i2c3, {100000U});", code)

    def test_a_configured_frequency_and_gpio_renames_are_applied(self):
        root, _code = self.generate("hpm5301evklite")
        config = root / "User" / "libxr_config.yaml"
        settings = yaml.safe_load(config.read_text(encoding="utf-8"))
        settings["PWM"]["pwm_gptmr0_ch1"]["frequency"] = 1000
        settings["GPIO"] = {"pa10": "LED", "pa3": "KEY"}
        config.write_text(yaml.dump(settings), encoding="utf-8")
        generator.generate(
            str(root / ".config.yaml"), str(root / "User" / "app_main.cpp"), True, ""
        )
        code = (root / "User" / "app_main.cpp").read_text(encoding="utf-8")
        self.assertIn("pwm_gptmr0_ch1.SetConfig({1000});", code)
        self.assertIn("static HPMGPIO LED(HPM_GPIO0, GPIO_DI_GPIOA, 10, IRQn_GPIO0_A);", code)
        self.assertIn("XR_REGISTER(KEY, LibXR::GPIO);", code)
        self.assertNotIn("pa10", code)
        self.assertNotIn("pa3", code)

    def test_a_null_gpio_rename_keeps_the_default_name(self):
        # 设置树里清空改名写成 null：对象名退回引脚名，而不是 "None"。
        # Clearing a rename in the settings tree writes null: the object name falls back to the
        # pin name instead of "None".
        root, _code = self.generate("hpm5301evklite")
        config = root / "User" / "libxr_config.yaml"
        settings = yaml.safe_load(config.read_text(encoding="utf-8"))
        settings["GPIO"] = {"pa10": None, "pa3": "KEY"}
        config.write_text(yaml.dump(settings), encoding="utf-8")
        generator.generate(
            str(root / ".config.yaml"), str(root / "User" / "app_main.cpp"), True, ""
        )
        code = (root / "User" / "app_main.cpp").read_text(encoding="utf-8")
        self.assertIn("static HPMGPIO pa10(HPM_GPIO0, GPIO_DI_GPIOA, 10, IRQn_GPIO0_A);", code)
        self.assertIn("XR_REGISTER(pa10, LibXR::GPIO);", code)

    def test_the_gptmr_pwm_object_is_generated_and_registered(self):
        # 5301 没有 PWM 外设：GPTMR 的比较器走 HPMPWM 的 fallback 路径。对象和登记都不能包
        # #if——xrobot 的生成器无法判断编译选项，guard 里的 XR_REGISTER 会被它拒绝。
        # A HPM5301 has no PWM peripheral: the GPTMR comparators take the HPMPWM fallback
        # path. Neither the object nor the registration may sit inside #if — xrobot's
        # generator cannot evaluate build options and rejects an XR_REGISTER there.
        _root, code = self.generate("hpm5301evklite")
        self.assertNotIn("#if", code)
        self.assertIn("static HPMPWM pwm_gptmr0_ch1(", code)
        self.assertIn("XR_REGISTER(pwm_gptmr0_ch1, LibXR::PWM);", code)

    def test_a_soc_with_a_pwm_peripheral_generates_no_gptmr_object(self):
        # 5361 有 PWM 外设：蜂鸣器和加热器的 GPTMR 比较器只作展示，不生成对象也不登记。
        # A HPM5361 has a PWM peripheral: the GPTMR comparators of the buzzer and the
        # heater stay display-only, no object and no registration.
        _root, code = self.generate("rmcs_slave_lite")
        self.assertNotIn("HPMPWM", code)
        self.assertNotIn("hpm_pwm.hpp", code)
        self.assertIn('#include "hpm_gpio.hpp"', code)

    def test_the_user_code_of_an_existing_file_is_kept(self):
        root, _code = self.generate("hpm5301evklite")
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
        _root, code = self.generate("hpm5301evklite", use_xrobot=False)
        self.assertNotIn("XR_REGISTER", code)
        self.assertNotIn("xrobot_main.hpp", code)
        self.assertIn("Thread::Sleep(UINT32_MAX);", code)
        self.assertNotIn("XROBOT_MAIN();", code)
        # 对象照旧生成。
        # The objects are still generated.
        self.assertIn("static HPMPWM pwm_gptmr0_ch1(", code)

    def test_power_manager_is_generated_always_and_registered_only_with_xrobot(self):
        # power_manager 对象总是生成，登记只在使用 XRobot 时；GPIO 不能改叫 power_manager。
        # The power_manager object is always generated; it is registered only with XRobot, and
        # a GPIO cannot be renamed to power_manager.
        root, code = self.generate("hpm5301evklite", use_xrobot=False)
        self.assertIn('#include "hpm_power.hpp"', code)
        self.assertIn("static HPMPowerManager power_manager;", code)
        self.assertNotIn("XR_REGISTER", code)
        output = root / "User" / "app_main.cpp"
        generator.generate(str(root / ".config.yaml"), str(output), True, "")
        self.assertIn(
            "XR_REGISTER(power_manager, LibXR::PowerManager);",
            output.read_text(encoding="utf-8"),
        )
        (root / "User" / "libxr_config.yaml").write_text(
            "GPIO:\n  pa10: power_manager\n", encoding="utf-8"
        )
        with self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit):
            generator.generate(str(root / ".config.yaml"), str(output), True, "")
        self.assertIn("a name of the HPM SDK or of the generated code", "\n".join(logs.output))


class Notices(TestCase):
    """解析的两条提示（P3 遗留）：main.c 不调用 pinmux 函数，两个活动函数选中同一个 pad。
    The two parse notices (the P3 leftovers): a main.c that calls no pinmux function, and two
    active functions that select the same pad.
    """

    HPMPC = json.dumps(
        {
            "content": {
                "info": {"socName": "HPM5301", "packageName": "QFN48"},
                "pinmux": {
                    "functions": {
                        "init_bsp_pins": {
                            "selectPins": {"PA10": {"signal": "GPIO.A.A[10]", "padCtls": {}}}
                        },
                        "init_bsp_more_pins": {
                            "selectPins": {"PA10": {"signal": "SPI3.A.CS[0]", "padCtls": {}}}
                        },
                    }
                },
            }
        }
    )

    def parsed(self, main_c: str, hpmpc: str) -> dict:
        """合成工程（给定的 main.c 和 .hpmpc）的工程 YAML。
        The project YAML of a synthetic project (the given main.c and .hpmpc).
        """
        root = Path(tempfile.mkdtemp(prefix="libxr-hpm-notice-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "app.yaml").write_text("dependency: []\n", encoding="utf-8")
        board = root / "boards" / "board"
        board.mkdir(parents=True)
        (board / "tool_config.hpmpc").write_text(hpmpc, encoding="utf-8")
        (root / "main.c").write_text(main_c, encoding="utf-8")
        parse_project(str(root), summary=False)
        return yaml.safe_load((root / ".config.yaml").read_text(encoding="utf-8"))

    def test_two_active_functions_on_one_pad_are_reported(self):
        main_c = textwrap.dedent(
            """\
            int main(void)
            {
              init_bsp_pins();
              init_bsp_more_pins();
            }
            """
        )
        with self.assertLogs(level="WARNING") as logs:
            data = self.parsed(main_c, self.HPMPC)
        self.assertEqual(len(logs.output), 1)
        # 路径是绝对临时目录，Windows 下分隔符是反斜杠；只核对路径之后的部分。
        # The path is an absolute temporary directory whose separator is a backslash on
        # Windows; check the part after the path only.
        self.assertTrue(
            logs.output[0].endswith(
                ": PA10 is selected by both init_bsp_pins (GPIO.A.A[10]) and "
                "init_bsp_more_pins (SPI3.A.CS[0]); the later one wins"
            )
        )
        self.assertEqual(data["MainFunctions"], ["init_bsp_pins", "init_bsp_more_pins"])
        # 后一个的信号生效：PA10 归 SPI3，不再是 GPIO。
        # The later signal wins: PA10 belongs to the SPI3 now, not to the GPIO.
        self.assertNotIn("pa10", data["GPIO"])
        self.assertEqual(data["Peripherals"]["Other"]["SPI3"]["Pins"]["CS_0"], "PA10")

    def test_a_main_c_that_calls_no_pinmux_function_is_a_warning(self):
        main_c = textwrap.dedent(
            """\
            int main(void)
            {
              board_init();
            }
            """
        )
        with self.assertLogs(level="WARNING") as logs:
            data = self.parsed(main_c, self.HPMPC)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("calls no pinmux function (using the init_bsp_pins fallback)", logs.output[0])
        # 退回 init_bsp_pins：引脚照读。
        # The init_bsp_pins fallback: the pins are read as usual.
        self.assertEqual(data["MainFunctions"], ["init_bsp_pins"])
        self.assertEqual(data["GPIO"]["pa10"]["Pad"], "PA10")

    def test_without_init_bsp_pins_no_pin_is_read(self):
        hpmpc = json.dumps(
            {
                "content": {
                    "info": {"socName": "HPM5301", "packageName": "QFN48"},
                    "pinmux": {
                        "functions": {
                            "init_bsp_extra_pins": {
                                "selectPins": {"PA10": {"signal": "GPIO.A.A[10]", "padCtls": {}}}
                            }
                        }
                    },
                }
            }
        )
        main_c = "int main(void) { board_init(); }\n"
        with self.assertLogs(level="WARNING") as logs:
            data = self.parsed(main_c, hpmpc)
        self.assertEqual(len(logs.output), 2)
        self.assertIn("no init_bsp_pins exists, so no pin is read", logs.output[0])
        self.assertIn("select no GPIO or peripheral pin", logs.output[1])
        self.assertEqual(data["MainFunctions"], [])
        self.assertEqual(data["GPIO"], {})


def synthetic_project(
    selections: dict,
    soc: str = "HPM5301",
    gpiom: dict | None = None,
    main_c: str = "int main(void)\n{\n  init_bsp_pins();\n}\n",
) -> Path:
    """一个合成的 HPM 工程：init_bsp_pins 选中 selections（pad -> 工具的信号名），main 调用它。
    返回工程目录。
    A synthetic HPM project: init_bsp_pins selects selections (pad -> the tool's signal name)
    and main calls it. The project directory is returned.
    """
    root = Path(tempfile.mkdtemp(prefix="libxr-hpm-review-"))
    (root / "app.yaml").write_text("dependency: []\n", encoding="utf-8")
    board = root / "boards" / "board"
    board.mkdir(parents=True)
    function = {
        "selectPins": {
            pad: {"signal": signal, "padCtls": {}} for pad, signal in selections.items()
        },
        "managers": {"gpiom": gpiom or {}},
    }
    hpmpc = {
        "content": {
            "info": {"socName": soc, "packageName": "QFN48"},
            "pinmux": {"functions": {"init_bsp_pins": function}},
        }
    }
    (board / "tool_config.hpmpc").write_text(json.dumps(hpmpc), encoding="utf-8")
    (root / "main.c").write_text(main_c, encoding="utf-8")
    return root


class ParseChecks(TestCase):
    """解析的核对：SoC、焊盘和信号按引脚数据核对，生成前的外设一致性检查。
    The checks of the parse: the SoC, the pads and the signals against the pin data, and the
    consistency of the peripherals before generation.
    """

    def parse(self, root: Path) -> dict:
        """解析 root 并返回工程 YAML；测试结束时删除 root。
        Parse root and return the project YAML; root is removed at the end of the test.
        """
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        parse_project(str(root), summary=False)
        return yaml.safe_load((root / ".config.yaml").read_text(encoding="utf-8"))

    def test_an_unknown_soc_is_an_error(self):
        root = synthetic_project({"PA10": "GPIO.A.A[10]"}, soc="HPM9999")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit):
            parse_project(str(root), summary=False)
        self.assertIn("Unknown HPM SoC: HPM9999", logs.output[0])

    def test_a_lower_case_soc_is_the_same_soc(self):
        data = self.parse(synthetic_project({"PA10": "GPIO.A.A[10]"}, soc="hpm5301"))
        self.assertEqual(data["Mcu"]["Type"], "HPM5301")

    def test_a_pad_off_the_package_and_a_signal_the_pad_cannot_carry_are_warned_about(self):
        with self.assertLogs(level="WARNING") as logs:
            data = self.parse(synthetic_project({"PA12": "GPIO.A.A[12]", "PA10": "UART0.A.TXD"}))
        self.assertTrue(
            any("PA12 (GPIO.A.A[12]) is not a pin of HPM5301 QFN48" in line for line in logs.output)
        )
        self.assertTrue(any("PA10 cannot carry UART0_TXD" in line for line in logs.output))
        self.assertNotIn("pa12", data["GPIO"])

    def test_an_i2c_without_sda_generates_nothing(self):
        with self.assertLogs(level="WARNING") as logs:
            data = self.parse(synthetic_project({"PB13": "I2C3.C.SCL"}))
        self.assertNotIn("I2C", data["Peripherals"])
        self.assertEqual(data["Peripherals"]["Other"]["I2C3"]["Pins"], {"SCL": "PB13"})
        self.assertTrue(any("I2C3 has no SDA pin" in line for line in logs.output))

    def test_a_gptmr_with_capture_only_is_no_pwm(self):
        data = self.parse(synthetic_project({"PB09": "GPTMR0.B.CAPT[1]"}))
        self.assertNotIn("PWM", data["Peripherals"])
        self.assertEqual(data["Peripherals"]["Other"]["GPTMR0"]["Pins"], {"CAPT_1": "PB09"})

    def test_a_gptmr_with_capture_and_compare_warns_about_the_capture(self):
        with self.assertLogs(level="WARNING") as logs:
            data = self.parse(
                synthetic_project({"PB08": "GPTMR0.B.COMP[1]", "PB09": "GPTMR0.B.CAPT[1]"})
            )
        self.assertEqual(
            data["Peripherals"]["PWM"]["GPTMR0"]["Channels"], [{"Index": 1, "Pad": "PB08"}]
        )
        self.assertTrue(any("GPTMR0 also selects CAPT_1" in line for line in logs.output))

    def test_a_pin_of_another_gpio_controller_gets_no_object(self):
        with self.assertLogs(level="WARNING") as logs:
            data = self.parse(
                synthetic_project(
                    {"PA10": "GPIO.A.A[10]", "PA03": "GPIO.A.A[03]"},
                    gpiom={"PA10": {"direction": "1", "gpioController": "2"}},
                )
            )
        self.assertEqual(list(data["GPIO"]), ["pa3"])
        self.assertTrue(
            any("PA10 is assigned to GPIO controller 2" in line for line in logs.output)
        )

    def test_no_fallback_when_every_call_is_conditional(self):
        # 审查 C2：与 libxr pins 一样，调用全在 #if 里时不退回 init_bsp_pins。
        # Review C2: as in libxr pins, no init_bsp_pins fallback when every call is in #if.
        main_c = "int main(void)\n{\n#if USE_PINS\n  init_bsp_pins();\n#endif\n}\n"
        with self.assertLogs(level="WARNING") as logs:
            data = self.parse(synthetic_project({"PA10": "GPIO.A.A[10]"}, main_c=main_c))
        self.assertEqual((data["MainFunctions"], data["GPIO"]), ([], {}))
        self.assertTrue(any("only inside preprocessor conditions" in line for line in logs.output))
        self.assertFalse(any("fallback" in line for line in logs.output))

    def test_a_pinmux_function_main_does_not_call_is_warned_about(self):
        root = synthetic_project({"PA10": "GPIO.A.A[10]"})
        hpmpc = root / "boards" / "board" / "tool_config.hpmpc"
        document = json.loads(hpmpc.read_text(encoding="utf-8"))
        document["content"]["pinmux"]["functions"]["init_unused_pins"] = {
            "selectPins": {"PA03": {"signal": "GPIO.A.A[03]", "padCtls": {}}}
        }
        hpmpc.write_text(json.dumps(document), encoding="utf-8")
        with self.assertLogs(level="WARNING") as logs:
            data = self.parse(root)
        self.assertEqual(list(data["GPIO"]), ["pa10"])
        self.assertTrue(
            any("init_unused_pins() is not called by main" in line for line in logs.output)
        )


class GenerationChecks(TestCase):
    """生成的核对：设置的类型、名字和大小写，改名表和终端设置的写法。
    The checks of generation: the type, names and case of the settings, and how the rename
    table and the terminal settings are written.
    """

    def setUp(self):
        super().setUp()
        generator.initialize_registry(False)
        generator.reset_settings()

    def generate(self, config: str | None = None, root: Path | None = None) -> tuple[Path, str]:
        """解析 hpm5301evklite 的临时工程（或 root），按 config（libxr_config.yaml 的内容）生成。
        返回工程目录和生成的 app_main。
        Parse the temporary project of hpm5301evklite (or root) and generate with config (the
        content of libxr_config.yaml). The project directory and the generated app_main are
        returned.
        """
        if root is None:
            root = project_of("hpm5301evklite")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        parse_project(str(root), summary=False)
        output = root / "User" / "app_main.cpp"
        output.parent.mkdir(exist_ok=True)
        if config is not None:
            (root / "User" / "libxr_config.yaml").write_text(config, encoding="utf-8")
        generator.generate(str(root / ".config.yaml"), str(output), True, "")
        return root, output.read_text(encoding="utf-8")

    def settings(self, root: Path) -> dict:
        """写出的 libxr_config.yaml。
        The libxr_config.yaml that was written.
        """
        return yaml.safe_load((root / "User" / "libxr_config.yaml").read_text(encoding="utf-8"))

    def assertGenerationFails(self, config: str, *messages: str):
        """按 config 生成时报错退出，错误信息含 messages。
        Generation with config logs an error that holds messages and exits.
        """
        with self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit):
            self.generate(config)
        for message in messages:
            self.assertIn(message, "\n".join(logs.output))

    def test_the_rename_table_lists_every_pin_and_no_terminal_is_written(self):
        root, _code = self.generate()
        settings = self.settings(root)
        self.assertEqual(settings["GPIO"], {"pa10": None, "pa3": None})
        self.assertNotIn("Terminal", settings)
        self.assertNotIn("terminal_source", settings)

    def test_a_stale_null_rename_goes_and_a_stale_name_stays(self):
        root, _code = self.generate("GPIO:\n  pa10: LED\n  pb1:\n  pb2: OLD\n")
        self.assertEqual(self.settings(root)["GPIO"], {"pa10": "LED", "pb2": "OLD", "pa3": None})

    def test_a_written_terminal_source_is_kept_and_warned_about(self):
        with self.assertLogs(level="WARNING") as logs:
            root, _code = self.generate("terminal_source: uart0\n")
        self.assertEqual(self.settings(root)["terminal_source"], "uart0")
        self.assertTrue(
            any("terminal_source 'uart0' has no effect" in line for line in logs.output)
        )

    def test_unusable_gpio_names_are_listed_together(self):
        self.assertGenerationFails(
            "GPIO:\n  pa10: int\n  pa3: HPM_GPIO0\n",
            "GPIO.pa10 'int': a C++ keyword",
            "GPIO.pa3 'HPM_GPIO0': a name of the HPM SDK or of the generated code",
        )
        for name, problem in (
            ("timebase", "a name of the HPM SDK or of the generated code"),
            ("i2c3", "already the name of a I2C object"),
            ("__led", "a reserved C++ identifier"),
            ("1led", "not a valid C++ identifier"),
        ):
            with self.subTest(name=name):
                generator.initialize_registry(False)
                generator.reset_settings()
                self.assertGenerationFails(f"GPIO:\n  pa10: '{name}'\n", problem)

    def test_sdk_macros_and_generated_names_are_refused_and_plain_names_pass(self):
        # 审查 C1：类函数宏和生成代码用到的名字（NORMAL 是 PWM 的极性）不能用；gpio_led 可以。
        # Review C1: function-like macros and names the generated code uses (NORMAL is the
        # polarity of the PWM) are refused; gpio_led is fine.
        self.assertGenerationFails("GPIO:\n  pa10: MAX\n", "GPIO.pa10 'MAX'")
        generator.initialize_registry(False)
        generator.reset_settings()
        self.assertGenerationFails(
            "GPIO:\n  pa10: NORMAL\n",
            "the generated code uses the names for something else: NORMAL",
        )
        generator.initialize_registry(False)
        generator.reset_settings()
        _root, code = self.generate("GPIO:\n  pa10: gpio_led\n  pa3: board_key\n")
        self.assertIn("static HPMGPIO gpio_led(", code)

    def test_a_null_or_literal_speed_is_read_as_on_the_other_platforms(self):
        # 审查 C3：null 用默认值，字符串按整数字面量读。
        # Review C3: null takes the default, a string is read as an integer literal.
        _root, code = self.generate("I2C:\n  i2c3:\n    speed:\n")
        self.assertIn("{100000U}", code)
        generator.initialize_registry(False)
        generator.reset_settings()
        _root, code = self.generate("I2C:\n  i2c3:\n    speed: '0x61A80'\n")
        self.assertIn("{400000U}", code)

    def test_a_gpio_rename_must_be_a_name(self):
        self.assertGenerationFails("GPIO:\n  pa10: 5\n", "GPIO.pa10 5 is not a name")

    def test_a_pwm_frequency_must_be_a_positive_integer(self):
        for value in ("0", "-5", "abc", "1.5", "true"):
            with self.subTest(value=value):
                generator.initialize_registry(False)
                generator.reset_settings()
                self.assertGenerationFails(
                    f"PWM:\n  pwm_gptmr0_ch1:\n    frequency: {value}\n",
                    "PWM.pwm_gptmr0_ch1.frequency",
                    "is not a positive integer",
                )

    def test_the_key_of_a_setting_is_found_in_any_case(self):
        # 文件写 I2C3、PWM_GPTMR0_CH1、PA10：生成与 libxr pins 一样读到这些段，不另写一份小写的。
        # The file writes I2C3, PWM_GPTMR0_CH1 and PA10: generation reads these entries as libxr
        # pins does, and writes no second, lower-case copy.
        root, code = self.generate(
            "I2C:\n  I2C3:\n    speed: 400000\n"
            "PWM:\n  PWM_GPTMR0_CH1:\n    frequency: 1000\n"
            "GPIO:\n  PA10: LED\n"
        )
        self.assertIn("static HPMI2C i2c3(HPM_I2C3, clock_i2c3, {400000U});", code)
        self.assertIn("pwm_gptmr0_ch1.SetConfig({1000});", code)
        self.assertIn("static HPMGPIO LED(", code)
        settings = self.settings(root)
        self.assertEqual(list(settings["I2C"]), ["I2C3"])
        self.assertEqual(list(settings["PWM"]), ["PWM_GPTMR0_CH1"])
        self.assertEqual(settings["GPIO"], {"PA10": "LED", "pa3": None})

    def test_an_unknown_soc_in_the_project_yaml_is_an_error(self):
        root = project_of("hpm5301evklite")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        parse_project(str(root), summary=False)
        config = root / ".config.yaml"
        config.write_text(
            config.read_text(encoding="utf-8").replace("Type: HPM5301", "Type: HPM9999"),
            encoding="utf-8",
        )
        (root / "User").mkdir()
        with self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit):
            generator.generate(str(config), str(root / "User" / "app_main.cpp"), True, "")
        self.assertIn("Unknown HPM SoC: HPM9999", logs.output[0])

    def test_the_notice_says_when_main_calls_no_pinmux_function(self):
        root = synthetic_project(
            {"PA10": "GPIO.A.A[10]"}, main_c="int main(void)\n{\n  board_init();\n}\n"
        )
        with self.assertLogs(level="WARNING"):
            _root, code = self.generate(root=root)
        notice = " ".join(line.removeprefix("//").strip() for line in code.splitlines()[:6])
        self.assertIn("defines the pins in init_bsp_pins(), which main.c does not call", notice)


if __name__ == "__main__":
    unittest.main()
