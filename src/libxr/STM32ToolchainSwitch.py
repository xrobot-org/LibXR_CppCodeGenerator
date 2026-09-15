#!/usr/bin/env python3
import argparse
import json
import os
import logging
import re
import sys

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

# Paths and constants
CMAKE_PRESETS_PATH = "CMakePresets.json"
CLANG_TOOLCHAIN = "cmake/starm-clang.cmake"
GCC_TOOLCHAIN = "cmake/gcc-arm-none-eabi.cmake"

# Mapping between command-line options and STARM configs
STD_MAP = {
    'g': 'STARM_HYBRID',
    'gnu': 'STARM_HYBRID',
    'hybrid': 'STARM_HYBRID',
    'n': 'STARM_NEWLIB',
    'newlib': 'STARM_NEWLIB',
    'p': 'STARM_PICOLIBC',
    'picolibc': 'STARM_PICOLIBC'
}

def patch_cmakepresets(compiler):
    """
    Patch the default toolchain in CMakePresets.json according to the given compiler.
    """
    if not os.path.exists(CMAKE_PRESETS_PATH):
        logging.error(f"{CMAKE_PRESETS_PATH} not found.")
        sys.exit(1)
    with open(CMAKE_PRESETS_PATH, "r", encoding="utf-8") as f:
        presets = json.load(f)
    default_preset = None
    for preset in presets.get("configurePresets", []):
        if preset.get("name") == "default":
            default_preset = preset
            break
    if not default_preset:
        logging.error("No 'default' preset found in CMakePresets.json!")
        sys.exit(1)
    if compiler == "gcc":
        new_toolchain = GCC_TOOLCHAIN
    elif compiler == "clang":
        new_toolchain = CLANG_TOOLCHAIN
    else:
        logging.error(f"Unsupported compiler: {compiler}")
        sys.exit(1)
    expected_value = "${sourceDir}/" + new_toolchain
    if default_preset.get("toolchainFile") != expected_value:
        logging.info(f"Switching toolchain in default preset to {new_toolchain}")
        default_preset["toolchainFile"] = expected_value
    else:
        logging.info(f"Toolchain in default preset already set to {new_toolchain}")
    with open(CMAKE_PRESETS_PATH, "w", encoding="utf-8") as f:
        json.dump(presets, f, indent=4)
        f.write('\n')
    logging.info("CMakePresets.json updated.")

def patch_clang_stdlib(starm_config):
    """
    Patch the starm-clang.cmake file to use the specified STARM_TOOLCHAIN_CONFIG value.
    """
    cmake_file = CLANG_TOOLCHAIN
    if not os.path.exists(cmake_file):
        logging.error(f"{cmake_file} not found.")
        sys.exit(1)
    with open(cmake_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    pat = re.compile(
        r'(set\s*\(\s*STARM_TOOLCHAIN_CONFIG\s+")([^"]+)(".*\))'
    )
    found = False
    for i, line in enumerate(lines):
        m = pat.search(line)
        if m:
            if m.group(2) == starm_config:
                logging.info(f"STARM_TOOLCHAIN_CONFIG already set to \"{starm_config}\".")
                found = True
                break
            else:
                lines[i] = pat.sub(rf'\1{starm_config}\3', line)
                found = True
                logging.info(f"Set STARM_TOOLCHAIN_CONFIG to \"{starm_config}\" in {cmake_file}")
                break
    if not found:
        logging.error(f"Could not find 'set(STARM_TOOLCHAIN_CONFIG ...)' in {cmake_file}")
        sys.exit(1)
    with open(cmake_file, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)
    logging.info(f"{cmake_file} updated.")

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Switch STM32 toolchain and clang standard library.\n"
            "Usage examples:\n"
            "  python switch_toolchain.py gcc\n"
            "  python switch_toolchain.py clang -g\n"
            "  python switch_toolchain.py clang --newlib\n"
            "  python switch_toolchain.py clang --picolibc"
        ),
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument('compiler', choices=['gcc', 'clang'], help='Compiler (gcc or clang)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-g', '--gnu', '--hybrid', dest='std', action='store_const', const='hybrid',
                       help='Use GNU(Hybrid) standard library')
    group.add_argument('-n', '--newlib', dest='std', action='store_const', const='newlib',
                       help='Use newlib standard library')
    group.add_argument('-p', '--picolibc', dest='std', action='store_const', const='picolibc',
                       help='Use picolibc standard library')
    args = parser.parse_args()
    compiler = args.compiler
    if compiler == "gcc":
        if args.std:
            logging.error("Standard library option (-g/-n/-p) cannot be used with gcc!")
            parser.print_usage()
            sys.exit(1)
        patch_cmakepresets("gcc")
    elif compiler == "clang":
        if not args.std:
            logging.error("Standard library option required for clang: -g/--gnu/--hybrid, -n/--newlib, -p/--picolibc")
            parser.print_usage()
            sys.exit(1)
        starm_config = STD_MAP[args.std]
        patch_cmakepresets("clang")
        patch_clang_stdlib(starm_config)
    logging.info("Done.")

if __name__ == "__main__":
    main()
