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
                            "selectPins": {"PA10": {"signal": "UART0.A.TXD", "padCtls": {}}}
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
                "init_bsp_more_pins (UART0.A.TXD); the later one wins"
            )
        )
        self.assertEqual(data["MainFunctions"], ["init_bsp_pins", "init_bsp_more_pins"])
        # 后一个的信号生效：PA10 归 UART0，不再是 GPIO。
        # The later signal wins: PA10 belongs to the UART0 now, not to the GPIO.
        self.assertNotIn("pa10", data["GPIO"])
        self.assertEqual(data["Peripherals"]["Other"]["UART0"]["Pins"]["TXD"], "PA10")

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


if __name__ == "__main__":
    unittest.main()
