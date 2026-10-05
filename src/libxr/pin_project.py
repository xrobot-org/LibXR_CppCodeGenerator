"""libxr pins -d：把工程里已选的引脚信号叠加到布局上，并对应到 libxr_config.yaml 的外设设置。
libxr pins -d: overlay the pin signals a project has selected on the layout, and tie them to the
peripheral settings of libxr_config.yaml.

STM32 的已选信号来自 CubeMX 的 .ioc（Pxn.Signal），MSPM0 的来自 SysConfig 生成的
ti_msp_dl_config.h（IOMUX_PINCMn_PF_ 宏）。信号按布局中该引脚的可选信号核对；核对不上的原样
给出，matched 为 false。
The selected signals of an STM32 come from the CubeMX .ioc (Pxn.Signal), those of an MSPM0 from
the ti_msp_dl_config.h SysConfig generates (the IOMUX_PINCMn_PF_ macros). A signal is checked
against the selectable signals of its pin in the layout; one that does not match is given as it
is, with matched false.
"""

import re
from pathlib import Path

import yaml
from xr_syntax.i18n import tr

from libxr import pin_layout
from libxr.pin_layout import Pin, PinLayout

# 外设类型到 libxr_config.yaml 的段：libxr gen 为这些类型生成对象。
# Peripheral kind to the section of libxr_config.yaml: libxr gen generates objects for these.
STM32_SECTIONS = {
    "USART": "USART",
    "UART": "USART",
    "SPI": "SPI",
    "I2C": "I2C",
    "ADC": "ADC",
    "DAC": "DAC",
    "TIM": "TIM",
    "CAN": "CAN",
    "FDCAN": "FDCAN",
    "USB_OTG": "USB",
    "USB_DRD": "USB",
}
# USB 实例在 libxr_config.yaml 中的键；USB_DRD_FS 和 USB 都叫 usb_fs。
# The key of a USB instance in libxr_config.yaml; USB_DRD_FS and USB are both usb_fs.
USB_KEYS = {"USB_DRD_FS": "usb_fs", "USB": "usb_fs"}


def read_ioc(path: Path) -> tuple[str, dict[str, dict], set[str]]:
    """读取 .ioc：返回芯片型号（Mcu.UserName）、引脚键 -> {signal, label} 和启用的外设（Mcu.IPn）。
    Read an .ioc: return the chip model (Mcu.UserName), pin key -> {signal, label} and the
    enabled peripherals (Mcu.IPn).

    引脚键可以带后缀，如 PH0-OSC_IN\\ (PH0)；转义的空格还原为空格。
    A pin key may carry a suffix such as PH0-OSC_IN\\ (PH0); an escaped space is restored.
    """
    model = ""
    pins: dict[str, dict] = {}
    enabled: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^(P[A-Z]\d+[^=]*?)\.(Signal|GPIO_Label)=(.*)$", line)
        if match:
            key = match.group(1).replace("\\ ", " ")
            field = "signal" if match.group(2) == "Signal" else "label"
            pins.setdefault(key, {})[field] = match.group(3).strip()
        elif line.startswith("Mcu.UserName="):
            model = line.split("=", 1)[1].strip()
        elif re.match(r"Mcu\.IP\d+=", line):
            enabled.add(line.split("=", 1)[1].strip())
    return model, {key: value for key, value in pins.items() if "signal" in value}, enabled


def find_pin(layout: PinLayout, key: str) -> Pin | None:
    """布局中与 .ioc 引脚键对应的引脚：先按名字，再按 P<字母><数字> 开头（PH0-OSC_IN (PH0)）。
    The pin of the layout that an .ioc pin key stands for: by name first, then by the leading
    P<letter><digits> (PH0-OSC_IN (PH0)).
    """
    for pin in layout.pins:
        if pin.name == key:
            return pin
    base = re.match(r"P[A-Z]\d+", key)
    if base is None:
        return None
    for pin in layout.pins:
        if re.match(rf"{base.group(0)}(?!\d)", pin.name):
            return pin
    return None


def resolve_stm32(pin: Pin, signal: str, enabled: set[str]) -> dict:
    """一个 .ioc 信号对应的外设、类型和功能。
    The peripheral, kind and function an .ioc signal stands for.

    CubeMX 在 .ioc 里有几种写法与 ST 的信号名不同：GPIO_Output、GPIO_Input 和 GPXTIn（GPIO、
    外部中断）、S_TIM2_CH1_ETR（共享引脚）、ADCx_INP19（不带实例的 ADC 通道）、FMC_D2_DA2（
    共用前缀的两个功能）、ETH_TXD1（省掉 MII 或 RMII）、SharedAnalog_PA4 和 COMP_DAC11_group
    （模拟）。
    CubeMX writes several forms in the .ioc that differ from ST's signal names: GPIO_Output,
    GPIO_Input and GPXTIn (GPIO, external interrupt), S_TIM2_CH1_ETR (shared pin), ADCx_INP19 (an
    ADC channel without its instance), FMC_D2_DA2 (two functions that share a prefix), ETH_TXD1
    (MII or RMII left out), SharedAnalog_PA4 and COMP_DAC11_group (analog).
    """
    port_line = pin_layout.gpio_port_and_line(pin.name)
    gpio = f"GPIO{port_line[0]}" if port_line else "GPIO"
    function = f"P{port_line[1]}" if port_line else "GPIO"
    if re.fullmatch(r"GPIO_(Input|Output|Analog)", signal):
        return {"peripheral": gpio, "kind": "GPIO", "function": function, "matched": True}
    external = re.fullmatch(r"GPXTI(\d+)", signal)
    if external:
        return {
            "peripheral": "EXTI",
            "kind": "EXTI",
            "function": f"LINE{external.group(1)}",
            "matched": True,
        }
    if signal.startswith(("SharedAnalog_", "COMP_DAC")):
        return {"peripheral": "ANALOG", "kind": "ANALOG", "function": signal, "matched": True}
    vendor = set(pin.signals)
    inner = signal[2:] if signal.startswith("S_") else signal
    shared = re.fullmatch(r"([A-Z]+)(\d*)(DFSDM\d)", inner) if signal.startswith("S_") else None
    if shared:
        # S_DATAIN1DFSDM1 是 DFSDM1_DATIN1。
        # S_DATAIN1DFSDM1 is DFSDM1_DATIN1.
        inner = f"{shared.group(3)}_{shared.group(1).replace('DATAIN', 'DATIN')}{shared.group(2)}"
    adc = re.fullmatch(r"ADCx_(.+)", inner)
    if adc:
        instances = sorted(
            {
                pin_layout.recognize_stm32(s)[0]
                for s in vendor
                if re.fullmatch(rf"ADC\d*_{re.escape(adc.group(1))}", s)
            },
            key=pin_layout.natural_key,
        )
        # 有多个 ADC 可选时，取工程里启用的那个。
        # With several ADCs to choose from, take the one the project enables.
        if len(instances) > 1 and len([i for i in instances if i in enabled]) == 1:
            instances = [i for i in instances if i in enabled]
        found = {
            "peripheral": "ADC",
            "kind": "ADC",
            "function": adc.group(1),
            "matched": bool(instances),
        }
        if len(instances) == 1:
            found["peripheral"] = instances[0]
        elif instances:
            found["candidates"] = instances
        return found
    if inner in vendor:
        peripheral, kind, function = pin_layout.recognize_stm32(inner)
        return {"peripheral": peripheral, "kind": kind, "function": function, "matched": True}
    for known in sorted(vendor, key=len, reverse=True):
        # FMC_D2_DA2：前一个信号 FMC_D2 加上共用前缀的 FMC_DA2。
        # FMC_D2_DA2: the signal FMC_D2 plus FMC_DA2, which shares its prefix.
        prefix = inner.split("_")[0] + "_"
        if inner.startswith(known + "_") and prefix + inner[len(known) + 1 :] in vendor:
            first = pin_layout.recognize_stm32(known)
            second = pin_layout.recognize_stm32(prefix + inner[len(known) + 1 :])
            return {
                "peripheral": first[0],
                "kind": first[1],
                "function": f"{first[2]}/{second[2]}",
                "matched": True,
            }
    for known in vendor:
        # ETH_TXD1：ST 写成 ETH_MII_TXD1 或 ETH_RMII_TXD1。
        # ETH_TXD1: ST writes ETH_MII_TXD1 or ETH_RMII_TXD1.
        if known.replace("_RMII_", "_", 1).replace("_MII_", "_", 1) == inner and inner.startswith(
            "ETH_"
        ):
            peripheral, kind, _ = pin_layout.recognize_stm32(known)
            return {
                "peripheral": peripheral,
                "kind": kind,
                "function": inner.split("_", 1)[1],
                "matched": True,
            }
    recognized = pin_layout.recognize_stm32(inner)
    if recognized is None:
        return {"peripheral": gpio, "kind": "GPIO", "function": function, "matched": False}
    return {
        "peripheral": recognized[0],
        "kind": recognized[1],
        "function": recognized[2],
        "matched": False,
    }


def stm32_assignments(directory: Path, layout_for) -> tuple[str, str, PinLayout, dict[str, dict]]:
    """工程里 STM32 的已选信号：型号、来源文件、布局和 引脚名 -> 信号及其识别结果。
    The selected signals of an STM32 in a project: the model, the source file, the layout and
    pin name -> signal and what it was recognized as.

    layout_for 由型号给出布局，使调用者可以自行决定封装。
    layout_for gives the layout of a model, so the caller decides the package.
    """
    iocs = sorted(directory.glob("*.ioc"))
    model, signals, enabled = read_ioc(iocs[0])
    layout = layout_for(model)
    assigned: dict[str, dict] = {}
    for key, value in signals.items():
        pin = find_pin(layout, key)
        if pin is None:
            continue
        entry = {"signal": value["signal"], **resolve_stm32(pin, value["signal"], enabled)}
        if "label" in value:
            entry["label"] = value["label"]
        assigned[pin.name] = entry
    return model, iocs[0].name, layout, assigned


def find_ti_header(directory: Path) -> Path | None:
    """工程里 SysConfig 生成的 ti_msp_dl_config.h：先看根目录和 sysconfig/，再找三层以内。
    The ti_msp_dl_config.h SysConfig generated in a project: the root and sysconfig/ first, then
    up to three levels down.
    """
    for candidate in (
        directory / "ti_msp_dl_config.h",
        directory / "sysconfig" / "ti_msp_dl_config.h",
    ):
        if candidate.is_file():
            return candidate
    for path in sorted(directory.glob("*/*/ti_msp_dl_config.h")) + sorted(
        directory.glob("*/*/*/ti_msp_dl_config.h")
    ):
        if not {"build", "cmake-build"} & set(path.parts):
            return path
    return None


def mspm0_model(header: Path) -> str:
    """ti_msp_dl_config.h 中的器件名：#define CONFIG_MSPM0G3507。
    The device name in ti_msp_dl_config.h: #define CONFIG_MSPM0G3507.
    """
    match = re.search(
        r"^#define\s+CONFIG_(MSPM0[A-Z]\d{4})\s*$", header.read_text(encoding="utf-8"), re.M
    )
    if match is None:
        raise ValueError(
            tr(
                f"{header} does not name the MSPM0 device (no CONFIG_MSPM0... line)",
                f"{header} 中没有器件名（没有 CONFIG_MSPM0... 行）",
            )
        )
    return match.group(1)


def mspm0_assignments(header: Path, layout: PinLayout) -> dict[str, dict]:
    """ti_msp_dl_config.h 中 MSPM0 的已选信号：引脚名 -> 信号及其识别结果。
    The selected signals of an MSPM0 in ti_msp_dl_config.h: pin name -> signal and what it was
    recognized as.

    外设引脚是 IOMUX_PINCMn_PF_<外设>_<功能> 宏；普通 GPIO 是 <名字>_IOMUX (IOMUX_PINCMn)，
    名字作为标签。
    A peripheral pin is an IOMUX_PINCMn_PF_<peripheral>_<function> macro; a plain GPIO is
    <name>_IOMUX (IOMUX_PINCMn), with the name as its label.
    """
    text = header.read_text(encoding="utf-8")
    by_pincm = {
        pin.extra["iomux_pincm"]: pin for pin in layout.pins if pin.extra.get("iomux_pincm")
    }
    assigned: dict[str, dict] = {}
    for match in re.finditer(r"^#define\s+\w+_FUNC\s+IOMUX_PINCM(\d+)_PF_(\w+)\s*$", text, re.M):
        pin = by_pincm.get(int(match.group(1)))
        if pin is None or "_" not in match.group(2):
            continue
        instance, function = match.group(2).split("_", 1)
        signal = f"{instance}.{function}"
        kind = re.sub(r"\d+$", "", instance)
        assigned[pin.name] = {
            "signal": signal,
            "peripheral": instance,
            "kind": kind,
            "function": function,
            "matched": signal in pin.signals,
        }
    for match in re.finditer(r"^#define\s+(\w+?)_IOMUX\s+\(IOMUX_PINCM(\d+)\)\s*$", text, re.M):
        pin = by_pincm.get(int(match.group(2)))
        if pin is None or pin.name in assigned:
            continue
        port_line = pin_layout.gpio_port_and_line(pin.name)
        assigned[pin.name] = {
            "signal": pin.name,
            "peripheral": f"GPIO{port_line[0]}" if port_line else "GPIO",
            "kind": "GPIO",
            "function": f"P{port_line[1]}" if port_line else "GPIO",
            "matched": pin.name in pin.signals,
            "label": match.group(1),
        }
    return assigned


def config_key(platform: str, entry: dict) -> tuple[str, str] | None:
    """一个已选外设在 libxr_config.yaml 中的段和键；libxr gen 不生成它时为 None。
    The section and key of a selected peripheral in libxr_config.yaml; None when libxr gen does
    not generate it.
    """
    if platform != "stm32":
        return None
    section = STM32_SECTIONS.get(entry["kind"])
    if section is None:
        return None
    instance = entry["peripheral"]
    return section, USB_KEYS.get(instance, instance.lower())


def project_overlay(layout: PinLayout, assigned: dict[str, dict], config_path: Path | None) -> dict:
    """工程叠加结果：每个引脚的已选信号，以及每个已选外设用到的引脚和它在 libxr_config.yaml
    中的设置。
    The project overlay: the selected signal of each pin, and for each selected peripheral the
    pins it uses and its settings in libxr_config.yaml.
    """
    settings = {}
    if config_path is not None and config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        settings = loaded if isinstance(loaded, dict) else {}
    peripherals: dict[str, dict] = {}
    for pin_name, entry in assigned.items():
        used = peripherals.setdefault(entry["peripheral"], {"kind": entry["kind"], "pins": {}})
        used["pins"][entry["function"]] = pin_name
    for instance, used in peripherals.items():
        key = config_key(layout.platform, {"peripheral": instance, "kind": used["kind"]})
        if key is None:
            continue
        section, name = key
        params = (
            settings.get(section, {}).get(name) if isinstance(settings.get(section), dict) else None
        )
        used["config"] = {"section": section, "key": name, "present": params is not None}
        if params is not None:
            used["config"]["params"] = params
    return {
        "assignments": dict(
            sorted(assigned.items(), key=lambda item: pin_layout.natural_key(item[0]))
        ),
        "peripherals": dict(
            sorted(peripherals.items(), key=lambda item: pin_layout.natural_key(item[0]))
        ),
    }


def layout_with_project(
    directory: str, model: str | None, package: str | None, libxr_config: str | None
) -> dict:
    """工程目录中的布局和叠加：识别平台，读出已选信号，给出带 project 段的布局字典。
    The layout and overlay of a project directory: recognize the platform, read the selected
    signals and return the layout dict with a project section.

    给出 model 时以它为准，否则取自工程（STM32 的 .ioc，MSPM0 的 ti_msp_dl_config.h）。
    libxr_config 默认为目录下的 User/libxr_config.yaml。
    A given model wins; otherwise it comes from the project (the .ioc of an STM32, the
    ti_msp_dl_config.h of an MSPM0). libxr_config defaults to User/libxr_config.yaml in the
    directory.
    """
    root = Path(directory)
    config_path = Path(libxr_config) if libxr_config else root / "User" / "libxr_config.yaml"
    if sorted(root.glob("*.ioc")):
        chip, source, layout, assigned = stm32_assignments(
            root, lambda chip: pin_layout.layout_pins(model or chip, package)
        )
    else:
        header = find_ti_header(root)
        if header is None:
            raise ValueError(
                tr(
                    f"{root}: no STM32CubeMX .ioc or SysConfig ti_msp_dl_config.h found",
                    f"{root}：没有找到 STM32CubeMX 的 .ioc 或 SysConfig 的 ti_msp_dl_config.h",
                )
            )
        layout = pin_layout.layout_pins(model or mspm0_model(header), package)
        assigned = mspm0_assignments(header, layout)
        source = header.relative_to(root).as_posix()
    info = pin_layout.layout_to_dict(layout)
    info["project"] = {
        "directory": str(root),
        "source": source,
        "libxr_config": str(config_path) if config_path.is_file() else None,
        **project_overlay(layout, assigned, config_path),
    }
    return info
