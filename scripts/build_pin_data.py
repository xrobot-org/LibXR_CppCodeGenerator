"""由厂商的引脚数据生成 libxr pins 使用的 src/libxr/pin_data/ 数据文件。
Build the data files in src/libxr/pin_data/ that libxr pins reads, from the vendors' pin data.

数据来源有两个：
There are two sources:

- STM32：ST 公开的 STM32_open_pin_data 仓库（BSD-3-Clause）的 mcu/*.xml。
  STM32: mcu/*.xml of ST's public STM32_open_pin_data repository (BSD-3-Clause).
- MSPM0：TI SysConfig 安装目录下 dist/deviceData/MSPM0*/ 中的器件 JSON（TI 有限许可，只可用于
  TI 器件）；以及 MSPM0 SDK 中 source/ti/boards/.meta/ 的板子定义（每块 LaunchPad 的器件和封装，
  BSD-3-Clause），用于由 .syscfg 中的 --board 得到封装。
  MSPM0: the device JSON in dist/deviceData/MSPM0*/ of a TI SysConfig installation (TI limited
  license, for TI devices only); and the board definitions in source/ti/boards/.meta/ of the MSPM0
  SDK (the device and package of each LaunchPad, BSD-3-Clause), which give the package of a
  .syscfg that names a --board.

用法 / Usage:
    python scripts/build_pin_data.py --st-data <STM32_open_pin_data> --sysconfig <SysConfig> \\
        --mspm0-sdk <mspm0-sdk>
"""

import argparse
import gzip
import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
ST_NAMESPACE = "{http://dummy.com}"


def expand_group(stem: str) -> list[str]:
    """展开文件名中的分组：STM32F407I(E-G)Hx 对应 STM32F407IEHx 和 STM32F407IGHx。
    Expand the group in a file name: STM32F407I(E-G)Hx stands for STM32F407IEHx and
    STM32F407IGHx.
    """
    match = re.fullmatch(r"(.*?)\(([^)]+)\)(.*)", stem)
    if match is None:
        return [stem]
    return [
        match.group(1) + alternative + match.group(3) for alternative in match.group(2).split("-")
    ]


def read_st_part(path: Path) -> tuple[str, list[list]]:
    """读取一个 STM32 型号的 XML，返回封装名和引脚：[位置, 名称, 类型, [信号...], [GPIO 模式...]]。
    GPIO 模式取自 GPIO 信号的 IOModes（Input、Output、Analog、EVENTOUT、EXTI……），引脚没有
    GPIO 功能时为空。
    Read the XML of one STM32 part; return the package name and the pins as
    [position, name, type, [signals...], [GPIO modes...]]. The GPIO modes come from the IOModes
    of the GPIO signal (Input, Output, Analog, EVENTOUT, EXTI, ...) and are empty for a pin
    without a GPIO function.
    """
    root = ET.parse(path).getroot()
    pins = []
    for pin in root.findall(f"{ST_NAMESPACE}Pin"):
        signals = pin.findall(f"{ST_NAMESPACE}Signal")
        modes = next(
            (s.get("IOModes") for s in signals if s.get("Name") == "GPIO" and s.get("IOModes")), ""
        )
        pins.append(
            [
                pin.get("Position"),
                pin.get("Name"),
                pin.get("Type"),
                [signal.get("Name") for signal in signals],
                modes.split(",") if modes else [],
            ]
        )
    return root.get("Package"), pins


def git_commit(directory: Path) -> str:
    """directory 这个 Git 仓库的当前提交。
    The current commit of the Git repository in directory.
    """
    return subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def stm32_shard(name: str) -> str:
    """一个 STM32 型号所属的数据分片（系列）：STM32H723VGTx 为 H7，STM32WBA52CGUx 为 WBA5。
    The data shard (series) of an STM32 part: H7 for STM32H723VGTx, WBA5 for STM32WBA52CGUx.
    """
    match = re.match(r"STM32([A-Z]+\d)", name)
    return match.group(1) if match else "OTHER"


def build_stm32(st_data: Path) -> tuple[dict, dict[str, dict]]:
    """STM32 数据：索引（来源，以及型号到分片和引脚集的映射），和按系列分片、按内容去重的引脚集。
    读一个型号只需要索引和它所在的分片。
    The STM32 data: the index (the source, and a map from part to shard and pin set) and the pin
    sets, split into shards by series and deduplicated by content. Reading one part needs only
    the index and its shard.
    """
    parts: dict[str, list[str]] = {}
    shards: dict[str, dict] = {}
    holder: dict[str, str] = {}
    for path in sorted((st_data / "mcu").glob("*.xml")):
        package, pins = read_st_part(path)
        content = json.dumps([package, pins], separators=(",", ":"))
        key = hashlib.sha1(content.encode()).hexdigest()[:10]
        for name in expand_group(path.stem):
            # 内容相同的引脚集只存一份，放在第一个用到它的系列的分片里。
            # A pin set of the same content is stored once, in the shard of the first series that
            # uses it.
            shard = holder.setdefault(key, stm32_shard(name))
            shards.setdefault(shard, {})[key] = {"package": package, "pins": pins}
            parts[name] = [shard, key]
    index = {
        "source": {
            "vendor": "STMicroelectronics",
            "dataset": "STM32_open_pin_data",
            "url": "https://github.com/STMicroelectronics/STM32_open_pin_data",
            "commit": git_commit(st_data),
            "license": "BSD-3-Clause",
        },
        "parts": parts,
    }
    return index, shards


def family_prefixes(family: str) -> list[str]:
    """一个 SysConfig 器件族目录名覆盖的型号前缀：MSPM0G350X 为 MSPM0G350，
    MSPM0C1105_C1106 为 MSPM0C1105 和 MSPM0C1106。
    The model prefixes a SysConfig family directory covers: MSPM0G350 for MSPM0G350X,
    MSPM0C1105 and MSPM0C1106 for MSPM0C1105_C1106.
    """
    if family.endswith("X"):
        return [family[:-1]]
    first, *others = family.split("_")
    return [first] + [first[: len(first) - len(other)] + other for other in others]


def read_mspm0_family(path: Path) -> tuple[dict, dict[str, int]]:
    """读取一个 MSPM0 器件族的 JSON，返回其每个封装的引脚：
    [位置, 名称, 类型, PINCM, [[信号, 模式]...]]；以及每个外设实例的电源域编号（0 为 PD0，
    总线时钟是 ULPCLK；1 为 PD1，是 MCLK），UART0 -> 1。
    Read the JSON of one MSPM0 family; return the pins of each of its packages as
    [position, name, type, PINCM, [[signal, mode]...]], and the power domain number of each
    peripheral instance (0 is PD0, whose bus clock is ULPCLK; 1 is PD1, on MCLK), UART0 -> 1.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    muxes = {mux["devicePinID"]: mux["muxSetting"] for mux in data["muxes"]}
    signals = data["peripheralPins"]
    packages = {}
    for package in data["packages"].values():
        balls = {pin["devicePinID"]: pin["ball"] for pin in package["packagePin"]}
        pins = []
        for pin_id, pin in data["devicePins"].items():
            if pin_id not in balls:
                continue
            pincm = pin["attributes"].get("iomux_pincm", "")
            pins.append(
                [
                    balls[pin_id],
                    pin["name"],
                    pin["devicePinType"],
                    int(pincm) if pincm.isdigit() else None,
                    [
                        [
                            signals[setting["peripheralPinID"]]["name"],
                            int(setting["mode"]) if setting["mode"].isdigit() else setting["mode"],
                        ]
                        for setting in muxes.get(pin_id, [])
                    ],
                ]
            )
        pins.sort(key=lambda pin: (len(pin[0]), pin[0]))
        packages[package["name"]] = pins
    power_domains = {
        peripheral["name"]: attributes["POWER_DOMAIN_NUMBER"]
        for peripheral in data["peripherals"].values()
        if isinstance(attributes := peripheral.get("attributes", {}), dict)
        and isinstance(attributes.get("POWER_DOMAIN_NUMBER"), int)
    }
    return packages, power_domains


def build_mspm0(sysconfig: Path) -> dict:
    """MSPM0 数据：每个器件族的型号前缀、各封装的引脚和外设实例的电源域。
    The MSPM0 data: the model prefixes of each family, the pins of its packages and the power
    domain of each peripheral instance.
    """
    root = sysconfig / "dist" / "deviceData"
    families = {}
    for directory in sorted(root.glob("MSPM0*")):
        packages, power_domains = read_mspm0_family(directory / f"{directory.name}.json")
        families[directory.name] = {
            "prefixes": family_prefixes(directory.name),
            "packages": packages,
            "power_domains": power_domains,
        }
    version = (sysconfig / "dist" / "version.txt").read_text(encoding="utf-8").strip()
    return {
        "source": {
            "vendor": "Texas Instruments",
            "dataset": "SysConfig dist/deviceData",
            "version": version,
            "license": "TI limited license (TI devices only)",
        },
        "families": families,
    }


HPM_IOMUX = re.compile(
    r"^#define\s+IOC_(P[A-Z]\d+)_FUNC_CTL_(\w+)\s+IOC_PAD_FUNC_CTL_ALT_SELECT_SET\((\d+)\)", re.M
)
HPM_PAD = re.compile(r"P[A-Z]\d+")


def read_hpm_signals(soc_dir: Path) -> dict[str, list[list]]:
    """一个 HPM SoC 的 hpm_iomux.h：焊盘 -> [[信号名, ALT 编号]...]，信号名与头文件的宏一致
    （UART0_TXD、GPTMR1_COMP_0、GPIO_A_00）。
    The hpm_iomux.h of one HPM SoC: pad -> [[signal name, ALT number]...], the names as the
    header's macros spell them (UART0_TXD, GPTMR1_COMP_0, GPIO_A_00).
    """
    signals: dict[str, list[list]] = {}
    for pad, name, alt in HPM_IOMUX.findall((soc_dir / "hpm_iomux.h").read_text(encoding="utf-8")):
        signals.setdefault(pad, []).append([name, int(alt)])
    return signals


def read_hpm_balls(datasheet: Path) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    """数据手册 PINMUX 表里的焊盘封装映射：焊盘 -> 封装 -> 球号；以及每个焊盘的模拟功能
    （ADC0_IN2 等，数字的 hpm_iomux.h 里没有它们；单元格里折行的名字拼回去）。
    The pad-package map from the PINMUX table of a datasheet: pad -> package -> ball; and the
    analog functions of each pad (ADC0_IN2, ...; the digital hpm_iomux.h does not have them,
    names wrapped inside a cell are joined back).
    """
    import pdfplumber

    balls: dict[str, dict[str, str]] = {}
    analog: dict[str, list[str]] = {}
    with pdfplumber.open(datasheet) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if (
                    len(table) < 3
                    or len(table[1]) < 5
                    or not table[1][4]
                    or table[1][4] != "PINNAME"
                ):
                    continue
                packages = [re.sub(r"[^A-Z0-9]", "", str(name).upper()) for name in table[1][:4]]
                for row in table[2:]:
                    pad = str(row[4]).strip() if row[4] else ""
                    if not HPM_PAD.fullmatch(pad):
                        continue
                    for index, package in enumerate(packages):
                        ball = str(row[index]).strip() if row[index] else ""
                        if not ball or ball == "-":
                            continue
                        balls.setdefault(pad, {})[package] = ball
                    names: list[str] = []
                    for part in str(row[6] or "").split("\n"):
                        if part in ("", "-"):
                            continue
                        # 名字在单元格里折行：下半段以下划线开头，拼回上一个名字。
                        # A name wraps inside the cell: the second half starts with an
                        # underscore and continues the previous name.
                        if part.startswith("_") and names:
                            names[-1] += part
                        else:
                            names.append(part)
                    if names:
                        known = analog.setdefault(pad, [])
                        known.extend(name for name in names if name not in known)
    return balls, analog


def build_hpm(sdk: Path, datasheet: Path, series: str) -> dict:
    """HPM 数据：一个系列（HPM5300）里每个 SoC 的各封装引脚；信号取自 SDK 的 hpm_iomux.h
    （BSD-3-Clause），球号取自该系列的数据手册 PINMUX 表（hpmicro.com）。
    The HPM data: the pins of each package of every SoC of one series (HPM5300); the signals
    come from the SDK's hpm_iomux.h (BSD-3-Clause), the balls from that series' datasheet PINMUX
    table (hpmicro.com).
    """
    balls, analog = read_hpm_balls(datasheet)
    socs = {}
    for soc_dir in sorted((sdk / "soc" / series).glob("*")):
        if not (soc_dir / "hpm_iomux.h").is_file():
            continue
        signals = read_hpm_signals(soc_dir)
        # 模拟功能（ADC、ACMP、OPA 的输入输出）补进信号表；数字头文件里没有它们。
        # The analog functions (the inputs and outputs of ADC, ACMP and OPA) join the signal
        # lists; the digital header does not have them.
        for pad, names in analog.items():
            known = {name for name, _ in signals.get(pad, [])}
            signals.setdefault(pad, []).extend([name, None] for name in names if name not in known)
        packages = {}
        for package in balls[next(iter(balls))]:
            pins = [
                [
                    ball,
                    pad,
                    "GPIO",
                    None,
                    signals.get(pad, []),
                ]
                for pad, package_balls in sorted(balls.items())
                for ball in [package_balls.get(package)]
                if ball and pad in signals
            ]
            pins.sort(key=lambda pin: (len(pin[0]), pin[0]))
            packages[package] = pins
        socs[soc_dir.name] = {"packages": packages}
    return {
        "source": {
            "vendor": "HPMicro",
            "dataset": f"hpm_sdk soc/{series}/*/hpm_iomux.h; {datasheet.stem} pin tables",
            "commit": git_commit(sdk),
            "license": "BSD-3-Clause",
        },
        "socs": socs,
    }


def read_boards(sdk: Path) -> dict:
    """MSPM0 SDK 的板子定义：板名（文件名）到器件和封装。
    The board definitions of the MSPM0 SDK: the board name (the file name) to its device and
    package.

    定义文件是带注释的 JSON，这里只取 gpn（器件）和 pkg（封装）两个字段。
    The definition files are JSON with comments; only gpn (the device) and pkg (the package) are
    read.
    """
    boards = {}
    for path in sorted((sdk / "source" / "ti" / "boards" / ".meta").glob("*.syscfg.json")):
        text = path.read_text(encoding="utf-8")
        device = re.search(r'"gpn":\s*"([^"]+)"', text)
        package = re.search(r'"pkg":\s*"([^"]+)"', text)
        if device and package:
            boards[path.name.removesuffix(".syscfg.json")] = {
                "device": device.group(1),
                "package": package.group(1),
            }
    return boards


def board_license(sdk: Path) -> str:
    """板子定义文件开头的版权和 BSD-3-Clause 许可证注释。
    The copyright and BSD-3-Clause license comment at the top of a board definition file.
    """
    path = next((sdk / "source" / "ti" / "boards" / ".meta").glob("*.syscfg.json"))
    text = path.read_text(encoding="utf-8")
    comment = text[: text.index("*/")]
    lines = [re.sub(r"^\s*/?\*+\$?\s?", "", line) for line in comment.splitlines()]
    return "\n".join(lines).strip() + "\n"


def write_gzip_json(path: Path, data: dict) -> None:
    """把 data 写成压缩的 JSON；不写时间戳，同样的数据得到同样的文件。
    Write data as compressed JSON; no timestamp is written, so the same
    data gives the same file.
    """
    # 键保持写入顺序（按型号名排序）：相近的型号相邻，压缩得更小。
    # The keys keep the order written (sorted by part name): similar parts sit together, which
    # compresses better.
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    with open(path, "wb") as file, gzip.GzipFile(fileobj=file, mode="wb", mtime=0) as zipped:
        zipped.write(text.encode("utf-8"))


def main() -> None:
    """解析参数，生成数据文件和许可证文本。
    Parse the arguments and write the data files and the license texts.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--st-data", type=Path, help="STM32_open_pin_data checkout")
    parser.add_argument("--sysconfig", type=Path, help="TI SysConfig installation")
    parser.add_argument("--mspm0-sdk", type=Path, help="TI MSPM0 SDK checkout")
    parser.add_argument("--hpm-sdk", type=Path, help="HPM SDK checkout")
    parser.add_argument("--hpm-datasheet", type=Path, help="HPM series datasheet PDF")
    parser.add_argument("--hpm-series", help="the HPM series the datasheet covers (HPM5300)")
    parser.add_argument(
        "--output", type=Path, default=REPOSITORY / "src" / "libxr" / "pin_data", help="output dir"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.sysconfig is None) != (args.mspm0_sdk is None):
        parser.error("--sysconfig and --mspm0-sdk go together")
    if (args.hpm_sdk is None) != (args.hpm_datasheet is None):
        parser.error("--hpm-sdk and --hpm-datasheet go together")
    if args.hpm_sdk is not None and not args.hpm_series:
        parser.error("--hpm-series names the series the datasheet covers (HPM5300)")
    if args.st_data is None and args.sysconfig is None and args.hpm_sdk is None:
        parser.error(
            "give --st-data, --sysconfig with --mspm0-sdk, or --hpm-datasheet with --hpm-sdk"
        )
    if args.st_data is not None:
        # 先清掉旧的分片，不留下已经不存在的系列。
        # Remove the old shards first, so no series that no longer exists stays.
        for old in args.output.glob("stm32*.json.gz"):
            old.unlink()
        index, shards = build_stm32(args.st_data)
        write_gzip_json(args.output / "stm32.json.gz", index)
        for shard, sets in shards.items():
            write_gzip_json(args.output / f"stm32-{shard}.json.gz", sets)
        (args.output / "LICENSE-ST.txt").write_bytes((args.st_data / "LICENSE").read_bytes())
    if args.sysconfig is not None:
        for old in ("mspm0.json.gz", "mspm0_boards.json", "LICENSE-TI-BOARDS.txt"):
            (args.output / old).unlink(missing_ok=True)
        write_gzip_json(args.output / "mspm0.json.gz", build_mspm0(args.sysconfig))
        boards = {
            "source": {
                "vendor": "Texas Instruments",
                "dataset": "MSPM0 SDK source/ti/boards/.meta",
                "commit": git_commit(args.mspm0_sdk),
                "license": "BSD-3-Clause",
            },
            "boards": read_boards(args.mspm0_sdk),
        }
        (args.output / "mspm0_boards.json").write_text(
            json.dumps(boards, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        (args.output / "LICENSE-TI-BOARDS.txt").write_text(
            board_license(args.mspm0_sdk), encoding="utf-8", newline="\n"
        )
        # 许可证原文随数据分发。TI 的文件不是 UTF-8，转成 UTF-8 写出。
        # The license texts are distributed with the data. TI's file is not UTF-8; it is written
        # as UTF-8.
        ti_bytes = (args.sysconfig / "dist" / "license.txt").read_bytes()
        ti_text = ti_bytes.decode("cp1252").replace("\r\n", "\n")
        (args.output / "LICENSE-TI.txt").write_text(ti_text, encoding="utf-8", newline="\n")
    if args.hpm_sdk is not None:
        (args.output / "hpm.json.gz").unlink(missing_ok=True)
        write_gzip_json(
            args.output / "hpm.json.gz",
            build_hpm(args.hpm_sdk, args.hpm_datasheet, args.hpm_series),
        )
        # SDK 的许可证原文随数据分发。
        # The SDK's license text is distributed with the data.
        (args.output / "LICENSE-HPM.txt").write_bytes((args.hpm_sdk / "LICENSE").read_bytes())


if __name__ == "__main__":
    main()
