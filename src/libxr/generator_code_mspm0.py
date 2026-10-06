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
BUSCLK 即可；SPI 驱动沿用 SysConfig 写下的时钟配置，传入时钟要除以 SysConfig 的 divideRatio
（时钟源为 MFCLK 时按 4 MHz 计算）。UART 用 SysConfig 的 INST_FREQUENCY 宏，PWM 的源时钟由
驱动从 INST_CLK_FREQ 求出，都不需要生成器计算。
Clocks (M5): the BUSCLK of PD1 is CPUCLK_FREQ, that of PD0 is CPUCLK_FREQ / the ULPCLK divider
(the DL_SYSCTL_setULPCLKDivider of the generated code, 1 by default). The I2C driver resets the
clock configuration itself (BUSCLK, divider 1), so the BUSCLK of the power domain is the right
argument; the SPI driver keeps the clock configuration SysConfig wrote, so its argument is the
BUSCLK divided by the divideRatio of SysConfig (4 MHz when the source is MFCLK). A UART takes
the INST_FREQUENCY macro of SysConfig and the source clock of a PWM is derived by the driver
from INST_CLK_FREQ, so neither needs a generator-side computation.

对象名：GPIO 用引脚标签（$name 去掉 PIN_ 前缀），UART/I2C/SPI 用外设名的小写（uart0），PWM 是
pwm_<timer>_c<n>。GPIO 中断遵循 STM32 的约定（模型 O1）：生成器只创建对象，边沿来自 SysConfig；
RegisterCallback、EnableInterrupt 和回调函数属于消费者（Module 或 User Code）。UART 没有 DMA TX
（或 SPI 没有一对 DMA 通道）时驱动没有对应的构造路径，生成报错。
Object names: a GPIO takes the pin label (the $name without its PIN_ prefix), a UART/I2C/SPI
the lower-case peripheral name (uart0), and a PWM is pwm_<timer>_c<n>. GPIO interrupts follow
the STM32 convention (model O1): the generator creates the object only, the edge comes from
SysConfig, and RegisterCallback, EnableInterrupt and the callback belong to the consumer (a
Module or the User Code). The driver has no construction path for a UART without DMA TX (or an
SPI without a pair of DMA channels), so generation reports an error then.
"""

from __future__ import annotations

import logging
import os
import re
import sys

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
# MFCLK 的固定频率（M5）。
# The fixed frequency of MFCLK (M5).
MFCLK_FREQ = 4000000

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


def _instance_settings(group: str, instance: str) -> dict:
    """libxr_settings 中小写实例键下的设置段，缺少的默认值补进段里。
    The settings section under the lower-case instance key of libxr_settings[group], with the
    missing defaults filled in.
    """
    section = stm32._settings(group, instance.lower())
    for key, default in MSPM0_DEFAULTS[group].items():
        section.setdefault(key, default)
    return section


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
            if kind == "PWM":
                # PWM 的对象名 pwm_<timer>_c<n>；定时器取实例名去掉 PWM_ 前缀（或外设名）。
                # The PWM object name pwm_<timer>_c<n>; the timer is the instance name without
                # its PWM_ prefix (or the peripheral name).
                timer = re.sub(r"^PWM_", "", instance).lower()
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


def _clock_constants(project_data: dict) -> list[str]:
    """时钟常量：每个用到的电源域一个 BUSCLK，SPI 的非默认时钟再各一个；用不到时不生成。
    The clock constants: one BUSCLK per power domain in use and one per SPI instance with a
    non-default clock; none when no clock is needed.
    """
    divider = project_data.get("ULPCLKDivider") or 1
    cpuclk = project_data.get("CPUCLK")
    domains = set()
    for section in ("I2C", "SPI"):
        for record in project_data.get("Peripherals", {}).get(section, {}).values():
            if record.get("PowerDomain") is not None and cpuclk is not None:
                domains.add(record["PowerDomain"])
    if not domains:
        return []
    domains_text = " and ".join(
        f"PD{domain} from {'MCLK (CPUCLK_FREQ)' if domain == 1 else _ulpclk_text(divider)}"
        for domain in sorted(domains)
    )
    lines = _comment(
        f"Input clocks: the BUSCLK of the power domain. {domains_text}. The I2C driver resets "
        "the clock configuration of its peripherals, so the BUSCLK of the domain is the right "
        "argument."
    )
    for domain in sorted(domains):
        expression = "CPUCLK_FREQ" if domain == 1 or divider == 1 else f"(CPUCLK_FREQ / {divider})"
        lines.append(f"static constexpr uint32_t PD{domain}_BUSCLK_FREQ = {expression};")
    mfclk = False
    for name, record in project_data.get("Peripherals", {}).get("SPI", {}).items():
        clock = record.get("SysConfigClock")
        if not clock:
            continue
        match = DIVIDER_NUMBER.search(str(clock.get("divideRatio") or ""))
        div = int(match.group(1)) if match else 1
        sel = str(clock.get("clockSel") or "")
        if sel.endswith("_MFCLK"):
            if not mfclk:
                lines.append(f"static constexpr uint32_t MFCLK_FREQ = {MFCLK_FREQ};")
                mfclk = True
            base = "MFCLK_FREQ"
        elif sel and not sel.endswith("_BUSCLK"):
            logging.warning(
                tr(
                    f"{name}: unsupported SysConfig clock source {sel}; the SPI driver keeps "
                    "the configuration SysConfig wrote, check the clock by hand",
                    f"{name}：不支持的 SysConfig 时钟源 {sel}；SPI 驱动会沿用 SysConfig 写下的"
                    "配置，请自行核对时钟",
                )
            )
            continue
        else:
            base = f"PD{record.get('PowerDomain')}_BUSCLK_FREQ"
        if div != 1:
            lines.append(f"static constexpr uint32_t {name}_CLK_FREQ = {base} / {div};")
    return lines


def _ulpclk_text(divider: int) -> str:
    """PD0 的 ULPCLK 说明文字，随 SysConfig 写入的分频变化。
    The notice text of the PD0 ULPCLK, following the divider SysConfig wrote.
    """
    return (
        f"ULPCLK = CPUCLK_FREQ / {divider} (DL_SYSCTL_setULPCLKDivider)"
        if divider != 1
        else "ULPCLK = CPUCLK_FREQ (no ULPCLK divider)"
    )


def _uart_dma_defines(uarts: list) -> list[str]:
    """UART 发送 DMA 通道的所有权标注：MSPM0_UART_MAIN_INIT 需要知道通道属于哪个 UART、服务于
    发送端。
    The ownership annotations of the UART transmit DMA channels: MSPM0_UART_MAIN_INIT needs to
    know which UART owns a channel and that the channel serves its transmitter.
    """
    lines: list[str] = []
    for _name, instance, record, _channel in uarts:
        channel = record.get("DMA_TX")
        if channel:
            lines.append(f"#define {channel}_LIBXR_UART_IRQN {instance}_INST_INT_IRQN")
            lines.append(f"#define {channel}_LIBXR_UART_TX 1")
    if lines:
        lines = [
            "// Ownership of the DMA channels that serve a UART transmitter;",
            "// MSPM0_UART_MAIN_INIT checks these names against the ones SysConfig generated.",
        ] + lines
    return lines


def _buffers(peripherals: dict) -> list[str]:
    """DMA 缓冲区的定义：UART 发送缓冲是两个半区（2 x N，按 size_t 对齐），SPI 收发各一，I2C 是
    轮询与 DMA 之间的暂存区。
    The DMA buffer declarations: a UART transmit buffer holds two halves (2 x N, aligned as
    size_t), an SPI gets one for each direction and an I2C one staging area between polling and
    DMA.
    """
    lines: list[str] = []
    for name, _instance, _record, _channel in peripherals.get("UART", []):
        settings = _instance_settings("UART", name)
        tx = settings["tx_buffer_size"]
        lines.append(f"alignas(size_t) static uint8_t {name}_tx_buf[2 * {tx}];")
    for name, _instance, _record, _channel in peripherals.get("SPI", []):
        settings = _instance_settings("SPI", name)
        lines.append(f"alignas(4) static uint8_t {name}_rx_buf[{settings['rx_buffer_size']}];")
        lines.append(f"alignas(4) static uint8_t {name}_tx_buf[{settings['tx_buffer_size']}];")
    for name, _instance, _record, _channel in peripherals.get("I2C", []):
        settings = _instance_settings("I2C", name)
        lines.append(f"alignas(4) static uint8_t {name}_buf[{settings['buffer_size']}];")
    if not lines:
        return []
    return [
        "// DMA buffers. A UART gets 2 x N bytes for its two transmit halves; the receive side",
        "// runs on byte interrupts. The SPI DMA buffers are split in two halves as well; the",
        "// I2C driver uses polling, its buffer is the staging area for DMA transfers.",
    ] + lines


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


def _uart_section(uarts: list) -> list[str]:
    """UART 对象：MSPM0_UART_MAIN_INIT，发送走 DMA；没有 DMA TX 的实例报错。
    The UART objects: MSPM0_UART_MAIN_INIT with DMA on the transmit side; an instance without
    DMA TX reports an error.
    """
    lines = [f"{INDENT}// UART: TX with DMA; the receive side runs on byte interrupts."]
    for name, instance, record, _channel in uarts:
        if not record.get("DMA_TX"):
            _fail(
                tr(
                    f"{instance}: the MSPM0 UART driver constructs only with DMA TX; enable "
                    "DMA TX in SysConfig or drop the UART",
                    f"{instance}：MSPM0 的 UART 驱动只支持带 DMA TX 的构造；请在 SysConfig 中"
                    "启用 DMA TX，或删去这个 UART",
                )
            )
        settings = _instance_settings("UART", name)
        tx_buf = f"{name}_tx_buf"
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


def _i2c_section(i2cs: list) -> list[str]:
    """I2C 对象：轮询驱动，缓冲是 DMA 传输的暂存区；时钟是所在电源域的 BUSCLK。
    The I2C objects: the polling driver whose buffer stages DMA transfers; the clock is the
    BUSCLK of the power domain.
    """
    if not i2cs:
        return []
    lines = [f"{INDENT}// I2C: polling; the buffer stages DMA transfers."]
    for name, instance, record, _channel in i2cs:
        settings = _instance_settings("I2C", name)
        domain = record.get("PowerDomain")
        clock = f"PD{domain}_BUSCLK_FREQ" if domain is not None else f"{instance}_INST_FREQUENCY"
        lines += layout(
            f"static MSPM0I2C {name}",
            [
                f"MSPM0_I2C_INIT({instance}",
                clock,
                f"{name}_buf",
                f"sizeof({name}_buf)",
                f"{settings['dma_enable_min_size']})",
                Braces(f"{instance}_BUS_SPEED_HZ"),
            ],
        )
    return lines


def _spi_section(spis: list) -> list[str]:
    """SPI 对象：收发各一个 DMA 通道；时钟是所在电源域的 BUSCLK 按 SysConfig 的 divideRatio
    分频后的值。
    The SPI objects: one DMA channel for each direction; the clock is the BUSCLK of the power
    domain divided by the divideRatio SysConfig wrote.
    """
    if not spis:
        return []
    lines = [f"{INDENT}// SPI: DMA for transfers longer than the threshold."]
    for name, instance, record, _channel in spis:
        dma_rx, dma_tx = record.get("DMA_RX"), record.get("DMA_TX")
        if not dma_rx or not dma_tx:
            _fail(
                tr(
                    f"{instance}: the MSPM0 SPI driver constructs only with a pair of DMA "
                    "channels; enable DMA in SysConfig or drop the SPI",
                    f"{instance}：MSPM0 的 SPI 驱动只支持带一对 DMA 通道的构造；请在 SysConfig "
                    "中启用 DMA，或删去这个 SPI",
                )
            )
        settings = _instance_settings("SPI", name)
        domain = record.get("PowerDomain")
        if record.get("SysConfigClock"):
            clock = f"{instance}_CLK_FREQ"
        elif domain is not None:
            clock = f"PD{domain}_BUSCLK_FREQ"
        else:
            clock = f"{instance}_INST_FREQUENCY"
        lines += layout(
            f"static MSPM0SPI {name}",
            [
                f"MSPM0_SPI_INIT({instance}",
                clock,
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
    """PWM 对象：定时器的每个通道一个 MSPM0PWM；频率设置在 libxr_config.yaml 中给出时对每个
    通道调用 SetConfig。
    The PWM objects: one MSPM0PWM per channel of the timer; SetConfig is called on every
    channel when the frequency is set in libxr_config.yaml.
    """
    if not pwms:
        return []
    lines = [
        f"{INDENT}// PWM: the channels of one timer share one period, which SysConfig configures.",
    ]
    for name, instance, _record, channel in pwms:
        lines += layout(f"static MSPM0PWM {name}", [f"MSPM0_PWM_CH({instance}, {channel})"])
        settings = stm32._settings("PWM", name)
        frequency = settings.setdefault("frequency", None)
        if frequency:
            lines += layout(f"{name}.SetConfig", [Braces(str(frequency))])
    return lines


def _setting_int(key: str, value) -> int:
    """整数设置：不是正整数时报错退出。
    An integer setting; a value that is not a positive integer logs an error and exits.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or int(value) != value
        or value < 1
    ):
        _fail(
            tr(
                f"libxr_config.yaml: {key} {value!r} is not a positive integer",
                f"libxr_config.yaml：{key} {value!r} 不是正整数",
            )
        )
    return int(value)


def _terminal_section(peripherals: dict) -> list[str]:
    """终端：terminal_source 指定的 UART 作为标准输入输出，RamFS 和 Terminal 由软件定时器任务
    每 10 ms 运行一次；terminal_source 未设置时用第一个 UART，写成 none 时不生成终端。
    The terminal: the UART named by terminal_source becomes the standard I/O, and the RamFS and
    Terminal objects run in a software timer task every 10 ms; the first UART is used when
    terminal_source is not set, and none skips the terminal.
    """
    uarts = peripherals.get("UART", [])
    source = str(stm32.libxr_settings.get("terminal_source") or "").lower()
    if not source:
        if not uarts:
            return []
        source = uarts[0][0]
        stm32.libxr_settings["terminal_source"] = source
        logging.info(
            tr(
                f"terminal_source is not set; using {source}",
                f"未设置 terminal_source；使用 {source}",
            )
        )
    elif source == "none":
        stm32.libxr_settings["terminal_source"] = ""
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
        _setting_int(f"Terminal.{key}", term_config.setdefault(key, default))
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


def _sections(project_data: dict, peripherals: dict, gpio: dict, use_xrobot: bool) -> list[str]:
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
        _uart_section(peripherals.get("UART", [])),
        _i2c_section(peripherals.get("I2C", [])),
        _spi_section(peripherals.get("SPI", [])),
        _pwm_section(peripherals.get("PWM", [])),
        _terminal_section(peripherals),
    ]
    registrations = stm32.generate_xrobot_registrations() if use_xrobot else []
    if registrations:
        blocks.append([f"{INDENT}// Hardware registration"] + registrations)
    return stm32.join_blocks([block for block in blocks if block])


def generate_full_code(project_data: dict, use_xrobot: bool, existing_code: str) -> str:
    """生成 app_main 源文件的完整内容，并填回已有代码中 User Code 区域的内容。
    Generate the full content of the app_main source file and put back the User Code bodies of
    the existing code.
    """
    peripherals = _peripherals(project_data)
    gpio = project_data.get("GPIO", {})
    for label in gpio:
        if not IDENTIFIER.fullmatch(label) or label in stm32.registered_devices:
            _fail(
                tr(
                    f"GPIO name {label} is not a valid unique C++ identifier",
                    f"GPIO 名字 {label} 不是合法且唯一的 C++ 标识符",
                )
            )
        stm32._register_device(label, "GPIO")
    uarts = peripherals.get("UART", [])
    if uarts and not any(record.get("DMA_TX") for _o, _i, record, _c in uarts):
        logging.warning(
            tr(
                "No UART has DMA TX; the MSPM0 UART driver constructs only with DMA TX",
                "没有任何 UART 配置 DMA TX；MSPM0 的 UART 驱动只支持带 DMA TX 的构造",
            )
        )
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
    clocks = _clock_constants(project_data)
    if clocks:
        lines += [""] + clocks
    defines = _uart_dma_defines(uarts)
    if defines:
        lines += [""] + defines
    buffers = _buffers(peripherals)
    if buffers:
        lines += [""] + buffers
    lines += ["", 'extern "C" void app_main(void)', "{"]
    lines += [f"{INDENT}/* User Code Begin 2 */", f"{INDENT}/* User Code End 2 */", ""]
    lines += _sections(project_data, peripherals, gpio, use_xrobot)
    lines += ["", f"{INDENT}/* User Code Begin 3 */"] + default_3
    lines.append(f"{INDENT}/* User Code End 3 */")
    if use_xrobot:
        lines.append(f"{INDENT}XROBOT_MAIN();")
    lines.append("}")
    return stm32._preserve_generated_regions(existing_code, "\n".join(lines) + "\n")


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

        files = {
            os.path.basename(output_path): generate_full_code(
                project_data, use_xrobot, existing_code
            ),
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
