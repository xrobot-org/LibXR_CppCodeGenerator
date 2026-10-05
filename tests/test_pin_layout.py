"""libxr pins（libxr.pin_layout）：由型号给出封装和引脚布局。
libxr pins (libxr.pin_layout): the package and pin layout of a model.
"""

import json
import re
import unittest

import yaml
from fixtures import TestCase, run_libxr

from libxr import pin_layout
from libxr.pin_layout import layout_pins, layout_to_dict


class Stm32Models(TestCase):
    """STM32 型号按 ST 的数据解析：温度等级、分组文件名和后缀都能对上。
    STM32 models resolve against ST's data: the temperature grade, grouped file names and
    suffixes all match.
    """

    def test_the_temperature_grade_is_not_part_of_the_data_name(self):
        layout = layout_pins("stm32h723vgt6")
        self.assertEqual((layout.part, layout.package), ("STM32H723VGTx", "LQFP100"))
        self.assertEqual(len(layout.pins), 100)
        first = layout.pins[0]
        self.assertEqual((first.position, first.name), ("1", "PE2"))
        self.assertIn("USART10_RX", first.signals)

    def test_grouped_file_names_cover_every_model_of_the_group(self):
        # ST 把 STM32F407IE 和 STM32F407IG 放在同一个文件 STM32F407I(E-G)Hx.xml 中。
        # ST keeps STM32F407IE and STM32F407IG in one file, STM32F407I(E-G)Hx.xml.
        for model in ("STM32F407IGH6", "STM32F407IEH6"):
            with self.subTest(model=model):
                layout = layout_pins(model)
                self.assertEqual(layout.package, "UFBGA176")
                self.assertEqual(layout_to_dict(layout)["pin_count"], 201)

    def test_a_suffix_after_the_temperature_grade_is_kept(self):
        self.assertEqual(layout_pins("STM32U5G9ZJT6Q").part, "STM32U5G9ZJTxQ")
        self.assertEqual(layout_pins("STM32G0B1CBT6N").part, "STM32G0B1CBTxN")

    def test_remapped_pins_share_one_position(self):
        # STM32G0 的 PA9 和 PA11 可以互换，同一个封装位置上有两个条目。
        # PA9 and PA11 of an STM32G0 can be swapped, so one package position has two entries.
        layout = layout_pins("STM32G0B1CBT6N")
        at_33 = [pin.name for pin in layout.pins if pin.position == "33"]
        self.assertEqual(sorted(at_33), ["PA11 [PA9]", "PA9 [PA11]"])
        self.assertEqual(layout_to_dict(layout)["pin_count"], 48)
        self.assertGreater(len(layout.pins), 48)

    def test_a_given_package_must_be_the_one_of_the_model(self):
        self.assertEqual(layout_pins("STM32H723VGT6", "lqfp100").package, "LQFP100")
        with self.assertRaises(ValueError) as caught:
            layout_pins("STM32H723VGT6", "LQFP64")
        self.assertEqual(
            str(caught.exception),
            "STM32H723VGT6 comes in LQFP100, not LQFP64; the package is part of the STM32 model",
        )

    def test_an_unknown_model_names_similar_ones(self):
        with self.assertRaises(ValueError) as caught:
            layout_pins("STM32H723VGT6X9")
        self.assertTrue(
            str(caught.exception).startswith("Unknown STM32 model: STM32H723VGT6X9; similar: "),
            str(caught.exception),
        )
        with self.assertRaises(ValueError) as caught:
            layout_pins("STM32Q999RGT6")
        self.assertEqual(str(caught.exception), "Unknown STM32 model: STM32Q999RGT6")


class Mspm0Models(TestCase):
    """MSPM0 型号按器件族和封装代码解析，每个引脚带 PINCM 和各信号的模式号。
    MSPM0 models resolve by family and package code; every pin carries its PINCM and the mode of
    each signal.
    """

    def test_the_package_code_in_the_model_chooses_the_package(self):
        layout = layout_pins("MSPM0G3507SPMR")
        self.assertEqual((layout.part, layout.package), ("MSPM0G3507", "LQFP-64(PM)"))
        self.assertEqual(layout_to_dict(layout)["pin_count"], 64)
        pin = next(pin for pin in layout.pins if pin.position == "33")
        self.assertEqual(pin.name, "PA0")
        self.assertEqual(pin.extra["iomux_pincm"], 1)
        self.assertEqual(pin.extra["modes"]["UART0.TX"], 2)
        self.assertEqual(pin.extra["modes"]["I2C0.SDA"], 3)

    def test_a_package_can_be_given_in_three_spellings(self):
        for spelling in ("LQFP-64(PM)", "LQFP-64", "pm"):
            with self.subTest(package=spelling):
                self.assertEqual(layout_pins("MSPM0G3507", spelling).package, "LQFP-64(PM)")

    def test_a_model_without_a_package_asks_for_one(self):
        with self.assertRaises(ValueError) as caught:
            layout_pins("MSPM0G3507")
        self.assertEqual(
            str(caught.exception),
            "Cannot tell the package of MSPM0G3507; give it with --package "
            "(LQFP-64(PM), VQFN-48(RGZ), LQFP-48(PT), VSSOP-28(DGS28), VQFN-32(RHB))",
        )
        with self.assertRaises(ValueError) as caught:
            layout_pins("MSPM0G3507", "BGA")
        self.assertTrue(
            str(caught.exception).startswith("MSPM0G3507 has no package BGA; this family has "),
            str(caught.exception),
        )

    def test_the_longest_family_prefix_wins(self):
        # MSPM0C1105 有自己的器件族，不属于 MSPM0C110X。
        # MSPM0C1105 has a family of its own and is not part of MSPM0C110X.
        layout = layout_pins("MSPM0C1105", "VQFN-32")
        self.assertEqual((layout.part, layout.package), ("MSPM0C1105", "VQFN-32(RHB)"))
        self.assertEqual(layout_pins("MSPM0C1103", "VSSOP-20").part, "MSPM0C1103")

    def test_an_unknown_model_is_an_error(self):
        with self.assertRaises(ValueError) as caught:
            layout_pins("MSPM0Z9999")
        self.assertEqual(str(caught.exception), "Unknown MSPM0 model: MSPM0Z9999")


class Dispatch(TestCase):
    """型号的前缀选平台。
    The prefix of the model chooses the platform.
    """

    def test_the_platform_follows_the_prefix(self):
        self.assertEqual(layout_pins("STM32F103C8T6").platform, "stm32")
        self.assertEqual(layout_pins("MSPM0G3507SPMR").platform, "mspm0")

    def test_an_unsupported_model_lists_the_platforms(self):
        with self.assertRaises(ValueError) as caught:
            layout_pins("CH32V203C8T6")
        self.assertEqual(
            str(caught.exception),
            "No supported platform for the model CH32V203C8T6 (supported: STM32, MSPM0)",
        )

    def test_the_output_fields_are_the_same_for_every_platform(self):
        for model in ("STM32F103C8T6", "MSPM0G3507SPMR"):
            with self.subTest(model=model):
                info = layout_to_dict(layout_pins(model))
                self.assertEqual(
                    list(info),
                    [
                        "model",
                        "platform",
                        "part",
                        "package",
                        "pin_count",
                        "source",
                        "peripherals",
                        "pins",
                    ],
                )
                self.assertLessEqual({"position", "name", "type", "signals"}, set(info["pins"][0]))


class Recognition(TestCase):
    """外设从信号名识别，与 LibXR 是否有对应的外设无关。
    Peripherals are recognized from the signal names, whether or not LibXR has a matching
    peripheral.
    """

    def test_st_signal_names(self):
        for signal, expected in (
            ("USART1_TX", ("USART1", "USART", "TX")),
            ("I2C2_SCL", ("I2C2", "I2C", "SCL")),
            ("FDCAN1_RX", ("FDCAN1", "FDCAN", "RX")),
            ("ETH_MII_TXD1", ("ETH", "ETH", "MII_TXD1")),
            ("FMC_A23", ("FMC", "FMC", "A23")),
            ("TIM2_CH1", ("TIM2", "TIM", "CH1")),
            ("ADC1_INP19", ("ADC1", "ADC", "INP19")),
            ("USB_OTG_HS_DM", ("USB_OTG_HS", "USB_OTG", "DM")),
            ("USB_DRD_FS_DP", ("USB_DRD_FS", "USB_DRD", "DP")),
            ("OCTOSPIM_P1_IO2", ("OCTOSPIM_P1", "OCTOSPIM", "IO2")),
            ("CEC", ("CEC", "CEC", "CEC")),
            ("BOOT0", ("BOOT0", "BOOT", "BOOT0")),
        ):
            with self.subTest(signal=signal):
                self.assertEqual(pin_layout.recognize_stm32(signal), expected)
        self.assertIsNone(pin_layout.recognize_stm32("GPIO"))

    def test_ti_signal_names(self):
        for signal, expected in (
            ("UART0.TX", ("UART0", "UART", "TX")),
            ("TIMA0.CCP0", ("TIMA0", "TIMA", "CCP0")),
            ("SYSCTL.FCC_IN", ("SYSCTL", "SYSCTL", "FCC_IN")),
            ("COMP2.IN0-", ("COMP2", "COMP", "IN0-")),
        ):
            with self.subTest(signal=signal):
                self.assertEqual(pin_layout.recognize_mspm0(signal), expected)
        for name in ("PA0", "PB22", "PA17/PA14"):
            with self.subTest(name=name):
                self.assertIsNone(pin_layout.recognize_mspm0(name))

    def test_peripherals_without_a_libxr_object_are_recognized(self):
        # LibXR 没有 ETH 和 OctoSPI 的抽象，它们仍然要被认出来，并给出可选的引脚。
        # LibXR has no abstraction for ETH or OctoSPI; they are still recognized, with their pins.
        peripherals = layout_to_dict(layout_pins("STM32H723VGT6"))["peripherals"]
        self.assertEqual(peripherals["ETH"]["kind"], "ETH")
        self.assertEqual(peripherals["ETH"]["signals"]["CRS"], ["PA0"])
        self.assertIn("IO2", peripherals["OCTOSPIM_P1"]["signals"])
        self.assertEqual(peripherals["USB_OTG_HS"]["signals"]["DM"], ["PA11"])

    def test_a_function_lists_the_pins_that_can_carry_it(self):
        usart1 = layout_to_dict(layout_pins("STM32H723VGT6"))["peripherals"]["USART1"]
        self.assertEqual(sorted(usart1["signals"]["TX"]), ["PA9", "PB14", "PB6"])
        uart0 = layout_to_dict(layout_pins("MSPM0G3507SPMR"))["peripherals"]["UART0"]
        self.assertEqual(sorted(uart0["signals"]["TX"]), ["PA0", "PA10", "PA28", "PB0"])

    def test_plain_gpio_is_listed_by_port_and_line(self):
        for model, pin in (("STM32H723VGT6", "PA9"), ("MSPM0G3507SPMR", "PA9")):
            with self.subTest(model=model):
                gpio_a = layout_to_dict(layout_pins(model))["peripherals"]["GPIOA"]
                self.assertEqual(gpio_a["kind"], "GPIO")
                self.assertEqual(gpio_a["signals"]["P9"], [pin])

    def test_external_interrupt_lines_list_the_pins_that_can_use_them(self):
        # 线号等于引脚号：LINE9 可以来自任一端口的第 9 脚（ST 数据中 GPIO 带 EXTI 模式）。
        # The line number is the pin number: LINE9 can come from pin 9 of any port (the GPIO has
        # the EXTI mode in ST's data).
        peripherals = layout_to_dict(layout_pins("STM32H723VGT6"))["peripherals"]
        self.assertEqual(
            sorted(peripherals["EXTI"]["signals"]["LINE9"]), ["PA9", "PB9", "PC9", "PD9", "PE9"]
        )
        info = layout_to_dict(layout_pins("STM32H723VGT6"))
        modes = {pin["name"]: pin.get("gpio_modes") for pin in info["pins"]}
        self.assertIn("EXTI", modes["PE2"])
        self.assertIsNone(modes["NRST"])

    def test_the_ti_data_does_not_say_which_pins_have_interrupts(self):
        # TI 的器件数据没有这一项，所以不列出 EXTI，而不是猜测。
        # TI's device data has no such field, so EXTI is not listed rather than guessed.
        peripherals = layout_to_dict(layout_pins("MSPM0G3507SPMR"))["peripherals"]
        self.assertNotIn("EXTI", peripherals)

    def test_timers_with_output_channels_are_marked_pwm(self):
        peripherals = layout_to_dict(layout_pins("STM32H723VGT6"))["peripherals"]
        self.assertEqual(peripherals["TIM1"]["capabilities"], ["pwm"])
        self.assertEqual(peripherals["LPTIM1"]["capabilities"], ["pwm"])
        self.assertNotIn("capabilities", peripherals["USART1"])
        ti = layout_to_dict(layout_pins("MSPM0G3507SPMR"))["peripherals"]
        self.assertEqual(ti["TIMA0"]["capabilities"], ["pwm"])
        self.assertNotIn("capabilities", ti["UART0"])

    def test_peripherals_sort_naturally(self):
        names = list(layout_to_dict(layout_pins("STM32H723VGT6"))["peripherals"])
        self.assertLess(names.index("USART2"), names.index("USART10"))

    def test_every_signal_in_the_data_is_recognized_except_plain_gpio(self):
        # ST 的 GPIO 和 TI 的引脚名（PA0、PA17/PA14）是引脚的普通用法，其余都是外设信号。
        # ST's GPIO and TI's pin names (PA0, PA17/PA14) are the plain use of the pin; the rest
        # are peripheral signals.
        st = pin_layout.load_data("stm32")["parts"]
        for shard in {shard for shard, _ in st.values()}:
            for pin_set in pin_layout.load_stm32_shard(shard).values():
                for _, _, _, signals, _ in pin_set["pins"]:
                    for signal in signals:
                        if signal != "GPIO":
                            self.assertIsNotNone(pin_layout.recognize_stm32(signal), signal)
        for family in pin_layout.load_data("mspm0")["families"].values():
            for pins in family["packages"].values():
                for _, _, _, _, signals in pins:
                    for signal, _ in signals:
                        if not re.fullmatch(r"P[A-C]\d+(/P[A-C]\d+)?", signal):
                            self.assertIsNotNone(pin_layout.recognize_mspm0(signal), signal)


class DataFiles(TestCase):
    """数据带着来源和许可证文本一起分发。
    The data is distributed with its source and the license texts.
    """

    def test_every_data_set_records_its_source_and_license(self):
        for platform, vendor in (("stm32", "STMicroelectronics"), ("mspm0", "Texas Instruments")):
            with self.subTest(platform=platform):
                source = pin_layout.load_data(platform)["source"]
                self.assertEqual(source["vendor"], vendor)
                self.assertTrue(source["license"])
        self.assertRegex(pin_layout.load_data("stm32")["source"]["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(pin_layout.load_data("mspm0")["source"]["version"], r"^\d+\.\d+\.\d+")

    def test_the_license_texts_are_next_to_the_data(self):
        st = (pin_layout.DATA / "LICENSE-ST.txt").read_text(encoding="utf-8")
        ti = (pin_layout.DATA / "LICENSE-TI.txt").read_text(encoding="utf-8")
        self.assertIn("BSD 3-Clause License", st)
        self.assertIn("STMicroelectronics", st)
        self.assertIn("Texas Instruments Incorporated", ti)
        self.assertIn("TI Devices", ti)

    def test_every_part_points_to_a_pin_set_in_an_existing_shard(self):
        parts = pin_layout.load_data("stm32")["parts"]
        for shard in {shard for shard, _ in parts.values()}:
            self.assertTrue((pin_layout.DATA / f"stm32-{shard}.json.gz").is_file(), shard)
        for name, (shard, key) in parts.items():
            with self.subTest(part=name):
                self.assertIn(key, pin_layout.load_stm32_shard(shard))

    def test_a_layout_reads_only_the_shard_of_its_series(self):
        pin_layout.load_stm32_shard.cache_clear()
        layout_pins("STM32H723VGT6")
        self.assertEqual(pin_layout.load_stm32_shard.cache_info().currsize, 1)

    def test_the_data_files_are_declared_as_package_data(self):
        project = (pin_layout.DATA.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"pin_data/*"', project)


class CommandLine(TestCase):
    """布局写到标准输出，报错以错误日志给出。
    The layout goes to stdout, and errors are logged.
    """

    def run_pins(self, *argv):
        """以这些参数运行 libxr pins，返回退出码、标准输出和标准错误。
        Run libxr pins with these arguments; return the exit code, stdout and stderr.
        """
        return run_libxr("pins", *argv)

    def test_the_layout_is_yaml_by_default(self):
        code, out, err = self.run_pins("STM32F103C8T6")
        self.assertEqual((code, err), (0, ""))
        info = yaml.safe_load(out)
        self.assertEqual(
            (info["model"], info["platform"], info["package"], info["pin_count"]),
            ("STM32F103C8T6", "stm32", "LQFP48", 48),
        )
        self.assertEqual(len(info["pins"]), 48)

    def test_the_layout_can_be_json(self):
        code, out, err = self.run_pins("MSPM0G3507SPMR", "--format", "json")
        self.assertEqual((code, err), (0, ""))
        info = json.loads(out)
        self.assertEqual((info["package"], info["pin_count"]), ("LQFP-64(PM)", 64))
        self.assertEqual(info["pins"][32]["name"], "PA0")

    def test_the_package_can_be_given(self):
        code, out, _ = self.run_pins("MSPM0G3507", "-p", "VQFN-48")
        self.assertEqual(code, 0)
        self.assertEqual(yaml.safe_load(out)["package"], "VQFN-48(RGZ)")

    def test_help_and_wrong_usage(self):
        code, out, err = self.run_pins("--help")
        self.assertEqual((code, err), (0, ""))
        usage = "usage: libxr pins [-h] [-p PACKAGE] [-f {yaml,json}] [--verbose] model\n"
        self.assertTrue(out.startswith(usage), out)
        self.assertIn("libxr pins STM32H723VGT6", out)
        code, out, err = self.run_pins()
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith(usage), err)

    def test_errors_are_logged(self):
        for argv, reason in (
            (("CH32V203C8T6",), pin_layout_error("CH32V203C8T6")),
            (("STM32Q999RGT6",), "Unknown STM32 model: STM32Q999RGT6"),
            (
                ("STM32H723VGT6", "-p", "LQFP64"),
                "STM32H723VGT6 comes in LQFP100, not LQFP64; the package is part of the STM32 "
                "model",
            ),
        ):
            with self.subTest(argv=argv), self.assertLogs(level="ERROR") as logs:
                code, out, _ = self.run_pins(*argv)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(logs.output, [f"ERROR:root:{reason}"])


def pin_layout_error(model):
    """没有支持的平台时的报错。
    The error for a model without a supported platform.
    """
    return f"No supported platform for the model {model} (supported: STM32, MSPM0)"


if __name__ == "__main__":
    unittest.main()
