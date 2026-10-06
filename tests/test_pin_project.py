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


class SyscfgPackage(TestCase):
    """MSPM0 的封装取自 SysConfig 工程的 .syscfg：--package，或 --board 对应的 LaunchPad。
    The package of an MSPM0 comes from the .syscfg of the SysConfig project: its --package, or the
    LaunchPad its --board names.
    """

    def package(self, syscfg: str, option=None):
        """给出的根目录 .syscfg 内容下，叠加结果里的封装。
        The package in the overlay for this root .syscfg content.
        """
        with tempfile.TemporaryDirectory() as directory:
            write(Path(directory), "a.syscfg", syscfg)
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

    def test_without_a_package_the_error_asks_for_one(self):
        with self.assertRaises(ValueError) as caught:
            self.package('//@cliArgs --device "MSPM0G3507"\n')
        self.assertTrue(
            str(caught.exception).startswith("Cannot tell the package of MSPM0G3507"),
            str(caught.exception),
        )

    def test_a_syscfg_without_a_device_or_a_known_board_needs_the_model(self):
        # 板子不在表里，器件也就无从得知。
        # The board is not in the table, so the device cannot be known either.
        with self.assertRaises(ValueError) as caught:
            self.package("// @cliArgs --board /ti/boards/LP_UNKNOWN\n")
        self.assertTrue(
            str(caught.exception).endswith("does not name a device; give the model"),
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


class SyscfgProjects(TestCase):
    """MSPM0 工程：根目录的 .syscfg 直接给出器件、封装和已选信号，不需要跑 SysConfig。
    An MSPM0 project: a root .syscfg gives the device, the package and the selected signals
    directly; SysConfig does not have to run.
    """

    PROJECT = textwrap.dedent("""\
        //@cliArgs --device "MSPM0G350X" --part "Default" --package "LQFP-64(PM)"
        //@v2CliArgs --device "MSPM0G3507" --package "LQFP-64(PM)"
        
        const Board  = scripting.addModule("/ti/driverlib/Board", {}, false);
        Board.peripheral.$assign          = "DEBUGSS";
        Board.peripheral.swdioPin.$assign = "PA19";
        
        const pinFunction1                       = system.clockTree["HFXT"];
        pinFunction1.peripheral.hfxInPin.$assign = "PA5";
        
        const GPIO  = scripting.addModule("/ti/driverlib/GPIO", {}, false);
        const GPIO1 = GPIO.addInstance();
        GPIO1.$name                         = "GPIO_KEYS";
        GPIO1.associatedPins.create(1);
        GPIO1.associatedPins[0].$name       = "PIN_KEY1";
        GPIO1.associatedPins[0].pin.$assign = "PB22";
        
        const UART  = scripting.addModule("/ti/driverlib/UART", {}, false);
        const UART1 = UART.addInstance();
        UART1.$name                         = "UART_0";
        UART1.peripheral.$assign            = "UART0";
        UART1.peripheral.txPin.$assign      = "PA0";
        UART1.peripheral.rxPin.$assign      = "PA1";
        
        const DAC12 = scripting.addModule("/ti/driverlib/DAC12");
        DAC12.peripheral.$assign        = "DAC0";
        DAC12.peripheral.OutPin.$assign = "PA15";
        """)

    def project(self, syscfg=PROJECT, model=None):
        """这个根目录 .syscfg 的叠加结果。
        The overlay of this root .syscfg.
        """
        with tempfile.TemporaryDirectory() as directory:
            write(Path(directory), "a.syscfg", syscfg)
            return layout_with_project(directory, model, None, None)

    def test_the_device_the_package_and_the_assignments_come_from_the_syscfg(self):
        info = self.project()
        self.assertEqual((info["part"], info["package"]), ("MSPM0G3507", "LQFP-64(PM)"))
        self.assertEqual(info["project"]["source"], "a.syscfg")
        assignments = info["project"]["assignments"]
        self.assertEqual(assignments["PA0"]["signal"], "UART0.TX")
        self.assertTrue(assignments["PA0"]["matched"])
        self.assertEqual(assignments["PA1"]["signal"], "UART0.RX")
        gpio = assignments["PB22"]
        self.assertEqual(
            (gpio["peripheral"], gpio["function"], gpio["label"]),
            ("GPIOB", "P22", "PIN_KEY1"),
        )

    def test_a_singleton_module_is_its_own_instance(self):
        # DAC12 没有 addInstance，直接配在模块变量上。
        # DAC12 has no addInstance; the module variable is configured directly.
        self.assertEqual(self.project()["project"]["assignments"]["PA15"]["signal"], "DAC0.OUT")

    def test_the_swd_pins_of_the_board_are_shown(self):
        self.assertEqual(self.project()["project"]["assignments"]["PA19"]["peripheral"], "DEBUGSS")

    def test_the_pins_of_the_clock_tree_are_left_out(self):
        self.assertNotIn("PA5", self.project()["project"]["assignments"])

    def test_a_canfd_member_matches_by_its_suffix(self):
        # TI 的功能名是 CANRX、CANTX，成员名是 rxPin、txPin。
        # TI names the functions CANRX and CANTX; the members are rxPin and txPin.
        syscfg = (
            '//@v2CliArgs --device "MSPM0G3507" --package "LQFP-64(PM)"\n'
            'const MCAN  = scripting.addModule("/ti/driverlib/MCAN", {}, false);\n'
            "const MCAN1 = MCAN.addInstance();\n"
            'MCAN1.peripheral.$assign       = "CANFD0";\n'
            'MCAN1.peripheral.rxPin.$assign = "PA13";\n'
            'MCAN1.peripheral.txPin.$assign = "PA12";\n'
        )
        assignments = self.project(syscfg)["project"]["assignments"]
        self.assertEqual(assignments["PA13"]["signal"], "CANFD0.CANRX")
        self.assertEqual(assignments["PA12"]["signal"], "CANFD0.CANTX")

    def test_a_member_the_pin_does_not_have_is_given_as_it_is(self):
        syscfg = (
            '//@v2CliArgs --device "MSPM0G3507" --package "LQFP-64(PM)"\n'
            'const UART  = scripting.addModule("/ti/driverlib/UART", {}, false);\n'
            "const UART1 = UART.addInstance();\n"
            'UART1.peripheral.$assign       = "UART9";\n'
            'UART1.peripheral.rxPin.$assign = "PA0";\n'
        )
        found = self.project(syscfg)["project"]["assignments"]["PA0"]
        self.assertEqual((found["signal"], found["matched"]), ("UART9.RX", False))

    def test_the_suggested_peripheral_is_used_without_an_assignment(self):
        syscfg = (
            '//@v2CliArgs --device "MSPM0G3507" --package "LQFP-64(PM)"\n'
            'const UART  = scripting.addModule("/ti/driverlib/UART", {}, false);\n'
            "const UART1 = UART.addInstance();\n"
            'UART1.peripheral.$suggestSolution = "UART0";\n'
            'UART1.peripheral.txPin.$assign    = "PA0";\n'
        )
        found = self.project(syscfg)["project"]["assignments"]["PA0"]
        self.assertEqual(found["peripheral"], "UART0")

    def test_dma_channels_are_not_pins(self):
        syscfg = self.PROJECT + 'UART1.DMA_CHANNEL_TX.peripheral.$assign = "DMA_CH3";\n'
        peripherals = self.project(syscfg)["project"]["peripherals"]
        self.assertNotIn("DMA_CH3", peripherals)

    def test_two_syscfg_files_in_the_root_are_an_error(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError) as caught:
            write(Path(directory), "a.syscfg", '//@cliArgs --device "MSPM0G3507"\n')
            write(Path(directory), "b.syscfg", '//@cliArgs --device "MSPM0G3507"\n')
            layout_with_project(directory, None, None, None)
        self.assertTrue(
            str(caught.exception).endswith("several .syscfg files in the root; keep one"),
            str(caught.exception),
        )

    def test_a_syscfg_without_a_device_needs_the_model(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError) as caught:
            write(Path(directory), "a.syscfg", '// @cliArgs --package "LQFP-64(PM)"\n')
            layout_with_project(directory, None, None, None)
        self.assertTrue(
            str(caught.exception).endswith("does not name a device; give the model"),
            str(caught.exception),
        )
        info = self.project('//@cliArgs --package "LQFP-64(PM)"\n', model="MSPM0G3507")
        self.assertEqual(info["part"], "MSPM0G3507")

    def test_libxr_gen_has_no_section_for_an_mspm0(self):
        peripherals = self.project()["project"]["peripherals"]
        self.assertNotIn("config", peripherals["UART0"])

    def test_the_pins_the_solver_picked_are_a_fallback(self):
        # 求解器选的引脚（$suggestSolution）也算数；测试用的 PA0 是 UART0.TX。
        # The pins the solver picked ($suggestSolution) count; PA0 is UART0.TX in the layout.
        syscfg = (
            '//@v2CliArgs --device "MSPM0G3507" --package "LQFP-64(PM)"\n'
            'const UART  = scripting.addModule("/ti/driverlib/UART", {}, false);\n'
            "const UART1 = UART.addInstance();\n"
            'UART1.peripheral.$assign = "UART0";\n'
            'UART1.peripheral.txPin.$suggestSolution = "PA0";\n'
        )
        self.assertEqual(
            self.project(syscfg)["project"]["assignments"]["PA0"]["signal"], "UART0.TX"
        )

    def test_an_assignment_wins_over_the_suggestion(self):
        # 同一个成员既有 $assign 又有 $suggestSolution 时，$assign 赢（PA0 被换成 PA28）。
        # When a member has both $assign and $suggestSolution, $assign wins (PA0 becomes PA28).
        syscfg = (
            '//@v2CliArgs --device "MSPM0G3507" --package "LQFP-64(PM)"\n'
            'const UART  = scripting.addModule("/ti/driverlib/UART", {}, false);\n'
            "const UART1 = UART.addInstance();\n"
            'UART1.peripheral.$assign = "UART0";\n'
            'UART1.peripheral.txPin.$suggestSolution = "PA0";\n'
            'UART1.peripheral.txPin.$assign = "PA28";\n'
        )
        assignments = self.project(syscfg)["project"]["assignments"]
        self.assertEqual(assignments["PA28"]["signal"], "UART0.TX")
        self.assertNotIn("PA0", assignments)


SYSCFG = textwrap.dedent("""\
    /**
     * These arguments were used when this file was generated.
     */
    // @cliArgs --board /ti/boards/LP_MSPM0G3507 --rtos nortos
    const GPIO  = scripting.addModule("/ti/driverlib/GPIO", {}, false);
    const GPIO1 = GPIO.addInstance();
    const UART  = scripting.addModule("/ti/driverlib/UART", {}, false);
    const UART1 = UART.addInstance();
    const SPI   = scripting.addModule("/ti/driverlib/SPI", {}, false);
    const SPI1  = SPI.addInstance();
    const SYSCTL = system.modules["/ti/driverlib/SYSCTL"].$static;

    GPIO1.$name = "GPIO_GRP_0";
    GPIO1.associatedPins[0].assignedPin = "22";

    UART1.$name             = "UART_0";
    UART1.enabledInterrupts = ["RX","TX"];
    UART1.targetBaudRate    = 2000000;
    UART1.enableFIFO        = false;
    UART1.ovsRate           = "3";
    UART1.txPinConfig.$name = "ti_driverlib_gpio_GPIOPinGeneric0";
    UART1.peripheral.$assign = "UART0";
    UART1.peripheral.txPin.$assign = "PA0";

    SPI1.$name = "SPI_0";
    SPI1.targetBitRate = 0x100000;
    SPI1.peripheral.$suggestSolution = "SPI1";

    SYSCTL.powerPolicy = "STANDBY0";
    // UART1.targetBaudRate = 9600;
""")


class HpmProjects(TestCase):
    """HPM 工程：SoC 和封装取自 boards/ 下的 .hpmpc，已选信号取根目录 main.c 无条件调用的 pinmux
    函数。
    An HPM project: the SoC and the package come from the .hpmpc under boards/, and the selected
    signals from the pinmux functions the root main.c calls without a condition.
    """

    HPMPC = json.dumps(
        {
            "content": {
                "info": {"socName": "HPM5301", "packageName": "QFN48"},
                "pinmux": {
                    "functions": {
                        "init_bsp_pins": {
                            "selectPins": {
                                "PA00": {"signal": "UART0.A.TXD", "padCtls": {}},
                                "PA03": {"signal": "I2C0.A.SCL", "padCtls": {}},
                                "PA10": {"signal": "GPIO.A.A[10]", "padCtls": {}},
                            }
                        },
                        "init_uart0_pins": {
                            "selectPins": {
                                "PA00": {"signal": "UART0.A.TXD", "padCtls": {}},
                                "PA01": {"signal": "UART0.A.RXD", "padCtls": {}},
                            }
                        },
                        "init_jtag_pins": {
                            "selectPins": {"PA04": {"signal": "GPIO.A.A[04]", "padCtls": {}}}
                        },
                    }
                },
            }
        }
    )
    MAIN = textwrap.dedent(
        """\
        int main()
        {
          board_init();
          init_bsp_pins();
          init_uart0_pins();
        #if !RMCS_KEEP_JTAG
          init_jtag_pins();
        #endif
        }
        """
    )

    def project(self, files):
        """给定文件的 HPM 工程的叠加结果。
        The overlay of an HPM project with these files.
        """
        with tempfile.TemporaryDirectory() as directory:
            for name, text in files.items():
                write(Path(directory), name, text)
            return layout_with_project(directory, None, None, None)

    def project_files(self, main=MAIN):
        """一个最小 HPM 工程的文件。
        The files of a minimal HPM project.
        """
        return {
            "app.yaml": "dependency: []\n",
            "boards/board/tool_config.hpmpc": self.HPMPC,
            "main.c": main,
        }

    def test_the_soc_the_package_and_the_called_functions_drive_the_overlay(self):
        info = self.project(self.project_files())
        self.assertEqual((info["part"], info["package"]), ("HPM5301", "QFN48"))
        self.assertEqual(info["project"]["source"], "boards/board/tool_config.hpmpc")
        assignments = info["project"]["assignments"]
        self.assertEqual(assignments["PA00"]["signal"], "UART0_TXD")
        self.assertTrue(assignments["PA00"]["matched"])
        self.assertEqual(assignments["PA01"]["signal"], "UART0_RXD")
        gpio = assignments["PA10"]
        self.assertEqual(
            (gpio["peripheral"], gpio["kind"], gpio["function"], gpio["matched"]),
            ("GPIOA", "GPIO", "P10", True),
        )

    def test_a_call_inside_a_conditional_is_not_active(self):
        # O2：条件编译里的函数不算，PA04 不出现。
        # O2: a function inside a conditional does not count; PA04 stays out.
        self.assertNotIn("PA04", self.project(self.project_files())["project"]["assignments"])

    def test_without_a_main_c_the_bsp_function_is_the_fallback(self):
        files = {key: value for key, value in self.project_files().items() if key != "main.c"}
        assignments = self.project(files)["project"]["assignments"]
        self.assertEqual(assignments["PA03"]["signal"], "I2C0_SCL")
        self.assertNotIn("PA01", assignments)

    def test_two_hpmpc_files_are_an_error(self):
        files = self.project_files()
        files["boards/other/tool_config.hpmpc"] = files["boards/board/tool_config.hpmpc"]
        with self.assertRaises(ValueError) as caught:
            self.project(files)
        self.assertTrue(
            str(caught.exception).endswith("several .hpmpc files under boards/; keep one"),
            str(caught.exception),
        )

    def test_a_hpmpc_without_a_soc_needs_the_model(self):
        hpmpc = json.loads(self.HPMPC)
        del hpmpc["content"]["info"]["socName"]
        files = self.project_files()
        files["boards/board/tool_config.hpmpc"] = json.dumps(hpmpc)
        with self.assertRaises(ValueError) as caught:
            self.project(files)
        self.assertTrue(
            str(caught.exception).endswith("does not name a SoC; give the model"),
            str(caught.exception),
        )


class SysconfigSettings(TestCase):
    """MSPM0 外设的设置取自 .syscfg，只读。
    The settings of an MSPM0 peripheral come from the .syscfg, read-only.
    """

    def test_the_settings_of_each_peripheral_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write(Path(directory), "a.syscfg", SYSCFG)
            settings = pin_project.read_syscfg_settings(path)
        self.assertEqual(
            settings["UART0"],
            {
                "module": "UART",
                "name": "UART_0",
                "params": {
                    "enabledInterrupts": ["RX", "TX"],
                    "targetBaudRate": 2000000,
                    "enableFIFO": False,
                    "ovsRate": "3",
                },
            },
        )

    def test_a_solver_suggestion_names_the_peripheral_and_a_hex_number_is_a_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write(Path(directory), "a.syscfg", SYSCFG)
            settings = pin_project.read_syscfg_settings(path)
        self.assertEqual(settings["SPI1"]["params"], {"targetBitRate": 0x100000})

    def test_what_is_not_a_setting_is_left_out(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write(Path(directory), "a.syscfg", SYSCFG)
            settings = pin_project.read_syscfg_settings(path)
        # Pin assignments, the solver's $ items, comments, and what has no peripheral of its own
        # (a GPIO group, a static module).
        self.assertNotIn("txPinConfig.$name", settings["UART0"]["params"])
        self.assertEqual(sorted(settings), ["SPI1", "UART0"])
        self.assertEqual(settings["UART0"]["params"]["targetBaudRate"], 2000000)

    def test_an_expression_is_kept_as_written(self):
        text = (
            'const UART = scripting.addModule("/ti/driverlib/UART", {}, false);\n'
            "const UART1 = UART.addInstance();\n"
            "UART1.targetBaudRate = 2 * 1000000;\n"
            'UART1.peripheral.$assign = "UART0";\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = write(Path(directory), "a.syscfg", text)
            settings = pin_project.read_syscfg_settings(path)
        self.assertEqual(settings["UART0"]["params"], {"targetBaudRate": "2 * 1000000"})

    def overlay(self, files):
        """给定文件的 MSPM0 工程的叠加结果。
        The overlay of an MSPM0 project with these files.
        """
        with tempfile.TemporaryDirectory() as directory:
            for name, text in files.items():
                write(Path(directory), name, text)
            return layout_with_project(directory, None, "PM", None)["project"]

    def test_a_used_peripheral_carries_its_sysconfig_settings(self):
        project = self.overlay({"a.syscfg": SYSCFG})
        self.assertEqual(project["sysconfig_file"], "a.syscfg")
        uart = project["peripherals"]["UART0"]["sysconfig"]
        self.assertEqual((uart["module"], uart["name"]), ("UART", "UART_0"))
        self.assertEqual(uart["params"]["targetBaudRate"], 2000000)
        # A peripheral without pins is not listed: SPI1 has no pin of its own here.
        self.assertNotIn("SPI1", project["peripherals"])


class NoProject(TestCase):
    """没有可识别的工程时报错。
    A directory without a recognizable project is an error.
    """

    def test_an_empty_directory_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError) as caught:
            layout_with_project(directory, None, None, None)
        self.assertTrue(
            str(caught.exception).endswith(
                ": no STM32CubeMX .ioc, HPM .hpmpc or root SysConfig .syscfg found"
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
                    f"{directory}: no STM32CubeMX .ioc, HPM .hpmpc or root SysConfig .syscfg found",
                ),
            ):
                with self.subTest(argv=argv), self.assertLogs(level="ERROR") as logs:
                    code, out, _ = run_libxr(*argv)
                    self.assertEqual((code, out), (1, ""))
                    self.assertEqual(logs.output, [f"ERROR:root:{reason}"])


if __name__ == "__main__":
    unittest.main()
