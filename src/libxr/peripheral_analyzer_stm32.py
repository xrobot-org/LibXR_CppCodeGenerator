#!/usr/bin/env python
"""libxr parse 的 STM32 解析器：把 STM32CubeMX 的 .ioc 文件解析成 libxr gen 读取的 YAML 配置。
The STM32 parser of libxr parse: parses an STM32CubeMX .ioc file into the YAML configuration
that libxr gen reads.

每类外设由一个 PeripheralParser 子类从 .ioc 的 key=value 表中读取，结果汇总到
ConfigurationManager，清理后写成 .config.yaml。
Each kind of peripheral is read from the key=value map of the .ioc file by a PeripheralParser
subclass; the results are collected in a ConfigurationManager, cleaned and written as
.config.yaml.
"""

import logging
import os
import re
import sys
from collections import defaultdict
from re import Pattern
from typing import Any, TextIO

import yaml
from xr_syntax.i18n import tr


# --------------------------
# 工具函数 / Utility Functions
# --------------------------
def sanitize_numeric(value: str) -> int | float | str:
    """把字符串转成数值：全是数字时为 int，其余能解析的为 float，都不能解析时原样返回。
    Convert a string to a number: all digits give an int, other parseable text a float, and
    anything else is returned unchanged.
    """
    try:
        return int(value) if value.isdigit() else float(value)
    except ValueError:
        return value


# --------------------------
# 配置容器 / Configuration Containers
# --------------------------
class ConfigurationManager:
    """保存各解析器写入的配置：引脚、外设、DMA、FreeRTOS、ThreadX、时基和 MCU 信息。
    Holds the configuration the parsers write: pins, peripherals, DMA, FreeRTOS, ThreadX, the
    timebase and the MCU.
    """

    def __init__(self) -> None:
        """创建空的配置容器；时基默认为 SysTick。
        Create empty configuration containers; the timebase defaults to SysTick.
        """
        self.pin_registry: defaultdict[str, dict[str, Any]] = defaultdict(dict)
        self.peripherals: defaultdict[str, defaultdict[str, dict]] = defaultdict(
            lambda: defaultdict(dict)
        )
        self.dma_types: dict[str, str] = {}
        self.dma_requests: dict[str, str] = {}
        self.dma_configs: dict[str, dict] = {}
        self.freertos = False
        self.threadx = False
        self.timebase: dict[str, str | None] = {"Source": "SysTick", "IRQ": None}
        self.mcu_config: dict[str, str | None] = {"Family": None, "Type": None}

    def clean_structure(self) -> dict[str, Any]:
        """返回写入 YAML 的最终结构：Platform、GPIO、Peripherals、DMA、Timebase 和 Mcu。
        Return the final structure written to YAML: Platform, GPIO, Peripherals, DMA, Timebase
        and Mcu.

        Platform 为 stm32，libxr gen 据此选择生成器。识别到 ThreadX 时加入 ThreadX 段，识别到
        FreeRTOS 时加入 FreeRTOS 段（见 ThreadXParser 和 FreeRTOSParser），两段都只有
        Enabled: true；libxr gen 按这两段选择 LibXR 的系统。
        Platform is stm32, from which libxr gen selects the generator. A ThreadX section is added
        when ThreadX is recognized and a FreeRTOS section when FreeRTOS is (see ThreadXParser
        and FreeRTOSParser), each holding only Enabled: true; libxr gen selects the LibXR system
        from these sections.
        """
        cleaned_data = {
            "Platform": "stm32",
            "GPIO": self._clean_gpio(),
            "Peripherals": self._clean_peripherals(),
            "DMA": {
                "Requests": self.dma_requests,
                "Configurations": self._clean_dma_configs(),
            },
            "Timebase": self.timebase,
            "Mcu": self.mcu_config,
        }

        if self.threadx:
            cleaned_data["ThreadX"] = {"Enabled": True}
        if self.freertos:
            cleaned_data["FreeRTOS"] = {"Enabled": True}
        return cleaned_data

    def _clean_gpio(self) -> dict[str, dict]:
        """GPIO 段：只含 GPIO 输入、输出和 GPXTI 外部中断引脚，字段限于 Signal、Label、Pull
        和 GPXTI。
        The GPIO section: only GPIO input, output and GPXTI external interrupt pins, with the
        fields Signal, Label, Pull and GPXTI.
        """
        return {
            pin: {k: v for k, v in config.items() if k in {"Signal", "Label", "Pull", "GPXTI"}}
            for pin, config in self.pin_registry.items()
            if self._is_valid_gpio(config)
        }

    def _is_valid_gpio(self, config: dict) -> bool:
        """引脚信号为 GPIO_Output、GPIO_Input 或以 GPXTI 开头时为真。
        True when the pin signal is GPIO_Output, GPIO_Input or starts with GPXTI.
        """
        return config.get("Signal") in {"GPIO_Output", "GPIO_Input"} or config.get(
            "Signal", ""
        ).startswith("GPXTI")

    def _clean_peripherals(self) -> dict[str, dict]:
        """Peripherals 段：按外设类型和实例名组织，每个实例去掉空值字段，没有实例的类型不写出。
        The Peripherals section, by peripheral type and instance name, with empty fields
        removed from each instance and types without instances left out.
        """
        return {
            p_type: {p: self._clean_peripheral_config(cfg) for p, cfg in p_group.items()}
            for p_type, p_group in self.peripherals.items()
            if p_group
        }

    def _clean_peripheral_config(self, config: dict) -> dict:
        """去掉值为 None、空字符串、空列表或空字典的字段。
        Drop the fields whose value is None, an empty string, an empty list or an empty dict.
        """
        return {k: v for k, v in config.items() if v not in (None, "", [], {})}

    def _clean_dma_configs(self) -> dict[str, dict]:
        """DMA 配置中非空的条目。
        The DMA configurations that are not empty.
        """
        return {k: v for k, v in self.dma_configs.items() if v}


# --------------------------
# 解析器基类 / Base Parser Class
# --------------------------
class PeripheralParser:
    """外设解析器基类：提供 .ioc key 的拆分与归一化工具和 GPIO 解析，子类实现 parse。
    Base class of the peripheral parsers: helpers that split and normalize .ioc keys, GPIO
    parsing, and a parse method that each subclass implements.
    """

    # 端口字母到 Z：STM32N6 有 PN～PQ 端口。
    # Port letters up to Z: STM32N6 has ports PN to PQ.
    _PIN_PROPERTY_PATTERN = re.compile(r"^((?:P[A-Z]\d+)[^.]*)\.(Signal|GPIO_Label|GPIO_PuPd)$")
    _PERIPHERAL_ROOT_PATTERN = re.compile(
        r"^((?:USART|LPUART|UART|I2C|SPI|TIM|LPTIM|HRTIM|ADC|DAC|FDCAN|CAN|USB)\d*)"
    )

    def __init__(
        self,
        config: ConfigurationManager,
        raw_map: dict[str, str],
        gpio_pattern: Pattern = _PIN_PROPERTY_PATTERN,
    ) -> None:
        """绑定要写入的配置和 .ioc 文件的 key=value 表。
        Bind the configuration to write to and the key=value map of the .ioc file.

        gpio_pattern 匹配引脚属性 key，默认匹配 PxN 引脚的 Signal、GPIO_Label 和 GPIO_PuPd。
        gpio_pattern matches pin property keys; by default the Signal, GPIO_Label and GPIO_PuPd
        of PxN pins.
        """
        self.config = config
        self.raw_map = raw_map
        self.gpio_pattern = gpio_pattern

    @staticmethod
    def _split_ioc_key(key: str) -> list[str]:
        """按点号拆分 .ioc 属性 key，各段保持原样。
        Split an .ioc property key at dots, keeping each token verbatim.
        """
        return str(key).split(".")

    @staticmethod
    def _ioc_key_root(key: str) -> str:
        """.ioc 属性 key 的第一段。
        The first token of an .ioc property key.
        """
        return PeripheralParser._split_ioc_key(key)[0]

    @staticmethod
    def _ioc_key_prop(key: str, default: str | None = None) -> str | None:
        """.ioc 属性 key 的第二段；key 只有一段时为 default。
        The second token of an .ioc property key; default when the key has a single token.
        """
        parts = PeripheralParser._split_ioc_key(key)
        return parts[1] if len(parts) > 1 else default

    @staticmethod
    def _has_ioc_prefix(key: str, prefix: str) -> bool:
        """key 等于 prefix 或以 "prefix." 开头时为真，即前缀只在段边界处匹配。
        True when the key equals prefix or starts with "prefix.", so the prefix matches only on
        a token boundary.
        """
        key = str(key)
        return key == prefix or key.startswith(f"{prefix}.")

    @staticmethod
    def _ioc_root_startswith(key: str, stem: str) -> bool:
        """key 的第一段以 stem 开头时为真。
        True when the first dot-separated token of the key starts with stem.
        """
        return PeripheralParser._ioc_key_root(key).startswith(stem)

    @staticmethod
    def _ioc_key_startswith(key: str, stem: str) -> bool:
        """整个 key 以 stem 开头时为真，用于 Mcu.IP0、Mcu.IPNb 这类带编号的 key。
        True when the whole key starts with stem; used for numbered keys such as Mcu.IP0 and
        Mcu.IPNb.
        """
        return str(key).startswith(stem)

    @staticmethod
    def _dma_request_id(key: str, prefix: str) -> str | None:
        """<prefix>.RequestN 形式的 key（如 Dma.Request0、Bdma.Request1）中的请求号 N；
        其他 key 为 None。
        The request id N of a <prefix>.RequestN key such as Dma.Request0 or Bdma.Request1;
        None for any other key.
        """
        match = re.fullmatch(rf"{re.escape(prefix)}\.Request(\d+)", str(key))
        return match.group(1) if match else None

    @staticmethod
    def _dma_request_key(prefix: str, req_id: str) -> str:
        """组成 "<prefix>.Request<req_id>" 形式的请求 key，使 DMA 与 BDMA 的同号请求互不冲突。
        Build a "<prefix>.Request<req_id>" request key, so DMA and BDMA requests with the same
        number stay apart.
        """
        return f"{prefix}.Request{req_id}"

    @staticmethod
    def _normalize_gpio_pin_token(pin: str) -> str:
        """把 CubeMX 引脚名归一为物理引脚名 PxN，例如 PC14-OSC32_IN 变为 PC14；不匹配时原样返回。
        Normalize a CubeMX pin token to the physical PxN name, e.g. PC14-OSC32_IN becomes PC14;
        a token that does not match is returned unchanged.
        """
        match = re.match(r"^(P[A-Z]\d+)", pin)
        return match.group(1) if match else pin

    @staticmethod
    def _normalize_ioc_key_pin(key: str) -> str:
        """取 .ioc 属性 key 的第一段并归一为 PxN 引脚名。
        Take the first token of an .ioc property key and normalize it to a PxN pin name.
        """
        return PeripheralParser._normalize_gpio_pin_token(PeripheralParser._ioc_key_root(key))

    @staticmethod
    def _normalize_signal_token(signal: str) -> str:
        """推导外设名之前归一 CubeMX 信号名：去掉首尾空白，转为大写，并去掉 S_ 别名前缀。
        Normalize a CubeMX signal name before peripheral names are derived from it: strip
        whitespace, convert to upper case and remove the S_ alias prefix.
        """
        signal = str(signal).strip().upper()
        return signal[2:] if signal.startswith("S_") else signal

    @staticmethod
    def _normalize_tim_channel_token(channel: str) -> str | None:
        """把 TIM_CHANNEL_x 或 CHx（均可带 N 后缀）归一为 CHx / CHxN；其他写法为 None。
        Normalize TIM_CHANNEL_x or CHx, each with an optional N suffix, to CHx / CHxN; None for
        any other form.
        """
        channel = str(channel).strip().upper()
        match = re.fullmatch(r"TIM_CHANNEL_(\d+)(N?)", channel)
        if match:
            return f"CH{match.group(1)}{match.group(2)}"
        match = re.fullmatch(r"CH(\d+)(N?)", channel)
        if match:
            return f"CH{match.group(1)}{match.group(2)}"
        return None

    @staticmethod
    def _signal_root(signal: str) -> str:
        """CubeMX 信号名中的外设实例名；已知外设按前缀匹配，其余取第一个下划线之前的部分。
        The peripheral instance in a CubeMX signal name; known peripherals match by prefix, and
        other names give the text before the first underscore.

        例如 USART1_TX 为 USART1，I2C2_SCL 为 I2C2，TIM1_CH1N 为 TIM1。
        For example USART1_TX gives USART1, I2C2_SCL gives I2C2 and TIM1_CH1N gives TIM1.
        """
        signal = PeripheralParser._normalize_signal_token(signal)
        match = PeripheralParser._PERIPHERAL_ROOT_PATTERN.match(signal)
        return match.group(1) if match else signal.split("_")[0]

    @staticmethod
    def _signal_suffix(signal: str) -> str:
        """CubeMX 信号名最后一个下划线之后的部分（大写）；没有下划线时为整个信号名。
        The part of a CubeMX signal name after the last underscore, in upper case; the whole
        name when it has no underscore.
        """
        signal = PeripheralParser._normalize_signal_token(signal)
        return signal.split("_")[-1].upper() if "_" in signal else signal.upper()

    @staticmethod
    def _parse_dma_request_endpoint(peripheral_full: str) -> tuple[str, str]:
        """把 USART1_TX 这类 DMA 请求目标拆成外设名和小写方向，例如 ("USART1", "tx")。
        Split a DMA request target such as USART1_TX into the peripheral and a lower-case
        direction, e.g. ("USART1", "tx").

        最后一个下划线之后全为字母时作为方向，否则整个目标为外设名，方向为 "general"。
        The letters after the last underscore are the direction; without such a suffix the
        whole target is the peripheral and the direction is "general".
        """
        endpoint = str(peripheral_full).strip()
        match = re.match(r"^(.*)_([A-Z]+)$", endpoint.upper())
        if not match:
            return endpoint, "general"
        return match.group(1), match.group(2).lower()

    @staticmethod
    def _normalize_dma_direction(value: str) -> str:
        """完整 DMA 方向的小写形式，只去掉 DMA_ 前缀，例如 DMA_PERIPH_TO_MEMORY 变为
        periph_to_memory。
        The complete DMA direction in lower case with only the DMA_ prefix removed, e.g.
        DMA_PERIPH_TO_MEMORY becomes periph_to_memory.
        """
        direction = str(value).strip().upper()
        if direction.startswith("DMA_"):
            direction = direction[4:]
        return direction.lower()

    def parse_gpio(self) -> None:
        """读取匹配 gpio_pattern 的引脚属性，按归一后的 PxN 引脚名写入 pin_registry。
        Read the pin properties that match gpio_pattern into pin_registry, keyed by the
        normalized PxN pin name.
        """
        for key, value in self.raw_map.items():
            if match := self.gpio_pattern.match(key):
                pin, prop = match.groups()
                self._process_gpio_property(self._normalize_gpio_pin_token(pin), prop, value)

    def _process_gpio_property(self, pin: str, prop: str, value: str) -> None:
        """把一个引脚属性写入 pin_registry：Signal 原样保存，GPIO_Label 的第一个词存为 Label，
        GPIO_PuPd 存为 Pull；值中含 GPXTI 时同时置 GPXTI 标记。
        Write one pin property to pin_registry: Signal as is, the first word of GPIO_Label as
        Label and GPIO_PuPd as Pull; a value containing GPXTI also sets the GPXTI flag.
        """
        prop_map = {
            "Signal": ("Signal", value),
            "GPIO_Label": ("Label", str(value).split()[0] if str(value).split() else ""),
            "GPIO_PuPd": ("Pull", value),
        }
        field, val = prop_map[prop]
        self.config.pin_registry[pin][field] = val
        if "GPXTI" in value:
            self.config.pin_registry[pin]["GPXTI"] = True

    def _pin_modes(self) -> dict[str, set[str]]:
        """每个外设实例在其引脚上记录的 CubeMX 模式名，例如 {"USART1": {"Synchronous Slave"}}。
        The CubeMX mode names recorded on the pins of each peripheral instance, e.g.
        {"USART1": {"Synchronous Slave"}}.

        .ioc 把外设的工作模式写在引脚的 <引脚>.Mode 上，实例取同一引脚 <引脚>.Signal 中的外设名。
        The .ioc file records the mode of a peripheral on its pins as <pin>.Mode; the instance
        is the peripheral in <pin>.Signal of the same pin.
        """
        modes: defaultdict[str, set[str]] = defaultdict(set)
        for key, value in self.raw_map.items():
            pin, _, prop = key.rpartition(".")
            if prop != "Mode" or not re.match(r"^P[A-Z]\d+", pin):
                continue
            signal = self.raw_map.get(f"{pin}.Signal")
            if signal:
                modes[self._signal_root(signal)].add(str(value).strip())
        return modes

    def parse(self, p_type: str) -> None:
        """解析 p_type 类外设的配置，由子类实现；基类调用时抛出 NotImplementedError。
        Parse the configuration of peripheral type p_type; implemented by subclasses, and the
        base class raises NotImplementedError.
        """
        raise NotImplementedError


# --------------------------
# MCU 解析器 / MCU Parser
# --------------------------
class McuParser(PeripheralParser):
    """读取 MCU 系列和型号。
    Reads the MCU family and part number.
    """

    def parse(self, p_type: str) -> None:
        """从 Mcu.* key 读取系列（Family）和型号（CPN），写入 mcu_config。
        Read the family (Family) and part number (CPN) from Mcu.* keys into mcu_config.
        """
        for key, value in self.raw_map.items():
            if not self._ioc_root_startswith(key, "Mcu"):
                continue
            prop = self._ioc_key_prop(key, "")
            if "Family" in prop:
                self.config.mcu_config["Family"] = value
            elif "CPN" in prop:
                self.config.mcu_config["Type"] = value


# --------------------------
# TIM 解析器 / TIM Parser
# --------------------------
class TIMParser(PeripheralParser):
    """读取定时器的计数模式、周期、预分频和 PWM 通道。
    Reads timer counter mode, period, prescaler and PWM channels.
    """

    _SHARED_CHANNEL_KEY = re.compile(r"^SH\.S_(TIM\d+)_CH\d+N?\.\d+$")
    # 没有引脚的通道（如内部信号的输入捕获）把模式写在虚拟引脚上；时钟源不是通道模式。
    # A channel without a pin, such as input capture from an internal signal, has its mode on
    # a virtual pin; the clock source is not a channel mode.
    _VIRTUAL_CHANNEL_KEY = re.compile(r"^VP_(TIM\d+)_VS_(?!ClockSource)\w+\.Mode$")

    def parse(self, p_type: str) -> None:
        """读取 TIMx.* 属性中的 PWM 通道、周期、预分频和计数模式，周期和预分频转为数值。
        Read the PWM channels, period, prescaler and counter mode from TIMx.* properties, with
        period and prescaler converted to numbers.

        TIMx.Channel-PWM GenerationN … 条目按 key 中列出的 CHx 和 CHxN 记录 PWM 通道，两者都列出
        时主输出和互补输出各记一个。单通道定时器（如 TIM10、TIM16）不论什么模式都只写
        TIMx.Channel，此时按通道模式（见 _shared_pwm_channels()）记录 PWM Generation 模式列出的
        通道，输入捕获等其他模式不记；找不到通道模式时把 TIMx.Channel 记为 PWM 通道。Period（或
        PeriodNoDither）为周期，Prescaler 为预分频，CounterMode 为 Mode。
        A TIMx.Channel-PWM GenerationN ... entry records the CHx and CHxN listed in its key as
        PWM channels, the main and the complementary output one each when both are listed. A
        single-channel timer such as TIM10 or TIM16 writes only TIMx.Channel whatever its mode;
        then the channel modes (see _shared_pwm_channels()) give the channels listed by a PWM
        Generation mode, and other modes such as input capture give none; without a channel
        mode TIMx.Channel is recorded as a PWM channel. Period (or PeriodNoDither) is the
        period, Prescaler the prescaler and CounterMode the Mode.
        """
        shared = self._shared_pwm_channels()
        for key, value in self.raw_map.items():
            tim_name = self._ioc_key_root(key)
            if not tim_name.startswith("TIM"):
                continue

            parts = self._split_ioc_key(key)
            if len(parts) < 2:
                continue

            self._ensure_tim_instance(p_type, tim_name)

            prop = parts[1]
            if "Channel-PWM" in key:
                self._add_pwm_channels(tim_name, re.findall(r"CH\d+N?", prop))
            elif prop == "Channel":
                channel = self._normalize_tim_channel_token(value)
                channels = shared.get(tim_name, [channel] if channel else [])
                self._add_pwm_channels(tim_name, channels)
            elif prop in ("Period", "PeriodNoDither"):
                self.config.peripherals[p_type][tim_name]["Period"] = sanitize_numeric(value)
            elif prop == "Prescaler":
                self.config.peripherals[p_type][tim_name]["Prescaler"] = sanitize_numeric(value)
            elif prop == "CounterMode":
                self.config.peripherals[p_type][tim_name]["Mode"] = value

    def _shared_pwm_channels(self) -> dict[str, list[str]]:
        """每个记录了通道模式的定时器在 PWM Generation 模式下列出的通道；只有其他模式的定时器为
        空列表。
        The channels listed by a PWM Generation mode for each timer with recorded channel
        modes; a timer with only other modes gets an empty list.

        通道模式取自引脚共享条目 SH.S_TIMx_CHn.k=TIMx_CHn,<模式>，例如
        SH.S_TIM10_CH1.0=TIM10_CH1,PWM Generation1 CH1 给出 {"TIM10": ["CH1"]}；以及虚拟引脚
        VP_TIMx_VS_*.Mode，例如 VP_TIM16_VS_NoInput1.Mode=Input_Capture1_from_TI1_REMAP_TIM16。
        Channel modes come from the shared pin entries SH.S_TIMx_CHn.k=TIMx_CHn,<mode>, e.g.
        SH.S_TIM10_CH1.0=TIM10_CH1,PWM Generation1 CH1 gives {"TIM10": ["CH1"]}, and from the
        virtual pins VP_TIMx_VS_*.Mode, e.g.
        VP_TIM16_VS_NoInput1.Mode=Input_Capture1_from_TI1_REMAP_TIM16.
        """
        channels: dict[str, list[str]] = {}
        for key, value in self.raw_map.items():
            match = self._SHARED_CHANNEL_KEY.match(key) or self._VIRTUAL_CHANNEL_KEY.match(key)
            if match is None:
                continue
            mode = str(value).split(",", 1)[-1].strip()
            found = re.findall(r"CH\d+N?", mode) if mode.startswith("PWM Generation") else []
            channels.setdefault(match.group(1), []).extend(found)
        return channels

    def _ensure_tim_instance(self, p_type: str, tim_name: str) -> None:
        """TIM 实例不存在时创建，模式、周期、预分频为空，通道表为空。
        Create the TIM instance when it does not exist, with empty mode, period and prescaler
        and an empty channel map.
        """
        if not self.config.peripherals[p_type].get(tim_name):
            self.config.peripherals[p_type][tim_name] = {
                "Mode": None,
                "Period": None,
                "Prescaler": None,
                "Channels": {},
            }

    def _add_pwm_channels(self, tim_name: str, channels: list[str]) -> None:
        """把 CHx / CHxN 记为定时器的 PWM 通道；CHxN 标记为互补输出，Label 为该通道所连引脚的
        标签。
        Record CHx / CHxN as PWM channels of the timer; CHxN is marked complementary, and Label
        is the label of the pin wired to the channel.
        """
        for channel in channels:
            channel_id = self._normalize_tim_channel_token(channel)
            if not channel_id:
                continue
            pin_label, _ = self._get_associated_pin_label(tim_name, channel_id)
            self.config.peripherals["TIM"][tim_name]["Channels"][channel_id] = {
                "Label": pin_label,
                "PWM": True,
                "Complementary": channel_id.endswith("N"),
            }

    def _get_associated_pin_label(self, timer_name: str, channel_id: str) -> tuple[str, bool]:
        """定时器通道所连引脚的 (标签, 是否互补输出)；引脚信号以 N 结尾时为互补输出。
        The (label, is_complementary) of the pin wired to a timer channel; the output is
        complementary when the pin signal ends with N.

        引脚没有标签时用引脚名；CH1 也匹配 TIMx_CH1_ETR 信号。通道名无效或找不到引脚时返回
        (timer_name, False)。
        A pin without a label gives its pin name; CH1 also matches the TIMx_CH1_ETR signal. An
        invalid channel, or no matching pin, gives (timer_name, False).
        """
        normalized_channel = self._normalize_tim_channel_token(channel_id)
        if not normalized_channel:
            return timer_name, False
        signal_candidates = {f"{timer_name}_{normalized_channel}"}
        if normalized_channel == "CH1":
            signal_candidates.add(f"{timer_name}_CH1_ETR")

        config = {}
        matched_pin = timer_name
        for pin_name, pin_cfg in self.config.pin_registry.items():
            normalized_signal = self._normalize_signal_token(pin_cfg.get("Signal", ""))
            if normalized_signal in signal_candidates:
                config = pin_cfg
                matched_pin = pin_name
                break

        label = config.get("Label", matched_pin)
        signal = self._normalize_signal_token(config.get("Signal", ""))
        is_complementary = signal.endswith("N")  # 例如 TIM1_CH1N / e.g., TIM1_CH1N
        return label, is_complementary


# --------------------------
# ADC 解析器 / ADC Parser
# --------------------------
class ADCParser(PeripheralParser):
    """读取 ADC 实例的规则转换通道、内部通道和 DMA 设置。
    Reads ADC instances: regular conversion channels, internal channels and the DMA setting.
    """

    _CHANNEL_PATTERN = re.compile(r"^ADC_CHANNEL_[A-Z0-9_]+$")
    # ADCx.Channel-N\#ChannelRegularConversion，读入时 \# 已去掉；N 从 0 起，即 rank N+1。
    # ADCx.Channel-N\#ChannelRegularConversion with \# removed on reading; N counts from 0,
    # so it is rank N+1.
    _REGULAR_CHANNEL_KEY = re.compile(r"^Channel-(\d+)ChannelRegularConversion$")

    def parse(self, p_type: str) -> None:
        """先读取 ADCx.* 属性，再把 VP_*.Signal 虚拟引脚映射为内部通道，最后整理通道列表。
        Read ADCx.* properties, then map VP_*.Signal virtual pins to internal channels, then
        put the channel lists in order.
        """
        self._regular: defaultdict[str, dict[int, str]] = defaultdict(dict)
        # 每个 ADC 的 CommonPathInternal 宏，只用于选择温度传感器宏，不写入配置。
        # The CommonPathInternal macros of each ADC, used only to choose the temperature sensor
        # macro and not written to the configuration.
        self._common_path: dict[str, list[str]] = {}
        for key, value in self.raw_map.items():
            if self._ioc_root_startswith(key, "ADC"):
                self._parse_adc_property(key, value)
        for key, value in self.raw_map.items():
            if self._ioc_root_startswith(key, "VP_") and key.endswith(".Signal"):
                self._parse_vp_adc_signal(key, value)
        self._finish_channels()

    def _map_internal_channel(self, value: str) -> str | None:
        """把 VP_* 虚拟引脚的 ADC 内部信号映射为 HAL 通道宏；无法识别时为 None。
        Map the internal ADC signal of a VP_* virtual pin to a HAL channel macro; None when the
        signal is not recognized.

        VREF 映射为 ADC_CHANNEL_VREFINT，VBAT 映射为 ADC_CHANNEL_VBAT，OPAMPn 映射为
        ADC_CHANNEL_VOPAMPn。温度传感器优先取该 ADC 的 CommonPathInternal 中带后缀的宏
        （如 ADC_CHANNEL_TEMPSENSOR_ADC1），否则为 ADC_CHANNEL_TEMPSENSOR；选择不依赖 MCU 系列。
        VREF maps to ADC_CHANNEL_VREFINT, VBAT to ADC_CHANNEL_VBAT and OPAMPn to
        ADC_CHANNEL_VOPAMPn. The temperature sensor takes a suffixed macro such as
        ADC_CHANNEL_TEMPSENSOR_ADC1 from that ADC's CommonPathInternal, and otherwise
        ADC_CHANNEL_TEMPSENSOR; the choice does not depend on the MCU family.

        ADC 实例取信号开头的 ADCn，没有时取第一个已记录的 ADC 实例。
        The ADC instance is the ADCn at the start of the signal, or else the first recorded ADC
        instance.
        """
        v_upper = value.upper()

        # 从 VP_* 的值（如 "ADC1_TempSensor"）取 ADC 实例，
        # 取不到时用第一个已记录的 ADC 实例。
        # Try to derive the ADC instance from the VP_* value (e.g., "ADC1_TempSensor"),
        # otherwise fall back to the first known ADC instance.
        m_adc = re.match(r"(ADC\d+)_", v_upper)
        adc_name = (m_adc.group(1) if m_adc else self._get_adc_instance_name()).upper()

        # 读取属性解析时记录的 CommonPathInternal（如有）。
        # Read CommonPathInternal (if present) captured during property parsing.
        cp_list = self._common_path.get(adc_name, [])

        # 直接映射
        # Direct maps
        if "VREF" in v_upper:
            return "ADC_CHANNEL_VREFINT"
        if "VBAT" in v_upper:
            return "ADC_CHANNEL_VBAT"

        # 温度传感器：优先取 CommonPathInternal 中带后缀的宏，其次用通用宏。
        # TempSensor: prefer suffixed macros from CommonPathInternal, then generic
        if "TEMP" in v_upper:
            for tok in cp_list:
                if re.match(r"ADC_CHANNEL_TEMPSENSOR_ADC\d+$", tok):
                    return tok
            return "ADC_CHANNEL_TEMPSENSOR"

        # 运算放大器 OPAMPn
        # OPAMPn
        m = re.search(r"OPAMP(\d+)", v_upper)
        if m:
            return f"ADC_CHANNEL_VOPAMP{m.group(1)}"

        return None

    def _parse_adc_property(self, key: str, value: str) -> None:
        """读取一个 ADCx.<setting> 属性并写入该 ADC 实例。
        Read one ADCx.<setting> property into that ADC instance.

        Channel-N#ChannelRegularConversion 记为第 N 个规则通道，同组的 Rank、SamplingTime 等
        属性不读；DMARegular 和 DMAContinuousRequests 归一为 "ENABLE" / "DISABLE" 存入 DMA。
        Channel-N#ChannelRegularConversion is recorded as regular channel N, while Rank,
        SamplingTime and the other properties of the group are not read; DMARegular and
        DMAContinuousRequests are normalized to "ENABLE" / "DISABLE" in DMA.

        CommonPathInternal（如 "null|ADC_CHANNEL_TEMPSENSOR_ADC1|null|null"）按 | 拆分，去掉
        null 后以大写列表记下，供之后选择温度传感器宏，选择不依赖 MCU 系列。
        CommonPathInternal, e.g. "null|ADC_CHANNEL_TEMPSENSOR_ADC1|null|null", is split at |
        and noted as an upper-case list without null entries, so the temperature sensor macro
        can be chosen later without depending on the MCU family.
        """
        parts = self._split_ioc_key(key)
        if len(parts) < 2:
            return

        adc_name = parts[0]
        setting = parts[1]
        self._ensure_adc_instance(adc_name)

        def _to_enable_str(v: str) -> str:
            """值为 ENABLE（忽略大小写和首尾空白）时为 "ENABLE"，否则为 "DISABLE"。
            "ENABLE" when the value is ENABLE, ignoring case and surrounding whitespace;
            otherwise "DISABLE".
            """
            return "ENABLE" if str(v).strip().upper() == "ENABLE" else "DISABLE"

        if "ChannelRegularConversion" in setting:
            match = self._REGULAR_CHANNEL_KEY.match(setting)
            if match and self._is_valid_channel(value.strip()):
                self._regular[adc_name][int(match.group(1))] = value.strip()
            elif match:
                logging.debug(f"Ignored invalid ADC channel: {key}={value}")
        elif setting == "DMARegular" or setting == "DMAContinuousRequests":
            self.config.peripherals["ADC"][adc_name]["DMA"] = _to_enable_str(value)
        elif setting == "CommonPathInternal":
            tokens = [t.strip() for t in str(value).split("|")]
            self._common_path[adc_name] = [t.upper() for t in tokens if t and t.lower() != "null"]

    def _get_adc_instance_name(self) -> str:
        """第一个已记录的 ADC 实例名；还没有 ADC 实例时为 "ADC"。
        The first recorded ADC instance name; "ADC" while there is none.
        """
        adc_instances = self.config.peripherals.get("ADC", {})
        return list(adc_instances.keys())[0] if adc_instances else "ADC"

    def _parse_vp_adc_signal(self, key: str, value: str) -> None:
        """把信号以 ADC 开头的 VP_* 虚拟引脚映射为内部通道宏，加入对应 ADC 实例的 Channels。
        Map a VP_* virtual pin whose signal starts with ADC to an internal channel macro and add
        it to Channels of that ADC instance.

        这里得到的通道只加入 Channels，不加入 RegularConversions。ADC 实例取信号开头的
        ADCn，没有时取第一个已记录的 ADC 实例。
        Channels found here go to Channels only, not to RegularConversions. The ADC instance is
        the ADCn at the start of the signal, or else the first recorded ADC instance.
        """
        if not self._normalize_signal_token(value).startswith("ADC"):
            return

        parts = self._normalize_signal_token(value).split("_")
        if parts[0].startswith("ADC") and parts[0][-1].isdigit():
            adc_name = parts[0]  # 例如 ADC1 / e.g., ADC1
        else:
            adc_name = self._get_adc_instance_name()

        self._ensure_adc_instance(adc_name)

        mapped_channel = self._map_internal_channel(value)
        if mapped_channel:
            # 只加入 Channels，不加入 RegularConversions。
            # Only add to Channels (not RegularConversions)
            self._add_unique_entry(adc_name, "Channels", mapped_channel)

    def _is_valid_channel(self, entry: str) -> bool:
        """entry 为 ADC_CHANNEL_ 后接大写字母、数字或下划线时为真。
        True when entry is ADC_CHANNEL_ followed by upper-case letters, digits or underscores.
        """
        return bool(self._CHANNEL_PATTERN.match(entry))

    def _ensure_adc_instance(self, adc_name: str) -> None:
        """ADC 实例不存在时创建：通道列表为空，DMA 为 "DISABLE"。
        Create the ADC instance when it does not exist: empty channel lists and DMA "DISABLE".
        """
        if adc_name not in self.config.peripherals["ADC"]:
            self.config.peripherals["ADC"][adc_name] = {
                "RegularConversions": [],
                "Channels": [],
                "DMA": "DISABLE",
            }

    def _add_unique_entry(self, adc_name: str, field: str, value: str) -> None:
        """value 不在 ADC 实例的 field 列表中时追加到末尾。
        Append value to the field list of the ADC instance when it is not already there.
        """
        target_list = self.config.peripherals["ADC"][adc_name][field]
        if value not in target_list:
            target_list.append(value)

    def _finish_channels(self) -> None:
        """写出每个 ADC 实例的 RegularConversions 和 Channels。
        Write RegularConversions and Channels of each ADC instance.

        RegularConversions 按 rank 排列，即 Channel-N 的 N 按数值排序；.ioc 的 key 按文本排序，
        Channel-10 排在 Channel-2 之前。同一通道排在多个 rank 时每次都保留，数量与 CubeMX 的
        NbrOfConversion 一致，DMA 模式下第 i 项就是 DMA 缓冲区的第 i 格。Channels 是规则通道加上
        虚拟引脚的内部通道，保序去重，供轮询模式使用。
        RegularConversions follows the ranks, that is N of Channel-N in numeric order; the .ioc
        keys are sorted as text, with Channel-10 before Channel-2. A channel in several ranks
        is kept every time, so the count matches NbrOfConversion of CubeMX and in DMA mode item
        i is slot i of the DMA buffer. Channels is the regular channels plus the internal
        channels of virtual pins, in order without repeats, for polling mode.

        通道列表或 CommonPathInternal 中出现 ADC_CHANNEL_TEMPSENSOR_ADCn 时，Channels 去掉通用的
        ADC_CHANNEL_TEMPSENSOR，RegularConversions 把它换成带后缀的宏。
        When ADC_CHANNEL_TEMPSENSOR_ADCn appears in a channel list or in CommonPathInternal,
        Channels drops the generic ADC_CHANNEL_TEMPSENSOR and RegularConversions replaces it
        with the suffixed macro.
        """
        for adc_name, adc_cfg in self.config.peripherals["ADC"].items():
            regs = [channel for _, channel in sorted(self._regular.get(adc_name, {}).items())]
            chs = list(dict.fromkeys(regs + adc_cfg.get("Channels", [])))
            cp_list = self._common_path.get(adc_name, [])
            suffixed = next(
                (x for x in chs + cp_list if re.match(r"ADC_CHANNEL_TEMPSENSOR_ADC\d+$", x)), None
            )
            if suffixed:
                chs = [x for x in chs if x != "ADC_CHANNEL_TEMPSENSOR"]
                regs = [suffixed if x == "ADC_CHANNEL_TEMPSENSOR" else x for x in regs]
            adc_cfg["Channels"] = chs
            adc_cfg["RegularConversions"] = regs


# --------------------------
# DAC 解析器 / DAC Parser
# --------------------------
class DACParser(PeripheralParser):
    """读取 DAC 实例的输出通道、触发源、DMA 和输出缓冲设置。
    Reads DAC instances: output channels, trigger, DMA and output buffer.
    """

    def parse(self, p_type: str) -> None:
        """读取两种 DAC 条目：SH.COMP_DAC<n>_group.<k> 和 DACx.* 属性。
        Read two kinds of DAC entries: SH.COMP_DAC<n>_group.<k> and DACx.* properties.

        SH.COMP_DAC 条目的值在第一个逗号处拆成通道和别名，没有逗号的条目记录警告后跳过。一位
        编号（如 COMP_DAC2_group）归入实例 DAC，通道键为值中的通道；两位编号（如
        COMP_DAC12_group）归入 DAC1 的 OUT2。
        The value of an SH.COMP_DAC entry is split at the first comma into a channel and an
        alias; an entry without a comma is logged as a warning and skipped. A one-digit number
        such as COMP_DAC2_group goes to the instance DAC under the channel from the value; a
        two-digit number such as COMP_DAC12_group goes to OUT2 of DAC1.
        """
        for key, value in self.raw_map.items():
            # 1. SH.COMP_DAC*_group 条目，单通道和多通道 DAC 都能识别。
            # 1. SH.COMP_DAC*_group. Compatible with single/multi-channel DAC recognition
            m = re.match(r"^SH\.COMP_DAC(\d{1,2})_group\.\d+$", key)
            if m:
                digits = m.group(1)
                if "," not in value:
                    logging.warning(
                        tr(
                            f"Ignored DAC entry without a channel and an alias: {key}={value}",
                            f"忽略缺少通道和别名的 DAC 条目：{key}={value}",
                        )
                    )
                    continue
                out, alias = value.split(",", 1)
                if len(digits) == 1:
                    # 一位数字（如 COMP_DAC2_group）：唯一的 DAC，通道 OUTx（通常为 OUT1/OUT2）。
                    # Only one digit (e.g., COMP_DAC2_group): unique DAC, OUTx (usually DAC's OUT1/OUT2)
                    self._ensure_dac_instance("DAC")
                    self.config.peripherals["DAC"]["DAC"]["Channels"][out] = alias
                elif len(digits) == 2:
                    # 两位数字（如 COMP_DAC12_group）：DAC1 的 OUT2。
                    # Two digits (e.g., COMP_DAC12_group): DAC1's OUT2
                    dac_idx = digits[0]
                    out_idx = digits[1]
                    dac_name = f"DAC{dac_idx}"
                    out_name = f"OUT{out_idx}"
                    self._ensure_dac_instance(dac_name)
                    self.config.peripherals["DAC"][dac_name]["Channels"][out_name] = alias
                continue

            # 2. 兼容 CubeMX 的新格式（如 DAC1.DAC_Channel-DAC_OUT1=DAC_CHANNEL_1）。
            # 2. Compatible with new CubeMX format (e.g. DAC1.DAC_Channel-DAC_OUT1=DAC_CHANNEL_1)
            if self._ioc_key_root(key).startswith("DAC"):
                self._parse_dac_property(key, value)

    def _parse_dac_property(self, key: str, value: str) -> None:
        """读取一个 DACx.<setting> 属性：DAC_Channel-DAC_OUTn 记为通道 OUTn，名字含 Trigger、
        DMA 或 OutputBuffer 的属性原样存入对应字段。
        Read one DACx.<setting> property: DAC_Channel-DAC_OUTn becomes channel OUTn, and a
        setting whose name contains Trigger, DMA or OutputBuffer is stored as is in that field.
        """
        parts = self._split_ioc_key(key)
        if len(parts) < 2:
            return
        dac_name = parts[0]
        setting = parts[1]
        self._ensure_dac_instance(dac_name)
        if "Channel" in setting:
            match = re.match(r"DAC_Channel-DAC_OUT(\d+)", setting)
            if match:
                ch_num = match.group(1)
                ch_key = f"OUT{ch_num}"
                ch_val = value.strip()
                self.config.peripherals["DAC"][dac_name]["Channels"][ch_key] = ch_val
        elif "Trigger" in setting:
            self.config.peripherals["DAC"][dac_name]["Trigger"] = value
        elif "DMA" in setting:
            self.config.peripherals["DAC"][dac_name]["DMA"] = value
        elif "OutputBuffer" in setting:
            self.config.peripherals["DAC"][dac_name]["OutputBuffer"] = value

    def _ensure_dac_instance(self, dac_name: str) -> None:
        """DAC 实例不存在时创建，通道表为空，触发源、DMA 和输出缓冲为空。
        Create the DAC instance when it does not exist, with no channels and empty trigger, DMA
        and output buffer.
        """
        if dac_name not in self.config.peripherals["DAC"]:
            self.config.peripherals["DAC"][dac_name] = {
                "Channels": {},
                "Trigger": None,
                "DMA": None,
                "OutputBuffer": None,
            }


# --------------------------
# SPI 解析器 / SPI Parser
# --------------------------
class SPIParser(PeripheralParser):
    """读取 SPI 实例和它的波特率。
    Reads SPI instances and their baud rates.
    """

    def parse(self, p_type: str) -> None:
        """有 SPIx.* 属性的实例都被记录；CalculateBaudRate（CubeMX 算出的波特率）写入 BaudRate。
        Record every instance with SPIx.* properties; CalculateBaudRate, the baud rate CubeMX
        calculates, sets BaudRate.
        """
        for key, value in self.raw_map.items():
            spi_name = self._ioc_key_root(key)
            if not spi_name.startswith("SPI"):
                continue

            parts = self._split_ioc_key(key)
            if len(parts) < 2:
                continue

            self._ensure_spi_instance(p_type, spi_name)

            prop = parts[1]
            if prop == "CalculateBaudRate":
                self.config.peripherals[p_type][spi_name]["BaudRate"] = sanitize_numeric(value)

    def _ensure_spi_instance(self, p_type: str, spi_name: str) -> None:
        """SPI 实例不存在时创建，波特率为空，DMA 表为空。
        Create the SPI instance when it does not exist, with no baud rate and an empty DMA map.
        """
        if not self.config.peripherals[p_type].get(spi_name):
            self.config.peripherals[p_type][spi_name] = {"BaudRate": None, "DMA": {}}


# --------------------------
# USART/UART 解析器 / USART/UART Parser
# --------------------------
class USARTParser(PeripheralParser):
    """读取 USART、UART 和 LPUART 实例，包括只在引脚信号中出现的实例。
    Reads USART, UART and LPUART instances, including instances named only by pin signals.
    """

    # CubeMX 模式库（db/mcu/IP/USART-*_Modes.xml 的 HalMode）中不使用 UART 句柄的模式，以及它们
    # 的 HAL 句柄类型；其余模式（异步、单线半双工、LIN、多处理器、RS485）都使用 UART 句柄。
    # The modes of the CubeMX mode database (HalMode in db/mcu/IP/USART-*_Modes.xml) that do
    # not use a UART handle, with their HAL handle types; every other mode (asynchronous,
    # single-wire half duplex, LIN, multiprocessor, RS485) uses the UART handle.
    _OTHER_HANDLES = {
        "Synchronous": "USART",
        "Synchronous Slave": "USART",
        "IrDA": "IRDA",
        "SmartCard": "SMARTCARD",
        "SmartCard_With_Clock": "SMARTCARD",
    }
    # <实例>.VirtualMode 的值对应的模式名。
    # The mode names of the values of <instance>.VirtualMode.
    _VIRTUAL_MODES = {
        "VM_ASYNC": "Asynchronous",
        "VM_SYNC": "Synchronous",
        "VM_IRDA": "IrDA",
        "VM_LIN": "LIN",
        "VM_SMARTCARD": "SmartCard",
    }

    def parse(self, p_type: str) -> None:
        """读取 USART、UART 和 LPUART 配置；只有引脚信号的实例也会创建。
        Read USART, UART and LPUART configurations; an instance with only pin signals is created
        too.

        第一遍读取 USARTx/UARTx/LPUARTx.* 属性中的波特率和 VirtualMode；第二遍从含 _TX 或 _RX 的
        引脚信号推断实例名，创建尚未出现的实例。最后由引脚上的模式和 VirtualMode 定出模式：同步、
        IrDA、SmartCard 模式的实例在 CubeMX 生成的代码中没有 UART 句柄，记录一条警告后去掉，其余
        实例写入 Mode。
        The first pass reads baud rate and VirtualMode from USARTx/UARTx/LPUARTx.* properties;
        the second pass derives instance names from pin
        signals containing _TX or _RX and creates the instances not seen yet. Finally the mode
        comes from the pin modes and VirtualMode: an instance in synchronous, IrDA or SmartCard
        mode has no UART handle in the code CubeMX generates and is dropped with a warning; the
        other instances get Mode.
        """
        found_instances = set()
        virtual_modes: dict[str, str] = {}

        # 第一遍：按 USART/UART/LPUART 的属性 key 正常解析。
        # First pass: normal parsing from USART/UART/LPUART property keys
        for key, value in self.raw_map.items():
            uart_name = self._ioc_key_root(key)
            if uart_name.startswith(("USART", "UART", "LPUART")):
                parts = self._split_ioc_key(key)
                if len(parts) < 2:
                    continue

                found_instances.add(uart_name)
                self._ensure_uart_instance(p_type, uart_name)

                prop = parts[1]
                if prop == "BaudRate":
                    self.config.peripherals[p_type][uart_name]["BaudRate"] = sanitize_numeric(value)
                elif prop.startswith("VirtualMode"):
                    virtual_modes[uart_name] = str(value).strip()

        # 第二遍：根据 GPIO 引脚信号推断缺少的 UART 实例。
        # Second pass: infer missing UART instances based on GPIO signals
        for pin_cfg in self.config.pin_registry.values():
            signal = pin_cfg.get("Signal", "")
            if "_TX" in signal or "_RX" in signal:
                uart_root = self._signal_root(signal)
                if (
                    uart_root.startswith(("USART", "UART", "LPUART"))
                    and uart_root not in found_instances
                ):
                    # 仅凭引脚信号发现了新的 UART 实例。
                    # Found a new UART based only on pin signals
                    logging.debug(f"Inferred USART instance from pin: {uart_root}")
                    self._ensure_uart_instance(p_type, uart_root)

        pin_modes = self._pin_modes()
        for name in list(self.config.peripherals[p_type]):
            other = sorted(m for m in pin_modes.get(name, ()) if m in self._OTHER_HANDLES)
            mode = other[0] if other else self._VIRTUAL_MODES.get(virtual_modes.get(name, ""))
            if mode in self._OTHER_HANDLES:
                del self.config.peripherals[p_type][name]
                handle = self._OTHER_HANDLES[mode]
                logging.warning(
                    tr(
                        f"{name} is not generated: {mode} mode uses {handle}_HandleTypeDef, "
                        "and LibXR's STM32UART needs a UART handle",
                        f"{name} 不会生成：{mode} 模式使用 {handle}_HandleTypeDef，LibXR 的 "
                        "STM32UART 需要 UART 句柄",
                    )
                )
            elif mode:
                self.config.peripherals[p_type][name]["Mode"] = mode

    def _ensure_uart_instance(self, p_type: str, uart_name: str) -> None:
        """UART/USART/LPUART 实例不存在时创建，模式为 Asynchronous，波特率为空，DMA 表为空。
        Create the UART/USART/LPUART instance when it does not exist, with mode Asynchronous,
        no baud rate and an empty DMA map.
        """
        if uart_name not in self.config.peripherals[p_type]:
            self.config.peripherals[p_type][uart_name] = {
                "BaudRate": None,
                "Mode": "Asynchronous",
                "DMA": {},
            }


# --------------------------
# I2C 解析器 / I2C Parser
# --------------------------
class I2CParser(PeripheralParser):
    """读取 I2C 实例的时钟速度、时序和 SCL/SDA 引脚。
    Reads I2C instances: clock speed, timing and SCL/SDA pins.
    """

    def parse(self, p_type: str) -> None:
        """从三类条目读取 I2C：Mcu.IP* 中列出的 I2C 实例；信号含 I2C 的引脚，记为该实例的 SCL
        或 SDA；I2Cx.* 属性。
        Read I2C from three kinds of entries: I2C instances listed in Mcu.IP*; pins whose signal
        contains I2C, recorded as SCL or SDA of that instance; and I2Cx.* properties.

        属性按 key 的最后一段精确匹配：ClockSpeed 转为数值，Timing 存为字符串。引脚信号属于
        FMPI2C 等名字不以 I2C 开头的外设时，记录一次警告后跳过，因为 LibXR 没有它们的驱动。引脚
        处于 SMBus 模式的实例在 CubeMX 生成的代码中只有 SMBUS 句柄，记录一条警告后去掉。
        Properties match the last token of the key exactly: ClockSpeed becomes a number and
        Timing a string. Pin signals of a peripheral whose name does not start with I2C, such as FMPI2C,
        are logged once as a warning and skipped, as LibXR has no driver for them. An instance
        whose pins are in an SMBus mode has only an SMBUS handle in the code CubeMX generates
        and is dropped with a warning.
        """
        unsupported: set[str] = set()
        for key, value in self.raw_map.items():
            if self._ioc_key_startswith(key, "Mcu.IP"):
                val = str(value)
                if val.startswith("I2C"):
                    self._ensure_i2c_instance(p_type, val)
                continue

            if key.endswith(".Signal") and "I2C" in self._normalize_signal_token(value):
                portpin = self._normalize_ioc_key_pin(key)
                per_sig = self._normalize_signal_token(value)
                i2c_name = self._signal_root(per_sig)
                if not i2c_name.startswith("I2C"):
                    # FMPI2C 等外设的信号名也含 I2C，但 LibXR 没有对应的驱动。
                    # FMPI2C and similar peripherals have I2C in their signal names, but LibXR
                    # has no driver for them.
                    if i2c_name not in unsupported:
                        unsupported.add(i2c_name)
                        logging.warning(
                            tr(
                                f"{i2c_name} is not generated: LibXR has no driver for it",
                                f"{i2c_name} 不会生成：LibXR 没有它的驱动",
                            )
                        )
                    continue
                self._ensure_i2c_instance(p_type, i2c_name)
                cfg = self.config.peripherals[p_type][i2c_name]
                pins = cfg.setdefault("Pins", {"SCL": None, "SDA": None})
                suffix = self._signal_suffix(per_sig)
                if suffix == "SCL":
                    pins["SCL"] = portpin
                if suffix == "SDA":
                    pins["SDA"] = portpin
                continue

            i2c_name = self._ioc_key_root(key)
            if not i2c_name.startswith("I2C"):
                continue

            parts = self._split_ioc_key(key)
            if len(parts) < 2:
                continue

            self._ensure_i2c_instance(p_type, i2c_name)

            prop = parts[-1]
            if prop == "ClockSpeed":
                self.config.peripherals[p_type][i2c_name]["ClockSpeed"] = sanitize_numeric(value)
            elif prop == "Timing":
                self.config.peripherals[p_type][i2c_name]["Timing"] = str(value)

        # 引脚模式为 SMBus-two-wire-Interface 或 SMBus-Alert-mode 的实例使用 SMBUS 句柄。
        # An instance whose pins are in SMBus-two-wire-Interface or SMBus-Alert-mode uses the
        # SMBUS handle.
        pin_modes = self._pin_modes()
        for name in list(self.config.peripherals[p_type]):
            if any(mode.startswith("SMBus") for mode in pin_modes.get(name, ())):
                del self.config.peripherals[p_type][name]
                logging.warning(
                    tr(
                        f"{name} is not generated: SMBus mode uses SMBUS_HandleTypeDef, and "
                        "LibXR's STM32I2C needs an I2C handle",
                        f"{name} 不会生成：SMBus 模式使用 SMBUS_HandleTypeDef，LibXR 的 STM32I2C "
                        "需要 I2C 句柄",
                    )
                )

    def _ensure_i2c_instance(self, p_type: str, i2c_name: str) -> None:
        """I2C 实例不存在时创建：时钟速度和时序为空，DMA 表为空，引脚未定。
        Create the I2C instance when it does not exist: no clock speed or timing, an empty DMA
        map, and no pins.
        """
        if not self.config.peripherals[p_type].get(i2c_name):
            self.config.peripherals[p_type][i2c_name] = {
                "ClockSpeed": None,
                "Timing": None,
                "DMA": {},
                "Pins": {"SCL": None, "SDA": None},
            }


# --------------------------
# CAN/FDCAN 解析器 / CAN/FDCAN Parser
# --------------------------
class CANParser(PeripheralParser):
    """读取 CAN 与 FDCAN 实例；实例名以 FDCAN 开头时归入 FDCAN，否则归入 CAN。
    Reads CAN and FDCAN instances; a name starting with FDCAN goes to FDCAN, any other to CAN.
    """

    def parse(self, p_type: str) -> None:
        """读取 CANx.* 和 FDCANx.* 属性；外设类型由实例名决定，传入的 p_type 被覆盖。
        Read CANx.* and FDCANx.* properties; the peripheral type follows the instance name and
        overrides the p_type argument.

        CAN 的 CalculateBaudRate 和 FDCAN 的 CalculateBaudRateNominal 存为 BaudRate，两类的 Mode
        存为 Mode；CAN 的 BS1、BS2 原样存为 TimeSeg1、TimeSeg2。
        CalculateBaudRate of CAN and CalculateBaudRateNominal of FDCAN are stored as BaudRate and
        Mode of both as Mode; BS1 and BS2 of CAN are stored as is in TimeSeg1 and TimeSeg2.
        """
        fields = {
            "CalculateBaudRate": "BaudRate",
            "CalculateBaudRateNominal": "BaudRate",
            "Mode": "Mode",
            "BS1": "TimeSeg1",
            "BS2": "TimeSeg2",
        }
        for key, value in self.raw_map.items():
            can_name = self._ioc_key_root(key)
            if not can_name.startswith(("CAN", "FDCAN")):
                continue

            p_type = "FDCAN" if can_name.startswith("FDCAN") else "CAN"
            parts = self._split_ioc_key(key)
            if len(parts) < 2:
                continue

            instance = self.config.peripherals[p_type].setdefault(can_name, {})
            if field := fields.get(parts[1]):
                instance[field] = value


# --------------------------
# USB 解析器 / USB Parser
# --------------------------
class USBParser(PeripheralParser):
    """把 USB 相关 IP 的 .ioc 属性原样收集到 USB 类型下，带 -<profile> 后缀的参数按 profile 分组。
    Collects the .ioc properties of USB-related IPs as is under the USB type; parameters with
    a -<profile> suffix are grouped by profile.
    """

    # 这些系列的 USB IP 是 USB_DRD_FS，CubeMX 把 PCD 句柄命名为 hpcd_USB_DRD_FS。.ioc 中的 IP 名
    # 是 USB，或者就是 USB_DRD_FS（如 STM32G0B1），后者同样记下 hpcd_USB_DRD_FS。
    # The USB IP of these families is USB_DRD_FS, whose PCD handle CubeMX names
    # hpcd_USB_DRD_FS. The IP name in the .ioc file is USB or USB_DRD_FS itself (as on the
    # STM32G0B1), and the latter records hpcd_USB_DRD_FS as well.
    _DRD_FAMILIES = {"STM32C0", "STM32G0", "STM32H5", "STM32U0", "STM32U3", "STM32U5"}

    def parse(self, p_type: str) -> None:
        """找出 USB 相关实例，并收集每个实例的全部属性。
        Find the USB-related instances and collect all properties of each.

        实例来自 Mcu.IP* 中含 USB 的 IP 名，以及以 USB.、USB_OTG_FS.、USB_OTG_HS. 开头的
        key，按 .ioc 中的出现顺序。
        The instances come from Mcu.IP* entries that contain USB and from keys starting with
        USB., USB_OTG_FS. or USB_OTG_HS., in .ioc order.

        "<param>-<profile>" 形式的参数存入 profiles[profile][param]，其余存在实例下；
        IPParameters 拆成列表。VirtualMode（含各 profile 中的）含 Device 时 Role 为 Device，
        含 Host 时为 Host，主机模式下 CubeMX 生成的是 HCD 句柄。_DRD_FAMILIES 系列的 USB 实例和
        IP 名为 USB_DRD_FS 的实例记下 PCDHandle hpcd_USB_DRD_FS。
        A "<param>-<profile>" parameter is stored in profiles[profile][param] and any other
        under the instance; IPParameters is split into a list. Role is Device when a
        VirtualMode, profiles included, contains Device and Host when one contains Host; in
        host mode CubeMX generates an HCD handle. The USB instance of the _DRD_FAMILIES and an
        instance with the IP name USB_DRD_FS get PCDHandle hpcd_USB_DRD_FS.
        """
        # 1. 按 .ioc 中的顺序找出 raw_map 里的全部 USB 外设名。
        # 1. Find all USB peripheral names in the raw_map, in .ioc order
        usb_names = {}
        for key, value in self.raw_map.items():
            if self._ioc_key_startswith(key, "Mcu.IP") and "USB" in value:
                usb_names.setdefault(value)
            elif re.match(r"^USB(_OTG(_FS|_HS))?\.", key):
                usb_names.setdefault(self._ioc_key_root(key))

        logging.debug(f"[USBParser] Detected USB peripherals: {list(usb_names)}")

        for usb_name in usb_names:
            self._ensure_usb_instance(usb_name)
            logging.debug(f"[USBParser] Parsing configuration for: {usb_name}")

            for key, value in self.raw_map.items():
                if not self._has_ioc_prefix(key, usb_name):
                    continue

                # 去掉 "USB_OTG_FS." 这类实例名前缀。
                # Remove the "USB_OTG_FS." prefix
                rest_key = key[len(usb_name) + 1 :]
                # 2.1 处理按 profile 区分的参数。
                # 2.1 Handle profile-specific parameters
                if "-" in rest_key:
                    param, profile = rest_key.split("-", 1)
                    logging.debug(
                        f"[USBParser] Profile param: {usb_name}.{param} (profile={profile}), value={value}"
                    )
                    self.config.peripherals["USB"][usb_name].setdefault("profiles", {})
                    self.config.peripherals["USB"][usb_name]["profiles"].setdefault(profile, {})
                    if param == "IPParameters":
                        self.config.peripherals["USB"][usb_name]["profiles"][profile][param] = (
                            value.split(",")
                        )
                        parameters = self.config.peripherals["USB"][usb_name]["profiles"][profile][
                            param
                        ]
                        logging.debug(
                            f"[USBParser] IPParameters for profile={profile}: {parameters}"
                        )
                    else:
                        self.config.peripherals["USB"][usb_name]["profiles"][profile][param] = value
                else:
                    # 2.2 处理全局参数。
                    # 2.2 Handle global parameters
                    logging.debug(f"[USBParser] Global param: {usb_name}.{rest_key} = {value}")
                    if rest_key == "IPParameters":
                        self.config.peripherals["USB"][usb_name][rest_key] = value.split(",")
                        parameters = self.config.peripherals["USB"][usb_name][rest_key]
                        logging.debug(f"[USBParser] IPParameters: {parameters}")
                    else:
                        self.config.peripherals["USB"][usb_name][rest_key] = value

        family = self.config.mcu_config.get("Family") or ""
        for usb_name, cfg in self.config.peripherals["USB"].items():
            modes = [cfg.get("VirtualMode")]
            modes += [profile.get("VirtualMode") for profile in cfg.get("profiles", {}).values()]
            modes = [str(mode) for mode in modes if mode]
            if any("Device" in mode for mode in modes):
                cfg["Role"] = "Device"
            elif any("Host" in mode for mode in modes):
                cfg["Role"] = "Host"
            if usb_name == "USB_DRD_FS" or (usb_name == "USB" and family in self._DRD_FAMILIES):
                cfg["PCDHandle"] = "hpcd_USB_DRD_FS"

    def _ensure_usb_instance(self, usb_name: str) -> None:
        """USB 实例不存在时创建空字典。
        Create an empty dict for the USB instance when it does not exist.
        """
        if usb_name not in self.config.peripherals["USB"]:
            self.config.peripherals["USB"][usb_name] = {}
            logging.debug(f"[USBParser] Initialized USB instance: {usb_name}")


# --------------------------
# DMA 解析器 / DMA Parser
# --------------------------
class DMAParser(PeripheralParser):
    """读取 DMA、BDMA 的请求与 stream 配置，以及 GPDMA、HPDMA、LPDMA 通道的请求，并挂到对应的
    外设实例下。
    Reads DMA and BDMA requests and stream configurations, and the channel requests of GPDMA,
    HPDMA and LPDMA, and attaches them to the peripheral instances they serve.
    """

    # CubeMX DMA 配置属性到内部字段名和转换函数的映射。
    # Maps CubeMX DMA config properties to internal fields and conversion logic
    _PROPERTY_MAP = {
        "Instance": ("stream", str),
        "Direction": ("direction", lambda v: v.split("_")[-1]),
        "PeriphInc": ("periph_inc", lambda v: v == "ENABLE"),
        "MemInc": ("mem_inc", lambda v: v == "ENABLE"),
        "PeriphDataAlignment": ("periph_align", lambda v: v.split("_")[-1].lower()),
        "MemDataAlignment": ("mem_align", lambda v: v.split("_")[-1].lower()),
        "Mode": ("mode", lambda v: v.split("_")[-1].capitalize()),
        # DMA_PRIORITY_VERY_HIGH 为 VeryHigh，DMA_PRIORITY_LOW 为 Low。
        # DMA_PRIORITY_VERY_HIGH gives VeryHigh and DMA_PRIORITY_LOW gives Low.
        "Priority": (
            "priority",
            lambda v: "".join(
                word.capitalize() for word in v.upper().removeprefix("DMA_PRIORITY_").split("_")
            ),
        ),
        "FIFOMode": ("fifo", lambda v: "Enabled" if "ENABLE" in v else "Disabled"),
    }

    # GPDMA、HPDMA、LPDMA 通道的简单请求：<控制器>.REQUEST_<通道>=<控制器>_REQUEST_<目标>。
    # Simple requests of GPDMA, HPDMA and LPDMA channels:
    # <controller>.REQUEST_<channel>=<controller>_REQUEST_<target>.
    _CHANNEL_REQUEST = re.compile(r"^((?:GP|HP|LP)DMA\d+)\.REQUEST_(\w+?(\d+))$")

    def parse(self, p_type: str) -> None:
        """依次解析 Dma. 与 Bdma. 前缀下的请求和配置、GPDMA/HPDMA/LPDMA 通道的请求，然后把配置
        挂到外设实例下；链表模式的请求只给出警告。
        Parse the requests and configurations under the Dma. and Bdma. prefixes and the
        channel requests of GPDMA/HPDMA/LPDMA, then attach the configurations to the peripheral
        instances; linked-list requests only get a warning.
        """
        for prefix, dma_type in (("Dma", "DMA"), ("Bdma", "BDMA")):
            self._parse_requests(prefix, dma_type)
            self._parse_configs(prefix, dma_type)
        self._parse_channel_requests()
        self._warn_linked_lists()
        self._link_configs()

    def _parse_channel_requests(self) -> None:
        """读取 GPDMA、HPDMA、LPDMA 通道的简单请求，每个请求生成一份配置。
        Read the simple requests of GPDMA, HPDMA and LPDMA channels, one configuration per
        request.

        STM32H5、U5、H7R/S、N6、WBA 等系列用这类控制器。配置包含请求目标（如 USART1_TX）、
        控制器类型和通道（如 GPDMA1_Channel0），有 DIRECTION_<通道> 时另存完整方向；_link_configs
        据此打开外设的 DMA 开关。
        Families such as STM32H5, U5, H7R/S, N6 and WBA use these controllers. A configuration
        holds the request target (such as USART1_TX), the controller type and the channel (such
        as GPDMA1_Channel0), and the complete direction when DIRECTION_<channel> is present;
        _link_configs then sets the DMA flags of the peripheral.
        """
        for key, value in self.raw_map.items():
            match = self._CHANNEL_REQUEST.match(key)
            if match is None:
                continue
            controller, channel, number = match.groups()
            marker = f"{controller}_REQUEST_"
            target = str(value).strip()
            if not target.startswith(marker):
                continue
            target = target[len(marker) :]
            request_key = f"{controller}.{channel}"
            dma_type = controller.rstrip("0123456789")
            self.config.dma_requests[request_key] = target
            self.config.dma_types[request_key] = dma_type
            structured = {
                "request_id": channel,
                "peripheral": target,
                "dma_type": dma_type,
                "stream": f"{controller}_Channel{number}",
            }
            direction = self.raw_map.get(f"{controller}.DIRECTION_{channel}")
            if direction:
                structured["direction_full"] = self._normalize_dma_direction(direction)
            self.config.dma_configs[f"{target}_{controller}_{channel}"] = structured

    def _warn_linked_lists(self) -> None:
        """链表模式的 DMA 请求（Linkedlist.*.Requestforcodegen）不读取，记录一条警告列出其目标。
        DMA requests in linked-list mode (Linkedlist.*.Requestforcodegen) are not read; one
        warning lists their targets.
        """
        targets = sorted(
            {
                str(value).split("_REQUEST_", 1)[1]
                for key, value in self.raw_map.items()
                if key.startswith("Linkedlist.")
                and key.endswith(".Requestforcodegen")
                and "_REQUEST_" in str(value)
            }
        )
        if targets:
            logging.warning(
                tr(
                    f"DMA requests in linked-list mode are not read: {', '.join(targets)}; "
                    "these peripherals get no DMA buffers",
                    f"链表模式的 DMA 请求不会识别：{'、'.join(targets)}；这些外设不生成 DMA 缓冲区",
                )
            )

    def _parse_requests(self, prefix="Dma", dma_type="DMA") -> None:
        """记录每个 <prefix>.RequestN 条目：请求 key 到目标外设信号的映射，以及 DMA 类型。
        Record each <prefix>.RequestN entry: the mapping from the request key to the target
        peripheral signal, and the DMA type.
        """
        for key, value in self.raw_map.items():
            req_id = self._dma_request_id(key, prefix)
            if req_id is not None:
                request_key = self._dma_request_key(prefix, req_id)
                # 请求目标外设以字符串保存。
                # Store peripheral as string
                self.config.dma_requests[request_key] = value
                # 记录 DMA 类型（DMA 或 BDMA）。
                # Store DMA type (DMA or BDMA)
                self.config.dma_types[request_key] = dma_type

    def _parse_configs(self, prefix="Dma", dma_type="DMA") -> None:
        """读取 <prefix>.<外设>.<N>[.<属性>] 条目，为每个外设与请求号生成一份结构化配置，
        存入 dma_configs["<外设>_<N>"]。
        Read <prefix>.<Periph>.<N>[.<Prop>] entries and build one structured configuration
        per peripheral and request number, stored in dma_configs["<Periph>_<N>"].

        配置包含请求号、请求目标、DMA 类型、stream 和经 _PROPERTY_MAP 转换的属性；有 Direction
        时另存完整方向 direction_full。转换失败的属性记录警告后跳过。
        A configuration holds the request number, the request target, the DMA type, the stream
        and the properties converted through _PROPERTY_MAP; a Direction property also gives
        direction_full, the complete direction. A property that fails to convert is logged as
        a warning and skipped.
        """
        config_map = defaultdict(dict)
        for key, value in self.raw_map.items():
            # 只处理格式为 <prefix>.<Periph>.<ReqID>[.<Prop>] 的 key。
            # Only process keys of format <prefix>.<Periph>.<ReqID>[.<Prop>]
            if not self._ioc_root_startswith(key, prefix):
                continue
            parts = self._split_ioc_key(key)
            if len(parts) < 3 or parts[0] != prefix:
                continue
            peripheral = parts[1]
            req_id = parts[2]
            if not req_id.isdigit():
                continue
            prop = parts[3] if len(parts) > 3 else "Instance"
            config_key = f"{peripheral}_{req_id}"
            config_map[config_key][prop] = value
            config_map[config_key]["_request_id"] = req_id

        # 把识别出的属性映射、转换成结构化的字典。
        # Map and convert all recognized properties into a structured dictionary
        for config_key, props in config_map.items():
            req_id = props.get("_request_id", "")
            request_key = self._dma_request_key(prefix, req_id)
            dma_type = self.config.dma_types.get(request_key, "DMA")
            structured = {
                "request_id": req_id,
                "peripheral": self.config.dma_requests.get(request_key, "Unknown"),
                "dma_type": dma_type,
                "stream": props.get("Instance", ""),
            }
            # 其余属性按 _PROPERTY_MAP 转换。
            # Convert all other properties using the property map
            for cube_prop, (field, converter) in self._PROPERTY_MAP.items():
                if cube_prop in props:
                    try:
                        structured[field] = converter(props[cube_prop])
                    except Exception as e:
                        logging.warning(
                            tr(
                                f"DMA property conversion failed for {config_key}.{cube_prop}: "
                                f"{str(e)}",
                                f"{config_key}.{cube_prop} 的 DMA 属性转换失败：{str(e)}",
                            )
                        )
            if "Direction" in props:
                structured["direction_full"] = self._normalize_dma_direction(props["Direction"])
            self.config.dma_configs[config_key] = structured

    def _link_configs(self) -> None:
        """把每份 DMA 配置挂到请求目标对应的外设实例下。
        Attach each DMA configuration to the peripheral instance its request targets.

        依次在 SPI、I2C、USART（含 UART 和 LPUART）、ADC、TIM 中查找同名实例，把配置存入其
        dma 字典的 dma_<方向> 或 dma 键。SPI、I2C、USART、ADC 的实例同时打开 DMA 开关，供生成
        缓冲区使用：方向为 tx/rx 时设 DMA_TX/DMA_RX 为 ENABLE 并记录 DMA 类型，没有方向时设
        DMA 为 ENABLE。
        The instance is looked up in SPI, I2C, USART (with UART and LPUART), ADC and TIM in that
        order, and the configuration goes into its dma dict under dma_<direction> or dma. For
        SPI, I2C, USART and ADC instances the DMA flags used for buffer generation are set too:
        a tx/rx direction sets DMA_TX/DMA_RX to ENABLE and records the DMA type, and no
        direction sets DMA to ENABLE.
        """
        for cfg in self.config.dma_configs.values():
            p_name, direction = self._parse_dma_request_endpoint(cfg["peripheral"])
            dma_type = cfg.get("dma_type", "DMA")
            for p_type in ("SPI", "I2C", "USART", "ADC", "TIM"):
                instance = self.config.peripherals.get(p_type, {}).get(p_name)
                if instance is None:
                    continue
                dir_key = f"dma_{direction}" if direction != "general" else "dma"
                instance.setdefault("dma", {})[dir_key] = cfg
                if p_type == "TIM":
                    break
                if direction == "tx":
                    instance["DMA_TX"] = "ENABLE"
                    instance["DMA_TX_TYPE"] = dma_type
                elif direction == "rx":
                    instance["DMA_RX"] = "ENABLE"
                    instance["DMA_RX_TYPE"] = dma_type
                elif direction == "general":
                    instance["DMA"] = "ENABLE"
                break


class ThreadXParser(PeripheralParser):
    """识别工程是否使用 ThreadX（Azure RTOS）。
    Recognizes whether the project uses ThreadX (Azure RTOS).
    """

    def parse(self, p_type: str) -> None:
        """有以下任一条目时记下工程使用 ThreadX：Mcu.IPn 为 THREADX，或有 THREADX.* key（CubeMX
        内置的 THREADX 中间件，如 STM32H5；参数全为默认值时 .ioc 中只有前者）；或有以
        AZRTOS_APP_MEM_ALLOCATION_METHOD 结尾的 key（X-CUBE-AZRTOS 扩展包）。
        Record that the project uses ThreadX when any of these entries exists: a Mcu.IPn of
        THREADX or THREADX.* keys (the THREADX middleware built into CubeMX, as on STM32H5; with
        every parameter at its default the .ioc file has only the former), or a key ending in
        AZRTOS_APP_MEM_ALLOCATION_METHOD (the X-CUBE-AZRTOS pack).
        """
        for key, value in self.raw_map.items():
            if (
                self._has_ioc_prefix(key, "THREADX")
                or (self._ioc_key_startswith(key, "Mcu.IP") and value == "THREADX")
                or key.endswith("AZRTOS_APP_MEM_ALLOCATION_METHOD")
            ):
                self.config.threadx = True


# --------------------------
# 看门狗解析器 / Watchdog Parser
# --------------------------
class WatchdogParser(PeripheralParser):
    """读取 IWDG 与 WWDG 实例的启用状态、预分频、重载值、窗口和计数值。
    Reads IWDG and WWDG instances: enable state, prescaler, reload, window and counter.
    """

    def parse(self, p_type: str) -> None:
        """读取看门狗条目，数值属性转为数值，Enable 转为布尔值 Enabled。
        Read watchdog entries; numeric properties become numbers and Enable becomes the
        boolean Enabled.

        VP_IWDG*_VS_IWDG.Mode 为 IWDG_Activate 时启用对应 IWDG 实例；IWDGx.* 读取 Prescaler、
        Reload、Window 和 Enable；WWDGx.* 读取 Prescaler、Window、Counter 和 Enable。
        VP_IWDG*_VS_IWDG.Mode set to IWDG_Activate enables that IWDG instance; IWDGx.* gives
        Prescaler, Reload, Window and Enable; WWDGx.* gives Prescaler, Window, Counter and
        Enable.
        """
        for key, value in self.raw_map.items():
            # 独立看门狗 IWDG
            # IWDG
            if (
                self._ioc_root_startswith(key, "VP_IWDG")
                and ".Mode" in key
                and value == "IWDG_Activate"
            ):
                # key 通常为 VP_IWDG_VS_IWDG.Mode；没有编号时归入实例 IWDG。
                # The key is usually VP_IWDG_VS_IWDG.Mode; no number means instance IWDG.
                match = re.match(r"VP_(IWDG\d*)_VS_IWDG\.Mode", key)
                if match:
                    # group(1) 为 "IWDG1"、"IWDG2" 或 ""。
                    # group(1) is "IWDG1", "IWDG2" or "".
                    wdg_name = match.group(1) or "IWDG"
                    self._ensure_wdg_instance("IWDG", wdg_name)
                    self.config.peripherals["IWDG"][wdg_name]["Enabled"] = True
            elif self._ioc_key_root(key).startswith("IWDG"):
                iwdg_name = self._ioc_key_root(key)  # IWDG 或 IWDG1 / IWDG or IWDG1
                self._ensure_wdg_instance("IWDG", iwdg_name)
                prop = self._ioc_key_prop(key)

                if prop == "Prescaler":
                    self.config.peripherals["IWDG"][iwdg_name]["Prescaler"] = sanitize_numeric(
                        value
                    )
                elif prop == "Reload":
                    self.config.peripherals["IWDG"][iwdg_name]["Reload"] = sanitize_numeric(value)
                elif prop == "Window":
                    self.config.peripherals["IWDG"][iwdg_name]["Window"] = sanitize_numeric(value)
                elif prop == "Enable":
                    self.config.peripherals["IWDG"][iwdg_name]["Enabled"] = value == "ENABLE"

            # 窗口看门狗 WWDG
            # WWDG
            elif self._ioc_key_root(key).startswith("WWDG"):
                wwdg_name = self._ioc_key_root(key)
                self._ensure_wdg_instance("WWDG", wwdg_name)
                prop = self._ioc_key_prop(key)

                if prop == "Prescaler":
                    self.config.peripherals["WWDG"][wwdg_name]["Prescaler"] = sanitize_numeric(
                        value
                    )
                elif prop == "Window":
                    self.config.peripherals["WWDG"][wwdg_name]["Window"] = sanitize_numeric(value)
                elif prop == "Counter":
                    self.config.peripherals["WWDG"][wwdg_name]["Counter"] = sanitize_numeric(value)
                elif prop == "Enable":
                    self.config.peripherals["WWDG"][wwdg_name]["Enabled"] = value == "ENABLE"

    def _ensure_wdg_instance(self, wdg_type: str, wdg_name: str) -> None:
        """wdg_type 类型下没有该实例时创建空字典。
        Create an empty dict for the instance under wdg_type when it does not exist.
        """
        if wdg_name not in self.config.peripherals[wdg_type]:
            self.config.peripherals[wdg_type][wdg_name] = {}


# --------------------------
# FreeRTOS 解析器 / FreeRTOS Parser
# --------------------------
class FreeRTOSParser(PeripheralParser):
    """识别工程是否使用 FreeRTOS。
    Recognizes whether the project uses FreeRTOS.
    """

    # STM32C0、H5、N6、U0、U3、U5、WBA 等系列的 FreeRTOS 来自这个扩展包，不是 FREERTOS 中间件。
    # FreeRTOS of STM32C0, H5, N6, U0, U3, U5, WBA and other families comes from this pack, not
    # from the FREERTOS middleware.
    _PACK = "STMicroelectronics.X-CUBE-FREERTOS"

    def parse(self, p_type: str) -> None:
        """有以下任一条目时记下工程使用 FreeRTOS：Mcu.IPn 为 FREERTOS，任何 FREERTOS.<参数>
        条目，或 Mcu.ThirdPartyN 为 X-CUBE-FREERTOS 扩展包（这类工程的任务写在用户代码中，
        .ioc 里没有）。
        Record that the project uses FreeRTOS when any of these entries exists: a Mcu.IPn of
        FREERTOS, any FREERTOS.<parameter> entry, or a Mcu.ThirdPartyN naming the
        X-CUBE-FREERTOS pack (the tasks of such a project live in user code, not in the .ioc
        file).
        """
        for key, value in self.raw_map.items():
            if (
                (self._ioc_key_startswith(key, "Mcu.ThirdParty") and value.startswith(self._PACK))
                or (self._ioc_key_startswith(key, "Mcu.IP") and value == "FREERTOS")
                or (
                    self._ioc_root_startswith(key, "FREERTOS") and len(self._split_ioc_key(key)) > 1
                )
            ):
                self.config.freertos = True


# --------------------------
# 解析主流程 / Core Parsing Workflow
# --------------------------
def parse_ioc_file(ioc_path: str, context: str | None = None) -> dict[str, Any] | None:
    """解析一个 .ioc 文件，返回清理后的配置结构；读取或解析失败时记录错误并返回 None。
    Parse one .ioc file and return the cleaned configuration; a read or parse failure is
    logged as an error and gives None.

    context 不为空时只保留全局设置和该 CubeMX 上下文（多核工程的一个核，如 CortexM7）拥有的
    条目（见 filter_ioc_context()）；上下文不存在时记录错误并返回 None。
    With a non-empty context, only global settings and the entries owned by that CubeMX context
    (one core of a multicore project, such as CortexM7) are kept (see filter_ioc_context());
    an unknown context is logged as an error and gives None.

    先读取时基（NVIC.TimeBaseIP、NVIC.TimeBase）和 GPIO，再运行各外设解析器；DMA 解析器最后
    运行，把 DMA 配置和开关挂到对应的外设实例下。之后去掉 CubeMX 不会为其生成 HAL 句柄的实例
    （见 _drop_ungenerated_instances()），检查外部中断引脚的 NVIC 设置，以及 CMSIS_V2 工程的
    FreeRTOS 事件标志设置（见 _check_os2_event_flags()）。
    The timebase (NVIC.TimeBaseIP, NVIC.TimeBase) and GPIO are read first, then each
    peripheral parser runs; the DMA parser runs last and attaches the DMA configurations and
    flags to the peripheral instances. Then the instances CubeMX generates no HAL handle for
    are dropped (see _drop_ungenerated_instances()), the NVIC setting of external interrupt
    pins is checked, and so is the FreeRTOS event-flags setting of a CMSIS_V2 project (see
    _check_os2_event_flags()).

    解析出错时记录错误；--verbose（调试日志）下同时记录调用栈。
    A parse error is logged; with --verbose (debug logging) the traceback is logged too.
    """
    config = ConfigurationManager()

    try:
        with open(ioc_path, encoding="utf-8") as f:
            raw_map = _extract_key_value_pairs(f)
    except UnicodeDecodeError as e:
        logging.error(
            tr(
                f"{ioc_path} is not UTF-8 text (byte {e.start + 1}); save it as UTF-8",
                f"{ioc_path} 不是 UTF-8 编码（第 {e.start + 1} 个字节）；请以 UTF-8 保存",
            )
        )
        return None
    except OSError as e:
        logging.error(tr(f"File processing failed: {str(e)}", f"文件处理失败：{str(e)}"))
        return None

    if context:
        # 上下文有误时不解析、不写 YAML；错误信息由 filter_ioc_context() 写好，这里补上 .ioc 路径。
        # A bad context parses nothing and writes no YAML; filter_ioc_context() writes the
        # message, and the path of the .ioc file is prefixed here.
        try:
            raw_map = filter_ioc_context(raw_map, context)
        except ValueError as error:
            logging.error(tr(f"{ioc_path}: {error}", f"{ioc_path}：{error}"))
            return None

    # 时基的特殊字段：单核文件写 NVIC.TimeBase*，多核文件按上下文分组写
    # NVIC1.TimeBase*、NVIC2.TimeBase*；过滤后剩下的 NVIC 分组就是本上下文的。
    # Timebase special fields parsing: a single-core file writes NVIC.TimeBase*, a
    # multicore one writes NVIC1.TimeBase* and NVIC2.TimeBase* per context group;
    # after filtering, the NVIC groups left are this context's.
    for key, value in raw_map.items():
        if re.fullmatch(r"NVIC\d*\.TimeBaseIP", key):
            config.timebase["Source"] = value
        elif re.fullmatch(r"NVIC\d*\.TimeBase", key):
            config.timebase["IRQ"] = value
    nvic_prefixes = _context_nvic_groups(raw_map, context) if context else ["NVIC"]
    _check_timebase(raw_map, config.timebase, nvic_prefixes)

    # 创建全部解析器。
    # Instantiate all parsers
    parsers = [
        ThreadXParser(config, raw_map),
        FreeRTOSParser(config, raw_map),
        McuParser(config, raw_map),
        TIMParser(config, raw_map),
        ADCParser(config, raw_map),
        DACParser(config, raw_map),
        SPIParser(config, raw_map),
        USARTParser(config, raw_map),
        I2CParser(config, raw_map),
        CANParser(config, raw_map),
        USBParser(config, raw_map),
        WatchdogParser(config, raw_map),
        DMAParser(config, raw_map),
    ]

    # 执行解析流程。
    # Execute parsing workflow
    try:
        # 阶段 1：通用的 GPIO 解析。
        # Phase 1: Common GPIO parsing
        parsers[0].parse_gpio()  # 各解析器都继承 GPIO 解析 / All parsers inherit GPIO capability

        # 阶段 2：各外设专用的解析。
        # Phase 2: Peripheral-specific parsing
        for parser in parsers:
            if isinstance(parser, McuParser):
                parser.parse("Mcu")
            else:
                # 去掉类名末尾的 'Parser'。
                # Strip 'Parser' suffix
                parser.parse(parser.__class__.__name__[:-6])

        _drop_ungenerated_instances(config, raw_map)
        _check_exti_nvic(raw_map, config.pin_registry, config.mcu_config.get("Family") or "")
        _check_os2_event_flags(raw_map, os.path.dirname(ioc_path))
        return config.clean_structure()
    except Exception as e:
        logging.error(tr(f"Parsing failed: {str(e)}", f"解析失败：{str(e)}"))
        logging.debug(tr("Traceback:", "调用栈："), exc_info=True)
        return None


def _check_timebase(
    raw_map: dict[str, str],
    timebase: dict[str, str | None],
    nvic_prefixes: list[str] | None = None,
) -> None:
    """HAL 时基仍是 SysTick，或其定时器中断的抢占优先级不是最高（0）时，记录警告。
    Warn when the HAL timebase is still SysTick, or when the preemption priority of
    its timer interrupt is not the highest (0).

    nvic_prefixes 是该上下文要查的 NVIC 分组前缀（如 ["NVIC2"]）；为空时按单核的 NVIC。
    nvic_prefixes names the NVIC group prefixes of the context to look in (such as
    ["NVIC2"]); empty means the single-core NVIC.
    """
    source = timebase.get("Source") or "SysTick"
    if source == "SysTick":
        logging.warning(
            tr(
                "The HAL timebase is SysTick. In STM32CubeMX, set SYS > Timebase Source to a "
                "general-purpose timer (such as TIM6) and give its interrupt the highest "
                "preemption priority (0) in NVIC.",
                "HAL 时基仍是 SysTick。请在 STM32CubeMX 的 SYS 中把 Timebase Source 改为普通"
                "定时器（例如 TIM6），并在 NVIC 中把它的中断抢占优先级设为最高（0）。",
            )
        )
        return
    irq = timebase.get("IRQ")
    # NVIC.<IRQ> 的值形如 true\:5\:0\:...，第二项是抢占优先级；多核文件中为 NVIC2.<IRQ>。
    # NVIC.<IRQ> reads like true\:5\:0\:..., the second field being the preemption
    # priority; a multicore file writes NVIC2.<IRQ>.
    entry = next(
        (
            raw_map.get(f"{prefix}.{irq}")
            for prefix in nvic_prefixes or ["NVIC"]
            if f"{prefix}.{irq}" in raw_map
        ),
        "",
    )
    fields = str(entry).replace("\\:", ":").split(":")
    if len(fields) > 1 and fields[1] != "0":
        logging.warning(
            tr(
                f"The HAL timebase interrupt {irq} ({source}) has preemption priority "
                f"{fields[1]}. In STM32CubeMX, set it to the highest (0) in NVIC.",
                f"HAL 时基中断 {irq}（{source}）的抢占优先级为 {fields[1]}。请在 STM32CubeMX "
                "的 NVIC 中把它设为最高（0）。",
            )
        )


def _init_functions(raw_map: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Project Manager 中每个 IP 的初始化函数设置，取自 ProjectManager.functionlistsort。
    The initialization function settings of each IP in the Project Manager, from
    ProjectManager.functionlistsort.

    条目形如 [false-]<序号>-<函数>-<IP>-<不调用>-<驱动>-<静态>[-<上下文>]：开头的 false 表示取消了
    Generate Code，<不调用> 为 true 表示勾选了 Do Not Generate Function Call，<驱动> 为 HAL 或
    LL。每个 IP 对应一个列表，多核或 TrustZone 工程的每个上下文各一项。
    An entry reads [false-]<rank>-<function>-<IP>-<no call>-<driver>-<static>[-<context>]: a
    leading false means Generate Code is off, <no call> true means Do Not Generate Function
    Call is set, and <driver> is HAL or LL. Each IP maps to a list with one item per context
    of a multi-core or TrustZone project.
    """
    functions: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in raw_map.get("ProjectManager.functionlistsort", "").split(","):
        parts = entry.strip().split("-")
        generate = True
        if parts[0] in ("true", "false"):
            generate = parts.pop(0) == "true"
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        functions[parts[2]].append(
            {
                "function": parts[1],
                "generate": generate,
                "called": parts[3] != "true",
                "driver": parts[4],
            }
        )
    return functions


def _drop_ungenerated_instances(config: ConfigurationManager, raw_map: dict[str, str]) -> None:
    """去掉 CubeMX 不会为其生成 HAL 句柄的外设实例，每个记录一条警告。
    Drop the peripheral instances CubeMX generates no HAL handle for, with one warning each.

    这些实例是：不在 Mcu.IPn 中，即没有启用、只在引脚上分配了信号的外设；Project Manager 中
    取消了 Generate Code 的外设；使用 LL 驱动的外设。勾选了 Do Not Generate Function Call 的
    外设保留，但警告 main() 不调用它的初始化函数（USB 除外，USB 对象默认不生成）。
    These are peripherals missing from Mcu.IPn, that is not enabled with only their signals
    assigned to pins; peripherals with Generate Code off in the Project Manager; and
    peripherals on the LL driver. A peripheral with Do Not Generate Function Call is kept, with
    a warning that main() does not call its initialization function (except USB, whose object
    is not generated by default).
    """
    enabled = {str(v).strip() for k, v in raw_map.items() if re.fullmatch(r"Mcu\.IP\d+", k)}
    functions = _init_functions(raw_map)
    for p_type, group in config.peripherals.items():
        for name in list(group):
            entries = [e for e in functions.get(name, []) if e["generate"]]
            if enabled and name not in enabled:
                reason = tr(
                    "it is not enabled in CubeMX (no Mcu.IP entry)",
                    "CubeMX 中没有启用它（Mcu.IP 中没有它）",
                )
            elif functions.get(name) and not entries:
                reason = tr(
                    "Generate Code is off for it in the CubeMX Project Manager",
                    "CubeMX 的 Project Manager 中取消了它的代码生成（Generate Code）",
                )
            elif entries and all(e["driver"] == "LL" for e in entries):
                reason = tr(
                    "it uses the LL driver in CubeMX, and LibXR needs a HAL handle",
                    "它在 CubeMX 中使用 LL 驱动，LibXR 需要 HAL 句柄",
                )
            else:
                uncalled = [e["function"] for e in entries if not e["called"]]
                if uncalled and p_type != "USB":
                    logging.warning(
                        tr(
                            f"{name}: main() does not call {uncalled[0]}() (Do Not Generate "
                            "Function Call is set in CubeMX); call it before app_main() "
                            "constructs the LibXR object",
                            f"{name}：main() 不调用 {uncalled[0]}()（CubeMX 中勾选了 Do Not "
                            "Generate Function Call）；须在 app_main() 构造 LibXR 对象之前调用它",
                        )
                    )
                continue
            del group[name]
            logging.warning(tr(f"{name} is not generated: {reason}", f"{name} 不会生成：{reason}"))


def _check_exti_nvic(raw_map: dict[str, str], pins: dict[str, dict], family: str) -> None:
    """外部中断引脚所在 EXTI 线的中断在 NVIC 中没有开启时记录警告。
    Warn about an external interrupt pin whose EXTI line has no interrupt enabled in NVIC.

    NVIC 没有开启时 CubeMX 不生成 EXTIx_IRQHandler；LibXR 的 EnableInterrupt() 打开中断后，
    中断会进入 Default_Handler。一个中断可以覆盖多条线，例如 EXTI9_5_IRQn 覆盖 5～9 线。
    STM32WB0 的引脚中断按端口分（GPIOA_IRQn），不检查。
    Without NVIC enabled CubeMX generates no EXTIx_IRQHandler; once EnableInterrupt() of LibXR
    enables the interrupt, it goes to Default_Handler. One interrupt may cover several lines,
    e.g. EXTI9_5_IRQn covers lines 5 to 9. STM32WB0 pin interrupts are per port (GPIOA_IRQn)
    and are not checked.
    """
    if family.startswith("STM32WB0"):
        return
    covered: set[int] = set()
    for key, value in raw_map.items():
        root, _, irq = key.partition(".")
        match = re.fullmatch(r"EXTI(\d+)(?:_(\d+))?_IRQn", irq)
        if not root.startswith("NVIC") or match is None:
            continue
        if str(value).replace("\\:", ":").split(":")[0] != "true":
            continue
        first, last = int(match.group(1)), int(match.group(2) or match.group(1))
        covered.update(range(min(first, last), max(first, last) + 1))
    for pin, cfg in pins.items():
        match = re.fullmatch(r"GPXTI(\d+)", str(cfg.get("Signal", "")))
        if match is None or int(match.group(1)) in covered:
            continue
        line = int(match.group(1))
        name = f"{pin} ({cfg['Label']})" if cfg.get("Label") else pin
        name_zh = f"{pin}（{cfg['Label']}）" if cfg.get("Label") else f"{pin} "
        logging.warning(
            tr(
                f"{name} is an external interrupt pin, but no NVIC interrupt of EXTI line {line} "
                "is enabled; enable it in STM32CubeMX NVIC, or the interrupt goes to "
                "Default_Handler once EnableInterrupt() is called",
                f"{name_zh}是外部中断引脚，但 NVIC 中没有开启 EXTI 线 {line} 的中断；请在 "
                "STM32CubeMX 的 NVIC 中开启，否则调用 EnableInterrupt() 后中断会进入 "
                "Default_Handler",
            )
        )


# FreeRTOSConfig.h 中把 configUSE_OS2_EVENTFLAGS_FROM_ISR 定义为 0 的行。
# A line of FreeRTOSConfig.h that defines configUSE_OS2_EVENTFLAGS_FROM_ISR as 0.
_OS2_EVENTFLAGS_FROM_ISR_OFF = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+configUSE_OS2_EVENTFLAGS_FROM_ISR[ \t]+\(?[ \t]*0\b", re.MULTILINE
)
# 在 FreeRTOSConfig.h 的 USER CODE BEGIN Defines 区中关闭软件定时器及依赖它的接口的覆盖块。
# The override block in the USER CODE BEGIN Defines section of FreeRTOSConfig.h that turns off
# the software timers and the interfaces that depend on them.
OS2_TIMER_OVERRIDES = (
    "/* USER CODE BEGIN Defines */",
    "#undef configUSE_TIMERS",
    "#define configUSE_TIMERS 0",
    "#undef configUSE_OS2_TIMER",
    "#define configUSE_OS2_TIMER 0",
    "#undef configUSE_OS2_EVENTFLAGS_FROM_ISR",
    "#define configUSE_OS2_EVENTFLAGS_FROM_ISR 0",
    "/* USER CODE END Defines */",
)


def _check_os2_event_flags(raw_map: dict[str, str], directory: str) -> None:
    """CMSIS_V2 工程关闭了 INCLUDE_xTimerPendFunctionCall，而 Core/Inc/FreeRTOSConfig.h 没有把
    configUSE_OS2_EVENTFLAGS_FROM_ISR 定义为 0 时，记录警告并附上覆盖块 OS2_TIMER_OVERRIDES。
    Warn, with the override block OS2_TIMER_OVERRIDES, when a CMSIS_V2 project turns off
    INCLUDE_xTimerPendFunctionCall and Core/Inc/FreeRTOSConfig.h does not define
    configUSE_OS2_EVENTFLAGS_FROM_ISR as 0.

    CMSIS-RTOS2 的 freertos_os2.h 在 INCLUDE_xTimerPendFunctionCall 为 0 而
    configUSE_OS2_EVENTFLAGS_FROM_ISR 为 1（默认值）时以 #error 停止编译。directory 是 .ioc 文件
    所在的工程目录；FreeRTOSConfig.h 还不存在（CubeMX 尚未生成代码）时同样警告。
    The freertos_os2.h of CMSIS-RTOS2 stops the build with #error when
    INCLUDE_xTimerPendFunctionCall is 0 and configUSE_OS2_EVENTFLAGS_FROM_ISR is 1, its default.
    directory is the project directory holding the .ioc file; a FreeRTOSConfig.h that does not
    exist yet, before CubeMX generated the code, gets the warning too.
    """
    if raw_map.get("VP_FREERTOS_VS_CMSIS_V2.Mode") != "CMSIS_V2":
        return
    if raw_map.get("FREERTOS.INCLUDE_xTimerPendFunctionCall") != "0":
        return
    config = "Core/Inc/FreeRTOSConfig.h"
    try:
        with open(
            os.path.join(directory, *config.split("/")), encoding="utf-8", errors="replace"
        ) as f:
            if _OS2_EVENTFLAGS_FROM_ISR_OFF.search(f.read()):
                return
    except OSError:
        pass
    logging.warning(
        tr(
            "FreeRTOS uses CMSIS_V2 with INCLUDE_xTimerPendFunctionCall = 0, and "
            f"{config} does not set configUSE_OS2_EVENTFLAGS_FROM_ISR to 0, so freertos_os2.h "
            '#error "Definition INCLUDE_xTimerPendFunctionCall must equal 1 to implement Event '
            'Flags API." stops the build. Override these definitions in the USER CODE BEGIN '
            f"Defines section of {config}, which CubeMX keeps when it regenerates the code:",
            "FreeRTOS 使用 CMSIS_V2 且 INCLUDE_xTimerPendFunctionCall = 0，而 "
            f"{config} 没有把 configUSE_OS2_EVENTFLAGS_FROM_ISR 设为 0，编译时 freertos_os2.h 报错 "
            '#error "Definition INCLUDE_xTimerPendFunctionCall must equal 1 to implement Event '
            'Flags API."。请在 '
            f"{config} 的 USER CODE BEGIN Defines 区中覆盖以下定义，CubeMX 重新生成代码时保留该区域：",
        )
    )
    for line in OS2_TIMER_OVERRIDES:
        logging.warning(f"  {line}")


def _extract_key_value_pairs(file_handler: TextIO) -> dict[str, str]:
    """把 .ioc 文本读成 key=value 表，key 和值去掉首尾空白。
    Read .ioc text into a key=value map, with keys and values stripped.

    跳过空行和以 # 开头的行；key 中转义的 # 连同反斜杠一起去掉；没有 = 的行记录警告后跳过。
    Empty lines and lines starting with # are skipped; an escaped # in a key is removed
    together with its backslash; a line without = is logged as a warning and skipped.
    """
    raw_map = {}
    for line_num, line in enumerate(file_handler, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        try:
            key, value = map(str.strip, line.split("=", 1))
            raw_map[key.replace("\\#", "")] = value
        except ValueError:
            logging.warning(
                tr(
                    f"Ignored malformed entry at line {line_num}: {line}",
                    f"忽略第 {line_num} 行格式错误的条目：{line}",
                )
            )

    return raw_map


# --------------------------
# CubeMX 上下文 / CubeMX Contexts
# --------------------------
_IOC_CONTEXT_KEY_RE = re.compile(r"^Mcu\.Context(\d+)$", re.IGNORECASE)


def _ioc_context_name(value: str) -> str:
    """归一化上下文名，如 CM7、CortexM7 和 Cortex_M7 都变成 CORTEXM7。
    Normalize a context name, so CM7, CortexM7 and Cortex_M7 all become CORTEXM7.
    """
    normalized = str(value).strip().replace("_", "").replace("-", "").replace("+", "PLUS").upper()
    if re.fullmatch(r"CM\d+(?:PLUS)?", normalized):
        return f"CORTEXM{normalized[2:]}"
    return normalized


def _context_aliases(context: str) -> set[str]:
    """一个上下文在 .ioc 中可能出现的几种写法。
    The spellings one context may use in an .ioc file.
    """
    normalized = _ioc_context_name(context)
    if normalized.startswith("CORTEXM"):
        core = normalized[len("CORTEXM") :]
        return {normalized, f"CM{core}", f"CORTEX_M{core}"}
    return {normalized}


def _unknown_context_error(context: str) -> ValueError:
    """找不到上下文的 ValueError；信息中说明上下文是多核工程的一个核。
    The ValueError of a context the .ioc file does not hold; its message says that a context
    names one core of a multicore project.
    """
    return ValueError(
        tr(
            f"Unknown CubeMX context: {context}; a context names one core of a multicore project",
            f"找不到 CubeMX 上下文 {context}；上下文指定多核工程的一个核",
        )
    )


def _split_ip_list_entry(item: str) -> tuple[str, bool]:
    """拆开一条 IP 列表条目：IP 名和它是否带 `\\:I`（该上下文初始化它）。
    Split one IP list entry into its IP name and whether it carries `\\:I`, meaning
    this context initializes it.

    多核 .ioc 中一个外设可以只由一个核初始化：初始化它的核列出 `USART3\\:I`，另一个核
    列出不带标记的 `USART3`，只作参考。
    A peripheral of a multicore project can be initialized by one core only: that
    core lists `USART3\\:I` while the other lists `USART3` without the marker, for
    reference only.
    """
    item = item.strip().replace("\\:", ":")
    if not item:
        return "", False
    name, _, marker = item.partition(":")
    return name.strip(), marker.strip().upper() == "I"


def _context_ip_lists(raw_map: dict[str, str]) -> dict[str, str]:
    """每个上下文的 IP 列表原文，键是上下文在 .ioc 中的写法。
    The IP list of every context as written in the .ioc file, keyed by its spelling.
    """
    lists: dict[str, str] = {}
    for key, value in raw_map.items():
        if not _IOC_CONTEXT_KEY_RE.fullmatch(key):
            continue
        name = str(value).strip()
        ip_key = f"{name}.IPs"
        if name and ip_key in raw_map:
            lists[name] = raw_map[ip_key]
    return lists


def _ip_lists_use_init_markers(raw_map: dict[str, str]) -> bool:
    """任一上下文的 IP 列表有条目带 `\\:I` 时为 True。
    True when any context's IP list has an entry carrying `\\:I`.

    CubeMX 6 起多核工程用 `\\:I` 标出初始化外设的核；早于它的文件不带标记，那时列表中
    的条目都归该上下文。
    CubeMX 6 marks the core that initializes a peripheral with `\\:I`; files written
    before it carry no marker, and then every entry belongs to its context.
    """
    return any(
        marked
        for ip_list in _context_ip_lists(raw_map).values()
        for _, marked in (_split_ip_list_entry(item) for item in str(ip_list).split(","))
    )


def _owned_ip_names(ip_list: str, init_marked_only: bool) -> set[str]:
    """一条 IP 列表中归该上下文的 IP 名。
    The IP names of one list that its context owns.

    init_marked_only 为真时只取带 `\\:I` 的条目；文件不用 `\\:I` 标记（早于 CubeMX 6）
    时为 False，全部条目都算该上下文的。
    With init_marked_only only the entries carrying `\\:I` count; a file that does not
    use the marker, one written before CubeMX 6, passes False and every entry belongs
    to its context.
    """
    names: set[str] = set()
    for item in str(ip_list).split(","):
        name, marked = _split_ip_list_entry(item)
        if name and (marked or not init_marked_only):
            names.add(name)
    return names


def _context_ip_names(raw_map: dict[str, str], context: str) -> set[str]:
    """返回一个 CubeMX 上下文拥有的 IP 实例名。
    Return the IP instance names assigned to a CubeMX context.

    上下文不存在或没有 IP 列表时抛出 ValueError。`\\:I` 标出初始化外设的核，文件用了
    标记时只有带标记的条目归该上下文（见 _ip_lists_use_init_markers()），两个核都列出
    但不带标记的外设归另一个核；上下文自身的名字也归它。
    An unknown context or a context without an IP list raises ValueError. `\\:I` marks
    the core that initializes a peripheral, so with the marker only marked entries
    belong to the context (see _ip_lists_use_init_markers()) and a peripheral listed
    by both cores without the marker belongs to the other core; the context's own
    name belongs to it as well.
    """
    normalized_context = _ioc_context_name(context)
    context_key = next(
        (
            key
            for key in raw_map
            if _IOC_CONTEXT_KEY_RE.fullmatch(key)
            and _ioc_context_name(raw_map[key]) == normalized_context
        ),
        None,
    )
    if context_key is None:
        raise _unknown_context_error(context)

    context_name = raw_map[context_key]
    ip_key = next((key for key in raw_map if key == f"{context_name}.IPs"), None)
    if ip_key is None:
        raise ValueError(
            tr(
                f"CubeMX context {context_name} has no IP list; the .ioc file is incomplete",
                f"CubeMX 上下文 {context_name} 没有 IP 列表；.ioc 文件不完整",
            )
        )

    names = _owned_ip_names(raw_map[ip_key], _ip_lists_use_init_markers(raw_map))
    names.add(context_name.upper())
    names.add(normalized_context)
    return names


def _context_ip_names_by_context(raw_map: dict[str, str]) -> dict[str, set[str]]:
    """返回每个上下文拥有的 IP 名，用于虚拟引脚的归属判断。
    Return the IP names owned by each context, for virtual-pin ownership checks.

    归属规则与 _context_ip_names() 相同：文件用 `\\:I` 标记时只取带标记的条目。
    The same rule as _context_ip_names() applies: with `\\:I` markers only the marked
    entries count.
    """
    init_marked_only = _ip_lists_use_init_markers(raw_map)
    result: dict[str, set[str]] = {}
    for key, context_name in raw_map.items():
        if not _IOC_CONTEXT_KEY_RE.fullmatch(key) or not context_name.strip():
            continue
        ip_key = f"{context_name}.IPs"
        if ip_key not in raw_map:
            continue
        normalized_context = _ioc_context_name(context_name)
        names = result.setdefault(normalized_context, set())
        names.update(_owned_ip_names(raw_map[ip_key], init_marked_only))
        names.add(context_name.upper())
        names.add(normalized_context)
    return result


def _context_nvic_groups(raw_map: dict[str, str], context_name: str) -> list[str]:
    """一个上下文的 NVIC 分组名（NVIC1、NVIC2 等）。
    The NVIC group names of a context (NVIC1, NVIC2 and so on).

    分组号取自该上下文 IP 列表中的 `NVIC<n>` 条目：Mcu.Context0=CortexM7 的列表写
    `NVIC1\\:I`、Mcu.Context1=CortexM4 的写 `NVIC2\\:I`，CubeMX 6.1.0 起如此。IP 列表
    没有 NVIC 条目时退回 Mcu.ContextN 的序号 +1；两者都没有时按单核的 `NVIC`。
    The group number comes from the `NVIC<n>` entry of the context's own IP list:
    Mcu.Context0=CortexM7 lists `NVIC1\\:I` and Mcu.Context1=CortexM4 lists
    `NVIC2\\:I`, the same since CubeMX 6.1.0. Without an NVIC entry the number is
    the index of the Mcu.ContextN key plus one; without either, the single-core
    `NVIC` applies.
    """
    names: list[str] = []
    ip_key = f"{context_name}.IPs"
    if ip_key in raw_map:
        for item in str(raw_map[ip_key]).split(","):
            name, _ = _split_ip_list_entry(item)
            if re.fullmatch(r"NVIC\d+", name):
                names.append(name)
    if names:
        return names
    normalized = _ioc_context_name(context_name)
    for key, value in raw_map.items():
        match = _IOC_CONTEXT_KEY_RE.fullmatch(key)
        if match and _ioc_context_name(value) == normalized:
            names.append(f"NVIC{int(match.group(1)) + 1}")
            break
    return names or ["NVIC"]


def _filter_functionlistsort(value: str, context_name: str, all_contexts: set[str]) -> str:
    """只保留 ProjectManager.functionlistsort 中属于一个上下文的条目。
    Keep only the ProjectManager.functionlistsort entries that belong to one context.

    每个条目的末段是所属上下文的写法（如 `...-CortexM7`）；不带上下文名的条目（单核
    工程）保留。另一个核的条目丢掉：其中的驱动（HAL/LL）和 Generate Code 只对该核的
    生成有效，留着会把本核的外设误判成使用 LL 驱动或取消了代码生成。
    The last field of an entry is the context it belongs to (such as `...-CortexM7`);
    an entry without a context, as in a single-core project, is kept. The other
    core's entries are dropped: their driver (HAL/LL) and Generate Code apply to that
    core's generation only, and keeping them would misjudge this core's peripherals
    as LL-driven or as having generation off.
    """
    normalized = _ioc_context_name(context_name)
    kept: list[str] = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        trailing = _ioc_context_name(entry.rsplit("-", 1)[-1])
        if trailing in all_contexts and trailing != normalized:
            continue
        kept.append(entry)
    return ",".join(kept)


def filter_ioc_context(raw_map: dict[str, str], context: str) -> dict[str, str]:
    """保留全局的 .ioc 设置和一个 CubeMX 上下文拥有的条目。
    Keep the global .ioc settings and the entries owned by one CubeMX context.

    上下文不存在时抛出 ValueError。Mcu 和 RCC 的条目、本上下文的 NVIC 分组、DEBUG
    条目和共享条目（SH.*）是全局的；引脚按 PinAttribute/ContextOwner 归属，
    PinAttribute=Free 的引脚（没有指定核的共享引脚）每个核都保留；虚拟引脚归给 IP 列表
    里信号前缀最长的上下文（如 SYS 与 SYS_M4）；其余条目按 IP 实例名或上下文名归属，
    IP 实例只归其带 `\\:I` 的上下文；ProjectManager.functionlistsort 只保留本上下文的
    条目，其中的驱动和 Generate Code 信息才不会丢失。
    An unknown context raises ValueError. The Mcu and RCC entries, the NVIC groups of
    this context, the DEBUG entries and the shared entries (SH.*) are global; pins
    follow PinAttribute/ContextOwner, and a pin with PinAttribute=Free, shared without
    a named core, is kept for every core; a virtual pin goes to the context whose IP
    list owns the longest matching signal prefix (such as SYS versus SYS_M4); the
    remaining entries follow the IP instance name or the context name, an IP instance
    going only to the context that lists it with `\\:I`; ProjectManager.functionlistsort
    keeps this context's entries only, so their driver and Generate Code information
    does not get lost.
    """
    aliases = _context_aliases(context)
    normalized_context = _ioc_context_name(context)
    context_suffix = (
        normalized_context[len("CORTEXM") :]
        if normalized_context.startswith("CORTEXM")
        else normalized_context
    )
    context_name = next(
        (
            value
            for key, value in raw_map.items()
            if _IOC_CONTEXT_KEY_RE.fullmatch(key) and _ioc_context_name(value) == normalized_context
        ),
        None,
    )
    if context_name is None:
        raise _unknown_context_error(context)

    ip_names = _context_ip_names(raw_map, context_name)
    all_context_ip_names = _context_ip_names_by_context(raw_map)
    nvic_groups = _context_nvic_groups(raw_map, context_name)
    all_contexts = {
        _ioc_context_name(value)
        for key, value in raw_map.items()
        if _IOC_CONTEXT_KEY_RE.fullmatch(key) and value.strip()
    }
    kept: dict[str, str] = {}

    for key, value in raw_map.items():
        prefix = key.split(".", 1)[0]
        upper_prefix = prefix.upper()

        # MCU 元数据和共享的时钟配置是每个核都需要的。
        # MCU metadata and shared clock configuration are needed by every core.
        if prefix in {"Mcu", "RCC"}:
            kept[key] = value
            continue

        # 本上下文的 NVIC 分组、未编号的 NVIC（单核写法）和 DEBUG 是每个核都需要的。双核
        # 文件按上下文给分组编号：NVIC1 归 Mcu.Context0 的核，NVIC2 归下一个核，分组号
        # 与 Cortex-M 的型号无关。
        # The NVIC groups of this context, the unnumbered NVIC of a single-core file
        # and DEBUG are needed by every core. A dual-core file numbers the groups by
        # context: NVIC1 belongs to the core of Mcu.Context0 and NVIC2 to the next
        # one, whatever the Cortex-M model number is.
        if upper_prefix in nvic_groups or upper_prefix in {"NVIC", "DEBUG"}:
            kept[key] = value
            continue

        # 共享外设条目（SH.*）两个核的解析器都要读：TIM 通道模式、DAC 组和共享的 GPIO
        # 外部中断线都写在这里，丢掉会把 TIM 输入捕获通道误判成 PWM 通道。
        # The shared entries (SH.*) are read by both cores' parsers: the TIM channel
        # modes, the DAC groups and the shared GPIO external interrupt lines live
        # here, and dropping them misjudges a TIM input capture channel as PWM.
        if prefix == "SH":
            kept[key] = value
            continue

        # Project Manager 的设置每个核都要用；functionlistsort 只留本上下文的条目。
        # The Project Manager settings are used by every core; functionlistsort keeps
        # this context's entries only.
        if prefix == "ProjectManager":
            if key == "ProjectManager.functionlistsort":
                kept[key] = _filter_functionlistsort(value, context_name, all_contexts)
            else:
                kept[key] = value
            continue

        # 双核 .ioc 文件中引脚的归属是显式写出的；PinAttribute=Free 的引脚没有指定核，
        # 两个核都可能用到，都保留。
        # Pin ownership is explicit in dual-core IOC files; a pin with
        # PinAttribute=Free names no core, both cores may use it, both keep it.
        if ".PinAttribute" in key or ".ContextOwner" in key:
            if _ioc_context_name(value) in aliases or value.strip().lower() == "free":
                kept[key] = value
            continue
        # 端口号到 Z：Nucleo 板上有 L 以后的端口（如 PL0），丢掉会少引脚配置。
        # Port letters up to Z: Nucleo boards carry ports beyond L (PL0 for one),
        # and dropping them loses pin configuration.
        if "." in key and re.match(r"^P[A-Z]\d+", prefix):
            owner = raw_map.get(f"{prefix}.PinAttribute") or raw_map.get(f"{prefix}.ContextOwner")
            if (
                owner is None
                or _ioc_context_name(owner) in aliases
                or owner.strip().lower() == "free"
            ):
                kept[key] = value
            continue

        # 虚拟引脚归给 IP 列表拥有最长匹配信号前缀的上下文（如 SYS 与 SYS_M4）。
        # Virtual pins go to the context whose IP list owns the longest matching signal
        # prefix (for example SYS versus SYS_M4).
        if upper_prefix.startswith("VP_"):
            signal = upper_prefix[3:].replace("_", "").replace("-", "")
            owners = []
            for owner, owner_ip_names in all_context_ip_names.items():
                for ip_name in owner_ip_names:
                    normalized_ip = (
                        str(ip_name).replace("_", "").replace("-", "").replace("+", "PLUS").upper()
                    )
                    if normalized_ip and signal.startswith(normalized_ip):
                        owners.append((len(normalized_ip), owner))
            if owners:
                longest = max(length for length, _ in owners)
                if normalized_context in {owner for length, owner in owners if length == longest}:
                    kept[key] = value
            elif any(alias.replace("_", "") in upper_prefix.replace("_", "") for alias in aliases):
                kept[key] = value
            continue

        if upper_prefix in {name.upper() for name in ip_names}:
            kept[key] = value
            continue

        # 保留本上下文的系统和用户名条目，丢掉其他核的。
        # Keep the context-specific system/user-name entries and discard the other cores'.
        if context_name and context_name.upper() in upper_prefix:
            kept[key] = value
            continue
        if context_suffix and upper_prefix.endswith(f"_M{context_suffix}"):
            kept[key] = value
            continue

    return kept


# --------------------------
# 输出生成 / Output Generation
# --------------------------
class _NoAliasDumper(yaml.SafeDumper):
    """不生成锚点和别名的 YAML Dumper：同一对象出现多次时每处都完整写出。
    A YAML dumper without anchors and aliases: an object that appears several times is written
    out in full each time.
    """

    def ignore_aliases(self, data: Any) -> bool:
        """总是忽略别名。
        Always ignore aliases.
        """
        return True


def save_to_yaml(data: dict[str, Any], output_path: str = "parsed_ioc.yaml") -> bool:
    """把配置写成 YAML 文件，首行为生成文件说明注释，键保持插入顺序；成功时为 True。
    Write the configuration as a YAML file with a generated-file comment on the first line and
    keys in insertion order; True on success.

    输出目录不存在时先创建。同一份 DMA 配置会同时出现在外设和 DMA 段中，两处都完整写出，不用
    锚点引用。写入或序列化失败时记录错误并返回 False。
    A missing output directory is created first. A DMA configuration appears under its
    peripheral and in the DMA section, and both places are written in full rather than as an
    anchor reference. A write or serialization failure is logged as an error and gives False.
    """
    try:
        directory = os.path.dirname(output_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(output_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                "# Generated by `libxr parse` from the CubeMX .ioc file; do not edit by hand.\n"
            )
            yaml.dump(
                data,
                f,
                Dumper=_NoAliasDumper,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
                indent=2,
            )
        logging.info(
            tr(f"Configuration exported to: {output_path}", f"配置已导出到：{output_path}")
        )
        return True
    except (OSError, yaml.YAMLError) as e:
        logging.error(tr(f"YAML export failed: {str(e)}", f"YAML 导出失败：{str(e)}"))
        return False


def print_summary(data: dict[str, Any]) -> None:
    """向标准输出打印配置摘要：MCU、GPIO 输出/输入/外部中断数量和各外设实例（含看门狗）。
    Print a configuration summary to standard output: the MCU, GPIO output/input/external
    interrupt counts and each peripheral instance, watchdogs included.
    """
    print(tr("\n===== [Configuration Summary] =====", "\n===== [配置摘要] ====="))

    # MCU 信息
    # MCU Info
    mcu = data.get("Mcu", {})
    family = mcu.get("Family") or tr("Unknown", "未知")
    print(tr(f"\nMCU: {family} {mcu.get('Type', '')}", f"\nMCU：{family} {mcu.get('Type', '')}"))

    # GPIO 摘要
    # GPIO Summary
    gpio = data.get("GPIO", {})
    outputs = sum(1 for c in gpio.values() if c.get("Signal") == "GPIO_Output")
    inputs = sum(1 for c in gpio.values() if c.get("Signal") == "GPIO_Input")
    interrupts = sum(1 for c in gpio.values() if c.get("GPXTI"))
    print(tr(f"\nGPIO ({len(gpio)} pins):", f"\nGPIO（{len(gpio)} 个引脚）："))
    print(tr(f"  Outputs: {outputs}", f"  输出：{outputs}"))
    print(tr(f"  Inputs: {inputs}", f"  输入：{inputs}"))
    print(tr(f"  External Interrupts: {interrupts}", f"  外部中断：{interrupts}"))

    # 外设摘要
    # Peripheral Summary
    print(tr("\nActive Peripherals:", "\n已启用的外设："))
    for p_type, group in data.get("Peripherals", {}).items():
        print(tr(f"  {p_type}: {len(group)} instance(s)", f"  {p_type}：{len(group)} 个实例"))
        for name, cfg in group.items():
            details = _format_peripheral_config(p_type, cfg)
            print(f"    {name}: {details}" if details else f"    {name}")


def _format_peripheral_config(p_type: str, config: dict) -> str:
    """外设实例的一行摘要，只列出有值的字段：TIM 为模式、周期、预分频和 PWM 通道，ADC 为规则
    转换通道数，DAC 为通道，SPI、USART、CAN、FDCAN 为波特率，I2C 为时钟速度和时序，IWDG、WWDG
    为启用状态、预分频、重载值、窗口和计数值；其他类型以及没有可列字段时为空字符串。
    A one-line summary of a peripheral instance listing only the fields that have a value:
    mode, period, prescaler and PWM channels for TIM, the number of regular conversion channels
    for ADC, the channels for DAC, the baud rate for SPI, USART, CAN and FDCAN, clock speed and
    timing for I2C, and enable state, prescaler, reload, window and counter for IWDG and WWDG;
    an empty string for other types or when no field has a value.
    """
    fields: list[tuple[str, Any]] = []
    if p_type == "TIM":
        fields = [
            ("Mode", config.get("Mode")),
            ("Period", config.get("Period")),
            ("Prescaler", config.get("Prescaler")),
            ("Channels", ",".join(config.get("Channels", {}))),
        ]
    elif p_type == "ADC":
        fields = [("Channels", len(config.get("RegularConversions", [])) or None)]
    elif p_type == "DAC":
        fields = [("Channels", ",".join(config.get("Channels", {})))]
    elif p_type in ("SPI", "USART", "CAN", "FDCAN"):
        fields = [("BaudRate", config.get("BaudRate"))]
    elif p_type == "I2C":
        fields = [("ClockSpeed", config.get("ClockSpeed")), ("Timing", config.get("Timing"))]
    elif p_type in ("IWDG", "WWDG"):
        fields = [
            (name, config.get(name))
            for name in ("Enabled", "Prescaler", "Reload", "Window", "Counter")
        ]
    return " | ".join(f"{name}={value}" for name, value in fields if value not in (None, ""))


# --------------------------
# 工程入口 / Project Entry
# --------------------------
def parse_project(
    directory: str, output: str | None = None, summary: bool = True, context: str | None = None
) -> None:
    """解析 directory 中唯一的 .ioc 文件并写出 YAML；summary 为真时打印摘要。
    Parse the single .ioc file in directory and write the YAML; print a summary when summary
    is set.

    context 不为空时只解析该 CubeMX 上下文（多核工程的一个核）拥有的条目；输出的 YAML 描述这
    一个核的硬件。context 为 .ioc 中没有的上下文时记录错误并以状态 1 退出。
    With a non-empty context only the entries owned by that CubeMX context (one core of a
    multicore project) are parsed; the YAML then describes that one core's hardware. An unknown
    context is logged as an error, and the command exits with status 1.

    输出路径默认为该目录下的 .config.yaml。调用方（libxr parse 和 setup）已确认目录存在且含有
    .ioc 文件；有多个 .ioc 文件时以状态 1 退出。
    The output defaults to .config.yaml in that directory. The callers, libxr parse and setup,
    have checked that the directory exists and holds an .ioc file; several .ioc files exit with
    status 1.
    """
    ioc_files = sorted(f for f in os.listdir(directory) if f.endswith(".ioc"))
    if len(ioc_files) > 1:
        # 一个目录对应一个 CubeMX 工程；多个 .ioc 会写进同一个输出文件。
        # One directory is one CubeMX project; several .ioc files would share the output.
        logging.error(
            tr(
                f"{directory} holds several .ioc files ({', '.join(ioc_files)}); "
                "run `libxr parse` on a directory with one CubeMX project",
                f"{directory} 中有多个 .ioc 文件（{'、'.join(ioc_files)}）；"
                "请对只含一个 CubeMX 工程的目录运行 `libxr parse`",
            )
        )
        sys.exit(1)

    ioc_file = ioc_files[0]
    logging.info(tr(f"Processing {ioc_file}...", f"正在处理 {ioc_file}……"))
    config_data = parse_ioc_file(os.path.join(directory, ioc_file), context)
    if config_data:
        # libxr gen 在生成文件的说明中写出 .ioc 文件名。
        # libxr gen names the .ioc file in the notice of the files it generates.
        config_data = {"Platform": config_data["Platform"], "Ioc": ioc_file, **config_data}
    output_path = output or os.path.join(directory, ".config.yaml")
    if not config_data or not save_to_yaml(config_data, output_path):
        sys.exit(1)
    if summary:
        print_summary(config_data)


if __name__ == "__main__":
    from libxr.legacy import run

    raise SystemExit(run("xr_parse_ioc"))
