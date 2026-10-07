"""libxr parse 对 HPM 工程：解析 Pinmux Tool 工程并写出工程 YAML。
libxr parse for an HPM project: parse the Pinmux Tool project and write the project YAML.

工程是 app.yaml 加 boards/ 下唯一的 .hpmpc。已选信号取根目录 main.c 的 main 在预处理条件外
调用的 pinmux 函数（O2：条件编译里的不算，例如 rmcs 的 JTAG 共用引脚），从 .hpmpc 的 selectPins
读出引脚和信号，化成 hpm_iomux.h 的宏名后按 SoC 封装的布局核对（封装上没有的焊盘、焊盘不能
承载的信号给出警告）。读 .hpmpc、找 main 的调用和合并各函数的选择与 libxr pins 共用
pin_project 的实现。GPIO 的复用（GPIO.A.B[11]）算引脚自己的普通用法：对象默认按引脚命名
（pb11，用户 2026-10-07 确认），libxr_config 可改名；其余外设按 recognize_hpm 识别，LibXR 有
驱动的（I2C；以及 SoC 没有 PWM 外设时 GPTMR 比较器上的 PWM——判定来自芯片引脚数据里有没有 PWM
信号，HPM5301 没有、HPM5361 有）进入 Peripherals，其余（UART、SPI、CAN、ACMP、只有捕获输入的
GPTMR、有 PWM 外设的 SoC 上的 GPTMR……）记入 Other，只作展示。
The project is app.yaml plus the single .hpmpc under boards/. The selected signals come from
the pinmux functions that main in the root main.c calls outside preprocessor conditions (O2: a
call inside a conditional does not count, such as the rmcs JTAG shared pins), read from the
selectPins of the .hpmpc and checked, as the macro names of hpm_iomux.h, against the layout of
the SoC package (a pad the package does not have, or a signal its pad cannot carry, is warned
about). Reading the .hpmpc, finding the calls of main and merging the selections of the
functions share the pin_project implementation with libxr pins. A GPIO mux (GPIO.A.B[11])
counts as the pin's own plain use: the object defaults to the pin name (pb11, confirmed by the
user 2026-10-07) and libxr_config renames it; the other peripherals go through recognize_hpm,
and the ones LibXR has drivers for (I2C; a PWM on a GPTMR comparator when the SoC has no PWM
peripheral — decided by whether the chip's pin data has any PWM signal, HPM5301 has none and
HPM5361 has 20) enter Peripherals while the rest (UART, SPI, CAN, ACMP, a GPTMR with capture
inputs only, a GPTMR on a SoC with a PWM peripheral, ...) are recorded under Other for display
only.

UART、SPI 和 MCAN 的驱动还压着没合（生成器认出它们，但不生成对象）。
The UART, SPI and MCAN drivers are still held back (the generator recognizes them but
generates no objects).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import yaml
from xr_syntax.i18n import tr

from libxr import pin_layout, pin_project

# .hpmpc 里 gpiom 管理器的 gpioController 值：1 是 SoC 的 GPIO0（工具为它生成
# gpiom_soc_gpio0），HPMGPIO 只驱动这一个控制器。
# The gpioController value of the gpiom manager of a .hpmpc: 1 is the GPIO0 of the SoC (the
# tool generates gpiom_soc_gpio0 for it), the only controller HPMGPIO drives.
GPIO0_CONTROLLER = "1"


def _fail(message: str):
    """记录错误并以状态 1 退出。
    Log an error and exit with status 1.
    """
    logging.error(message)
    sys.exit(1)


def _gpio_entry(pad: str, pad_ctls: dict, gpiom: dict) -> dict | None:
    """一个 GPIO 引脚的工程记录：端口字母、引脚号、方向（gpiom 写下的）和 pad 设置；焊盘名不是
    P<字母><数字> 时为 None。
    The project record of one GPIO pin: the port letter, the pin line, the direction the gpiom
    manager wrote and the pad settings; None when the pad name is not P<letter><digits>.
    """
    port_line = pin_layout.gpio_port_and_line(pad)
    if port_line is None or not re.fullmatch(r"P[A-Z]\d+", pad):
        return None
    entry = {
        "Pad": pad,
        "Port": port_line[0],
        "Line": int(port_line[1]),
        "PadCtls": pad_ctls or {},
    }
    direction = str(gpiom.get("direction") or "")
    if direction == "1":
        entry["Direction"] = "OUTPUT"
    elif direction == "0":
        entry["Direction"] = "INPUT"
    return entry


def _instance(signal: str) -> str:
    """信号所属的外设实例（UART0_TXD 为 UART0）；GPIO 的复用都算 GPIO。
    The peripheral instance a signal belongs to (UART0 for UART0_TXD); every GPIO mux counts
    as GPIO.
    """
    recognized = pin_layout.recognize_hpm(signal)
    return recognized[0] if recognized else "GPIO" if signal.startswith("GPIO_") else signal


def _project(directory: str) -> tuple[Path, Path, pin_project.HpmProject]:
    """directory 中唯一的 .hpmpc、根目录 main.c 和读出的工程；缺少或读不出时报错退出。SoC 和
    封装必须在芯片引脚数据里（SoC 名不区分大小写）。
    The single .hpmpc of directory, the root main.c and the project read from it; a missing or
    unreadable one logs an error and exits. The SoC and the package must be in the chip pin
    data (the SoC name in any case).
    """
    root = Path(directory)
    hpmpcs = sorted(root.glob("boards/*/*.hpmpc"))
    if len(hpmpcs) != 1:
        names = ", ".join(path.as_posix() for path in hpmpcs) or tr("none found", "没有找到")
        _fail(
            tr(
                f"{directory} must hold exactly one .hpmpc under boards/ ({names})",
                f"{directory} 的 boards/ 下必须有且只有一个 .hpmpc（{names}）",
            )
        )
    main_c = root / "main.c"
    if not main_c.is_file():
        _fail(
            tr(
                f"{directory}: no root main.c; the HPM main sits at the project root",
                f"{directory}：根目录没有 main.c；HPM 的 main 在工程根目录",
            )
        )
    try:
        project = pin_project.read_hpmpc(hpmpcs[0])
    except ValueError as error:
        _fail(str(error))
    if project.soc is None:
        _fail(tr(f"{hpmpcs[0]} does not name a SoC", f"{hpmpcs[0]} 没有给出 SoC"))
    return hpmpcs[0], main_c, project


def _layout(project: pin_project.HpmProject) -> pin_layout.PinLayout:
    """工程 SoC 封装的布局；SoC 或封装不在引脚数据里时报错退出。
    The layout of the project's SoC package; a SoC or package missing from the pin data logs an
    error and exits.
    """
    try:
        return pin_layout.layout_hpm(project.soc, project.package)
    except ValueError as error:
        _fail(f"{project.path}: {error}")


def _report_calls(
    hpmpc: Path,
    main_c: Path,
    project: pin_project.HpmProject,
    called: list[str],
    conditional: list[str],
    active: list[str],
) -> None:
    """main.c 调用情况的警告：条件编译里调用的函数、没有被调用的函数，以及完全没有调用时的
    init_bsp_pins 兜底。
    The warnings about the calls of main.c: the functions called inside a conditional, the
    functions not called, and the init_bsp_pins fallback when there is no call at all.
    """
    for name in conditional:
        logging.warning(
            tr(
                f"{main_c}: {name}() is called inside a preprocessor condition; its pins are not "
                "read (the generated objects must not depend on build options)",
                f"{main_c}：{name}() 在预处理条件里调用；不读它的引脚（生成的对象不能依赖编译选项）",
            )
        )
    if not called:
        detail = (
            tr("using the init_bsp_pins fallback", "退回 init_bsp_pins")
            if active
            else tr(
                "no init_bsp_pins exists, so no pin is read", "没有 init_bsp_pins 可退，读不到引脚"
            )
        )
        logging.warning(
            tr(
                f"{main_c}: main calls no pinmux function ({detail}); a pin function called "
                "from elsewhere (board.c, a helper) is not visible",
                f"{main_c}：main 没有调用任何 pinmux 函数（{detail}）；从别处（board.c、辅助函数）"
                "调用的引脚函数看不到",
            )
        )
        return
    for name in project.functions:
        if name not in called and name not in conditional:
            logging.warning(
                tr(
                    f"{hpmpc}: the pinmux function {name}() is not called by main in "
                    f"{main_c.name}; its pins are not read",
                    f"{hpmpc}：pinmux 函数 {name}() 没有被 {main_c.name} 的 main 调用；不读它的引脚",
                )
            )


def parse_project(directory: str, output: str | None = None, summary: bool = True) -> None:
    """解析 directory 中的 HPM 工程并写出 YAML；summary 为真时打印摘要。
    Parse the HPM project in directory and write the YAML; print a summary when summary is
    set.

    输出路径默认为该目录下的 .config.yaml。调用方（libxr parse）已确认目录存在且是 HPM 工程。
    The output defaults to .config.yaml in that directory. The caller, libxr parse, has
    checked that the directory exists and is an HPM project.
    """
    root = Path(directory)
    hpmpc, main_c, project = _project(directory)
    layout = _layout(project)
    called, conditional = pin_project.hpm_main_calls(main_c, project.functions)
    active = called or (["init_bsp_pins"] if "init_bsp_pins" in project.functions else [])
    _report_calls(hpmpc, main_c, project, called, conditional, active)
    selections, conflicts = pin_project.hpm_selections(project, active)
    for conflict in conflicts:
        logging.warning(pin_project.hpm_conflict_message(hpmpc, *conflict))

    pins = {pin.name: pin for pin in layout.pins}
    known = {_instance(signal) for pin in layout.pins for signal in pin.signals}
    gpio: dict[str, dict] = {}
    peripherals: dict[str, dict] = {}
    other: dict[str, dict] = {}
    # GPTMR 只在 SoC 没有 PWM 外设时生成对象（HPM5301）；有 PWM 外设的（HPM5361）归入 Other
    # 只作展示。判定来自芯片引脚数据里有没有 PWM 信号。
    # A GPTMR generates objects only when the SoC has no PWM peripheral (HPM5301); on a SoC
    # with one (HPM5361) it lands in Other for display only. The decision comes from whether
    # the chip's pin data has any PWM signal.
    soc_has_pwm = pin_layout.hpm_soc_has_pwm(project.soc)
    for selection in selections:
        pad, signal, pad_ctls = selection["pad"], selection["signal"], selection["padCtls"]
        canonical = pin_project.hpm_canonical(signal)
        pin = pins.get(pad)
        if pin is None:
            logging.warning(
                tr(
                    f"{hpmpc}: {pad} ({signal}) is not a pin of {project.soc} {layout.package}; "
                    "skipped",
                    f"{hpmpc}：{pad}（{signal}）不是 {project.soc} {layout.package} 的引脚；跳过",
                )
            )
            continue
        # 只核对引脚数据认识的外设：工具自己的伪信号（USBPHY.A.USB_DM 等，hpm_iomux.h 和数据
        # 手册的引脚表里都没有）不算错。
        # Only peripherals the pin data knows are checked: the tool's own pseudo signals
        # (USBPHY.A.USB_DM and the like, in neither hpm_iomux.h nor the datasheet pin table)
        # are no error.
        if canonical not in pin.signals and _instance(canonical) in known:
            logging.warning(
                tr(
                    f"{hpmpc}: {pad} cannot carry {canonical} ({signal}) on {project.soc}",
                    f"{hpmpc}：{project.soc} 的 {pad} 不能承载 {canonical}（{signal}）",
                )
            )
        if canonical.startswith("GPIO_"):
            controller = str(selection["gpiom"].get("gpioController") or GPIO0_CONTROLLER)
            entry = _gpio_entry(pad, pad_ctls, selection["gpiom"])
            if entry is None:
                logging.warning(
                    tr(
                        f"{hpmpc}: {pad} does not look like a GPIO pad; skipped",
                        f"{hpmpc}：{pad} 不是 GPIO 引脚的写法；跳过",
                    )
                )
                continue
            if controller != GPIO0_CONTROLLER:
                logging.warning(
                    tr(
                        f"{hpmpc}: {pad} is assigned to GPIO controller {controller}, not the "
                        "GPIO0 HPMGPIO drives; no object is generated for it",
                        f"{hpmpc}：{pad} 分给了 GPIO 控制器 {controller}，不是 HPMGPIO 驱动的 "
                        "GPIO0；不为它生成对象",
                    )
                )
                continue
            gpio[f"p{entry['Port'].lower()}{entry['Line']}"] = entry
            continue
        recognized = pin_layout.recognize_hpm(canonical)
        instance, kind, function = recognized or (canonical, "OTHER", "PIN")
        comparator = re.fullmatch(r"COMP_(\d+)", function)
        if kind == "I2C" or (kind == "GPTMR" and comparator and not soc_has_pwm):
            section = "I2C" if kind == "I2C" else "PWM"
            record = peripherals.setdefault(section, {}).setdefault(
                instance, {"Peripheral": instance, "Pins": {}}
            )
            if comparator:
                # GPTMR 的比较器输出：序号是 PWM 通道（pwm_gptmr0_ch1）。
                # A GPTMR comparator output: its index is the PWM channel (pwm_gptmr0_ch1).
                index = int(comparator.group(1))
                channels = record.setdefault("Channels", [])
                taken = next((channel for channel in channels if channel["Index"] == index), None)
                if taken is None:
                    channels.append({"Index": index, "Pad": pad})
                elif taken["Pad"] != pad:
                    # 同一比较器只有一个输出对象，第一个焊盘为准。
                    # One comparator has one output object; the first pad wins.
                    logging.warning(
                        tr(
                            f"{hpmpc}: {instance} {function} is selected on both {taken['Pad']} "
                            f"and {pad}; the PWM object drives {taken['Pad']}",
                            f"{hpmpc}：{instance} 的 {function} 同时选在 {taken['Pad']} 和 {pad}；"
                            f"PWM 对象以 {taken['Pad']} 为准",
                        )
                    )
                    continue
            elif function in record["Pins"] and record["Pins"][function] != pad:
                logging.warning(
                    tr(
                        f"{hpmpc}: {instance} {function} is assigned to both "
                        f"{record['Pins'][function]} and {pad}; the later one wins",
                        f"{hpmpc}：{instance} 的 {function} 同时分给了 "
                        f"{record['Pins'][function]} 和 {pad}；以后一个为准",
                    )
                )
            record["Pins"][function] = pad
        else:
            # LibXR 还没有驱动的信号（UART、SPI、CAN、ACMP、GPTMR 的捕获输入……）只作展示。
            # Signals without a LibXR driver yet (UART, SPI, CAN, ACMP, the capture inputs of
            # a GPTMR, ...) are display-only.
            record = other.setdefault(
                instance, {"Module": kind, "Peripheral": instance, "Pins": {}}
            )
            record["Pins"][function] = pad

    _check_peripherals(hpmpc, peripherals, other)
    data = {
        "Platform": "hpm",
        "Hpmpc": hpmpc.relative_to(root).as_posix(),
        "Mcu": {"Family": "HPM", "Type": project.soc},
        "Package": layout.package,
        "MainFunctions": active,
        "MainCalls": list(called),
        "GPIO": gpio,
        "Peripherals": {**peripherals, "Other": other} if other else peripherals,
    }

    output_path = output or os.path.join(directory, ".config.yaml")
    if not gpio and not peripherals and not other:
        logging.warning(
            tr(
                "the active pinmux functions select no GPIO or peripheral pin",
                "活动的 pinmux 函数没有选择任何 GPIO 或外设引脚",
            )
        )
    _save_to_yaml(data, str(output_path))
    if summary:
        _print_summary(data)


def _check_peripherals(hpmpc: Path, peripherals: dict, other: dict) -> None:
    """生成前的一致性检查：I2C 必须有 SCL 和 SDA，缺一个时不生成，改记入 Other；生成 PWM 的
    GPTMR 同时选了捕获输入（记在 Other）时给出警告（捕获不生成对象）。
    The consistency checks before generation: an I2C needs both SCL and SDA, and one missing
    either is not generated but recorded under Other; a GPTMR that generates PWM and also
    selects capture inputs (recorded under Other) is warned about (capture generates no
    object).
    """
    for instance, record in list(peripherals.get("I2C", {}).items()):
        missing = [name for name in ("SCL", "SDA") if name not in record["Pins"]]
        if missing:
            logging.warning(
                tr(
                    f"{hpmpc}: {instance} has no {' or '.join(missing)} pin; no I2C object is "
                    "generated for it",
                    f"{hpmpc}：{instance} 没有 {'、'.join(missing)} 引脚；不为它生成 I2C 对象",
                )
            )
            del peripherals["I2C"][instance]
            other[instance] = {"Module": "I2C", **record}
    for instance in peripherals.get("PWM", {}):
        captures = sorted(other.get(instance, {}).get("Pins", {}))
        if captures:
            logging.warning(
                tr(
                    f"{hpmpc}: {instance} also selects {', '.join(captures)}; the capture inputs "
                    "generate no object",
                    f"{hpmpc}：{instance} 还选了 {'、'.join(captures)}；捕获输入不生成对象",
                )
            )
    for section in ("I2C", "PWM"):
        if section in peripherals and not peripherals[section]:
            del peripherals[section]


def _save_to_yaml(data: dict, output_path: str) -> bool:
    """把工程数据写成 YAML，首行为 Pinmux Tool 工程的生成文件说明；成功时为 True。
    Write the project data as YAML with a generated-file notice for a Pinmux Tool project on
    the first line; True on success.
    """
    try:
        created = os.path.dirname(output_path)
        if created:
            os.makedirs(created, exist_ok=True)
        with open(output_path, "w", encoding="utf-8", newline="\n") as file:
            file.write(
                "# Generated by `libxr parse` from the HPM Pinmux Tool project; "
                "do not edit by hand.\n"
            )
            yaml.dump(
                data, file, allow_unicode=True, sort_keys=False, default_flow_style=False, indent=2
            )
        logging.info(
            tr(f"Configuration exported to: {output_path}", f"配置已导出到：{output_path}")
        )
        return True
    except (OSError, yaml.YAMLError) as error:
        logging.error(tr(f"YAML export failed: {str(error)}", f"YAML 导出失败：{str(error)}"))
        return False


def _print_summary(data: dict) -> None:
    """向标准输出打印配置摘要：SoC、GPIO 引脚和各类外设实例。
    Print a configuration summary to standard output: the SoC, the GPIO pins and the
    peripheral instances of each kind.
    """
    print(tr("\n===== [Configuration Summary] =====", "\n===== [配置摘要] ====="))
    mcu = data.get("Mcu", {})
    print(
        tr(
            f"\nSoC: {mcu.get('Type', '')} ({data.get('Package', '')})",
            f"\nSoC：{mcu.get('Type', '')}（{data.get('Package', '')}）",
        )
    )
    gpio = data.get("GPIO", {})
    print(tr(f"\nGPIO ({len(gpio)} pins):", f"\nGPIO（{len(gpio)} 个引脚）："))
    for label, pin in gpio.items():
        print(f"  {label}: {pin['Pad']}")
    print(tr("\nActive Peripherals:", "\n已启用的外设："))
    for section, group in data.get("Peripherals", {}).items():
        print(tr(f"  {section}: {len(group)} instance(s)", f"  {section}：{len(group)} 个实例"))
        for name in group:
            print(f"    {name}")
