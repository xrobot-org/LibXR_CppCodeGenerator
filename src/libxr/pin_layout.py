"""libxr pins：由型号给出芯片的封装和引脚布局。
libxr pins: the package and pin layout of a chip, from its model.

每个引脚给出封装上的位置、名称、类型和全部可选信号。数据来自厂商的引脚数据（STM32：ST 的
STM32_open_pin_data；MSPM0：TI SysConfig 的器件数据），由 scripts/build_pin_data.py 生成并放在
pin_data/ 中，与许可证文本一起分发。
Each pin gives its position on the package, its name, its type and all its selectable signals.
The data comes from the vendors' pin data (STM32: ST's STM32_open_pin_data; MSPM0: the device
data of TI SysConfig); scripts/build_pin_data.py writes it to pin_data/, which is distributed
with the license texts.
"""

import gzip
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml
from xr_syntax.i18n import tr

DATA = Path(__file__).with_name("pin_data")


@dataclass
class Pin:
    """封装上的一个引脚：位置、名称、类型、可选信号，以及平台给出的其他字段。
    One pin of a package: position, name, type, selectable signals, and the other fields a
    platform gives.
    """

    position: str
    name: str
    type: str
    signals: list[str]
    extra: dict = field(default_factory=dict)


@dataclass
class PinLayout:
    """一个型号的引脚布局：平台、型号、数据中的器件名、封装、数据来源和全部引脚。
    The pin layout of a model: platform, model, the part name in the data, package, data source
    and all pins.
    """

    platform: str
    model: str
    part: str
    package: str
    source: dict
    pins: list[Pin]


@cache
def load_data(platform: str) -> dict:
    """读取并缓存一个平台的数据文件。
    Read and cache the data file of a platform.
    """
    with gzip.open(DATA / f"{platform}.json.gz", "rt", encoding="utf-8") as file:
        return json.load(file)


@cache
def load_stm32_shard(shard: str) -> dict:
    """读取并缓存 STM32 的一个数据分片（一个系列的引脚集）。只读用到的分片，命令因此启动得快，
    占用的内存也小。
    Read and cache one data shard of the STM32 (the pin sets of a series). Only the shard in use
    is read, which keeps the command quick to start and its memory small.
    """
    with gzip.open(DATA / f"stm32-{shard}.json.gz", "rt", encoding="utf-8") as file:
        return json.load(file)


def stm32_candidates(model: str) -> list[str]:
    """型号在 ST 数据中可能的写法：原样，以及把温度等级数字换成 X（STM32H723VGT6 为
    STM32H723VGTX）。
    The ways a model may be written in ST's data: as it is, and with the temperature digit
    replaced by X (STM32H723VGTX for STM32H723VGT6).
    """
    candidates = [model]
    match = re.fullmatch(r"(.*?[A-Z])[0-9]([A-Z]*)", model)
    if match:
        candidates.append(f"{match.group(1)}X{match.group(2)}")
    return candidates


def layout_stm32(model: str, package: str | None) -> PinLayout:
    """STM32 型号的布局。封装包含在型号中，给出的 package 必须与之相同。
    The layout of an STM32 model. The package is part of the model; a given package must be the
    same.
    """
    data = load_data("stm32")
    by_upper = {name.upper(): name for name in data["parts"]}
    for candidate in stm32_candidates(model):
        if candidate in by_upper:
            part = by_upper[candidate]
            break
    else:
        near = sorted(name for name in by_upper.values() if name.upper().startswith(model[:10]))
        hint = tr(f"; similar: {', '.join(near[:5])}", f"；相近的有：{'、'.join(near[:5])}")
        raise ValueError(
            tr(f"Unknown STM32 model: {model}", f"未知的 STM32 型号：{model}")
            + (hint if near else "")
        )
    shard, key = data["parts"][part]
    found = load_stm32_shard(shard)[key]
    if package is not None and package.upper() != found["package"].upper():
        raise ValueError(
            tr(
                f"{model} comes in {found['package']}, not {package}; the package is part of "
                "the STM32 model",
                f"{model} 的封装是 {found['package']}，不是 {package}；STM32 的封装由型号决定",
            )
        )
    pins = [
        Pin(position, name, kind, signals, {"gpio_modes": modes} if modes else {})
        for position, name, kind, signals, modes in found["pins"]
    ]
    return PinLayout("stm32", model, part, found["package"], data["source"], pins)


def mspm0_family(model: str, data: dict) -> tuple[str, dict, str]:
    """model 所属的器件族：名称、数据，以及型号中器件名之后的部分（含封装代码）。取最长的前缀。
    The family a model belongs to: its name, its data, and the part of the model after the
    device name (which holds the package code). The longest prefix wins.
    """
    best = None
    for name, family in data["families"].items():
        for prefix in family["prefixes"]:
            if model.startswith(prefix) and (best is None or len(prefix) > len(best[2])):
                best = (name, family, prefix)
    if best is None:
        raise ValueError(tr(f"Unknown MSPM0 model: {model}", f"未知的 MSPM0 型号：{model}"))
    name, family, prefix = best
    # 器件族名以 X 结尾时，前缀之后还有一位型号数字。
    # A family name ending in X leaves one more digit of the model after the prefix.
    wildcard = name.endswith("X")
    return name, family, model[len(prefix) + (1 if wildcard else 0) :]


def code_of(name: str) -> str:
    """MSPM0 封装名括号里的代码：LQFP-64(PM) 为 PM，没有括号时为空。
    The code in parentheses of an MSPM0 package name: PM for LQFP-64(PM), empty without
    parentheses.
    """
    match = re.search(r"\(([^)]+)\)$", name)
    return match.group(1) if match else ""


def mspm0_package(model: str, family: dict, rest: str, package: str | None) -> str:
    """MSPM0 的封装名：先看 package（封装名、括号中的代码或不含代码的名称），没有时在型号的
    后缀中找封装代码。
    The package name of an MSPM0: package first (the name, the code in parentheses, or the name
    without the code), otherwise the package code in the suffix of the model.
    """
    names = list(family["packages"])
    if package is not None:
        wanted = package.upper()
        for name in names:
            if wanted in (name.upper(), code_of(name).upper(), name.split("(")[0].upper()):
                return name
        raise ValueError(
            tr(
                f"{model} has no package {package}; this family has {', '.join(names)}",
                f"{model} 没有封装 {package}；该器件族有 {'、'.join(names)}",
            )
        )
    codes = [name for name in names if code_of(name) and code_of(name).upper() in rest]
    if not codes:
        raise ValueError(
            tr(
                f"Cannot tell the package of {model}; give it with --package ({', '.join(names)})",
                f"无法从 {model} 判断封装；请用 --package 给出（{'、'.join(names)}）",
            )
        )
    return max(codes, key=lambda name: len(code_of(name)))


def layout_mspm0(model: str, package: str | None) -> PinLayout:
    """MSPM0 型号的布局。封装来自 package，或来自型号后缀中的封装代码（MSPM0G3507SPMR 的 PM）。
    The layout of an MSPM0 model. The package comes from package, or from the package code in
    the model suffix (PM of MSPM0G3507SPMR).
    """
    data = load_data("mspm0")
    _, family, rest = mspm0_family(model, data)
    chosen = mspm0_package(model, family, rest, package)
    pins = [
        Pin(
            position,
            name,
            kind,
            [signal for signal, _ in signals],
            {"iomux_pincm": pincm, "modes": {signal: mode for signal, mode in signals}},
        )
        for position, name, kind, pincm, signals in family["packages"][chosen]
    ]
    part = re.match(r"MSPM0[A-Z]\d{4}", model)
    return PinLayout("mspm0", model, part.group(0) if part else model, chosen, data["source"], pins)


def layout_hpm(model: str, package: str | None) -> PinLayout:
    """HPM SoC 的布局：型号即 SoC 名（HPM5301），封装名与数据一致（QFN48）。
    The layout of an HPM SoC: the model is the SoC name (HPM5301), and the package name matches
    the data (QFN48).
    """
    data = load_data("hpm")
    socs = data["socs"]
    soc = socs.get(model)
    if soc is None:
        raise ValueError(
            tr(
                f"Unknown HPM SoC: {model}; this data has {', '.join(socs)}",
                f"未知的 HPM SoC：{model}；数据里有 {'、'.join(socs)}",
            )
        )
    packages = soc["packages"]
    chosen = None
    if package is not None:
        chosen = next((name for name in packages if package.upper() == name.upper()), None)
        if chosen is None:
            raise ValueError(
                tr(
                    f"{model} has no package {package}; this SoC has {', '.join(packages)}",
                    f"{model} 没有封装 {package}；该 SoC 有 {'、'.join(packages)}",
                )
            )
    elif len(packages) == 1:
        chosen = next(iter(packages))
    if chosen is None:
        raise ValueError(
            tr(
                f"Cannot tell the package of {model}; give it with --package"
                f" ({', '.join(packages)})",
                f"无法从 {model} 判断封装；请用 --package 给出（{'、'.join(packages)}）",
            )
        )
    pins = [
        Pin(
            position,
            name,
            kind,
            [signal for signal, _ in signals],
            {"modes": {signal: mode for signal, mode in signals}},
        )
        for position, name, kind, _, signals in packages[chosen]
    ]
    return PinLayout("hpm", model, model, chosen, data["source"], pins)


def recognize_stm32(signal: str) -> tuple[str, str, str] | None:
    """识别 ST 的信号名：返回外设实例、外设类型和功能，不是外设信号（GPIO）时返回 None。
    Recognize an ST signal name: return the peripheral instance, the peripheral kind and the
    function, or None when the signal is not a peripheral's (GPIO).

    名字是 <实例>_<功能>：USART1_TX 为 USART1、USART、TX。类型是实例去掉末尾的数字（I2C2 为
    I2C）。USB_OTG_HS_DM、USB_DRD_FS_DP 和 OCTOSPIM_P1_IO2 的实例有多段，单独处理。识别只看
    名字，不依赖 LibXR 是否有对应的外设。
    A name is <instance>_<function>: USART1_TX is USART1, USART, TX. The kind is the instance
    without its trailing digits (I2C for I2C2). The instances of USB_OTG_HS_DM, USB_DRD_FS_DP and
    OCTOSPIM_P1_IO2 have several parts and are handled separately. Recognition looks at the name
    only, whether or not LibXR has a matching peripheral.
    """
    match = re.fullmatch(r"(USB_OTG|USB_DRD)_([A-Z]+)_(.+)", signal)
    if match:
        return f"{match.group(1)}_{match.group(2)}", match.group(1), match.group(3)
    match = re.fullmatch(r"(OCTOSPIM)_(P\d+)_(.+)", signal)
    if match:
        return f"{match.group(1)}_{match.group(2)}", match.group(1), match.group(3)
    match = re.fullmatch(r"([A-Za-z0-9]+?)_(.+)", signal)
    if match is not None:
        return match.group(1), re.sub(r"\d+$", "", match.group(1)), match.group(2)
    # 没有下划线的信号（CEC、BOOT0、AUDIOCLK）自己就是实例和功能；GPIO 只是引脚的普通用法。
    # A signal without an underscore (CEC, BOOT0, AUDIOCLK) is its own instance and function;
    # GPIO is only the plain use of the pin.
    if signal == "GPIO":
        return None
    return signal, re.sub(r"\d+$", "", signal), signal


def recognize_mspm0(signal: str) -> tuple[str, str, str] | None:
    """识别 TI 的信号名：<实例>.<功能>，UART0.TX 为 UART0、UART、TX；引脚自己的名字（PA0）
    不是外设信号，返回 None。
    Recognize a TI signal name, <instance>.<function>: UART0.TX is UART0, UART, TX; the name of
    the pin itself (PA0) is not a peripheral's, so None.
    """
    match = re.fullmatch(r"([A-Za-z0-9]+)\.(.+)", signal)
    if match is None:
        return None
    return match.group(1), re.sub(r"\d+$", "", match.group(1)), match.group(2)


def recognize_hpm(signal: str) -> tuple[str, str, str] | None:
    """识别 HPM 的信号名（hpm_iomux.h 的宏名）：UART0_TXD 为 UART0、UART、TXD；GPIO 的复用
    （GPIO_A_00）是引脚自己的普通用法，返回 None。
    Recognize an HPM signal name (the macro name of hpm_iomux.h): UART0_TXD is UART0, UART, TXD;
    the GPIO mux (GPIO_A_00) is the pin's own plain use, so None.
    """
    if signal.startswith("GPIO_"):
        return None
    match = re.fullmatch(r"([A-Za-z0-9]+)_(.+)", signal)
    if match is None:
        return None
    return match.group(1), re.sub(r"\d+$", "", match.group(1)), match.group(2)


@cache
def hpm_soc_has_pwm(model: str) -> bool:
    """SoC 的引脚数据里有没有 PWM 外设的信号（HPM5361 有 20 个，HPM5301 没有）：有的 SoC 上
    GPTMR 只作展示，没有的才生成 HPMPWM 的 fallback 对象。
    Whether the pin data of the SoC has PWM peripheral signals (HPM5361 has 20, HPM5301 has
    none): on a SoC with them a GPTMR is display-only, and only a SoC without them generates
    the HPMPWM fallback objects.
    """
    soc = load_data("hpm")["socs"].get(model)
    if soc is None:
        return False
    for package in soc["packages"].values():
        for _position, _name, _kind, _x, signals in package:
            for signal, _mode in signals:
                recognized = recognize_hpm(signal)
                if recognized is not None and recognized[1] == "PWM":
                    return True
    return False


# 各平台识别信号名的函数。
# The function of each platform that recognizes a signal name.
RECOGNIZERS = {"stm32": recognize_stm32, "mspm0": recognize_mspm0, "hpm": recognize_hpm}


def natural_key(name: str) -> list:
    """自然排序的键：USART2 排在 USART10 之前。
    A natural sort key: USART2 sorts before USART10.
    """
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


# 定时器的哪些功能是可以输出 PWM 的通道：类型 -> 功能的正则。
# Which functions of a timer are channels that can output PWM: kind -> regex of the function.
PWM_CHANNELS = {
    "TIM": r"CH\d+N?",
    "LPTIM": r"OUT|CH\d+",
    "HRTIM": r"CH[A-F]\d",
    "TIMA": r"CCP\d+(_CMPL)?",
    "TIMG": r"CCP\d+(_CMPL)?",
}


def gpio_port_and_line(pin_name: str) -> tuple[str, str] | None:
    """引脚名的端口字母和线号：PA9 为 (A, 9)，PC2_C 为 (C, 2)；不是 P<字母><数字> 形式时为 None。
    The port letter and the line number of a pin name: (A, 9) for PA9, (C, 2) for PC2_C; None
    when the name is not of the form P<letter><digits>.
    """
    match = re.match(r"P([A-Z])(\d+)", pin_name)
    return (match.group(1), match.group(2)) if match else None


def pin_has_gpio(pin: Pin) -> bool:
    """引脚能当普通 GPIO：ST 的信号里有 GPIO；TI 的信号里有引脚自己的名字（PA0）。
    Whether the pin can be a plain GPIO: ST lists GPIO among the signals, TI lists the name of
    the pin itself (PA0).
    """
    return "GPIO" in pin.signals or pin.name in pin.signals


def peripheral_index(layout: PinLayout) -> dict:
    """布局中识别出的全部外设：实例名 -> 类型和各功能可选的引脚。
    All the peripherals recognized in the layout: instance name -> kind and, per function, the
    pins that can carry it.

    除信号名中的外设外，还有：GPIOA、GPIOB……（普通 GPIO，每条线 P0、P1……）；EXTI（外部中断，
    线 LINE0……可选的引脚取自 ST 数据中 GPIO 的 EXTI 模式）；可输出 PWM 的定时器带
    capabilities: [pwm]。
    Besides the peripherals in the signal names there are GPIOA, GPIOB, ... (plain GPIO, one
    function P0, P1, ... per line); EXTI (external interrupts, lines LINE0, ..., the pins taken
    from the EXTI mode of the GPIO in ST's data); and a timer that can output PWM carries
    capabilities: [pwm].
    """
    recognize = RECOGNIZERS[layout.platform]
    found: dict[str, dict] = {}

    def add(instance: str, kind: str, function: str, pin: Pin) -> None:
        """把 pin 记为外设 instance 的 function 可选的引脚。
        Record pin as one that can carry the function of the peripheral instance.
        """
        entry = found.setdefault(instance, {"kind": kind, "signals": {}})
        entry["signals"].setdefault(function, []).append(pin.name)

    for pin in layout.pins:
        for signal in pin.signals:
            recognized = recognize(signal)
            if recognized is not None:
                add(*recognized, pin)
        port_line = gpio_port_and_line(pin.name)
        if port_line is not None and pin_has_gpio(pin):
            port, line = port_line
            add(f"GPIO{port}", "GPIO", f"P{line}", pin)
            if any(mode.startswith("EXTI") for mode in pin.extra.get("gpio_modes", [])):
                add("EXTI", "EXTI", f"LINE{line}", pin)
    index = {}
    for instance in sorted(found, key=natural_key):
        entry = found[instance]
        index[instance] = {
            "kind": entry["kind"],
            "signals": dict(sorted(entry["signals"].items(), key=lambda s: natural_key(s[0]))),
        }
        pattern = PWM_CHANNELS.get(entry["kind"])
        if pattern and any(re.fullmatch(pattern, function) for function in entry["signals"]):
            index[instance]["capabilities"] = ["pwm"]
    return index


# 各平台：名称、型号前缀和布局函数。新平台在此加一行。
# The platforms: name, model prefix and layout function. A new platform adds a line here.
PLATFORMS = (
    ("stm32", "STM32", layout_stm32),
    ("mspm0", "MSPM0", layout_mspm0),
    ("hpm", "HPM", layout_hpm),
)


def layout_pins(model: str, package: str | None = None) -> PinLayout:
    """由型号的前缀选平台，返回该型号的引脚布局；型号或封装无法识别时抛出 ValueError。
    Choose the platform by the prefix of the model and return the pin layout of the model; a
    model or package that is not recognized raises ValueError.
    """
    model = model.strip().upper()
    for _, prefix, layout in PLATFORMS:
        if model.startswith(prefix):
            return layout(model, package)
    raise ValueError(
        tr(
            f"No supported platform for the model {model} (supported: "
            f"{', '.join(prefix for _, prefix, _ in PLATFORMS)})",
            f"型号 {model} 不属于任何支持的平台（支持：{'、'.join(p for _, p, _ in PLATFORMS)}）",
        )
    )


def layout_to_dict(layout: PinLayout) -> dict:
    """布局的字典形式，用于输出；各平台的其他字段与 position、name、type、signals 并列。
    The layout as a dict for output; the other fields of a platform sit next to position, name,
    type and signals.

    pin_count 是封装上的位置数。一个位置可以有多个条目：STM32G0 的引脚可以重映射，PA9 和 PA11
    共用一个位置，名称写成 PA11 [PA9] 和 PA9 [PA11]。
    pin_count is the number of positions on the package. A position can have several entries:
    the pins of an STM32G0 can be remapped, so PA9 and PA11 share one position, written
    PA11 [PA9] and PA9 [PA11].

    peripherals 列出识别出的全部外设（USART1、ETH、FMC、GPIOA、EXTI……），每个功能可选的引脚
    都在其中；它与 LibXR 是否有对应的外设无关。
    peripherals lists every peripheral recognized (USART1, ETH, FMC, GPIOA, EXTI, ...) with the
    pins that can carry each function, whether or not LibXR has a matching peripheral.
    """
    return {
        "model": layout.model,
        "platform": layout.platform,
        "part": layout.part,
        "package": layout.package,
        "pin_count": len({pin.position for pin in layout.pins}),
        "source": layout.source,
        "peripherals": peripheral_index(layout),
        "pins": [
            {
                "position": pin.position,
                "name": pin.name,
                "type": pin.type,
                "signals": pin.signals,
                **pin.extra,
            }
            for pin in layout.pins
        ],
    }


def print_pin_layout(
    model: str | None,
    package: str | None,
    output_format: str,
    directory: str | None = None,
    libxr_config: str | None = None,
) -> None:
    """以 YAML 或 JSON 向标准输出打印型号的引脚布局；给出 directory 时叠加工程已选的信号。
    Print the pin layout of a model as YAML or JSON to standard output; with directory, the
    signals the project has selected are overlaid.

    型号、封装或工程无法识别时记录错误（写到标准错误）并以状态 1 退出；输出不会混入报错。
    A model, package or project that is not recognized logs an error, on standard error, and
    exits with status 1; the output never mixes with the error.
    """
    try:
        if directory is not None:
            from libxr.pin_project import layout_with_project

            info = layout_with_project(directory, model, package, libxr_config)
        elif model is None:
            raise ValueError(
                tr(
                    "Give a chip model, or a project directory with -d",
                    "请给出芯片型号，或用 -d 给出工程目录",
                )
            )
        else:
            info = layout_to_dict(layout_pins(model, package))
    except ValueError as error:
        logging.error(str(error))
        sys.exit(1)
    if output_format == "json":
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(
            yaml.safe_dump(
                info, sort_keys=False, allow_unicode=True, default_flow_style=False
            ).rstrip()
        )
