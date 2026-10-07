"""libxr gen 对 HPM 工程：由工程 YAML 生成 User/app_main.cpp 和 libxr_config.yaml。
libxr gen for an HPM project: generate User/app_main.cpp and libxr_config.yaml from the
project YAML.

时钟和引脚都由 Pinmux Tool 配置（根目录 main.c 调用 init_bsp_pins 等函数），main.c 是 BSP 的
财产，生成代码只写 User/ 下的 app_main。对象全部使用 SDK 的宏（HPM_GPIO0、GPIO_DI_GPIOx、
IRQn_GPIO0_x、HPM_I2Cn、HPM_GPTMRn、clock_i2cn、clock_gptmrn），布局沿 bsp_stm32f103。设置来自
libxr_config.yaml（首次生成把默认值写进该文件，已有值保持不变）：I2C 的速率，PWM 的频率，GPIO
的改名表。
Clocks and pins are configured by the Pinmux Tool (the root main.c calls init_bsp_pins and
friends), main.c stays BSP-owned, and the generated code is only the app_main under User/.
Every object uses the SDK macros (HPM_GPIO0, GPIO_DI_GPIOx, IRQn_GPIO0_x, HPM_I2Cn, HPM_GPTMRn,
clock_i2cn, clock_gptmrn), and the layout follows bsp_stm32f103. The settings come from
libxr_config.yaml (the first generation writes the defaults into the file and keeps the values
that are already there): the I2C speed, the PWM frequency and the renames of the GPIO pins.

对象名：GPIO 默认按引脚命名（pb11，端口字母小写加行号，用户 2026-10-07 确认），libxr_config 的
GPIO 段把它映射到新名字；I2C 用外设名的小写（i2c3），PWM 是 pwm_gptmr0_ch1。HPM 的 .hpmpc 不带
速率和频率，I2C 默认 100 kHz，PWM 只在 libxr_config 给出频率时调用 SetConfig。GPIO 中断遵循
STM32 的约定（模型 O1）：生成器只创建对象（HPM 上带端口 IRQ），边沿来自 pinmux；RegisterCallback
和 EnableInterrupt 属于消费者（Module 或 User Code）。UART、SPI 和 MCAN 只被识别，不生成对象
（适配层还没合入 libxr）。GPTMR 上的 PWM 走 HPMPWM 的 fallback 路径，只在 SoC 没有 PWM 外设时
生成——解析按芯片引脚数据判定（HPM5361 有 PWM 外设，它的 GPTMR 归入 Other 只作展示）——对象
和登记都无条件生成，不能包 #if：xrobot 的生成器无法判断编译选项，guard 里的 XR_REGISTER 会被
它拒绝。
Object names: a GPIO defaults to its pin name (pb11, the lower-case port letter plus the line,
confirmed by the user 2026-10-07) and the GPIO section of libxr_config maps it to a new name; an
I2C takes the lower-case peripheral name (i2c3) and a PWM is pwm_gptmr0_ch1. The .hpmpc of an
HPM carries no speed or frequency, so an I2C starts at 100 kHz and a PWM calls SetConfig only
when libxr_config gives a frequency. GPIO interrupts follow the STM32 convention (model O1):
the generator creates the object only (with its port IRQ on HPM), the edge comes from the
pinmux, and RegisterCallback and EnableInterrupt belong to the consumer (a Module or the User
Code). UART, SPI and MCAN are recognized but generate no objects (the adapters are still held
back from libxr). A PWM on a GPTMR takes the fallback path of HPMPWM and is generated only
when the SoC has no PWM peripheral — the parse decides from the chip's pin data (a HPM5361 has
one, so its GPTMRs land in Other for display only) — and both the object and its registration
are generated unconditionally; they must not sit inside #if, because xrobot's generator cannot
evaluate build options and rejects an XR_REGISTER inside a guard.
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
# HPM 各设置段的默认值；STM32 段（USART/TIM/USB……）不适用于 HPM，生成前剔除。
# The defaults of the HPM settings sections; the STM32 sections (USART/TIM/USB, ...) do not
# apply to HPM and are removed before generation.
HPM_DEFAULTS = {"I2C": {"speed": 100000}}
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
# HPM 的 I2C 驱动只接受这三个速率（hpm_i2c.hpp 的构造说明）。
# The HPM I2C driver accepts only these three rates (the constructor notes of hpm_i2c.hpp).
I2C_SPEEDS = (100000, 400000, 1000000)
# GPTMR fallback 路径的占空比比较器固定为 0，重装比较器由驱动取另一个（hpm_pwm.cpp）。
# The duty comparator of the GPTMR fallback path is fixed at 0; the driver takes the other
# one for the reload comparator (hpm_pwm.cpp).
PWM_DUTY_CMP = 0

IDENTIFIER = re.compile(r"[A-Za-z_]\w*")


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
    for key, default in HPM_DEFAULTS[group].items():
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
                f"{name} is not a valid C++ identifier; rename the instance in the Pinmux Tool",
                f"{name} 不是合法的 C++ 标识符；请在 Pinmux Tool 中重命名该实例",
            )
        )
    if name in stm32.registered_devices:
        _fail(
            tr(
                f"Two objects are named {name} ({kind} and {stm32.registered_devices[name]}); "
                "rename one of them in libxr_config.yaml",
                f"两个对象都叫 {name}（{kind} 与 {stm32.registered_devices[name]}）；请在 "
                "libxr_config.yaml 中重命名其中一个",
            )
        )
    return name


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


def _peripherals(project_data: dict) -> dict[str, list]:
    """工程 YAML 中的外设对象：kind -> [(对象名, 实例名, 记录, PWM 通道)]，按 YAML 顺序，同时
    登记进 XR_REGISTER 的对象表。
    The peripheral objects of the project YAML: kind -> [(object name, instance name, record,
    PWM channel)], in YAML order, registered into the XR_REGISTER table as well.
    """
    result: dict[str, list] = {}
    for kind, records in project_data.get("Peripherals", {}).items():
        if kind == "Other":
            continue
        objects = []
        for instance, record in records.items():
            if kind == "PWM":
                # GPTMR 的对象名 pwm_gptmr0_ch1；通道序号是 pinmux 的比较器序号。
                # The object name of a GPTMR is pwm_gptmr0_ch1; the channel index is the
                # comparator index of the pinmux.
                for channel in record.get("Channels", []):
                    index = channel["Index"]
                    name = _object_name(f"pwm_{str(instance).lower()}_ch{index}", "PWM")
                    objects.append((name, instance, record, index))
                    stm32._register_device(name, "PWM")
            else:
                name = _object_name(str(record["Peripheral"]), kind)
                objects.append((name, instance, record, None))
                stm32._register_device(name, kind)
        result[kind] = objects
    return result


def _gpio_names(project_data: dict) -> dict[str, dict]:
    """GPIO 对象的名字：默认按引脚（pb11），libxr_config 的 GPIO 段把它映射到新名字，值为
    null 的改名保留默认名；名字必须合法且唯一，不满足时报错退出，改名单里没有的引脚给出警告。
    The names of the GPIO objects: the pin name (pb11) by default, which the GPIO section of
    libxr_config maps to a new one while a null rename keeps the default; a name that is not a
    valid unique identifier logs an error and generation exits, and a rename that names no pin is
    warned about.
    """
    gpio = project_data.get("GPIO", {})
    renames = stm32.libxr_settings.get("GPIO") or {}
    if not isinstance(renames, dict):
        _fail(
            tr(
                f"{stm32.libxr_config_origin}: GPIO {renames!r} is not a mapping of a pin name "
                "to an object name",
                f"{stm32.libxr_config_origin}：GPIO {renames!r} 不是引脚名到对象名的映射",
            )
        )
    names: dict[str, dict] = {}
    for label, pin in gpio.items():
        rename = renames.get(label)
        name = str(label if rename is None else rename)
        if not IDENTIFIER.fullmatch(name) or name in stm32.registered_devices:
            _fail(
                tr(
                    f"GPIO name {name} is not a valid unique C++ identifier",
                    f"GPIO 名字 {name} 不是合法且唯一的 C++ 标识符",
                )
            )
        stm32._register_device(name, "GPIO")
        names[name] = pin
    for label in renames:
        if label not in gpio:
            logging.warning(
                tr(
                    f"{stm32.libxr_config_origin}: the GPIO rename {label} names no generated "
                    "pin; it is ignored",
                    f"{stm32.libxr_config_origin}：GPIO 改名 {label} 没有对应的生成引脚；忽略",
                )
            )
    return names


def _pull_text(pad_ctls: dict) -> str:
    """padCtls（PE/PS）的上下拉描述；没有上拉使能时为空。
    The pull description of padCtls (PE/PS); empty when the pull is not enabled.
    """
    if str(pad_ctls.get("PE")) != "1":
        return ""
    return " with a pull-up" if str(pad_ctls.get("PS")) == "1" else " with a pull-down"


def _gpio_description(label: str, pin: dict) -> str:
    """一个 GPIO 引脚在说明注释里的描述：方向来自 gpiom，上下拉来自 padCtls。
    The description of one GPIO pin in the notice comment: the direction comes from gpiom and
    the pull from padCtls.
    """
    pad = pin["Pad"]
    direction = pin.get("Direction")
    if direction == "OUTPUT":
        return f"{label} ({pad}) as an output pin"
    if direction == "INPUT":
        return f"{label} ({pad}) as an input pin{_pull_text(pin.get('PadCtls') or {})}"
    return f"{label} ({pad})"


def _gpio_section(project_data: dict, gpio: dict[str, dict]) -> list[str]:
    """GPIO 对象：每个引脚一个 HPMGPIO，构造带端口 IRQ；中断的注册和使能属于消费者（O1），不
    在这里生成。
    The GPIO objects: one HPMGPIO per pin, constructed with its port IRQ; registering and
    enabling an interrupt belongs to the consumer (O1) and is not generated here.
    """
    if not gpio:
        return []
    functions = ", ".join(
        f"{name}()" for name in project_data.get("MainFunctions") or ["init_bsp_pins"]
    )
    summary = ", ".join(_gpio_description(label, pin) for label, pin in gpio.items())
    lines = _comment(
        f"GPIO: {functions} configured {summary}. The interrupt edge comes from the pinmux "
        "and every object carries its port IRQ; RegisterCallback and EnableInterrupt belong "
        "to the consumer (a Module or the User Code).",
        INDENT,
    )
    for label, pin in gpio.items():
        port = pin["Port"]
        lines += layout(
            f"static HPMGPIO {label}",
            ["HPM_GPIO0", f"GPIO_DI_GPIO{port}", str(pin["Line"]), f"IRQn_GPIO0_{port}"],
        )
    return lines


def _i2c_section(i2cs: list) -> list[str]:
    """I2C 对象：速率是 libxr_config 中的设置，驱动只接受固定的几档。
    The I2C objects: the speed is a setting of libxr_config, and the driver accepts only the
    fixed rates.
    """
    if not i2cs:
        return []
    lines = [f"{INDENT}// I2C: blocking transfers; the DMA-backed background path runs on dma_mgr."]
    for name, instance, _record, _channel in i2cs:
        speed = _setting_int(f"I2C.{name}.speed", _instance_settings("I2C", name)["speed"])
        if speed not in I2C_SPEEDS:
            _fail(
                tr(
                    f"libxr_config.yaml: I2C.{name}.speed {speed} is not one of "
                    f"{', '.join(map(str, I2C_SPEEDS))}; the HPM I2C driver supports only "
                    "these rates",
                    f"libxr_config.yaml：I2C.{name}.speed {speed} 不是 "
                    f"{', '.join(map(str, I2C_SPEEDS))} 之一；HPM 的 I2C 驱动只支持这几档",
                )
            )
        lines += layout(
            f"static HPMI2C {name}",
            [f"HPM_{instance}", f"clock_{instance.lower()}", Braces(f"{speed}U")],
        )
    return lines


def _pwm_section(pwms: list) -> list[str]:
    """PWM 对象：GPTMR 的每个比较器输出一个 HPMPWM，占空比比较器固定为 0；对象只在 SoC 没有
    PWM 外设时生成（解析已判定，HPMPWM 在那里走 GPTMR fallback 路径），libxr_config.yaml 中
    给出频率时才调用 SetConfig。对象和登记都不能包 #if：xrobot 的生成器无法判断编译选项，
    guard 里的 XR_REGISTER 会被它拒绝。
    The PWM objects: one HPMPWM per comparator output of a GPTMR, with the duty comparator
    fixed at 0; an object is generated only when the SoC has no PWM peripheral (decided by
    the parse, where HPMPWM takes its GPTMR fallback path). SetConfig is called when the
    frequency is set in libxr_config.yaml. Neither the objects nor the registrations may sit
    inside #if: xrobot's generator cannot evaluate build options and rejects an XR_REGISTER
    inside a guard.
    """
    if not pwms:
        return []
    lines = _comment(
        "PWM on a GPTMR comparator: the SoC has no PWM peripheral, so HPMPWM takes its GPTMR "
        "fallback path; the duty comparator is fixed at 0 and SetConfig is called when "
        "libxr_config sets a frequency.",
        INDENT,
    )
    for name, instance, _record, channel in pwms:
        lines += layout(
            f"static HPMPWM {name}",
            [
                f"reinterpret_cast<LibXRHpmPwmType*>(HPM_{instance})",
                f"clock_{instance.lower()}",
                str(channel),
                str(PWM_DUTY_CMP),
                "HPMPWM::Polarity::NORMAL",
            ],
        )
        frequency = stm32._settings("PWM", name).setdefault("frequency", None)
        if frequency:
            lines += layout(f"{name}.SetConfig", [Braces(str(frequency))])
    return lines


def _terminal_section() -> list[str]:
    """终端：HPM 生成器不创建 UART，terminal_source 没有可用的终端；设置了就给出警告。
    The terminal: the HPM generator creates no UART, so terminal_source has no terminal to
    name; a set one is warned about.
    """
    source = str(stm32.libxr_settings.get("terminal_source") or "")
    if source and source != "none":
        logging.warning(
            tr(
                f"terminal_source '{source}' has no effect: the HPM generator creates no "
                "UART, so the terminal is not initialized",
                f"terminal_source '{source}' 无效：HPM 生成器不创建 UART，终端不初始化",
            )
        )
    return []


def _registrations(use_xrobot: bool) -> list[str]:
    """XRobot 登记表：每个对象一行 XR_REGISTER，按种类分组。
    The XRobot registrations: one XR_REGISTER line per object, grouped by kind.
    """
    if not use_xrobot:
        return []
    return stm32.generate_xrobot_registrations()


def _sections(
    project_data: dict, peripherals: dict[str, list], gpio: dict[str, dict], use_xrobot: bool
) -> list[str]:
    """app_main 函数体的各段：时基、GPIO、I2C、PWM、终端和登记。
    The sections of the app_main body: timebase, GPIO, I2C, PWM, terminal and the
    registrations.
    """
    blocks = [
        [
            f"{INDENT}// Timebase and platform",
            f"{INDENT}static HPMTimebase timebase;",
            f"{INDENT}PlatformInit();",
        ],
        _gpio_section(project_data, gpio),
        _i2c_section(peripherals.get("I2C", [])),
        _pwm_section(peripherals.get("PWM", [])),
        _terminal_section(),
    ]
    registrations = _registrations(use_xrobot)
    if registrations:
        blocks.append([f"{INDENT}// Hardware registration"] + registrations)
    return stm32.join_blocks([block for block in blocks if block])


def _notice(project_data: dict) -> list[str]:
    """文件开头的说明：由哪个 .hpmpc 生成、Pinmux Tool 和 BSP 各管什么、只改 User Code 区域。
    The notice at the start of the file: which .hpmpc it was generated from, what the Pinmux
    Tool and the BSP own and that only the User Code regions are for editing.
    """
    functions = ", ".join(
        f"{name}()" for name in project_data.get("MainFunctions") or ["init_bsp_pins"]
    )
    return [GENERATED_NOTICE] + _comment(
        f"Generated from {project_data.get('Hpmpc', 'the HPM Pinmux Tool project')}"
        f" ({project_data.get('Mcu', {}).get('Type', '')}); the Pinmux Tool configures the"
        f" pins via {functions} in main.c and main.c stays BSP-owned. Put application code"
        ' between the "User Code Begin" and "User Code End" markers.'
    )


def _includes(project_data: dict, use_xrobot: bool) -> list[str]:
    """include 列表：libxr.hpp、hpm_soc.h、用到的 hpm 驱动头和时基头，XRobot 再加
    xrobot_main.hpp；均按字母顺序。
    The includes: libxr.hpp, hpm_soc.h, the hpm driver headers in use and the timebase one
    and, with XRobot, xrobot_main.hpp; all in alphabetical order.
    """
    peripherals = project_data.get("Peripherals", {})
    drivers = ["hpm_soc.h", "hpm_timebase.hpp"]
    if project_data.get("GPIO"):
        drivers.append("hpm_gpio.hpp")
    for kind in ("I2C", "PWM"):
        if peripherals.get(kind):
            drivers.append(f"hpm_{kind.lower()}.hpp")
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


def generate_full_code(project_data: dict, use_xrobot: bool, existing_code: str) -> str:
    """生成 app_main 源文件的完整内容，并填回已有代码中 User Code 区域的内容。
    Generate the full content of the app_main source file and put back the User Code bodies of
    the existing code.
    """
    peripherals = _peripherals(project_data)
    gpio = _gpio_names(project_data)
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
    """清空生成对象的登记表，使同一次进程中的下一次生成不带上次的对象。
    Clear the registry of generated objects, so the next generation in the same process
    carries no objects over.
    """
    stm32.registered_devices.clear()
    stm32.registered_origins.clear()
    stm32.used_headers.clear()


def reset_settings() -> None:
    """把生效的设置恢复为 HPM 的默认集合：STM32 专用的段剔除，I2C、PWM 和 GPIO 段补上。
    Restore the effective settings to the HPM default set: the STM32-only sections removed
    and the I2C, PWM and GPIO sections added.
    """
    stm32.reset_settings()
    for key in STM32_ONLY_SECTIONS:
        stm32.libxr_settings.pop(key, None)
    stm32._settings("I2C")
    stm32._settings("PWM")
    stm32._settings("GPIO")


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
        # load_configuration 又写入了 SYSTEM（HPM 只有裸机），再剔除一次。
        # load_configuration has written SYSTEM again (HPM is bare metal only); remove it
        # once more.
        stm32.libxr_settings.pop("SYSTEM", None)
        stm32._settings("I2C")
        stm32._settings("PWM")
        stm32._settings("GPIO")
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
