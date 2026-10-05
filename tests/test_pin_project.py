"""libxr pins -d（libxr.pin_project）：把工程已选的引脚信号叠加到布局上。
libxr pins -d (libxr.pin_project): overlay the pin signals a project has selected on the layout.
"""

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml
from fixtures import IOC, TestCase, run_libxr

from libxr import pin_layout, pin_project
from libxr.pin_project import layout_with_project

# 一个 MSPM0G3507 工程的 ti_msp_dl_config.h 片段：UART0 的两个引脚和一个 GPIO 组。
# An excerpt of the ti_msp_dl_config.h of an MSPM0G3507 project: two pins of UART0 and a GPIO
# group.
TI_HEADER = textwrap.dedent("""\
    #define CONFIG_LP_MSPM0G3507
    #define CONFIG_MSPM0G3507
    #define GPIO_UART_0_IOMUX_RX                                      (IOMUX_PINCM2)
    #define GPIO_UART_0_IOMUX_TX                                      (IOMUX_PINCM1)
    #define GPIO_UART_0_IOMUX_RX_FUNC                       IOMUX_PINCM2_PF_UART0_RX
    #define GPIO_UART_0_IOMUX_TX_FUNC                       IOMUX_PINCM1_PF_UART0_TX
    #define GPIO_GRP_0_PORT                                                  (GPIOB)
    #define GPIO_GRP_0_PIN_0_IOMUX                                   (IOMUX_PINCM50)
""")

CONFIG = textwrap.dedent("""\
    USART:
      usart1:
        tx_buffer_size: 128
        rx_buffer_size: 64
    SPI:
      spi1:
        dma_section: .axi_ram
""")


def write(directory: Path, name: str, text: str) -> Path:
    """在 directory 下写出文本文件 name（必要时建目录），返回其路径。
    Write the text file name under directory, creating the folders it needs; return its path.
    """
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def ioc(model: str, enabled: list[str], pins: dict[str, str]) -> str:
    """一个最小的 .ioc：型号、启用的外设和 引脚 -> 信号。
    A minimal .ioc: the model, the enabled peripherals and pin -> signal.
    """
    lines = [f"Mcu.UserName={model}"]
    lines += [f"Mcu.IP{index}={name}" for index, name in enumerate(enabled)]
    lines += [f"{pin}.Signal={signal}" for pin, signal in pins.items()]
    return "\n".join(lines) + "\n"


class Stm32Projects(TestCase):
    """STM32 工程：已选信号取自 .ioc，设置取自 libxr_config.yaml。
    An STM32 project: the selected signals come from the .ioc, the settings from
    libxr_config.yaml.
    """

    def project(self, directory, config=CONFIG, model=None, config_path=None):
        """工程目录中的叠加结果（工程 .ioc 为共用的样例，config 是 User/libxr_config.yaml 的内容）。
        The overlay of the project directory (its .ioc is the shared sample, config is the
        content of User/libxr_config.yaml).
        """
        write(Path(directory), "Project.ioc", IOC)
        if config is not None:
            write(Path(directory), "User/libxr_config.yaml", config)
        return layout_with_project(directory, model, None, config_path)

    def test_the_model_and_the_signals_come_from_the_ioc(self):
        with tempfile.TemporaryDirectory() as directory:
            info = self.project(directory)
        self.assertEqual((info["part"], info["package"]), ("STM32F407IGHx", "UFBGA176"))
        self.assertEqual(info["project"]["source"], "Project.ioc")
        assignments = info["project"]["assignments"]
        self.assertEqual(
            assignments["PA9"],
            {
                "signal": "USART1_TX",
                "peripheral": "USART1",
                "kind": "USART",
                "function": "TX",
                "matched": True,
            },
        )
        self.assertTrue(all(entry["matched"] for entry in assignments.values()))

    def test_gpio_labels_and_external_interrupts(self):
        with tempfile.TemporaryDirectory() as directory:
            info = self.project(directory)
        assignments = info["project"]["assignments"]
        # ST 给 PC13 的名字带功能后缀（PC13-ANTI_TAMP）；叠加结果用布局中的名字。
        # ST names PC13 with its function (PC13-ANTI_TAMP); the overlay uses the name in the
        # layout.
        led = next(entry for name, entry in assignments.items() if name.startswith("PC13"))
        self.assertEqual(
            (led["label"], led["peripheral"], led["function"]), ("LED", "GPIOC", "P13")
        )
        self.assertEqual(
            (assignments["PE12"]["peripheral"], assignments["PE12"]["function"]),
            ("EXTI", "LINE12"),
        )
        self.assertEqual(info["project"]["peripherals"]["EXTI"]["pins"], {"LINE12": "PE12"})

    def test_shared_pin_signals_are_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            info = self.project(directory)
        tim1 = info["project"]["peripherals"]["TIM1"]
        self.assertEqual((tim1["kind"], tim1["pins"]), ("TIM", {"CH1": "PE9"}))

    def test_a_peripheral_is_tied_to_its_libxr_config_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            info = self.project(directory)
        peripherals = info["project"]["peripherals"]
        self.assertEqual(
            peripherals["USART1"]["config"],
            {
                "section": "USART",
                "key": "usart1",
                "present": True,
                "params": {"tx_buffer_size": 128, "rx_buffer_size": 64},
            },
        )
        self.assertEqual(
            peripherals["USART6"]["config"], {"section": "USART", "key": "usart6", "present": False}
        )
        self.assertEqual(peripherals["SPI1"]["config"]["params"], {"dma_section": ".axi_ram"})
        self.assertNotIn("config", peripherals["GPIOC"])

    def test_the_case_of_a_key_in_the_file_is_kept(self):
        # 有的 libxr_config.yaml 写 fdcan1，有的写 FDCAN1，两种都要找得到。
        # One libxr_config.yaml writes fdcan1 and another FDCAN1; both have to be found.
        with tempfile.TemporaryDirectory() as directory:
            write(Path(directory), "a.ioc", ioc("STM32H723VGTx", [], {"PD0": "FDCAN1_RX"}))
            write(
                Path(directory), "User/libxr_config.yaml", "FDCAN:\n  FDCAN1:\n    queue_size: 5\n"
            )
            info = layout_with_project(directory, None, None, None)
        self.assertEqual(
            info["project"]["peripherals"]["FDCAN1"]["config"],
            {"section": "FDCAN", "key": "FDCAN1", "present": True, "params": {"queue_size": 5}},
        )

    def test_a_missing_configuration_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            info = self.project(directory, config=None)
        self.assertIsNone(info["project"]["libxr_config"])
        self.assertFalse(info["project"]["peripherals"]["USART1"]["config"]["present"])

    def test_another_configuration_file_can_be_given(self):
        with tempfile.TemporaryDirectory() as directory:
            other = write(
                Path(directory), "other.yaml", "SPI:\n  spi1:\n    dma_enable_min_size: 8\n"
            )
            info = self.project(directory, config_path=str(other))
        self.assertEqual(
            info["project"]["peripherals"]["SPI1"]["config"]["params"], {"dma_enable_min_size": 8}
        )
        self.assertFalse(info["project"]["peripherals"]["USART1"]["config"]["present"])

    def test_a_given_model_wins_over_the_ioc(self):
        with tempfile.TemporaryDirectory() as directory:
            info = self.project(directory, model="STM32F407IGT6")
        self.assertEqual((info["part"], info["package"]), ("STM32F407IGTx", "LQFP176"))


class CubeMxSpellings(TestCase):
    """CubeMX 在 .ioc 里的几种写法，都对应到 ST 的信号。
    The forms CubeMX writes in an .ioc, each tied to ST's signals.
    """

    def overlay(self, model, enabled, pins):
        """这个 .ioc 的已选信号。
        The selected signals of this .ioc.
        """
        with tempfile.TemporaryDirectory() as directory:
            write(Path(directory), "a.ioc", ioc(model, enabled, pins))
            info = layout_with_project(directory, None, None, None)
        return info["project"]["assignments"]

    def test_an_adc_channel_takes_the_adc_the_project_enables(self):
        found = self.overlay("STM32H723VGTx", ["ADC1"], {"PA5": "ADCx_INP19"})["PA5"]
        self.assertEqual((found["peripheral"], found["function"]), ("ADC1", "INP19"))
        self.assertTrue(found["matched"])

    def test_an_adc_channel_with_several_candidates_lists_them(self):
        found = self.overlay("STM32H723VGTx", [], {"PA5": "ADCx_INP19"})["PA5"]
        self.assertEqual(found["peripheral"], "ADC")
        self.assertEqual(found["candidates"], ["ADC1", "ADC2"])

    def test_two_functions_that_share_a_prefix(self):
        found = self.overlay("STM32H563ZITx", [], {"PD0": "FMC_D2_DA2"})["PD0"]
        self.assertEqual((found["peripheral"], found["function"]), ("FMC", "D2/DA2"))
        self.assertTrue(found["matched"])

    def test_the_mii_or_rmii_infix_may_be_left_out(self):
        found = self.overlay("STM32H563ZITx", [], {"PB15": "ETH_TXD1"})["PB15"]
        self.assertEqual((found["peripheral"], found["function"]), ("ETH", "TXD1"))

    def test_analog_pins(self):
        found = self.overlay("STM32G071RBTx", [], {"PA4": "SharedAnalog_PA4"})["PA4"]
        self.assertEqual((found["peripheral"], found["kind"]), ("ANALOG", "ANALOG"))

    def test_a_signal_the_pin_does_not_have_is_given_as_it_is(self):
        found = self.overlay("STM32H723VGTx", [], {"PA9": "USART3_TX"})["PA9"]
        self.assertEqual((found["peripheral"], found["function"]), ("USART3", "TX"))
        self.assertFalse(found["matched"])

    def test_a_pin_with_a_suffix_in_its_name(self):
        # .ioc 里的键写成 PH0-OSC_IN\ (PH0)（空格转义）；布局里的引脚叫 PH0-OSC_IN。
        # The .ioc key reads PH0-OSC_IN\ (PH0), the space escaped; the pin is PH0-OSC_IN in the
        # layout.
        found = self.overlay("STM32H723VGTx", [], {r"PH0-OSC_IN\ (PH0)": "RCC_OSC_IN"})
        self.assertEqual(list(found), ["PH0-OSC_IN"])
        self.assertEqual(found["PH0-OSC_IN"]["peripheral"], "RCC")


class Mspm0Projects(TestCase):
    """MSPM0 工程：已选信号取自 SysConfig 生成的 ti_msp_dl_config.h。
    An MSPM0 project: the selected signals come from the ti_msp_dl_config.h SysConfig
    generates.
    """

    def overlay(self, package="PM", header=TI_HEADER):
        """这个头文件的叠加结果。
        The overlay of this header.
        """
        with tempfile.TemporaryDirectory() as directory:
            write(Path(directory), "sysconfig/ti_msp_dl_config.h", header)
            return layout_with_project(directory, None, package, None)

    def test_the_device_and_the_pins_come_from_the_header(self):
        info = self.overlay()
        self.assertEqual((info["part"], info["package"]), ("MSPM0G3507", "LQFP-64(PM)"))
        self.assertEqual(info["project"]["source"], "sysconfig/ti_msp_dl_config.h")
        assignments = info["project"]["assignments"]
        self.assertEqual(
            assignments["PA0"],
            {
                "signal": "UART0.TX",
                "peripheral": "UART0",
                "kind": "UART",
                "function": "TX",
                "matched": True,
            },
        )
        self.assertEqual(assignments["PA1"]["signal"], "UART0.RX")
        self.assertEqual(
            info["project"]["peripherals"]["UART0"]["pins"], {"TX": "PA0", "RX": "PA1"}
        )

    def test_a_gpio_is_named_by_its_label(self):
        gpio = self.overlay()["project"]["assignments"]["PB22"]
        self.assertEqual(
            (gpio["peripheral"], gpio["function"], gpio["label"]),
            ("GPIOB", "P22", "GPIO_GRP_0_PIN_0"),
        )

    def test_libxr_gen_has_no_section_for_an_mspm0(self):
        peripherals = self.overlay()["project"]["peripherals"]
        self.assertNotIn("config", peripherals["UART0"])

    def test_the_package_is_asked_for(self):
        with self.assertRaises(ValueError) as caught:
            self.overlay(package=None)
        self.assertTrue(
            str(caught.exception).startswith("Cannot tell the package of MSPM0G3507"),
            str(caught.exception),
        )

    def test_a_header_without_the_device_is_an_error(self):
        with self.assertRaises(ValueError) as caught:
            self.overlay(header="#define SOMETHING\n")
        self.assertTrue(
            str(caught.exception).endswith(
                "does not name the MSPM0 device (no CONFIG_MSPM0... line)"
            ),
            str(caught.exception),
        )


class SyscfgPackage(TestCase):
    """MSPM0 的封装取自 SysConfig 工程的 .syscfg：--package，或 --board 对应的 LaunchPad。
    The package of an MSPM0 comes from the .syscfg of the SysConfig project: its --package, or the
    LaunchPad its --board names.
    """

    def package(self, syscfg: str | None, option=None, header_folder="sysconfig", syscfg_path=None):
        """给出的 .syscfg 内容下，叠加结果里的封装（syscfg 为 None 时没有 .syscfg）。
        The package in the overlay for this .syscfg content (no .syscfg when it is None).
        """
        with tempfile.TemporaryDirectory() as directory:
            write(Path(directory), f"{header_folder}/ti_msp_dl_config.h", TI_HEADER)
            if syscfg is not None:
                write(Path(directory), syscfg_path or "sysconfig/project.syscfg", syscfg)
            return layout_with_project(directory, None, option, None)["package"]

    def test_a_device_based_project_names_its_package(self):
        for line in (
            '//@cliArgs --device "MSPM0G350X" --package "LQFP-64(PM)" --part "Default"',
            ' * @cliArgs --device "MSPM0G3507" --package "LQFP-64(PM)"',
        ):
            with self.subTest(line=line):
                self.assertEqual(self.package(line + "\n"), "LQFP-64(PM)")

    def test_a_board_based_project_gets_the_package_of_the_board(self):
        for line in (
            "// @cliArgs --board /ti/boards/LP_MSPM0G3507 --rtos nortos",
            ' * @cliArgs --board "/ti/boards/LP_MSPM0G3507" --product "mspm0_sdk@2.09.00.00"',
        ):
            with self.subTest(line=line):
                self.assertEqual(self.package(line + "\n"), "LQFP-64(PM)")

    def test_the_v2_line_comes_last_and_wins(self):
        text = (
            '//@cliArgs --device "MSPM0G350X" --package "VQFN-48(RGZ)" --part "Default"\n'
            '//@v2CliArgs --device "MSPM0G3507" --package "LQFP-64(PM)"\n'
        )
        self.assertEqual(self.package(text), "LQFP-64(PM)")

    def test_a_given_package_wins_over_the_project(self):
        text = '//@cliArgs --device "MSPM0G350X" --package "LQFP-64(PM)"\n'
        self.assertEqual(self.package(text, option="VQFN-48"), "VQFN-48(RGZ)")

    def test_the_syscfg_may_sit_in_the_root_while_the_header_is_in_a_build_output(self):
        # CCS 的工程：.syscfg 在根目录，生成的头文件在 Debug/syscfg/。
        # A CCS project: the .syscfg in the root, the generated header in Debug/syscfg/.
        text = '//@cliArgs --device "MSPM0G350X" --package "LQFP-48(PT)"\n'
        self.assertEqual(
            self.package(text, header_folder="Debug/syscfg", syscfg_path="empty.syscfg"),
            "LQFP-48(PT)",
        )

    def test_without_a_package_the_error_asks_for_one(self):
        for text in (None, "// nothing here\n", "// @cliArgs --board /ti/boards/LP_UNKNOWN\n"):
            with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                self.package(text)
            self.assertTrue(
                str(caught.exception).startswith("Cannot tell the package of MSPM0G3507"),
                str(caught.exception),
            )

    def test_the_board_table_has_the_launchpads_with_their_devices(self):
        boards = pin_project.load_boards()
        self.assertEqual(
            boards["LP_MSPM0G3507"], {"device": "MSPM0G3507", "package": "LQFP-64(PM)"}
        )
        self.assertEqual(boards["LP_MSPM0C1106"]["device"], "MSPM0C1106")
        self.assertTrue(all(board["package"] for board in boards.values()))

    def test_the_board_table_ships_with_its_license(self):
        text = (pin_layout.DATA / "LICENSE-TI-BOARDS.txt").read_text(encoding="utf-8")
        self.assertIn("Texas Instruments Incorporated", text)
        self.assertIn("Redistribution and use in source and binary forms", text)
        source = json.loads((pin_layout.DATA / "mspm0_boards.json").read_text(encoding="utf-8"))
        self.assertRegex(source["source"]["commit"], r"^[0-9a-f]{40}$")


class NoProject(TestCase):
    """没有可识别的工程时报错。
    A directory without a recognizable project is an error.
    """

    def test_an_empty_directory_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError) as caught:
            layout_with_project(directory, None, None, None)
        self.assertTrue(
            str(caught.exception).endswith(
                ": no STM32CubeMX .ioc or SysConfig ti_msp_dl_config.h found"
            ),
            str(caught.exception),
        )


class CommandLine(TestCase):
    """libxr pins 的 -d 和 -c 选项。
    The -d and -c options of libxr pins.
    """

    def test_a_project_directory_replaces_the_model(self):
        with tempfile.TemporaryDirectory() as directory:
            write(Path(directory), "Project.ioc", IOC)
            write(Path(directory), "User/libxr_config.yaml", CONFIG)
            code, out, err = run_libxr("pins", "-d", directory)
        self.assertEqual((code, err), (0, ""))
        info = yaml.safe_load(out)
        self.assertEqual(info["part"], "STM32F407IGHx")
        self.assertEqual(info["project"]["peripherals"]["USART1"]["config"]["present"], True)

    def test_the_configuration_file_can_be_given_with_c(self):
        with tempfile.TemporaryDirectory() as directory:
            write(Path(directory), "Project.ioc", IOC)
            other = write(Path(directory), "x.yaml", "SPI:\n  spi1:\n    tx_buffer_size: 7\n")
            code, out, _ = run_libxr("pins", "-d", directory, "-c", str(other), "-f", "yaml")
        info = yaml.safe_load(out)
        self.assertEqual(code, 0)
        self.assertEqual(
            info["project"]["peripherals"]["SPI1"]["config"]["params"], {"tx_buffer_size": 7}
        )

    def test_errors_are_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            for argv, reason in (
                (("pins",), "Give a chip model, or a project directory with -d"),
                (
                    ("pins", "-d", directory),
                    f"{directory}: no STM32CubeMX .ioc or SysConfig ti_msp_dl_config.h found",
                ),
            ):
                with self.subTest(argv=argv), self.assertLogs(level="ERROR") as logs:
                    code, out, _ = run_libxr(*argv)
                    self.assertEqual((code, out), (1, ""))
                    self.assertEqual(logs.output, [f"ERROR:root:{reason}"])


if __name__ == "__main__":
    unittest.main()
