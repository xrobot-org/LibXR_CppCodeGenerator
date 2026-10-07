"""libxr parse 对 HPM 工程：解析 Pinmux Tool 工程并写出工程 YAML。
libxr parse for an HPM project: parse the Pinmux Tool project and write the project YAML.

工程是 app.yaml 加 boards/ 下唯一的 .hpmpc。已选信号取根目录 main.c 在预处理条件外调用的
pinmux 函数（O2：条件编译里的不算，例如 rmcs 的 JTAG 共用引脚），从 .hpmpc 的 selectPins
读出引脚和信号，化成 hpm_iomux.h 的宏名后按布局核对。GPIO 的复用（GPIO.A.B[11]）算引脚自己
的普通用法：对象默认按引脚命名（pb11，用户 2026-10-07 确认），libxr_config 可改名；其余外设
按 recognize_hpm 识别，LibXR 有驱动的（I2C；以及 SoC 没有 PWM 外设时 GPTMR 上的 PWM——判定
来自芯片引脚数据里有没有 PWM 信号，HPM5301 没有、HPM5361 有）进入 Peripherals，其余（UART、
SPI、CAN、ACMP、有 PWM 外设的 SoC 上的 GPTMR……）记入 Other，只作展示。
The project is app.yaml plus the single .hpmpc under boards/. The selected signals come from
the pinmux functions the root main.c calls outside preprocessor conditions (O2: a call inside
a conditional does not count, such as the rmcs JTAG shared pins), read from the selectPins of
the .hpmpc and checked against the layout as the macro names of hpm_iomux.h. A GPIO mux
(GPIO.A.B[11]) counts as the pin's own plain use: the object defaults to the pin name (pb11,
confirmed by the user 2026-10-07) and libxr_config renames it; the other peripherals go
through recognize_hpm, and the ones LibXR has drivers for (I2C; a PWM on a GPTMR when the SoC
has no PWM peripheral — decided by whether the chip's pin data has any PWM signal, HPM5301 has
none and HPM5361 has 20) enter Peripherals while the rest (UART, SPI, CAN, ACMP, a GPTMR on a
SoC with a PWM peripheral, ...) are recorded under Other for display only.

UART、SPI 和 MCAN 的驱动还压着没合（生成器认出它们，但不生成对象）。
The UART, SPI and MCAN drivers are still held back (the generator recognizes them but
generates no objects).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

import yaml
from xr_syntax.i18n import tr

from libxr import pin_layout, pin_project

# LibXR 有驱动、gen 生成对象的信号：段 -> 识别出的 kind。
# The signals LibXR has drivers for and gen generates objects for: section -> the kind
# recognized.
GENERATED_KINDS = {"I2C": "I2C", "GPTMR": "PWM"}
HPM_SOC_PREFIX = "HPM"


class Project:
    """一个 HPM 工程：.hpmpc 的内容、活动的 pinmux 函数和它们的引脚。
    One HPM project: the .hpmpc content, the active pinmux functions and their pins.
    """

    def __init__(self, root: Path, hpmpc: Path, main_c: Path) -> None:
        """读 .hpmpc 与根目录 main.c，记下 SoC、封装、活动的 pinmux 函数和它们的引脚。同一 pad
        被两个活动函数选中时记进 conflicts，信号以后一个为准。
        Read the .hpmpc and the root main.c, keeping the SoC, the package, the active pinmux
        functions and their pins. A pad that two active functions select is recorded in
        conflicts; the later signal wins.
        """
        self.root = root
        self.hpmpc = hpmpc
        document = json.loads(hpmpc.read_text(encoding="utf-8"))
        info = document["content"]["info"]
        self.soc = info.get("socName")
        self.package = info.get("packageName")
        functions = document["content"]["pinmux"]["functions"]
        # main.c 真正调用过的函数（O2）：没有时 parse 会提示，并退回 init_bsp_pins。
        # The functions main.c really calls (O2): when there are none, parse says so and
        # falls back to init_bsp_pins.
        self.called = pin_project.hpm_called_functions(main_c, functions)
        self.active = self.called or (["init_bsp_pins"] if "init_bsp_pins" in functions else [])
        self.pins: list[dict] = []
        self.conflicts: list[tuple[str, str, str, str, str]] = []
        owners: dict[str, tuple[str, str]] = {}
        for function in self.active:
            for pad, selection in functions[function].get("selectPins", {}).items():
                if not selection.get("signal"):
                    continue
                if pad in owners and owners[pad][0] != function:
                    self.conflicts.append(
                        (pad, owners[pad][0], owners[pad][1], function, selection["signal"])
                    )
                owners[pad] = (function, selection["signal"])
                entry = {
                    "pad": pad,
                    "signal": selection["signal"],
                    "padCtls": selection.get("padCtls", {}),
                    "gpiom": functions[function].get("managers", {}).get("gpiom", {}).get(pad, {}),
                }
                # 同一 pad 被再次选中时替换前面的记录（以后一个为准）。
                # A pad that is selected again replaces the earlier record (the later one
                # wins).
                for index, previous in enumerate(self.pins):
                    if previous["pad"] == pad:
                        self.pins[index] = entry
                        break
                else:
                    self.pins.append(entry)


def _pad_port_line(pad: str) -> tuple[str, int] | None:
    """物理引脚名（PA10）的端口字母和引脚号；不合式样时为 None。
    The port letter and the pin line of a pad name (PA10); None for a name that does not fit.
    """
    match = re.fullmatch(r"P([A-Z])(\d+)", pad)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def _gpio_name(pad: str) -> str | None:
    """GPIO 对象的默认名：p 加端口字母小写加引脚号（PA10 -> pa10，用户 2026-10-07 确认）。
    The default GPIO object name: p, the lower-case port letter and the pin line
    (PA10 -> pa10, confirmed by the user 2026-10-07).
    """
    port_line = _pad_port_line(pad)
    if port_line is None:
        return None
    port, line = port_line
    return f"p{port.lower()}{line:d}"


def _gpio_entry(pad: str, pad_ctls: dict, gpiom: dict) -> dict | None:
    """一个 GPIO 引脚的工程记录：端口字母、引脚号、方向（gpiom 写下的）和 pad 设置。
    The project record of one GPIO pin: the port letter, the pin line, the direction the
    gpiom manager wrote and the pad settings.
    """
    port_line = _pad_port_line(pad)
    if port_line is None:
        return None
    entry = {
        "Pad": pad,
        "Port": port_line[0],
        "Line": port_line[1],
        "PadCtls": pad_ctls or {},
    }
    direction = str(gpiom.get("direction") or "")
    if direction == "1":
        entry["Direction"] = "OUTPUT"
    elif direction == "0":
        entry["Direction"] = "INPUT"
    return entry


def parse_project(directory: str, output: str | None = None, summary: bool = True) -> None:
    """解析 directory 中的 HPM 工程并写出 YAML；summary 为真时打印摘要。
    Parse the HPM project in directory and write the YAML; print a summary when summary is
    set.

    输出路径默认为该目录下的 .config.yaml。调用方（libxr parse）已确认目录存在且是 HPM 工程。
    The output defaults to .config.yaml in that directory. The caller, libxr parse, has
    checked that the directory exists and is an HPM project.
    """
    root = Path(directory)
    hpmpcs = sorted(root.glob("boards/*/*.hpmpc"))
    if len(hpmpcs) != 1:
        names = ", ".join(path.as_posix() for path in hpmpcs) or tr("none found", "没有找到")
        logging.error(
            tr(
                f"{directory} must hold exactly one .hpmpc under boards/ ({names})",
                f"{directory} 的 boards/ 下必须有且只有一个 .hpmpc（{names}）",
            )
        )
        sys.exit(1)
    main_c = root / "main.c"
    if not main_c.is_file():
        logging.error(
            tr(
                f"{directory}: no root main.c; the HPM main sits at the project root",
                f"{directory}：根目录没有 main.c；HPM 的 main 在工程根目录",
            )
        )
        sys.exit(1)
    project = Project(root, hpmpcs[0], main_c)
    if project.soc is None:
        logging.error(
            tr(
                f"{hpmpcs[0]} does not name a SoC",
                f"{hpmpcs[0]} 没有给出 SoC",
            )
        )
        sys.exit(1)
    if not project.called:
        detail = (
            tr("using the init_bsp_pins fallback", "退回 init_bsp_pins")
            if project.active
            else tr(
                "no init_bsp_pins exists, so no pin is read", "没有 init_bsp_pins 可退，读不到引脚"
            )
        )
        logging.warning(
            tr(
                f"{main_c} calls no pinmux function ({detail}); a pin function called from "
                "elsewhere (board.c, a helper) is not visible",
                f"{main_c} 没有调用任何 pinmux 函数（{detail}）；从别处（board.c、辅助函数）"
                "调用的引脚函数看不到",
            )
        )
    for pad, first, first_signal, second, second_signal in project.conflicts:
        logging.warning(
            tr(
                f"{hpmpcs[0]}: {pad} is selected by both {first} ({first_signal}) and "
                f"{second} ({second_signal}); the later one wins",
                f"{hpmpcs[0]}：{pad} 同时被 {first}（{first_signal}）和 {second}"
                f"（{second_signal}）选中；以后一个为准",
            )
        )

    gpio: dict[str, dict] = {}
    peripherals: dict[str, dict] = {}
    other: dict[str, dict] = {}
    # GPTMR 只在 SoC 没有 PWM 外设时生成对象（HPM5301）；有 PWM 外设的（HPM5361）归入 Other
    # 只作展示。判定来自芯片引脚数据里有没有 PWM 信号。
    # A GPTMR generates objects only when the SoC has no PWM peripheral (HPM5301); on a SoC
    # with one (HPM5361) it lands in Other for display only. The decision comes from whether
    # the chip's pin data has any PWM signal.
    soc_has_pwm = pin_layout.hpm_soc_has_pwm(project.soc)
    for selection in project.pins:
        pad, signal, pad_ctls = selection["pad"], selection["signal"], selection["padCtls"]
        canonical = pin_project.hpm_canonical(signal)
        if canonical.startswith("GPIO_"):
            name = _gpio_name(pad)
            entry = _gpio_entry(pad, pad_ctls, selection["gpiom"])
            if name is None or entry is None:
                logging.warning(
                    tr(
                        f"{hpmpcs[0]}: {pad} does not look like a GPIO pad; skipped",
                        f"{hpmpcs[0]}：{pad} 不是 GPIO 引脚的写法；跳过",
                    )
                )
                continue
            if name in gpio:
                logging.warning(
                    tr(
                        f"{hpmpcs[0]}: two GPIO pins are named {name}; the later one wins",
                        f"{hpmpcs[0]}：两个 GPIO 引脚都叫 {name}；以后一个为准",
                    )
                )
            gpio[name] = entry
            continue
        recognized = pin_layout.recognize_hpm(canonical)
        instance, kind, function = recognized or (canonical, "OTHER", "PIN")
        if kind in GENERATED_KINDS and not (kind == "GPTMR" and soc_has_pwm):
            section = GENERATED_KINDS[kind]
            record = peripherals.setdefault(section, {}).setdefault(
                instance, {"Peripheral": instance, "Pins": {}}
            )
            if function.startswith("COMP"):
                # GPTMR 的比较器输出：序号是 PWM 通道（pwm_gptmr0_ch1）。
                # A GPTMR comparator output: its index is the PWM channel (pwm_gptmr0_ch1).
                index = int(re.fullmatch(r"COMP_(\d+)", function).group(1))
                channels = record.setdefault("Channels", [])
                if not any(channel["Index"] == index for channel in channels):
                    channels.append({"Index": index, "Pad": pad})
            elif function in record["Pins"] and record["Pins"][function] != pad:
                logging.warning(
                    tr(
                        f"{hpmpcs[0]}: {instance} {function} is assigned to both "
                        f"{record['Pins'][function]} and {pad}; the later one wins",
                        f"{hpmpcs[0]}：{instance} 的 {function} 同时分给了 "
                        f"{record['Pins'][function]} 和 {pad}；以后一个为准",
                    )
                )
            record["Pins"][function] = pad
        else:
            # LibXR 还没有驱动的信号（UART、SPI、CAN、ACMP……）只作展示。
            # Signals without a LibXR driver yet (UART, SPI, CAN, ACMP, ...) are display-only.
            record = other.setdefault(
                instance, {"Module": kind, "Peripheral": instance, "Pins": {}}
            )
            record["Pins"][function] = pad

    data = {
        "Platform": "hpm",
        "Hpmpc": hpmpcs[0].relative_to(root).as_posix(),
        "Mcu": {"Family": "HPM", "Type": project.soc},
        "Package": project.package,
        "MainFunctions": project.active,
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
