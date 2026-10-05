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
    found = data["sets"][data["parts"][part]]
    if package is not None and package.upper() != found["package"].upper():
        raise ValueError(
            tr(
                f"{model} comes in {found['package']}, not {package}; the package is part of "
                "the STM32 model",
                f"{model} 的封装是 {found['package']}，不是 {package}；STM32 的封装由型号决定",
            )
        )
    pins = [Pin(position, name, kind, signals) for position, name, kind, signals in found["pins"]]
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


# 各平台：名称、型号前缀和布局函数。新平台在此加一行。
# The platforms: name, model prefix and layout function. A new platform adds a line here.
PLATFORMS = (
    ("stm32", "STM32", layout_stm32),
    ("mspm0", "MSPM0", layout_mspm0),
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
    """
    return {
        "model": layout.model,
        "platform": layout.platform,
        "part": layout.part,
        "package": layout.package,
        "pin_count": len({pin.position for pin in layout.pins}),
        "source": layout.source,
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


def print_pin_layout(model: str, package: str | None, output_format: str) -> None:
    """以 YAML 或 JSON 向标准输出打印型号的引脚布局。
    Print the pin layout of a model as YAML or JSON to standard output.

    型号或封装无法识别时记录错误（写到标准错误）并以状态 1 退出；输出不会混入报错。
    A model or package that is not recognized logs an error, on standard error, and exits with
    status 1; the output never mixes with the error.
    """
    try:
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
