"""由厂商的引脚数据生成 libxr pins 使用的 src/libxr/pin_data/ 数据文件。
Build the data files in src/libxr/pin_data/ that libxr pins reads, from the vendors' pin data.

数据来源有两个：
There are two sources:

- STM32：ST 公开的 STM32_open_pin_data 仓库（BSD-3-Clause）的 mcu/*.xml。
  STM32: mcu/*.xml of ST's public STM32_open_pin_data repository (BSD-3-Clause).
- MSPM0：TI SysConfig 安装目录下 dist/deviceData/MSPM0*/ 中的器件 JSON（TI 有限许可，只可用于
  TI 器件）。
  MSPM0: the device JSON in dist/deviceData/MSPM0*/ of a TI SysConfig installation (TI limited
  license, for TI devices only).

用法 / Usage:
    python scripts/build_pin_data.py --st-data <STM32_open_pin_data> --sysconfig <SysConfig>
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
    """读取一个 STM32 型号的 XML，返回封装名和引脚：[位置, 名称, 类型, [信号...]]。
    Read the XML of one STM32 part; return the package name and the pins as
    [position, name, type, [signals...]].
    """
    root = ET.parse(path).getroot()
    pins = [
        [
            pin.get("Position"),
            pin.get("Name"),
            pin.get("Type"),
            [signal.get("Name") for signal in pin.findall(f"{ST_NAMESPACE}Signal")],
        ]
        for pin in root.findall(f"{ST_NAMESPACE}Pin")
    ]
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


def read_mspm0_family(path: Path) -> dict:
    """读取一个 MSPM0 器件族的 JSON，返回其每个封装的引脚：
    [位置, 名称, 类型, PINCM, [[信号, 模式]...]]。
    Read the JSON of one MSPM0 family; return the pins of each of its packages as
    [position, name, type, PINCM, [[signal, mode]...]].
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
    return packages


def build_mspm0(sysconfig: Path) -> dict:
    """MSPM0 数据：每个器件族的型号前缀和各封装的引脚。
    The MSPM0 data: the model prefixes of each family and the pins of its packages.
    """
    root = sysconfig / "dist" / "deviceData"
    families = {}
    for directory in sorted(root.glob("MSPM0*")):
        families[directory.name] = {
            "prefixes": family_prefixes(directory.name),
            "packages": read_mspm0_family(directory / f"{directory.name}.json"),
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
    parser.add_argument("--st-data", type=Path, required=True, help="STM32_open_pin_data checkout")
    parser.add_argument("--sysconfig", type=Path, required=True, help="TI SysConfig installation")
    parser.add_argument(
        "--output", type=Path, default=REPOSITORY / "src" / "libxr" / "pin_data", help="output dir"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    # 先清掉旧的数据文件，不留下已经不存在的分片。
    # Remove the old data files first, so no shard that no longer exists stays.
    for old in args.output.glob("*.json.gz"):
        old.unlink()
    index, shards = build_stm32(args.st_data)
    write_gzip_json(args.output / "stm32.json.gz", index)
    for shard, sets in shards.items():
        write_gzip_json(args.output / f"stm32-{shard}.json.gz", sets)
    write_gzip_json(args.output / "mspm0.json.gz", build_mspm0(args.sysconfig))
    # 许可证原文随数据分发。TI 的文件不是 UTF-8，转成 UTF-8 写出。
    # The license texts are distributed with the data. TI's file is not UTF-8; it is written as
    # UTF-8.
    (args.output / "LICENSE-ST.txt").write_bytes((args.st_data / "LICENSE").read_bytes())
    ti_bytes = (args.sysconfig / "dist" / "license.txt").read_bytes()
    ti_text = ti_bytes.decode("cp1252").replace("\r\n", "\n")
    (args.output / "LICENSE-TI.txt").write_text(ti_text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
