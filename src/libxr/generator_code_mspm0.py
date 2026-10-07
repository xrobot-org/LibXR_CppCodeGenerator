"""libxr gen 对 MSPM0 工程：由工程 YAML 生成 User/app_main.cpp 和 libxr_config.yaml。
libxr gen for an MSPM0 project: generate User/app_main.cpp and libxr_config.yaml from the
project YAML.

时钟、引脚、DMA 通道和外设都由 SysConfig 配置（main.c 里的 SYSCFG_DL_init()）；生成代码只声明
LibXR 对象并把它们登记进 XRobot。构造全部使用 SysConfig 的宏（UART_0_INST、GPIO_LEDS_PIN_LED1_PORT
……），布局沿 bsp_stm32f103。缓冲区大小、队列长度和终端设置来自 libxr_config.yaml（首次生成把
默认值写进该文件，已有值保持不变）。
Clocks, pins, DMA channels and peripherals are configured by SysConfig (SYSCFG_DL_init() in
main.c); the generated code only declares the LibXR objects and registers them with XRobot.
Every construction uses the SysConfig macros (UART_0_INST, GPIO_LEDS_PIN_LED1_PORT, ...), and
the layout follows bsp_stm32f103. The buffer sizes, queue lengths and terminal settings come
from libxr_config.yaml (the first generation writes the defaults into the file and keeps the
values that are already there).

时钟（M5）：PD1 的 BUSCLK 是 CPUCLK_FREQ；PD0 的是 CPUCLK_FREQ / ULPCLK 分频（生成代码里的
DL_SYSCTL_setULPCLKDivider，默认 1）。I2C 驱动自己重设时钟（BUSCLK、分频 1），传电源域的
BUSCLK 即可；SPI 驱动沿用 SysConfig 写下的时钟配置，传入时钟是 SysConfig 选的时钟源（BUSCLK、
MFCLK 4 MHz 或 LFCLK 32.768 kHz）除以它的 divideRatio，其他时钟源报错。SysConfig 不为 I2C 和
SPI 导出时钟宏，工程 YAML 缺少电源域或 CPUCLK 时报错。UART 用 SysConfig 的 INST_FREQUENCY 宏，
PWM 的源时钟由驱动从 INST_CLK_FREQ 求出，都不需要生成器计算。
Clocks (M5): the BUSCLK of PD1 is CPUCLK_FREQ, that of PD0 is CPUCLK_FREQ / the ULPCLK divider
(the DL_SYSCTL_setULPCLKDivider of the generated code, 1 by default). The I2C driver resets the
clock configuration itself (BUSCLK, divider 1), so the BUSCLK of the power domain is the right
argument; the SPI driver keeps the clock configuration SysConfig wrote, so its argument is the
clock source SysConfig chose (BUSCLK, MFCLK at 4 MHz or LFCLK at 32.768 kHz) divided by its
divideRatio, and any other source is an error. SysConfig exports no clock macro for an I2C or
an SPI, so a project YAML without the power domain or CPUCLK is an error. A UART takes the
INST_FREQUENCY macro of SysConfig and the source clock of a PWM is derived by the driver from
INST_CLK_FREQ, so neither needs a generator-side computation.

设置（libxr_config.yaml）：值为 null 时用默认值，缓冲区和队列必须是正整数，UART 的
tx_buffer_size 还要满足驱动的对齐和 DMA 长度断言；PWM 的 frequency 为 null 时不调用
SetConfig。terminal_source 沿用 STM32 的约定（空值或 none 不生成终端），只在文件里还没有这个键
时选第一个 UART。设置键不分大小写，文件里的写法保持不变。
Settings (libxr_config.yaml): a null value takes the default, the buffers and queues must be
positive integers and the tx_buffer_size of a UART must also meet the alignment and DMA length
assertions of the driver; a null PWM frequency calls no SetConfig. terminal_source follows the
STM32 convention (an empty value or none generates no terminal), and the first UART is chosen
only while the file has no such key. Settings keys match without regard to case, and the
spelling of the file is kept.

对象名：GPIO 用引脚标签（$name 去掉 PIN_ 前缀），UART/I2C/SPI 用外设名的小写（uart0），PWM 是
pwm_<timer>_c<n>。GPIO 中断遵循 STM32 的约定（模型 O1）：生成器只创建对象，边沿来自 SysConfig；
RegisterCallback、EnableInterrupt 和回调函数属于消费者（Module 或 User Code）。UART 没有 DMA TX
（或 SPI 没有一对 DMA 通道）时驱动没有对应的构造路径，生成报错。UART 默认用
MSPM0_UART_MAIN_INIT（接收走字节中断）；配了 DMA RX 的 UART Extend 实例用
MSPM0_UART_EXTEND_INIT（接收走 FULL-DMA 通道上的循环 DMA，接收环大小是 rx_dma_buffer_size），
RX 通道不是 FULL-DMA 通道时报错；UART Main 实例上的 DMA RX 通道用不上，给出警告。
Object names: a GPIO takes the pin label (the $name without its PIN_ prefix), a UART/I2C/SPI
the lower-case peripheral name (uart0), and a PWM is pwm_<timer>_c<n>. GPIO interrupts follow
the STM32 convention (model O1): the generator creates the object only, the edge comes from
SysConfig, and RegisterCallback, EnableInterrupt and the callback belong to the consumer (a
Module or the User Code). The driver has no construction path for a UART without DMA TX (or an
SPI without a pair of DMA channels), so generation reports an error then. A UART takes
MSPM0_UART_MAIN_INIT by default (receive on byte interrupts); a UART Extend instance with DMA RX
takes MSPM0_UART_EXTEND_INIT (receive by circular DMA on a FULL-DMA channel into a ring of
rx_dma_buffer_size bytes), an RX channel that is no FULL-DMA channel being an error; the DMA RX
channel of a UART Main instance cannot be used and is warned about.
"""

from __future__ import annotations

import logging
import os
import re
import sys

from xr_syntax.cpp import CppDocument
from xr_syntax.i18n import tr

from libxr import generator_code_stm32 as stm32
from libxr.cpp_layout import Braces, layout
from libxr.generator_code_stm32 import GENERATED_NOTICE, INDENT

# generator_code_stm32 的其余接口（登记表、设置、写入助手）经模块属性访问：测试会用
# importlib.reload 重载它，直接导入的名字会指向重载前的旧全局对象。
# The rest of the generator_code_stm32 interface (the registry, the settings, the writing
# helpers) is reached through module attributes: tests reload it with importlib.reload, and
# directly imported names would keep pointing at the pre-reload global objects.
# MSPM0 各设置段的默认值；STM32 段（USART/TIM/USB……）不适用于 MSPM0，生成前剔除。
# The defaults of the MSPM0 settings sections; the STM32 sections (USART/TIM/USB, ...) do not
# apply to MSPM0 and are removed before generation.
MSPM0_DEFAULTS = {
    "UART": {"tx_buffer_size": 128, "rx_buffer_size": 128, "tx_queue_size": 5},
    # 只有 MSPM0_UART_EXTEND_INIT 的 UART 有：接收 DMA 环的字节数（偶数）。
    # Only a UART of MSPM0_UART_EXTEND_INIT has it: the bytes of the receive DMA ring (even).
    "UART_EXTEND": {"rx_dma_buffer_size": 128},
    "I2C": {"buffer_size": 32, "dma_enable_min_size": 8},
    "SPI": {"tx_buffer_size": 32, "rx_buffer_size": 32, "dma_enable_min_size": 3},
}
STM32_ONLY_SECTIONS = (
    "ADC",
    "TIM",
    "CAN",
    "FDCAN",
    "USB",
    "database",
    "software_timer",
    "USART",
    "SYSTEM",
)
# UART 发送缓冲的一半 N（tx_buffer_size）：驱动断言 2 x N 是 2 * alignof(size_t) 的倍数
# （Cortex-M0+ 上 size_t 按 4 字节对齐，所以 N 是 4 的倍数），且 N 不超过单次 DMA 的
# 0xFFFF 字节（mspm0_uart.cpp）。
# The half N of the UART transmit buffer (tx_buffer_size): the driver asserts that 2 x N is a
# multiple of 2 * alignof(size_t) (size_t aligns to 4 bytes on the Cortex-M0+, so N is a
# multiple of 4) and that N is at most the 0xFFFF bytes of one DMA transfer (mspm0_uart.cpp).
UART_TX_ALIGN = 4
UART_TX_MAX = 0xFFFF // UART_TX_ALIGN * UART_TX_ALIGN
# SPI 可用的固定时钟源的频率（M5）：MFCLK 4 MHz，LFCLK 32.768 kHz。
# The frequencies of the fixed clock sources an SPI can use (M5): MFCLK 4 MHz, LFCLK
# 32.768 kHz.
MFCLK_FREQ = 4000000
LFCLK_FREQ = 32768

IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
DIVIDER_NUMBER = re.compile(r"(?:DIVIDE|RATIO)_(\d+)$")


def _comment(text: str, indent: str = "") -> list[str]:
    """一段注释折行成不超过 90 列的行，每行带 indent 缩进和 "// " 前缀。
    Wrap a comment into lines of at most 90 columns; every line carries the indent and the
    "// " prefix.
    """
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > 85:
            lines.append(f"{indent}// {line}")
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(f"{indent}// {line}")
    return lines


def _fail(message: str):
    """记录错误并以状态 1 退出。
    Log an error and exit with status 1.
    """
    logging.error(message)
    sys.exit(1)


def _settings_of(group: str, name: str) -> dict:
    """libxr_settings[group] 中对象 name 的设置段：键不分大小写，文件里已有的键保留它的写法
    （libxr pins -d 也这样找），没有时以小写的 name 新建。
    The settings section of the object name in libxr_settings[group]: the key is matched
    without regard to case and a key already in the file keeps its spelling (libxr pins -d
    finds it the same way); a missing one is created as the lower-case name.
    """
    section = stm32._settings(group)
    key = next((key for key in section if str(key).lower() == name.lower()), name.lower())
    return stm32._settings(group, key)


def _setting(section: dict, key: str, default):
    """设置段 section 中 key 的值；缺少或为 null（YAML 中只写了键）时写入并返回 default。
    The value of key in the settings section; a missing or null value (only the key written in
    YAML) is replaced by default, which is returned.
    """
    if section.get(key) is None:
        section[key] = default
    return section[key]


def _instance_settings(group: str, instance: str, extend: bool = False) -> dict:
    """实例的设置段，缺少的默认值补进段里，每个值都检查过：缓冲区和队列是正整数，DMA 阈值是非负
    整数；UART 的发送缓冲还要满足驱动的断言（见 UART_TX_ALIGN 和 UART_TX_MAX）。返回检查后的
    整数值。
    The settings of an instance with the missing defaults filled in, every value checked: the
    buffers and queues are positive integers, a DMA threshold a non-negative integer, and the
    UART transmit buffer also meets the assertions of the driver (see UART_TX_ALIGN and
    UART_TX_MAX). The checked integers are returned.
    """
    section = _settings_of(group, instance)
    values = {}
    defaults = dict(MSPM0_DEFAULTS[group])
    if extend:
        defaults.update(MSPM0_DEFAULTS["UART_EXTEND"])
    for key, default in defaults.items():
        minimum = 0 if key == "dma_enable_min_size" else 1
        values[key] = stm32._integer(
            f"{group}.{instance}.{key}", _setting(section, key, default), minimum
        )
    if group == "UART":
        size = values["tx_buffer_size"]
        if size % UART_TX_ALIGN or size > UART_TX_MAX:
            raise stm32._invalid_setting(
                f"UART.{instance}.tx_buffer_size",
                size,
                f"a multiple of {UART_TX_ALIGN} up to {UART_TX_MAX} (the driver takes 2 x N "
                f"bytes, a multiple of 2 * alignof(size_t), and sends at most "
                f"{UART_TX_MAX + 3} bytes per DMA transfer)",
                f"{UART_TX_ALIGN} 的倍数且不大于 {UART_TX_MAX}（驱动使用 2 x N 字节，必须是 "
                f"2 * alignof(size_t) 的倍数，单次 DMA 最多发送 {UART_TX_MAX + 3} 字节）",
            )
    if extend:
        size = values["rx_dma_buffer_size"]
        if size < 2 or size % 2 or size > 0xFFFF:
            raise stm32._invalid_setting(
                f"UART.{instance}.rx_dma_buffer_size",
                size,
                "an even number from 2 to 65534 (the driver receives into a ring of two halves, "
                "at most one DMA transfer long)",
                "2 到 65534 之间的偶数（驱动的接收环分两半，最长一次 DMA 传输）",
            )
    return values


def _object_name(name: str, kind: str) -> str:
    """对象的 C++ 名字：小写、必须是合法标识符且不与其他对象重名；不满足时报错退出。
    The C++ name of an object: lower case, a valid identifier and unique among the objects;
    otherwise an error is logged and generation exits.
    """
    name = name.lower()
    if not IDENTIFIER.fullmatch(name):
        _fail(
            tr(
                f"{name} is not a valid C++ identifier; rename the instance in SysConfig",
                f"{name} 不是合法的 C++ 标识符；请在 SysConfig 中重命名该实例",
            )
        )
    if name in stm32.registered_devices:
        _fail(
            tr(
                f"Two objects are named {name} ({kind} and {stm32.registered_devices[name]}); rename "
                "one of the instances in SysConfig",
                f"两个对象都叫 {name}（{kind} 与 {stm32.registered_devices[name]}）；请在 SysConfig 中"
                "重命名其中一个实例",
            )
        )
    return name


def _peripherals(project_data: dict) -> dict[str, list]:
    """工程 YAML 中的外设对象：kind -> [(对象名, SysConfig 实例名, 记录, PWM 通道)]，按 YAML
    顺序，同时登记进 XR_REGISTER 的对象表。
    The peripheral objects of the project YAML: kind -> [(object name, SysConfig instance name,
    record, PWM channel)], in YAML order, registered into the XR_REGISTER table as well.
    """
    result: dict[str, list] = {}
    for kind, records in project_data.get("Peripherals", {}).items():
        if kind == "Other":
            continue
        objects = []
        for instance, record in records.items():
            if not record.get("Peripheral"):
                _fail(
                    tr(
                        f"{instance}: the project YAML names no peripheral for it; assign one "
                        "in SysConfig and run `libxr parse` again",
                        f"{instance}：工程 YAML 没有给出它的外设；请在 SysConfig 中分配外设后"
                        "重新运行 `libxr parse`",
                    )
                )
            if kind == "PWM":
                # PWM 的对象名 pwm_<定时器>_c<n>，定时器是外设名（TIMG0），与 libxr pins -d 给出
                # 的设置键一致；实例名（PWM_0）不进名字。
                # The PWM object name pwm_<timer>_c<n>, the timer being the peripheral (TIMG0),
                # as in the settings keys libxr pins -d gives; the instance name (PWM_0) does
                # not enter the name.
                timer = str(record["Peripheral"]).lower()
                for channel in record.get("Channels", []):
                    name = _object_name(f"pwm_{timer}_c{channel}", "PWM")
                    objects.append((name, instance, record, channel))
                    stm32._register_device(name, "PWM")
            else:
                name = _object_name(str(record["Peripheral"]), kind)
                objects.append((name, instance, record, None))
                stm32._register_device(name, kind)
        result[kind] = objects
    return result


def _domain_clock(instance: str, record: dict, cpuclk) -> int:
    """实例所在的电源域（0 或 1）；工程 YAML 没有电源域或 CPUCLK 时报错退出：SysConfig 不为
    I2C 和 SPI 导出输入时钟的宏，生成器必须自己算出 BUSCLK。
    The power domain (0 or 1) of an instance; a project YAML without the power domain or
    CPUCLK is an error: SysConfig exports no input clock macro for an I2C or an SPI, so the
    generator has to compute the BUSCLK itself.
    """
    domain = record.get("PowerDomain")
    if domain not in (0, 1) or cpuclk is None:
        missing = "CPUCLK" if cpuclk is None else f"the power domain of {record.get('Peripheral')}"
        missing_zh = "CPUCLK" if cpuclk is None else f"{record.get('Peripheral')} 的电源域"
        _fail(
            tr(
                f"{instance}: the project YAML has no {missing}, so the input clock of the "
                "driver cannot be computed (SysConfig defines no clock macro for an I2C or "
                "SPI); run `libxr parse` again with a SysConfig output that defines CPUCLK_FREQ",
                f"{instance}：工程 YAML 里没有{missing_zh}，算不出驱动的输入时钟（SysConfig 不为 "
                "I2C 和 SPI 定义时钟宏）；请用定义了 CPUCLK_FREQ 的 SysConfig 输出重新运行 "
                "`libxr parse`",
            )
        )
    return domain


def _spi_clock(instance: str, record: dict) -> tuple[str | None, str, int]:
    """SPI 的输入时钟：(时钟源常量名，或 BUSCLK 时为 None；时钟源说明；分频数)。驱动沿用
    SysConfig 写下的时钟配置；不支持的时钟源报错退出。
    The input clock of an SPI: (the constant of the clock source, or None for the BUSCLK; a
    description of the source; the divider). The driver keeps the clock configuration SysConfig
    wrote; an unsupported clock source is an error.
    """
    clock = record.get("SysConfigClock") or {}
    match = DIVIDER_NUMBER.search(str(clock.get("divideRatio") or ""))
    divider = int(match.group(1)) if match else 1
    sel = str(clock.get("clockSel") or "")
    if sel == "" or sel.endswith("_BUSCLK"):
        return None, "BUSCLK", divider
    if sel.endswith("_MFCLK"):
        return "MFCLK_FREQ", "MFCLK (4 MHz)", divider
    if sel.endswith("_LFCLK"):
        return "LFCLK_FREQ", "LFCLK (32.768 kHz)", divider
    _fail(
        tr(
            f"{instance}: the SysConfig clock source {sel} is not supported (BUSCLK, MFCLK and "
            "LFCLK are); choose one of them in SysConfig",
            f"{instance}：不支持 SysConfig 时钟源 {sel}（支持 BUSCLK、MFCLK 和 LFCLK）；请在 "
            "SysConfig 中选择其中之一",
        )
    )


def _clocks(project_data: dict) -> tuple[list[str], dict[str, str]]:
    """时钟常量和每个 I2C、SPI 实例的时钟实参：用到的电源域各一个 BUSCLK 常量；SysConfig 给
    SPI 配了非默认时钟（其他时钟源或分频）时再有 MFCLK/LFCLK 常量和该实例的 <实例>_CLK_FREQ，
    凡被引用的常量都会生成。用不到时钟时两者都为空。
    The clock constants and the clock argument of every I2C and SPI instance: one BUSCLK
    constant per power domain in use; when SysConfig gave an SPI a non-default clock (another
    source or a divider) there is also the MFCLK/LFCLK constant and the <instance>_CLK_FREQ of
    that instance, and every constant that is referenced is generated. Both are empty when no
    clock is needed.
    """
    divider = project_data.get("ULPCLKDivider") or 1
    cpuclk = project_data.get("CPUCLK")
    peripherals = project_data.get("Peripherals", {})
    domains: set[int] = set()
    arguments: dict[str, str] = {}
    spi_lines: list[str] = []
    sources: dict[str, int] = {}
    for instance, record in peripherals.get("I2C", {}).items():
        domain = _domain_clock(instance, record, cpuclk)
        domains.add(domain)
        arguments[instance] = f"PD{domain}_BUSCLK_FREQ"
    for instance, record in peripherals.get("SPI", {}).items():
        source, text, div = _spi_clock(instance, record)
        if source is None:
            domain = _domain_clock(instance, record, cpuclk)
            domains.add(domain)
            base, text = f"PD{domain}_BUSCLK_FREQ", f"the BUSCLK of PD{domain}"
        else:
            base = source
            sources[source] = MFCLK_FREQ if source == "MFCLK_FREQ" else LFCLK_FREQ
        if not record.get("SysConfigClock"):
            arguments[instance] = base
            continue
        constant = f"{instance}_CLK_FREQ"
        spi_lines += _comment(
            f"{instance} runs from {text} divided by {div}, as SysConfig configured it; the SPI "
            "driver keeps that clock configuration."
        )
        expression = base if div == 1 else f"{base} / {div}"
        spi_lines.append(f"static constexpr uint32_t {constant} = {expression};")
        arguments[instance] = constant
    lines: list[str] = []
    if domains:
        domains_text = " and ".join(
            f"PD{domain} from {'MCLK (CPUCLK_FREQ)' if domain == 1 else _ulpclk_text(divider)}"
            for domain in sorted(domains)
        )
        lines = _comment(
            f"Input clocks: the BUSCLK of the power domain. {domains_text}. The I2C driver "
            "resets the clock configuration of its peripherals, so the BUSCLK of the domain is "
            "the right argument."
        )
        for domain in sorted(domains):
            expression = (
                "CPUCLK_FREQ" if domain == 1 or divider == 1 else f"(CPUCLK_FREQ / {divider})"
            )
            lines.append(f"static constexpr uint32_t PD{domain}_BUSCLK_FREQ = {expression};")
    for source in sorted(sources):
        lines.append(f"static constexpr uint32_t {source} = {sources[source]};")
    return lines + spi_lines, arguments


def _ulpclk_text(divider: int) -> str:
    """PD0 的 ULPCLK 说明文字，随 SysConfig 写入的分频变化。
    The notice text of the PD0 ULPCLK, following the divider SysConfig wrote.
    """
    return (
        f"ULPCLK = CPUCLK_FREQ / {divider} (DL_SYSCTL_setULPCLKDivider)"
        if divider != 1
        else "ULPCLK = CPUCLK_FREQ (no ULPCLK divider)"
    )


def _uart_dma_defines(uarts: list, extend: set[str]) -> list[str]:
    """UART DMA 通道的所有权标注：MSPM0_UART_MAIN_INIT 需要知道发送通道属于哪个 UART、服务于
    发送端；MSPM0_UART_EXTEND_INIT 还要接收通道的同样标注、它是 FULL-DMA 通道、是否用半传输
    中断（FULL-DMA 通道都低于 8，用），以及实例是 UART Extend。
    The ownership annotations of the UART DMA channels: MSPM0_UART_MAIN_INIT needs to know which
    UART owns the transmit channel and that it serves the transmitter; MSPM0_UART_EXTEND_INIT
    also needs the same for the receive channel, that it is a FULL-DMA channel, whether it uses
    the half-transfer interrupt (every FULL-DMA channel is below 8, so it does) and that the
    instance is a UART Extend.
    """
    lines: list[str] = []
    for _name, instance, record, _channel in uarts:
        channel = record.get("DMA_TX")
        if channel:
            lines.append(f"#define {channel}_LIBXR_UART_IRQN {instance}_INST_INT_IRQN")
            lines.append(f"#define {channel}_LIBXR_UART_TX 1")
        if instance in extend:
            rx = record["DMA_RX"]
            lines.append(f"#define {rx}_LIBXR_UART_IRQN {instance}_INST_INT_IRQN")
            lines.append(f"#define {rx}_LIBXR_UART_RX 1")
            lines.append(f"#define {rx}_LIBXR_FULL_CHANNEL 1")
            lines.append(f"#define {rx}_LIBXR_HALF_INTERRUPT 1")
            lines.append(f"#define {instance}_LIBXR_EXTEND_CAPABLE 1")
    if lines and extend:
        lines = [
            "// Ownership of the DMA channels that serve a UART; MSPM0_UART_MAIN_INIT and",
            "// MSPM0_UART_EXTEND_INIT check these names against the ones SysConfig generated.",
        ] + lines
    elif lines:
        lines = [
            "// Ownership of the DMA channels that serve a UART transmitter;",
            "// MSPM0_UART_MAIN_INIT checks these names against the ones SysConfig generated.",
        ] + lines
    return lines


def _extend_uarts(project_data: dict, uarts: list) -> set[str]:
    """用 MSPM0_UART_EXTEND_INIT 的 UART（SysConfig 实例名）：配了 DMA RX 的 UART Extend 实例。
    RX 通道不是 FULL-DMA 通道时报错退出；UART Main 实例上的 DMA RX 给出警告（用
    MSPM0_UART_MAIN_INIT，RX 通道不用）。
    The UARTs (SysConfig instance names) that take MSPM0_UART_EXTEND_INIT: the UART Extend
    instances with DMA RX. An RX channel that is no FULL-DMA channel logs an error and exits; DMA
    RX on a UART Main instance is warned about (it takes MSPM0_UART_MAIN_INIT and the RX channel
    stays unused).
    """
    full = project_data.get("DMAFullChannels")
    extend = set()
    for _name, instance, record, _channel in uarts:
        rx = record.get("DMA_RX")
        if not rx:
            continue
        peripheral = record.get("Peripheral")
        if not record.get("Extend"):
            logging.warning(
                tr(
                    f"{instance}: the DMA RX channel {rx} is not used; {peripheral} is a UART Main "
                    "instance, which receives on byte interrupts (MSPM0_UART_MAIN_INIT); DMA "
                    "reception needs a UART Extend instance",
                    f"{instance}：DMA RX 通道 {rx} 不会被使用；{peripheral} 是 UART Main 实例，接收"
                    "走字节中断（MSPM0_UART_MAIN_INIT）；DMA 接收需要 UART Extend 实例",
                )
            )
            continue
        channel_id = record.get("DMA_RX_ID")
        if channel_id is None or full is None or channel_id >= full:
            where = (
                f"channel {channel_id}" if channel_id is not None else "a channel of unknown number"
            )
            _fail(
                tr(
                    f"{instance}: the DMA RX channel {rx} is {where}, but MSPM0_UART_EXTEND_INIT "
                    f"receives on a FULL-DMA channel (0 to {(full or 1) - 1}); choose one in "
                    "SysConfig (UART > DMA Configuration > DMA RX channel) and run `libxr parse` "
                    "again",
                    f"{instance}：DMA RX 通道 {rx} 是{where}，但 MSPM0_UART_EXTEND_INIT 只能用 "
                    f"FULL-DMA 通道（0 到 {(full or 1) - 1}）接收；请在 SysConfig（UART > DMA "
                    "Configuration 的 DMA RX 通道）中选择后重新运行 `libxr parse`",
                )
            )
        extend.add(instance)
    return extend


def _buffers(peripherals: dict, extend: set[str]) -> list[str]:
    """DMA 缓冲区的定义：UART 发送缓冲是两个半区（2 x N，按 size_t 对齐），SPI 收发各一，I2C 是
    轮询与 DMA 之间的暂存区。
    The DMA buffer declarations: a UART transmit buffer holds two halves (2 x N, aligned as
    size_t), an SPI gets one for each direction and an I2C one staging area between polling and
    DMA.
    """
    lines: list[str] = []
    for name, instance, _record, _channel in peripherals.get("UART", []):
        settings = _instance_settings("UART", name, instance in extend)
        tx = settings["tx_buffer_size"]
        lines.append(f"alignas(size_t) static uint8_t {name}_tx_buf[2 * {tx}];")
        if instance in extend:
            rx = settings["rx_dma_buffer_size"]
            lines.append(f"alignas(4) static uint8_t {name}_rx_dma_buf[{rx}];")
    for name, _instance, _record, _channel in peripherals.get("SPI", []):
        settings = _instance_settings("SPI", name)
        lines.append(f"alignas(4) static uint8_t {name}_rx_buf[{settings['rx_buffer_size']}];")
        lines.append(f"alignas(4) static uint8_t {name}_tx_buf[{settings['tx_buffer_size']}];")
    for name, _instance, _record, _channel in peripherals.get("I2C", []):
        settings = _instance_settings("I2C", name)
        lines.append(f"alignas(4) static uint8_t {name}_buf[{settings['buffer_size']}];")
    if not lines:
        return []
    if extend:
        heading = [
            "// DMA buffers. A UART gets 2 x N bytes for its two transmit halves; a UART Extend with",
            "// DMA RX also gets its receive ring. The SPI DMA buffers are split in two halves as",
            "// well; the I2C driver uses polling, its buffer is the staging area for DMA transfers.",
        ]
    else:
        heading = [
            "// DMA buffers. A UART gets 2 x N bytes for its two transmit halves; the receive side",
            "// runs on byte interrupts. The SPI DMA buffers are split in two halves as well; the",
            "// I2C driver uses polling, its buffer is the staging area for DMA transfers.",
        ]
    return heading + lines


def _notice(project_data: dict) -> list[str]:
    """文件开头的说明：由哪个 SysConfig 工程生成、SysConfig 负责什么、只改 User Code 区域。
    The notice at the start of the file: which SysConfig project it was generated from, what
    SysConfig owns and that only the User Code regions are for editing.
    """
    return [
        GENERATED_NOTICE,
        f"// Generated from {project_data.get('Syscfg', 'the SysConfig project')}"
        f" ({project_data.get('Mcu', {}).get('Type', '')}); SysConfig configures the",
        "// clocks, pins, DMA channels and peripherals (SYSCFG_DL_init() in main.c). Put",
        '// application code between the "User Code Begin" and "User Code End" markers.',
    ]


def _includes(project_data: dict, use_xrobot: bool) -> list[str]:
    """include 列表：libxr.hpp、用到的 mspm0 驱动头、ti_msp_dl_config.h，XRobot 再加
    xrobot_main.hpp；均按字母顺序。
    The includes: libxr.hpp, the mspm0 driver headers in use, ti_msp_dl_config.h and, with
    XRobot, xrobot_main.hpp; all in alphabetical order.
    """
    peripherals = project_data.get("Peripherals", {})
    drivers = ["mspm0_timebase.hpp"]
    if project_data.get("GPIO"):
        drivers.append("mspm0_gpio.hpp")
    for kind in ("I2C", "PWM", "SPI", "UART"):
        if peripherals.get(kind):
            drivers.append(f"mspm0_{kind.lower()}.hpp")
    drivers.append("ti_msp_dl_config.h")
    if use_xrobot:
        drivers.append("xrobot_main.hpp")
    return [
        '#include "app_main.h"',
        "",
        '#include "libxr.hpp"',
        *[f'#include "{header}"' for header in sorted(drivers)],
        "",
        "using namespace LibXR;",
    ]


def _gpio_section(gpio: dict) -> list[str]:
    """GPIO 对象：每个引脚一个 MSPM0GPIO；中断的注册和使能属于消费者（O1），不在这里生成。
    The GPIO objects: one MSPM0GPIO per pin; registering and enabling an interrupt belongs to
    the consumer (O1) and is not generated here.
    """
    if not gpio:
        return []
    by_direction: dict[str, list[str]] = {}
    for label, pin in gpio.items():
        by_direction.setdefault(pin.get("Direction", "OUTPUT").lower(), []).append(
            f"{label} ({pin['Pin']})"
        )
    summary = " and ".join(
        f"{', '.join(pins)} as {direction} pins" for direction, pins in by_direction.items()
    )
    lines = _comment(
        f"GPIO: SysConfig configured {summary}. The interrupt edge comes from SysConfig; "
        "RegisterCallback and EnableInterrupt belong to the consumer (a Module or the "
        "User Code).",
        INDENT,
    )
    for label, pin in gpio.items():
        lines += layout(f"static MSPM0GPIO {label}", [pin["Port"], pin["PinMacro"], pin["IOMUX"]])
    return lines


def _uart_section(uarts: list, extend: set[str]) -> list[str]:
    """UART 对象：发送走 DMA；MSPM0_UART_MAIN_INIT 接收走字节中断，extend 中的实例用
    MSPM0_UART_EXTEND_INIT 走循环 DMA 接收（见 _extend_uarts()）。没有 DMA TX 的实例报错。
    The UART objects: DMA on the transmit side; MSPM0_UART_MAIN_INIT receives on byte
    interrupts, and the instances in extend take MSPM0_UART_EXTEND_INIT with circular DMA
    reception (see _extend_uarts()). An instance without DMA TX reports an error.
    """
    if not uarts:
        return []
    if extend:
        heading = "TX with DMA; a UART Extend with DMA RX receives by circular DMA, the others "
        heading += "on byte interrupts."
    else:
        heading = "TX with DMA; the receive side runs on byte interrupts."
    lines = _comment(f"UART: {heading}", INDENT)
    for name, instance, record, _channel in uarts:
        if not record.get("DMA_TX"):
            _fail(
                tr(
                    f"{instance}: no DMA TX channel in the SysConfig project (UART > DMA "
                    "Configuration: Enable DMA TX with the TX trigger, which also gives the "
                    f"{instance}_INST_DMA_TRIGGER_n and <channel>_CHAN_ID macros); the MSPM0 UART "
                    "driver constructs only with DMA TX. Enable it and run `libxr parse` again, "
                    "or drop the UART",
                    f"{instance}：SysConfig 工程里没有 DMA TX 通道（UART > DMA Configuration："
                    f"启用 DMA TX 并选 TX 触发，SysConfig 随之生成 {instance}_INST_DMA_TRIGGER_n "
                    "和 <通道>_CHAN_ID 宏）；MSPM0 的 UART 驱动只支持带 DMA TX 的构造。请启用后"
                    "重新运行 `libxr parse`，或删去这个 UART",
                )
            )
        settings = _instance_settings("UART", name, instance in extend)
        tx_buf = f"{name}_tx_buf"
        if instance in extend:
            rx_buf = f"{name}_rx_dma_buf"
            lines += layout(
                f"static MSPM0UART {name}",
                [
                    f"MSPM0_UART_EXTEND_INIT({instance}",
                    record["DMA_TX"],
                    record["DMA_RX"],
                    tx_buf,
                    f"sizeof({tx_buf})",
                    str(settings["tx_queue_size"]),
                    rx_buf,
                    f"sizeof({rx_buf})",
                    f"{settings['rx_buffer_size']})",
                ],
            )
            continue
        lines += layout(
            f"static MSPM0UART {name}",
            [
                f"MSPM0_UART_MAIN_INIT({instance}",
                record["DMA_TX"],
                tx_buf,
                f"sizeof({tx_buf})",
                str(settings["tx_queue_size"]),
                f"{settings['rx_buffer_size']})",
            ],
        )
    return lines


def _i2c_section(i2cs: list, clocks: dict[str, str]) -> list[str]:
    """I2C 对象：轮询驱动，缓冲是 DMA 传输的暂存区；时钟是所在电源域的 BUSCLK（clocks 中的
    实参，见 _clocks()）。
    The I2C objects: the polling driver whose buffer stages DMA transfers; the clock is the
    BUSCLK of the power domain (the argument in clocks, see _clocks()).
    """
    if not i2cs:
        return []
    lines = [f"{INDENT}// I2C: polling; the buffer stages DMA transfers."]
    for name, instance, _record, _channel in i2cs:
        settings = _instance_settings("I2C", name)
        lines += layout(
            f"static MSPM0I2C {name}",
            [
                f"MSPM0_I2C_INIT({instance}",
                clocks[instance],
                f"{name}_buf",
                f"sizeof({name}_buf)",
                f"{settings['dma_enable_min_size']})",
                Braces(f"{instance}_BUS_SPEED_HZ"),
            ],
        )
    return lines


def _spi_section(spis: list, clocks: dict[str, str]) -> list[str]:
    """SPI 对象：收发各一个 DMA 通道；时钟是 SysConfig 配给它的输入时钟（clocks 中的实参，见
    _clocks()）。
    The SPI objects: one DMA channel for each direction; the clock is the input clock SysConfig
    configured for it (the argument in clocks, see _clocks()).
    """
    if not spis:
        return []
    lines = [f"{INDENT}// SPI: DMA for transfers longer than the threshold."]
    for name, instance, record, _channel in spis:
        dma_rx, dma_tx = record.get("DMA_RX"), record.get("DMA_TX")
        if not dma_rx or not dma_tx:
            _fail(
                tr(
                    f"{instance}: the MSPM0 SPI driver constructs only with one DMA channel for "
                    f"RX and one for TX, and the SysConfig project has RX {dma_rx or 'none'}, "
                    f"TX {dma_tx or 'none'}. Configure SPI > DMA Configuration (DMA Event 1/2 "
                    "triggers: one RX, one TX), run `libxr parse` again, or drop the SPI",
                    f"{instance}：MSPM0 的 SPI 驱动只支持收发各一个 DMA 通道的构造，而 SysConfig "
                    f"工程里 RX 为 {dma_rx or '没有'}，TX 为 {dma_tx or '没有'}。请配置 SPI > DMA "
                    "Configuration（DMA Event 1/2 的触发：一个 RX，一个 TX）后重新运行 "
                    "`libxr parse`，或删去这个 SPI",
                )
            )
        if str(record.get("DMA_RX_TRIGGER", "")).endswith("_RX_TIMEOUT"):
            # 驱动按 SysConfig 配好的触发搬运每个接收字节；RX timeout 只在接收超时时请求 DMA，
            # 计数的传输等不到结束。
            # The driver moves every received byte on the trigger SysConfig configured; RX
            # timeout requests DMA only when reception times out, so a counted transfer never
            # completes.
            _fail(
                tr(
                    f"{instance}: the DMA RX channel {dma_rx} is triggered by RX timeout; the "
                    "MSPM0 SPI driver needs the RX trigger (SPI > DMA Configuration: DMA Event "
                    "trigger DL_SPI_DMA_INTERRUPT_RX). Change it and run `libxr parse` again",
                    f"{instance}：DMA RX 通道 {dma_rx} 由 RX timeout 触发；MSPM0 的 SPI 驱动需要 RX "
                    "触发（SPI > DMA Configuration 的 DMA Event 触发选 DL_SPI_DMA_INTERRUPT_RX）。"
                    "请修改后重新运行 `libxr parse`",
                )
            )
        settings = _instance_settings("SPI", name)
        lines += layout(
            f"static MSPM0SPI {name}",
            [
                f"MSPM0_SPI_INIT({instance}",
                clocks[instance],
                dma_rx,
                dma_tx,
                f"{name}_rx_buf",
                f"sizeof({name}_rx_buf)",
                f"{name}_tx_buf",
                f"sizeof({name}_tx_buf)",
                f"{settings['dma_enable_min_size']})",
            ],
        )
    return lines


def _pwm_section(pwms: list) -> list[str]:
    """PWM 对象：定时器的每个通道一个 MSPM0PWM；libxr_config.yaml 给出频率（正整数）时对该
    通道调用 SetConfig，null 表示不调用。同一定时器的通道共用周期，频率不同时最后一次 SetConfig
    生效，给出警告。
    The PWM objects: one MSPM0PWM per channel of the timer; SetConfig is called on a channel
    when libxr_config.yaml gives its frequency (a positive integer), and null means no call.
    The channels of one timer share one period, so differing frequencies are warned about: the
    last SetConfig wins.
    """
    if not pwms:
        return []
    lines = [
        f"{INDENT}// PWM: the channels of one timer share one period, which SysConfig configures.",
    ]
    frequencies: dict[str, dict[str, int]] = {}
    for name, instance, _record, channel in pwms:
        lines += layout(f"static MSPM0PWM {name}", [f"MSPM0_PWM_CH({instance}, {channel})"])
        settings = _settings_of("PWM", name)
        frequency = settings.setdefault("frequency", None)
        if frequency is not None:
            frequency = stm32._integer(f"PWM.{name}.frequency", frequency)
            frequencies.setdefault(instance, {})[name] = frequency
            lines += layout(f"{name}.SetConfig", [Braces(str(frequency))])
    for instance, channels in frequencies.items():
        if len(set(channels.values())) > 1:
            listed = ", ".join(f"{name} {value}" for name, value in channels.items())
            last = list(channels)[-1]
            logging.warning(
                tr(
                    f"libxr_config.yaml: the PWM channels of {instance} share one period but "
                    f"set different frequencies ({listed}); the last SetConfig ({last}) wins",
                    f"libxr_config.yaml：{instance} 的 PWM 通道共用一个周期，却设了不同的频率"
                    f"（{listed}）；以最后一次 SetConfig（{last}）为准",
                )
            )
    return lines


def _terminal_source(uarts: list) -> str:
    """生效的终端设备，与 STM32 的约定相同：空值表示不生成终端，none 也一样（写法保持不变）。
    只有 libxr_config.yaml 里还没有 terminal_source 这个键时（第一次生成），才选第一个 UART 并
    写进文件；之后用户清空或写成 none，重新生成都不会再打开终端。
    The effective terminal device, with the STM32 convention: an empty value means no terminal,
    and so does none (kept as written). Only when libxr_config.yaml has no terminal_source key
    yet (the first generation) is the first UART chosen and written to the file; once the user
    empties it or writes none, regenerating never turns the terminal back on.
    """
    document = stm32.libxr_config_document
    source = stm32._text("terminal_source", stm32.libxr_settings.get("terminal_source"))
    if source == "" and uarts and (document is None or "terminal_source" not in document):
        source = uarts[0][0]
        stm32.libxr_settings["terminal_source"] = source
        logging.info(
            tr(
                f"terminal_source is not set; using {source}",
                f"未设置 terminal_source；使用 {source}",
            )
        )
    source = source.lower()
    return "" if source == "none" else source


def _terminal_section(peripherals: dict) -> list[str]:
    """终端：terminal_source 指定的 UART 作为标准输入输出，RamFS 和 Terminal 由软件定时器任务
    每 10 ms 运行一次（terminal_source 的取值见 _terminal_source()）。
    The terminal: the UART named by terminal_source becomes the standard I/O, and the RamFS and
    Terminal objects run in a software timer task every 10 ms (see _terminal_source() for the
    values of terminal_source).
    """
    uarts = peripherals.get("UART", [])
    source = _terminal_source(uarts)
    if not source:
        return []
    names = {name for name, _instance, _record, _channel in uarts}
    if source not in names:
        logging.warning(
            tr(
                f"terminal_source '{source}' is not one of the generated UARTs "
                f"({', '.join(sorted(names)) or 'none'}); the terminal is not initialized",
                f"terminal_source '{source}' 不是生成的 UART 之一"
                f"（{'、'.join(sorted(names)) or '没有'}）；终端不初始化",
            )
        )
        return []
    term_config = stm32._settings("Terminal")
    params = [
        stm32._integer(f"Terminal.{key}", _setting(term_config, key, default))
        for key, default in (
            ("read_buff_size", 32),
            ("max_line_size", 32),
            ("max_arg_number", 5),
            ("max_history_number", 5),
        )
    ]
    terminal_type = f"Terminal<{', '.join(map(str, params))}>"
    stm32._register_device("ramfs", "RamFS")
    stm32._register_device("terminal", terminal_type)
    return [
        f"{INDENT}// Terminal on {source}",
        f"{INDENT}STDIO::read_ = {source}.read_port_;",
        f"{INDENT}STDIO::write_ = {source}.write_port_;",
        f'{INDENT}static RamFS ramfs("XRobot");',
        f"{INDENT}static {terminal_type} terminal(ramfs);",
        f"{INDENT}static auto terminal_task = Timer::CreateTask(terminal.TaskFun, &terminal, 10);",
        f"{INDENT}Timer::Add(terminal_task);",
        f"{INDENT}Timer::Start(terminal_task);",
    ]


def _sections(
    peripherals: dict, gpio: dict, use_xrobot: bool, clocks: dict[str, str], extend: set[str]
) -> list[str]:
    """app_main 函数体的各段：时基、GPIO、UART、I2C、SPI、PWM、终端和登记。
    The sections of the app_main body: timebase, GPIO, UART, I2C, SPI, PWM, terminal and the
    registrations.
    """
    blocks = [
        [
            f"{INDENT}// Timebase and platform",
            f"{INDENT}static MSPM0Timebase timebase;",
            f"{INDENT}PlatformInit();",
        ],
        _gpio_section(gpio),
        _uart_section(peripherals.get("UART", []), extend),
        _i2c_section(peripherals.get("I2C", []), clocks),
        _spi_section(peripherals.get("SPI", []), clocks),
        _pwm_section(peripherals.get("PWM", [])),
        _terminal_section(peripherals),
    ]
    registrations = stm32.generate_xrobot_registrations() if use_xrobot else []
    if registrations:
        blocks.append([f"{INDENT}// Hardware registration"] + registrations)
    return stm32.join_blocks([block for block in blocks if block])


def _check_gpio_name(label: str) -> None:
    """GPIO 对象名（SysConfig 的引脚名）必须是合法、唯一、非关键字且非保留的 C++ 标识符；不满足
    时报错退出，说明在 SysConfig 中改名。
    A GPIO object name (the pin name of SysConfig) must be a valid, unique C++ identifier that
    is neither a keyword nor reserved; otherwise generation exits with an error that says to
    rename the pin in SysConfig.
    """
    if (
        not IDENTIFIER.fullmatch(label)
        or label in stm32.CPP_KEYWORDS
        or "__" in label
        or re.match(r"_[A-Z]", label)
    ):
        problem = ("is not a valid C++ identifier", "不是可用的 C++ 标识符")
    elif label in stm32.registered_devices:
        problem = (
            f"is already used by the {stm32.registered_devices[label]} object",
            f"已被 {stm32.registered_devices[label]} 对象使用",
        )
    else:
        return
    _fail(
        tr(
            f"GPIO name {label} {problem[0]}; rename the pin in SysConfig",
            f"GPIO 名字 {label} {problem[1]}；请在 SysConfig 中给引脚改名",
        )
    )


def generate_full_code(project_data: dict, use_xrobot: bool, existing_code: str) -> str:
    """生成 app_main 源文件的完整内容，并填回已有代码中 User Code 区域的内容。
    Generate the full content of the app_main source file and put back the User Code bodies of
    the existing code.
    """
    peripherals = _peripherals(project_data)
    gpio = project_data.get("GPIO", {})
    for label in gpio:
        _check_gpio_name(str(label))
        stm32._register_device(label, "GPIO")
    uarts = peripherals.get("UART", [])
    extend = _extend_uarts(project_data, uarts)
    default_3 = (
        []
        if use_xrobot
        else [
            f"{INDENT}while (true)",
            f"{INDENT}{{",
            f"{INDENT * 2}Thread::Sleep(UINT32_MAX);",
            f"{INDENT}}}",
        ]
    )
    lines = _notice(project_data)
    lines += _includes(project_data, use_xrobot)
    lines += ["", "/* User Code Begin 1 */", "/* User Code End 1 */"]
    clock_lines, clocks = _clocks(project_data)
    if clock_lines:
        lines += [""] + clock_lines
    defines = _uart_dma_defines(uarts, extend)
    if defines:
        lines += [""] + defines
    buffers = _buffers(peripherals, extend)
    if buffers:
        lines += [""] + buffers
    lines += ["", 'extern "C" void app_main(void)', "{"]
    lines += [f"{INDENT}/* User Code Begin 2 */", f"{INDENT}/* User Code End 2 */", ""]
    lines += _sections(peripherals, gpio, use_xrobot, clocks, extend)
    lines += ["", f"{INDENT}/* User Code Begin 3 */"] + default_3
    lines.append(f"{INDENT}/* User Code End 3 */")
    if use_xrobot:
        lines.append(f"{INDENT}XROBOT_MAIN();")
    lines.append("}")
    return stm32._preserve_generated_regions(existing_code, "\n".join(lines) + "\n")


# 手写文件被替换时最多列出的行数。
# At most this many lines are listed when a hand-written file is replaced.
HAND_WRITTEN_LINES = 20


def _warn_hand_written(path: str, existing_code: str, generated_code: str) -> None:
    """已有的 app_main 不是 libxr gen 生成的（前两行没有生成说明）时给出警告，列出 User Code
    区域之外、新文件里不再有的行：这些行会被替换。与 STM32 一样照常生成，不拒绝。
    Warn when the existing app_main was not generated by libxr gen (no generated-file notice in
    its first two lines) and list the lines outside the User Code regions that the new file no
    longer has: they are replaced. Generation goes on as on STM32; it is not refused.
    """
    if not existing_code.strip() or GENERATED_NOTICE in existing_code.splitlines()[:2]:
        return
    source = existing_code.encode("utf-8", errors="surrogateescape")
    spans = [region.body_span for region in CppDocument.parse(existing_code).user_regions()]
    outside, start = [], 0
    for span in sorted(spans, key=lambda span: span.start):
        outside.append(source[start : span.start])
        start = span.end
    outside.append(source[start:])
    kept = {line.strip() for line in generated_code.splitlines()}
    replaced = [
        line.strip()
        for part in outside
        for line in part.decode("utf-8", errors="replace").splitlines()
        if line.strip() and line.strip() not in kept
    ]
    if not replaced:
        return
    listed = "\n".join(f"  {line}" for line in replaced[:HAND_WRITTEN_LINES])
    more = len(replaced) - HAND_WRITTEN_LINES
    listed += tr(f"\n  ... and {more} more", f"\n  ……另有 {more} 行") if more > 0 else ""
    logging.warning(
        tr(
            f"{path} was not generated by libxr gen; everything outside the User Code regions "
            f"is regenerated, and these {len(replaced)} line(s) of it are replaced; code to "
            f"keep belongs in the User Code regions:\n{listed}",
            f"{path} 不是 libxr gen 生成的；User Code 区域之外的内容全部重新生成，以下 "
            f"{len(replaced)} 行被替换；要保留的代码应放在 User Code 区域中：\n{listed}",
        )
    )


def initialize_registry() -> None:
    """清空生成对象的登记表和本次生成用到的头文件，使同一次进程中的下一次生成不带上次的对象。
    Clear the registry of generated objects and the headers of this generation, so the next
    generation in the same process carries no objects over.
    """
    stm32.registered_devices.clear()
    stm32.registered_origins.clear()
    stm32.used_headers.clear()


def reset_settings() -> None:
    """把生效的设置恢复为 MSPM0 的默认集合：STM32 专用的段剔除，UART 和 PWM 段补上。
    Restore the effective settings to the MSPM0 default set: the STM32-only sections removed
    and the UART and PWM sections added.
    """
    stm32.reset_settings()
    for key in STM32_ONLY_SECTIONS:
        stm32.libxr_settings.pop(key, None)
    stm32._settings("UART")
    stm32._settings("PWM")


def generate(
    input_path: str, output_path: str, use_xrobot: bool = False, libxr_config: str = ""
) -> None:
    """生成 output_path 指定的 app_main 源文件，以及同一目录中的 app_main.h 和 libxr_config.yaml。
    Generate the app_main source file output_path, and app_main.h and libxr_config.yaml in the
    same directory.

    每次从默认设置开始，合并 libxr_config（libxr_config.yaml 的路径或 URL，为空时读取输出目录中
    的文件）。全部文件先在内存中生成，没有错误时才写出，并且只写内容有变化的文件。已有输出文件
    中 User Code 区域的内容被保留。出错时记录错误（调试日志另记调用栈）并以状态 1 退出。
    Every run starts from the default settings and merges libxr_config, the path or URL of
    libxr_config.yaml, or the file in the output directory when empty. All files are generated
    in memory first and written only when nothing failed, and only the files whose content
    changed are written. The User Code bodies of an existing output file are kept. An error is
    logged, with the traceback at debug level, and exits with status 1.
    """
    try:
        output_dir = os.path.dirname(output_path) or os.curdir

        reset_settings()
        project_data = stm32.load_configuration(input_path)
        stm32.load_libxr_config(output_dir, libxr_config)
        # load_configuration 又写入了 SYSTEM（MSPM0 只有裸机），再剔除一次。
        # load_configuration has written SYSTEM again (MSPM0 is bare metal only); remove it
        # once more.
        stm32.libxr_settings.pop("SYSTEM", None)
        stm32._settings("UART")
        stm32._settings("PWM")
        initialize_registry()

        existing_code = ""
        if os.path.exists(output_path):
            with open(output_path, encoding="utf-8") as file:
                existing_code = file.read()
        stm32.reject_user_hardware_container(existing_code, use_xrobot)
        if use_xrobot:
            stm32.reject_user_xrobot_main(existing_code)
            existing_code = stm32._without_default_loop(existing_code)

        code = generate_full_code(project_data, use_xrobot, existing_code)
        _warn_hand_written(output_path, existing_code, code)
        files = {
            os.path.basename(output_path): code,
            "app_main.h": stm32.APP_MAIN_HEADER,
            "libxr_config.yaml": stm32.libxr_config_text(),
        }

        os.makedirs(output_dir, exist_ok=True)
        written, unchanged = [], []
        for name, text in files.items():
            path = os.path.join(output_dir, name)
            (written if stm32._write_if_changed(path, text) else unchanged).append(name)
        stm32._report_files(output_dir, written, unchanged, [])
    except SystemExit:
        raise
    except Exception as error:
        logging.error(tr(f"Generation failed: {str(error)}", f"生成失败：{str(error)}"))
        logging.debug(tr("Traceback:", "调用栈："), exc_info=True)
        sys.exit(1)
