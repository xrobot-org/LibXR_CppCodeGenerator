#!/usr/bin/env python3
"""Smoke test for stale GPIO device aliases produced from CubeMX pin names."""

from __future__ import annotations

import re
import sys
import tempfile
from importlib import import_module, reload
from pathlib import Path

import yaml


GPIO_PC14_ENTRY_RE = re.compile(r"XR_REGISTER\(PC14,\s*LibXR::GPIO\)")
STALE_PC14_ENTRY_RE = re.compile(r"XR_REGISTER\(PC14_OSC32_IN\b")


LEGACY_ALIAS_CASES = {
    "typed-dict": {
        "type": "GPIO",
        "aliases": ["PC14_OSC32_IN"],
    },
    "legacy-list": ["PC14_OSC32_IN"],
    "legacy-string": "PC14_OSC32_IN",
}


def run_stm32_generator(repo_root: Path, config_path: Path, output_path: Path) -> int:
    src_path = str(repo_root / "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    generator = import_module("libxr.GeneratorCodeSTM32")
    generator = reload(generator)

    old_argv = sys.argv[:]
    sys.argv = [
        "libxr.GeneratorCodeSTM32",
        "-i",
        str(config_path),
        "-o",
        str(output_path),
        "--xrobot",
    ]
    try:
        try:
            generator.main()
        except SystemExit as exit_status:
            return exit_status.code if isinstance(exit_status.code, int) else 1
    finally:
        sys.argv = old_argv

    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    for case_name, alias_entry in LEGACY_ALIAS_CASES.items():
        result = run_alias_case(repo_root, case_name, alias_entry)
        if result != 0:
            return result

    print("GPIO alias smoke passed.")
    return 0


def run_alias_case(repo_root: Path, case_name: str, alias_entry) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        config_path = tmp_dir / "config.yaml"
        output_path = tmp_dir / "app_main.cpp"
        libxr_config_path = tmp_dir / "libxr_config.yaml"

        config = {
            "Mcu": {"Family": "STM32H7", "Type": "STM32H723VGTx"},
            "Timebase": {"Source": "SysTick"},
            "GPIO": {"PC14": {"Signal": "GPIO_Output"}},
            "Peripherals": {},
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        libxr_config = {
            "SYSTEM": "None",
            "device_aliases": {
                "PC14_OSC32_IN": alias_entry,
            },
        }
        libxr_config_path.write_text(
            yaml.safe_dump(libxr_config, sort_keys=False),
            encoding="utf-8",
        )

        result = run_stm32_generator(repo_root, config_path, output_path)
        if result != 0:
            return result

        generated = output_path.read_text(encoding="utf-8")
        entry = GPIO_PC14_ENTRY_RE.search(generated)

        if entry is None:
            print(f"{case_name}: missing merged PC14 alias entry")
            print(generated)
            return 1

        saved = yaml.safe_load(libxr_config_path.read_text(encoding="utf-8"))
        aliases = set(saved["device_aliases"]["PC14"]["aliases"])
        expected_aliases = {"PC14", "PC14_OSC32_IN"}
        if not expected_aliases.issubset(aliases):
            print(
                f"{case_name}: PC14 alias entry missing aliases: "
                f"{sorted(expected_aliases - aliases)}"
            )
            print(generated)
            return 1

        if STALE_PC14_ENTRY_RE.search(generated):
            print(f"{case_name}: stale PC14_OSC32_IN variable entry was generated")
            print(generated)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
