"""libxr pins -d：把工程里已选的引脚信号叠加到布局上，并对应到 libxr_config.yaml 的外设设置。
libxr pins -d: overlay the pin signals a project has selected on the layout, and tie them to the
peripheral settings of libxr_config.yaml.

STM32 的已选信号来自 CubeMX 的 .ioc（Pxn.Signal），MSPM0 的来自根目录 SysConfig 工程
（.syscfg）的 $assign 行（求解器选的 $suggestSolution 作兜底），HPM 的来自 boards/ 下 .hpmpc
里 main.cpp 调用的 pinmux 函数。信号按布局中该引脚的可选信号核对；核对不上的原样给出，
matched 为 false。
The selected signals of an STM32 come from the CubeMX .ioc (Pxn.Signal), those of an MSPM0 from
the $assign lines of a SysConfig project (.syscfg) in the root (the solver's $suggestSolution
as the fallback), and those of an HPM from the pinmux functions of the .hpmpc under boards/
that main.cpp calls. A signal is checked against the selectable signals of its pin in the
layout; one that does not match is given as it is, with matched false.
"""

import json
import re
from dataclasses import dataclass
from functools import cache
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


SYSCFG_ARGS = re.compile(r"^[ \t]*(?://|\*)?[ \t]*@(v2)?[Cc]li[Aa]rgs[ \t]+(.*)$", re.M)


@dataclass
class Syscfg:
    """一个 .syscfg 的 @cliArgs 里的器件、封装和板子；没有的为 None。
    The device, package and board in the @cliArgs of a .syscfg; None for what it does not give.
    """

    device: str | None = None
    package: str | None = None
    board: str | None = None


def cli_option(arguments: str, name: str) -> str | None:
    """@cliArgs 的参数串中 --name 的值（带引号或不带）。
    The value of --name in the argument string of an @cliArgs, quoted or not.
    """
    match = re.search(rf'--{name}[ \t]+(?:"([^"]*)"|(\S+))', arguments)
    return (match.group(1) if match.group(1) is not None else match.group(2)) if match else None


def read_syscfg(path: Path) -> Syscfg:
    """读 .syscfg 开头的 @cliArgs 和 @v2CliArgs（后者在后，覆盖前者）。
    Read the @cliArgs and @v2CliArgs at the top of a .syscfg (the latter comes last and overrides
    the former).
    """
    info = Syscfg()
    text = path.read_text(encoding="utf-8", errors="replace")
    for _, arguments in sorted(SYSCFG_ARGS.findall(text), key=lambda found: found[0] == "v2"):
        info.device = cli_option(arguments, "device") or info.device
        info.package = cli_option(arguments, "package") or info.package
        board = cli_option(arguments, "board")
        info.board = board.rsplit("/", 1)[-1] if board else info.board
    return info


@cache
def load_boards() -> dict:
    """MSPM0 SDK 的板子（LP_MSPM0G3507 等）到器件和封装。
    The boards of the MSPM0 SDK (LP_MSPM0G3507, ...) to their device and package.
    """
    path = pin_layout.DATA / "mspm0_boards.json"
    return json.loads(path.read_text(encoding="utf-8"))["boards"]


SYSCFG_MODULE = re.compile(
    r'^const\s+(\w+)\s*=\s*scripting\.addModule\(\s*"/ti/driverlib/(\w+)"', re.M
)
SYSCFG_INSTANCE = re.compile(r"^const\s+(\w+)\s*=\s*(\w+)\.addInstance\(\)", re.M)
SYSCFG_ASSIGNMENT = re.compile(r"^(\w+)((?:\.[$\w]+|\[\d+\])+)\s*=\s*(.+?);\s*$", re.M)
SYSCFG_GPIO_PIN = re.compile(r"^\.associatedPins\[(\d+)\]\.pin\.\$assign$")
SYSCFG_GPIO_PIN_SUGGESTED = re.compile(r"^\.associatedPins\[(\d+)\]\.pin\.\$suggestSolution$")
SYSCFG_GPIO_NAME = re.compile(r"^\.associatedPins\[(\d+)\]\.\$name$")
SYSCFG_PERIPH_PIN = re.compile(r"^\.peripheral\.(\w*Pin\w*)\.\$assign$")
SYSCFG_PERIPH_PIN_SUGGESTED = re.compile(r"^\.peripheral\.(\w*Pin\w*)\.\$suggestSolution$")
SYSCFG_ADC_PIN = re.compile(r"^[Aa][Dd][Cc]Pin(\d+)$")


def syscfg_value(text: str):
    """.syscfg 中赋值的右边：字符串、数字、布尔值和数组按其类型，表达式原样保留为字符串。
    The right side of an assignment in a .syscfg: strings, numbers, booleans and arrays as what
    they are; an expression is kept as written, as a string.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
        return int(text, 16)
    return text


def read_syscfg_settings(path: Path) -> dict[str, dict]:
    """.syscfg 中每个外设的设置：外设实例（UART0）-> 模块、名字（UART_0）和参数。
    The settings of each peripheral in a .syscfg: the peripheral instance (UART0) -> its module,
    its name (UART_0) and its parameters.

    SysConfig 的配置是脚本：UART1.targetBaudRate = 9600; 和 UART1.peripheral.$assign = "UART0";。
    变量 UART1 是模块 UART 的一个实例，peripheral.$assign（用户指定）或 $suggestSolution（求解器
    给出）说明它用的外设。引脚的指定和求解器内部的 $ 项不是设置，不收。
    The configuration of SysConfig is a script: UART1.targetBaudRate = 9600; and
    UART1.peripheral.$assign = "UART0";. The variable UART1 is an instance of the module UART, and
    peripheral.$assign (the user's) or $suggestSolution (the solver's) names the peripheral it
    uses. The assignment of pins and the solver's own $ items are not settings and are left out.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^[ \t]*//.*$", "", text, flags=re.M)
    modules = {var: module for var, module in SYSCFG_MODULE.findall(text)}
    instances = {
        var: modules[module] for var, module in SYSCFG_INSTANCE.findall(text) if module in modules
    }
    found: dict[str, dict] = {}
    for var, tail, value in SYSCFG_ASSIGNMENT.findall(text):
        if var not in instances:
            continue
        entry = found.setdefault(
            var,
            {
                "module": instances[var],
                "name": None,
                "assigned": None,
                "suggested": None,
                "params": {},
            },
        )
        if tail == ".$name":
            entry["name"] = syscfg_value(value)
        elif tail == ".peripheral.$assign":
            entry["assigned"] = syscfg_value(value)
        elif tail == ".peripheral.$suggestSolution":
            entry["suggested"] = syscfg_value(value)
        elif not tail.startswith(".peripheral.") and "$" not in tail:
            entry["params"][tail.lstrip(".")] = syscfg_value(value)
    settings = {}
    for entry in found.values():
        peripheral = entry["assigned"] or entry["suggested"]
        if peripheral:
            settings[peripheral] = {
                "module": entry["module"],
                "name": entry["name"],
                "params": entry["params"],
            }
    return settings


def peripheral_entry(pin: Pin, instance: str, member: str) -> dict:
    """外设引脚的叠加项：在引脚的信号表中认出 <外设>.<功能>；认不出时按成员名原样给出。
    The overlay entry of a peripheral pin: recognize <peripheral>.<function> among the signals of
    the pin; without a match the signal is given from the member name as it is.

    成员名是 SysConfig 的引脚成员：rxPin、cs0Pin、OutPin、adcPin3（数字是 ADC 的通道）。
    The member is a pin member of SysConfig: rxPin, cs0Pin, OutPin, adcPin3 (the digit is the
    channel of the ADC).

    TI 对 CANFD 的功能名带 CAN 前缀（CANRX、CANTX），成员名没有，按后缀核对。
    TI names the functions of a CANFD with the CAN prefix (CANRX, CANTX) that the member names
    leave out, so the check also accepts the suffix.
    """
    adc = SYSCFG_ADC_PIN.match(member)
    target = adc.group(1) if adc else member.replace("Pin", "").upper()
    candidates = [s for s in pin.signals if "." in s and s.split(".", 1)[0] == instance]
    chosen = None
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        for signal in candidates:
            function = signal.split(".", 1)[1]
            if function.upper() == target or (target and function.upper().endswith(target)):
                chosen = signal
                break
    return {
        "signal": chosen or f"{instance}.{target}",
        "peripheral": instance,
        "kind": re.sub(r"\d+$", "", instance),
        "function": chosen.split(".", 1)[1] if chosen else target,
        "matched": chosen is not None,
    }


def syscfg_assignments(path: Path, layout: PinLayout) -> dict[str, dict]:
    """.syscfg 中 MSPM0 的已选信号：引脚名 -> 信号及其识别结果。
    The selected signals of an MSPM0 in a .syscfg: pin name -> signal and what it was recognized
    as.

    外设引脚是 <实例>.peripheral.<功能>Pin.$assign（<实例> 取 peripheral.$assign 指定的外设，
    功能按引脚的信号表核对），GPIO 是 <变量>.associatedPins[i].pin.$assign，标签取引脚的
    $name。求解器选的 $suggestSolution 兜底，$assign 优先；DMA 通道和 clockTree 的晶振引脚
    不收。
    A peripheral pin is <instance>.peripheral.<function>Pin.$assign (the instance is the
    peripheral peripheral.$assign names, and the function is checked against the signals of the
    pin), a GPIO one is <variable>.associatedPins[i].pin.$assign, with the label from the $name
    of the pin. The solver's $suggestSolution is the fallback, and $assign wins; the DMA
    channels and the oscillator pins of the clock tree are left out.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^[ \t]*//.*$", "", text, flags=re.M)
    modules = {var: module for var, module in SYSCFG_MODULE.findall(text)}
    # 单例外设（DAC12 等）没有 addInstance，直接配在模块变量上。
    # A singleton peripheral (DAC12, ...) has no addInstance; the module variable is configured
    # directly.
    instances = dict(modules)
    instances.update(
        {var: modules[module] for var, module in SYSCFG_INSTANCE.findall(text) if module in modules}
    )
    peripheral_of: dict[str, str] = {}
    periph_pin: dict[tuple[str, str], str] = {}
    gpio_pin: dict[tuple[str, int], str] = {}
    gpio_label: dict[tuple[str, int], str] = {}
    for var, tail, value in SYSCFG_ASSIGNMENT.findall(text):
        if var not in instances:
            continue
        if tail == ".peripheral.$assign":
            peripheral_of[var] = syscfg_value(value)
        elif tail == ".peripheral.$suggestSolution":
            peripheral_of.setdefault(var, syscfg_value(value))
        elif tail == ".$name" and instances[var] == "GPIO":
            # 组的 $name 是没有引脚 $name 时的标签。
            # The $name of the group is the label when a pin has no $name of its own.
            gpio_label[(var, -1)] = syscfg_value(value)
            continue
        index = SYSCFG_GPIO_NAME.match(tail)
        if index:
            gpio_label[(var, int(index.group(1)))] = syscfg_value(value)
            continue
        member = SYSCFG_PERIPH_PIN.match(tail) or SYSCFG_PERIPH_PIN_SUGGESTED.match(tail)
        if member:
            key = (var, member.group(1))
            # $assign 覆盖同名的 $suggestSolution。
            # $assign overrides a $suggestSolution of the same member.
            if member.re is SYSCFG_PERIPH_PIN or key not in periph_pin:
                periph_pin[key] = syscfg_value(value)
            continue
        gpio = SYSCFG_GPIO_PIN.match(tail) or SYSCFG_GPIO_PIN_SUGGESTED.match(tail)
        if gpio:
            key = (var, int(gpio.group(1)))
            if gpio.re is SYSCFG_GPIO_PIN or key not in gpio_pin:
                gpio_pin[key] = syscfg_value(value)
    by_name = {pin.name: pin for pin in layout.pins}
    assigned: dict[str, dict] = {}
    for (var, member), pin_name in periph_pin.items():
        instance = peripheral_of.get(var)
        pin = by_name.get(pin_name) if instance else None
        if pin is not None:
            assigned[pin.name] = peripheral_entry(pin, instance, member)
    for (var, index), pin_name in gpio_pin.items():
        pin = by_name.get(pin_name)
        if pin is None:
            continue
        port_line = pin_layout.gpio_port_and_line(pin.name)
        entry = {
            "signal": pin.name,
            "peripheral": f"GPIO{port_line[0]}" if port_line else "GPIO",
            "kind": "GPIO",
            "function": f"P{port_line[1]}" if port_line else "GPIO",
            "matched": pin.name in pin.signals,
        }
        label = gpio_label.get((var, index)) or gpio_label.get((var, -1))
        if label:
            entry["label"] = label
        assigned[pin.name] = entry
    return assigned


def hpm_canonical(signal: str) -> str:
    """.hpmpc 的信号（UART0.A.TXD、GPIO.A.A[10]）到 hpm_iomux.h 的宏名（UART0_TXD、GPIO_A_10）：
    去掉中间的 IOC 通道字母，方括号换成下划线。
    The signal of a .hpmpc (UART0.A.TXD, GPIO.A.A[10]) to the macro name of hpm_iomux.h
    (UART0_TXD, GPIO_A_10): the IOC channel letter in the middle goes, and brackets become
    underscores.
    """
    parts = signal.split(".")
    if len(parts) == 3:
        parts = [parts[0], parts[2]]
    return "_".join(parts).replace("[", "_").replace("]", "")


HPM_DIRECTIVE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b")
HPM_CALL = re.compile(r"^\s*(\w+)\(\);\s*$")


def hpm_active_functions(main_cpp: Path, functions: dict) -> list[str]:
    """main.cpp 在预处理条件外调用的、.hpmpc 里存在的 pinmux 函数；main.cpp 不存在或没有调用
    时退回 init_bsp_pins。条件编译里的调用不算（rmcs 的 JTAG 共用引脚在 #if 里，O2 的决定）。
    The pinmux functions of the .hpmpc that main.cpp calls outside preprocessor conditions; the
    fallback is init_bsp_pins when main.cpp does not exist or calls none. A call inside a
    conditional does not count (rmcs' JTAG shared pins sit in an #if, the decision of O2).
    """
    if not main_cpp.is_file():
        return ["init_bsp_pins"] if "init_bsp_pins" in functions else []
    active: list[str] = []
    depth = 0
    for line in main_cpp.read_text(encoding="utf-8", errors="replace").splitlines():
        directive = HPM_DIRECTIVE.match(line)
        if directive:
            if directive.group(1) in ("if", "ifdef", "ifndef"):
                depth += 1
            elif directive.group(1) == "endif":
                depth = max(0, depth - 1)
            continue
        call = HPM_CALL.match(line)
        if call and depth == 0 and call.group(1) in functions and call.group(1) not in active:
            active.append(call.group(1))
    if not active and "init_bsp_pins" in functions:
        active = ["init_bsp_pins"]
    return active


def hpm_assignments(hpmpc: Path, main_cpp: Path, layout: PinLayout) -> dict[str, dict]:
    """.hpmpc 里 main.cpp 调用的 pinmux 函数的已选信号：引脚名 -> 信号及其识别结果。
    The selected signals of the pinmux functions of the .hpmpc that main.cpp calls: pin name ->
    signal and what it was recognized as.

    信号是工具的三段式（UART0.A.TXD），化成 hpm_iomux.h 的宏名（UART0_TXD）后按引脚的信号表
    核对；GPIO 的（GPIO.A.A[10]）与 MSPM0 一样算普通用法。.hpmpc 不带名字，没有标签。
    A signal is the tool's three-part form (UART0.A.TXD), checked against the signals of the pin
    as the macro name of hpm_iomux.h (UART0_TXD); a GPIO one (GPIO.A.A[10]) counts as the plain
    use like MSPM0. The .hpmpc carries no names, so there is no label.
    """
    functions = json.loads(hpmpc.read_text(encoding="utf-8"))["content"]["pinmux"]["functions"]
    by_name = {pin.name: pin for pin in layout.pins}
    assigned: dict[str, dict] = {}
    for function in hpm_active_functions(main_cpp, functions):
        for pad, selection in functions[function].get("selectPins", {}).items():
            pin = by_name.get(pad)
            if pin is None or not selection.get("signal"):
                continue
            canonical = hpm_canonical(selection["signal"])
            if canonical.startswith("GPIO_"):
                port_line = pin_layout.gpio_port_and_line(pin.name)
                assigned[pin.name] = {
                    "signal": canonical,
                    "peripheral": f"GPIO{port_line[0]}" if port_line else "GPIO",
                    "kind": "GPIO",
                    "function": f"P{port_line[1]}" if port_line else "GPIO",
                    "matched": canonical in pin.signals,
                }
                continue
            recognized = pin_layout.recognize_hpm(canonical)
            instance, kind, function = recognized or (canonical, "", "")
            assigned[pin.name] = {
                "signal": canonical,
                "peripheral": instance,
                "kind": kind,
                "function": function,
                "matched": canonical in pin.signals,
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


def project_overlay(
    layout: PinLayout,
    assigned: dict[str, dict],
    config_path: Path | None,
    sysconfig: dict[str, dict] | None = None,
) -> dict:
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
        # MSPM0：外设的设置在 SysConfig 工程（.syscfg）里，只读给出。
        # MSPM0: the settings of a peripheral are in the SysConfig project (.syscfg), given read-only.
        if sysconfig and instance in sysconfig:
            used["sysconfig"] = sysconfig[instance]
        key = config_key(layout.platform, {"peripheral": instance, "kind": used["kind"]})
        if key is None:
            continue
        section, name = key
        # 键的大小写以文件为准：同一个文件里有 fdcan1，也有 FDCAN1。
        # The case of the key is the file's: one file has fdcan1, another FDCAN1.
        entries = settings.get(section) if isinstance(settings.get(section), dict) else {}
        actual = next((key for key in entries if str(key).lower() == name), None)
        used["config"] = {"section": section, "key": actual or name, "present": actual is not None}
        if actual is not None:
            used["config"]["params"] = entries[actual]
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

    给出 model 时以它为准，否则取自工程（STM32 的 .ioc，MSPM0 的根目录 .syscfg，HPM 的
    boards/ 下 .hpmpc 的 SoC 名）。MSPM0 的封装取自 .syscfg 的 --package 或 --board，HPM 的取
    自 .hpmpc 的 packageName，package 给出时以它为准。
    libxr_config 默认为目录下的 User/libxr_config.yaml。
    A given model wins; otherwise it comes from the project (the .ioc of an STM32, the root
    .syscfg of an MSPM0, the SoC name of the .hpmpc under boards/ of an HPM). The package of an
    MSPM0 comes from the --package or --board of the .syscfg, that of an HPM from the
    packageName of the .hpmpc, and a given package wins. libxr_config defaults to
    User/libxr_config.yaml in the directory.
    """
    root = Path(directory)
    config_path = Path(libxr_config) if libxr_config else root / "User" / "libxr_config.yaml"
    syscfg_file, sysconfig = None, {}
    hpmpcs = sorted(root.glob("boards/*/*.hpmpc")) if (root / "app.yaml").is_file() else []
    if sorted(root.glob("*.ioc")):
        chip, source, layout, assigned = stm32_assignments(
            root, lambda chip: pin_layout.layout_pins(model or chip, package)
        )
    elif hpmpcs:
        if len(hpmpcs) > 1:
            raise ValueError(
                tr(
                    f"{root}: several .hpmpc files under boards/; keep one",
                    f"{root}：boards/ 下有多个 .hpmpc；请只保留一个",
                )
            )
        # HPM 的工程：SoC 和封装在 .hpmpc 的 info 里，已选信号取 main.cpp 无条件调用的 pinmux
        # 函数（O2：条件编译里的不算）。
        # An HPM project: the SoC and the package are in the info of the .hpmpc, and the selected
        # signals come from the pinmux functions main.cpp calls without a condition (O2: a call
        # inside a conditional does not count).
        info = json.loads(hpmpcs[0].read_text(encoding="utf-8"))["content"]["info"]
        soc = model or info.get("socName")
        if soc is None:
            raise ValueError(
                tr(
                    f"{hpmpcs[0]} does not name a SoC; give the model",
                    f"{hpmpcs[0]} 没有给出 SoC；请给出型号",
                )
            )
        layout = pin_layout.layout_pins(soc, package or info.get("packageName"))
        assigned = hpm_assignments(hpmpcs[0], root / "User" / "main.cpp", layout)
        source = hpmpcs[0].relative_to(root).as_posix()
        # sysconfig_file 对 HPM 是 .hpmpc 本身：外设配置就住在里面。
        # sysconfig_file is the .hpmpc itself for an HPM: the peripheral configuration lives in
        # it.
        syscfg_file, sysconfig = hpmpcs[0], {}
    else:
        syscfgs = sorted(root.glob("*.syscfg"))
        if len(syscfgs) > 1:
            raise ValueError(
                tr(
                    f"{root}: several .syscfg files in the root; keep one",
                    f"{root}：根目录里有多个 .syscfg；请只保留一个",
                )
            )
        if not syscfgs:
            raise ValueError(
                tr(
                    f"{root}: no STM32CubeMX .ioc, HPM .hpmpc or root SysConfig .syscfg found",
                    f"{root}：没有找到 STM32CubeMX 的 .ioc、HPM 的 .hpmpc 或根目录的 SysConfig"
                    " .syscfg",
                )
            )
        # SysConfig 工程在根目录：器件、封装、已选信号和设置都直接取自它，不需要跑 SysConfig。
        # A SysConfig project in the root: device, package, selected signals and settings all
        # come from it directly; SysConfig does not have to run.
        meta = read_syscfg(syscfgs[0])
        board = load_boards().get(meta.board or "")
        # 板子型工程（--board）的器件和封装由板子表给出。
        # A board-based project (--board) takes its device and package from the board table.
        device = model or meta.device or (board or {}).get("device")
        if device is None:
            raise ValueError(
                tr(
                    f"{syscfgs[0]} does not name a device; give the model",
                    f"{syscfgs[0]} 没有给出器件；请给出型号",
                )
            )
        if package is None:
            package = meta.package or (board or {}).get("package")
        layout = pin_layout.layout_pins(device, package)
        assigned = syscfg_assignments(syscfgs[0], layout)
        source = syscfgs[0].name
        syscfg_file, sysconfig = syscfgs[0], read_syscfg_settings(syscfgs[0])
    info = pin_layout.layout_to_dict(layout)
    info["project"] = {
        "directory": str(root),
        "source": source,
        "libxr_config": str(config_path) if config_path.is_file() else None,
        "sysconfig_file": syscfg_file.relative_to(root).as_posix() if syscfg_file else None,
        **project_overlay(layout, assigned, config_path, sysconfig),
    }
    return info
