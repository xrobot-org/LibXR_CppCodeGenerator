#!/usr/bin/env python
"""libxr gen 的 STM32 生成器：由 libxr parse 写出的 CubeMX 工程 YAML 生成 LibXR 的 app_main 源文件。
The STM32 generator of libxr gen: generate the LibXR app_main source file from the CubeMX
project YAML that libxr parse writes.

同时生成 app_main.h 和 flash_map.hpp，并更新 libxr_config.yaml；已有 app_main 源文件中 User Code
区域的内容被保留。
It also generates app_main.h and flash_map.hpp and updates libxr_config.yaml; the User Code
bodies of an existing app_main source file are kept.
"""

import copy
import logging
import math
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, replace

import yaml
from xr_syntax.cpp import CppDocument, identifier_occurrences
from xr_syntax.i18n import tr

from libxr import libxr_config_file, update_notice
from libxr.cpp_layout import COLUMN_LIMIT, Braces, layout
from libxr.libxr_config_file import LibXRConfigError

# --------------------------
# 全局配置 / Global Configuration
# --------------------------
# 生成的设备对象及其 LibXR 接口，用 XR_REGISTER 登记。
# Generated device objects and their LibXR interface, registered with XR_REGISTER.
registered_devices = {"power_manager": "PowerManager"}
# 每个登记的名字由什么产生，用于冲突诊断。
# What produced each registered name, for collision diagnostics.
registered_origins = {}
# 本次生成的代码用到的驱动头文件和 HAL 句柄（类型，名字），入口源文件只 include 和声明它们。
# The driver headers and HAL handles (type, name) the generated code uses; the entry source
# includes and declares only these.
used_headers = set()
used_handles = set()
# 本次生成的 DMA 缓冲区，键为缓冲区名。
# The DMA buffers of this generation, keyed by buffer name.
dma_buffers = {}
# 本次生成是否带 --xrobot：不带时不被引用的通道引用用 UNUSED 标记。
# Whether this generation has --xrobot: without it, channel references nothing uses are
# marked with UNUSED.
generating_for_xrobot = False
# 生效设置的默认值；每次生成都从这里重新开始，再合并 libxr_config.yaml。
# Defaults of the effective settings; every generation starts again from here and then merges
# libxr_config.yaml.
DEFAULT_SETTINGS = {
    "terminal_source": "",
    "software_timer": {"priority": 2, "stack_depth": 1024},
    "SPI": {},
    "I2C": {},
    "USART": {},
    "ADC": {},
    "TIM": {},
    "CAN": {},
    "FDCAN": {},
    "USB": {},
    "Terminal": {
        "read_buff_size": 32,
        "max_line_size": 32,
        "max_arg_number": 5,
        "max_history_number": 5,
    },
    "database": {"enable": False, "block_size": "auto"},
    "SYSTEM": "None",
}
# 生效的设置：DEFAULT_SETTINGS 合并 libxr_config.yaml，生成过程中再补上缺少的默认值。
# The effective settings: DEFAULT_SETTINGS merged with libxr_config.yaml, completed with
# missing defaults during generation.
libxr_settings = copy.deepcopy(DEFAULT_SETTINGS)
# 已加载的 libxr_config.yaml 的往返文档（含注释和用户的键）。
# Round-trip document of the loaded libxr_config.yaml (comments, user keys).
libxr_config_document = None
# 生效设置来自的 libxr_config.yaml（路径或 URL）；设置无效时的报错写出它。
# The libxr_config.yaml, a path or URL, that the effective settings come from; errors about
# invalid settings name it.
libxr_config_origin = "libxr_config.yaml"
# 从 URL 下载 libxr_config.yaml 的时限（秒）。
# Time limit in seconds for downloading libxr_config.yaml from a URL.
CONFIG_DOWNLOAD_TIMEOUT = 30
# 工程的 RTOS 优先级数，generate() 用 read_rtos_priorities() 读出；不知道时为 None。
# The RTOS priority count of the project, which generate() reads with read_rtos_priorities();
# None when unknown.
rtos_priorities: int | None = None


# --------------------------
# 配置初始化 / Configuration Initialization
# --------------------------
def reset_settings() -> None:
    """把生效的设置恢复为 DEFAULT_SETTINGS，并丢掉已加载的 libxr_config.yaml 文档和它的来源以及
    RTOS 的优先级数，使同一进程中的下一次生成不带上一次的设置。
    Restore the effective settings to DEFAULT_SETTINGS and drop the loaded libxr_config.yaml
    document, its origin and the RTOS priority count, so the next generation in the same process
    carries nothing over.
    """
    global libxr_config_document, libxr_config_origin, libxr_config_label, rtos_priorities
    libxr_settings.clear()
    libxr_settings.update(copy.deepcopy(DEFAULT_SETTINGS))
    libxr_config_document = None
    libxr_config_origin = "libxr_config.yaml"
    libxr_config_label = "libxr_config.yaml"
    rtos_priorities = None


def initialize_registry(use_xrobot: bool) -> None:
    """清空生成对象的登记表以及本次生成用到的头文件、句柄和 DMA 缓冲区；use_xrobot 为真时先登记
    power_manager（PowerManager）。
    Reset the registry of generated objects and the headers, handles and DMA buffers of this
    generation; with use_xrobot, power_manager (PowerManager) is registered first.
    """
    global generating_for_xrobot
    generating_for_xrobot = use_xrobot
    registered_devices.clear()
    registered_origins.clear()
    used_headers.clear()
    used_handles.clear()
    dma_buffers.clear()
    if use_xrobot:
        _register_device("power_manager", "PowerManager", tr("power manager", "电源管理器"))


# --------------------------
# 设备登记 / Device Registration
# --------------------------
def _register_device(name: str, dev_type: str, origin: str = ""):
    """登记一个生成的对象及其 LibXR 接口类型；每个名字只登记一种类型。
    Record one generated object with its LibXR interface type; one name has exactly one
    registered type.

    Args:
        origin: 冲突信息中对该对象的描述；为空时为 "<dev_type> object"。
            How collision messages describe the object; "<dev_type> object" when empty.

    Raises:
        ValueError: 该名字已经登记。
            The name is already registered.
    """
    origin = origin or tr(f"{dev_type} object", f"{dev_type} 对象")
    if name in registered_devices:
        existing = registered_origins.get(name, registered_devices[name])
        raise ValueError(
            tr(
                f"Generated name '{name}' ({origin}) collides with the existing "
                f"'{name}' ({existing}); every generated object needs its own name",
                f"生成的名字 '{name}'（{origin}）与已有的 '{name}'（{existing}）冲突；"
                "每个生成的对象都需要自己的名字",
            )
        )
    registered_devices[name] = dev_type
    registered_origins[name] = origin


# 入口函数中一级缩进的空格。
# The spaces of one indent level in the entry function.
INDENT = "  "
# 说明注释中写出的 libxr_config.yaml 名字，generate() 按输出目录设置。
# The name of libxr_config.yaml written in the notice comment, set by generate() from the
# output directory.
libxr_config_label = "libxr_config.yaml"


def _use_header(header: str) -> None:
    """记录本次生成的代码用到了驱动头文件 header。
    Record that the code of this generation uses the driver header header.
    """
    used_headers.add(header)


def _use_handle(handle_type: str, name: str) -> None:
    """记录本次生成的代码用到了类型为 handle_type 的 HAL 句柄 name。
    Record that the code of this generation uses the HAL handle name of the type handle_type.
    """
    used_handles.add((handle_type, name))


def _generate_fdcan_can_alias(instance: str) -> str:
    """让 FDCAN 对象也能通过经典 CAN 接口使用，返回声明引用 canN 的 C++ 行。
    Expose an FDCAN object under the classic CAN interface as well and return the C++ line
    that declares the reference canN.

    fdcanN 仍登记为 LibXR::FDCAN；引用 canN 以 LibXR::CAN 指向同一对象，因此每个登记的名字只有
    一种类型。
    fdcanN stays registered as LibXR::FDCAN; the reference canN names the
    same object as LibXR::CAN, so each registered name keeps one type.

    Raises:
        ValueError: 实例名不是 fdcan<N> 形式，或 canN 已经登记。
            The instance name is not of the form fdcan<N>, or canN is already registered.
    """
    fdcan_name = instance.lower()
    match = re.fullmatch(r"fdcan(\d+)", fdcan_name)
    if match is None:
        raise ValueError(
            tr(
                f"Cannot derive the CAN alias of FDCAN instance '{instance}'",
                f"无法推导 FDCAN 实例 '{instance}' 的 CAN 别名",
            )
        )
    can_name = f"can{match.group(1)}"
    _register_device(
        can_name,
        "CAN",
        tr(f"LibXR::CAN alias of {fdcan_name}", f"{fdcan_name} 的 LibXR::CAN 别名"),
    )
    return f"{INDENT}LibXR::CAN& {can_name} = {fdcan_name};"


# --------------------------
# 外设实例生成 / Peripheral Instance Generation
# --------------------------
# 入口函数中的分节：生成方法返回的外设种类所属的节。
# The sections of the entry function: the section each peripheral kind that a generator method
# returns belongs to.
_PERIPHERAL_SECTIONS = {
    "adc": "adc",
    "dac": "dac",
    "pwm": "pwm",
    "spi": "comm",
    "uart": "comm",
    "i2c": "comm",
    "can": "comm",
    "watchdog": "watchdog",
    "usb": "usb",
}
# 通信外设一节的标题按这个顺序列出节中出现的种类。
# The title of the communication section lists the kinds it holds in this order.
_COMM_KINDS = (("spi", "SPI"), ("uart", "UART"), ("i2c", "I2C"), ("can", "CAN"))


def generate_peripheral_instances(project_data: dict, use_xrobot: bool = False) -> dict:
    """生成所有外设对象的构造代码，返回 {节: 带标题注释的各行}。
    Generate the construction code of all peripheral objects and return {section: lines with
    the title comment}.

    节为 adc、dac、pwm、comm（SPI、UART、I2C 和 CAN 对象，保持工程中的顺序）、usb 和 watchdog；
    没有对象的节不出现。每个 USB 设备自带一行说明注释，设备之间空一行。启用 XRobot 时每个
    FDCAN 对象另有一个 LibXR::CAN 引用；没有生成方法的外设类型不产生代码。
    The sections are adc, dac, pwm, comm (the SPI, UART, I2C and CAN objects in the order of the
    project), usb and watchdog; a section without objects is absent. Each USB device carries a
    comment line of its own, and devices are separated by a blank line. With XRobot each FDCAN
    object also gets a LibXR::CAN reference; peripheral types without a generator method
    produce no code.
    """
    blocks: dict[str, list[list[str]]] = {}
    comm_kinds = set()
    for p_type, instances in project_data.get("Peripherals", {}).items():
        for instance_name, config in instances.items():
            kind, lines = PeripheralFactory.create(p_type, instance_name, config)
            if not lines:
                continue
            if use_xrobot and p_type.upper() == "FDCAN":
                lines = lines + [_generate_fdcan_can_alias(instance_name)]
            section = _PERIPHERAL_SECTIONS[kind]
            blocks.setdefault(section, []).append(lines)
            if section == "comm":
                comm_kinds.add(kind)
    titles = {
        "adc": "ADC",
        "dac": "DAC",
        "pwm": "PWM",
        "comm": ", ".join(title for kind, title in _COMM_KINDS if kind in comm_kinds),
        "watchdog": "Watchdog",
    }
    sections = {}
    for section, section_blocks in blocks.items():
        if section == "usb":
            sections[section] = join_blocks(section_blocks)
        else:
            sections[section] = [f"{INDENT}// {titles[section]}"] + [
                line for block in section_blocks for line in block
            ]
    return sections


def join_blocks(blocks: list[list[str]]) -> list[str]:
    """把各块代码接成一组，块与块之间空一行。
    Join blocks of code into one list of lines with a blank line between blocks.
    """
    lines: list[str] = []
    for block in blocks:
        if lines:
            lines.append("")
        lines.extend(block)
    return lines


# --------------------------
# 配置加载 / Configuration Loading
# --------------------------
def load_configuration(file_path: str) -> dict:
    """读取工程 YAML，检查必需的 Mcu、GPIO 和 Peripherals 段，并返回其内容。
    Read the project YAML, check the required Mcu, GPIO and Peripherals sections and return
    its content.

    同时按 FreeRTOS 或 ThreadX 段设置 libxr_settings 的 SYSTEM，并删除空的外设条目。文件不存在、
    YAML 语法错误、内容不是映射或缺少必需段时记录错误并以状态 1 退出。
    It also sets SYSTEM in libxr_settings from the FreeRTOS or ThreadX section and deletes
    empty peripheral entries. A missing file, a YAML syntax error, content that is not a
    mapping or a missing section logs an error and exits with status 1.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

            if not isinstance(config, dict):
                raise ValueError(
                    tr(
                        f"{file_path} must contain a YAML mapping written by `libxr parse`",
                        f"{file_path} 的内容必须是 `libxr parse` 写出的 YAML 映射",
                    )
                )

            # 基本的结构检查
            # Basic schema validation
            required_sections = ["Mcu", "GPIO", "Peripherals"]
            for section in required_sections:
                if section not in config:
                    raise ValueError(
                        tr(
                            f"Missing required section: {section}",
                            f"缺少必需的段：{section}",
                        )
                    )

            # 检测 RTOS
            # Detect RTOS
            if "FreeRTOS" in config:
                libxr_settings["SYSTEM"] = "FreeRTOS"
                logging.info(tr("System: FreeRTOS", "系统：FreeRTOS"))
            elif "ThreadX" in config:
                libxr_settings["SYSTEM"] = "ThreadX"
                logging.info(tr("System: ThreadX", "系统：ThreadX"))
            else:
                libxr_settings["SYSTEM"] = "None"
                logging.info(tr("System: bare metal", "系统：裸机"))

            for key in [k for k, v in config["Peripherals"].items() if not v]:
                logging.info(
                    tr(f"Skipping empty peripheral config: {key}", f"跳过空的外设配置：{key}")
                )
                del config["Peripherals"][key]

            return config
    except FileNotFoundError:
        logging.error(
            tr(f"Configuration file not found: {file_path}", f"找不到配置文件：{file_path}")
        )
        sys.exit(1)
    except yaml.YAMLError as e:
        logging.error(tr(f"YAML syntax error: {str(e)}", f"YAML 语法错误：{str(e)}"))
        sys.exit(1)
    except ValueError as e:
        logging.error(tr(f"Configuration validation failed: {str(e)}", f"配置校验失败：{str(e)}"))
        sys.exit(1)


# --------------------------
# 库配置 / Library Configuration
# --------------------------
def load_libxr_config(output_dir: str, config_source: str) -> None:
    """把 output_dir 中的 libxr_config.yaml（或 --libxr-config 给出的路径或 URL）合并进生效的设置。
    Merge libxr_config.yaml in output_dir, or the path or URL given by --libxr-config, into the
    effective settings.

    文件中的 SYSTEM 被忽略，它由工程 YAML 决定；config_version 大于 1 时给出警告。URL 的下载
    时限为 CONFIG_DOWNLOAD_TIMEOUT 秒。没有配置文件时保留默认设置并使用新文档，其中只有固定为
    已安装 libxr 版本的 generator（包没有安装时为空文档）。已存在但无法读取或解析的配置会中止
    生成，而不是换用默认值。配置的路径或 URL 记为 libxr_config_origin，之后设置无效的报错都写出它。
    SYSTEM from the file is ignored because the project YAML decides it; a config_version
    above 1 is warned about. A URL download times out after CONFIG_DOWNLOAD_TIMEOUT seconds.
    Without a configuration file the defaults stay and a new document is used that holds only
    generator, pinned to the installed libxr version (an empty document when the package is
    not installed). A configuration that exists but cannot be read or parsed stops generation
    instead of falling back to the defaults. The path or URL of the configuration becomes
    libxr_config_origin, which every later error about an invalid setting names.

    Raises:
        LibXRConfigError: 配置无法下载、找到、读取或解析，或某个设置段不是映射。
            The configuration cannot be downloaded, located, read or parsed, or a settings
            section is not a mapping.
    """
    global libxr_settings, libxr_config_document, libxr_config_origin
    config_path = os.path.join(output_dir, "libxr_config.yaml")
    libxr_config_origin = config_source or config_path

    if config_source:
        if config_source.startswith("http://") or config_source.startswith("https://"):
            logging.info(
                tr(
                    f"Downloading libxr_config.yaml from {config_source}",
                    f"正在从 {config_source} 下载 libxr_config.yaml",
                )
            )
            try:
                with urllib.request.urlopen(
                    config_source, timeout=CONFIG_DOWNLOAD_TIMEOUT
                ) as response:
                    text = response.read().decode("utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise LibXRConfigError(
                    tr(
                        f"Cannot download {config_source}: {error}",
                        f"无法下载 {config_source}：{error}",
                    )
                ) from error
            document, saved_config = libxr_config_file.parse(text, config_source)
        elif os.path.exists(config_source):
            logging.info(
                tr(
                    f"Using external libxr_config.yaml from {config_source}",
                    f"使用外部的 libxr_config.yaml：{config_source}",
                )
            )
            document, saved_config = libxr_config_file.read(config_source)
        else:
            raise LibXRConfigError(
                tr(
                    f"Cannot locate config source: {config_source}",
                    f"找不到配置来源：{config_source}",
                )
            )
    elif os.path.exists(config_path):
        config_source = config_path
        document, saved_config = libxr_config_file.read(config_path)
    else:
        logging.info(
            tr(
                f"{config_path} does not exist; creating it with the default settings",
                f"{config_path} 不存在，按默认设置新建",
            )
        )
        libxr_config_document = libxr_config_file.new_document(update_notice.installed_version())
        if "generator" in libxr_config_document:
            libxr_settings["generator"] = libxr_config_document["generator"]
        return

    version = saved_config.get("config_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version > 1:
        logging.warning(
            tr(
                f"{config_source} has config_version {version!r}, but this libxr supports "
                "version 1; settings of a newer format may have no effect",
                f"{config_source} 的 config_version 是 {version!r}，本版本 libxr 只支持 1；"
                "较新格式的设置可能不起作用",
            )
        )
    saved_config.pop("SYSTEM", None)
    libxr_settings = _deep_merge(libxr_settings, saved_config)
    libxr_config_document = document


def _report_dropped_device_aliases(aliases) -> None:
    """以警告列出已移除的 device_aliases 表中与设备名不同的别名（别名 -> 设备），便于迁移配置。
    Log a warning that names every alias of the removed device_aliases table that differs from
    its device (alias -> device), for migrating configurations.

    与设备名相同的别名不列出：对象仍叫这个名字（5.x 的 --xrobot 默认每项只有设备名本身）。表中的
    别名都与设备名相同时只记录一条说明；表不是映射时列出表的 repr。
    An alias equal to its device name is not listed: the object still has that name (5.x with
    --xrobot wrote only the device name itself for each entry by default). When every alias of
    the table equals its device name, a single notice is logged; a table that is not a mapping
    is listed by its repr.
    """
    pairs = []
    if isinstance(aliases, dict):
        for device, entry in aliases.items():
            names = entry.get("aliases", []) if isinstance(entry, dict) else entry
            if isinstance(names, str):
                names = [names]
            for name in names or []:
                if str(name) != str(device):
                    pairs.append(f"{name} -> {device}")
        if not pairs:
            logging.info(
                tr(
                    "libxr_config.yaml: removed device_aliases, which is no longer used; each "
                    "alias in it equals the name of its object, so nothing needs to change",
                    "libxr_config.yaml：已删除不再使用的 device_aliases；其中的别名都与对象名相同，"
                    "无需改动",
                )
            )
            return
    logging.warning(
        tr(
            "Removed the legacy device_aliases table from libxr_config.yaml; generated "
            "objects are registered only under their own names. Update configurations "
            "that used these aliases (alias -> device):",
            "已从 libxr_config.yaml 中删除旧的 device_aliases 表；"
            "生成的对象只以自己的名字登记。请更新使用了以下别名的配置（别名 -> 设备）：",
        )
    )
    for pair in pairs or [repr(aliases)]:
        logging.warning(f"  {pair}")


def save_libxr_config(config_path: str) -> bool:
    """把 libxr_config_text() 写入 config_path；内容相同时不写。写入时为 True。
    Write libxr_config_text() to config_path, unless the file already holds it; True when
    written.
    """
    return _write_if_changed(config_path, libxr_config_text())


def _write_if_changed(path: str, text: str) -> bool:
    """以 UTF-8 和 LF 换行把 text 写入 path；文件内容已相同时不写，修改时间不变。写入时为 True。
    Write text to path as UTF-8 with LF line endings; a file that already holds it is left
    alone, keeping its modification time. True when written.
    """
    data = text.encode("utf-8")
    try:
        with open(path, "rb") as stream:
            if stream.read() == data:
                return False
    except FileNotFoundError:
        pass
    with open(path, "wb") as stream:
        stream.write(data)
    return True


def libxr_config_text() -> str:
    """libxr_config.yaml 的新内容：生效的设置，去掉生成器补出来的空段和旧的 device_aliases 表。
    The new content of libxr_config.yaml: the effective settings without the empty sections the
    generator added and without the legacy device_aliases table.

    生成器不解释的键（例如 ``generator`` 版本固定项）、文件里原有的键（包括空映射）和注释被保留；
    device_aliases 中的别名以警告列出。
    Keys the generator does not interpret (such as the ``generator`` pin), keys already in the
    file, empty mappings included, and comments are kept; the aliases of device_aliases are
    listed in a warning.
    """
    # device_aliases 是旧的运行时别名表，已不再使用。
    # device_aliases was the legacy runtime alias table; it is no longer used.
    if "device_aliases" in libxr_settings:
        _report_dropped_device_aliases(libxr_settings["device_aliases"])
    document = libxr_config_document
    if document is None:
        document = libxr_config_file.new_document()
    cleaned_config = {}
    for key, value in libxr_settings.items():
        if key == "device_aliases":
            continue
        if isinstance(value, dict) and not value:
            # 生成器补出来的空段不写；文件里原有的键保留，只写了 "KEY:" 的仍写成 null。
            # Empty sections the generator added are left out; keys already in the file
            # stay, and a bare "KEY:" stays null.
            if key not in document:
                continue
            if document[key] is None:
                value = None
        cleaned_config[key] = value
    libxr_config_file.update(document, cleaned_config)
    return libxr_config_file.dump(document)


def _deep_merge(base: dict, update: dict, path: str = "") -> dict:
    """把 update 递归合并进 base 并返回 base；base 中的设置段逐键合并，其他值直接覆盖，设置段
    对应的 null 视为空映射。单个值的类型在生成时用到它的地方检查。
    Merge update into base recursively and return base; settings sections of base are merged
    key by key, other values overwrite, and a null meeting a settings section counts as an empty
    mapping. The type of a single value is checked where generation uses it.

    Args:
        path: base 在设置中的位置，例如 Terminal；报错时写出完整的键。
            Where base sits in the settings, for example Terminal; errors name the full key.

    Raises:
        LibXRConfigError: base 中的设置段在 update 中不是映射，例如 USART: 5。
            A settings section of base is not a mapping in update, for example USART: 5.
    """
    for key, value in update.items():
        full_key = f"{path}.{key}" if path else str(key)
        section = base.get(key)
        if not isinstance(section, dict):
            base[key] = value
        elif isinstance(value, dict):
            _deep_merge(section, value, full_key)
        # 空的段（例如只写了 "I2C:"）等同于空映射。
        # An empty section such as a bare "I2C:" counts as an empty mapping.
        elif value is not None:
            raise _invalid_setting(full_key, value, "a mapping", "映射")
    return base


# --------------------------
# GPIO 配置 / GPIO Configuration
# --------------------------
def _sanitize_cpp_identifier(name: str) -> str:
    """把名字转换为 C++ 标识符：非单词字符替换为下划线，以数字开头时在前面加下划线。
    Turn a name into a C++ identifier: non-word characters become underscores, and a leading
    digit gets an underscore in front.
    """
    return re.sub(r"\W|^(?=\d)", "_", name)


CPP_KEYWORDS = frozenset(
    [
        "alignas",
        "alignof",
        "and",
        "and_eq",
        "asm",
        "auto",
        "bitand",
        "bitor",
        "bool",
        "break",
        "case",
        "catch",
        "char",
        "char8_t",
        "char16_t",
        "char32_t",
        "class",
        "compl",
        "concept",
        "const",
        "consteval",
        "constexpr",
        "constinit",
        "const_cast",
        "continue",
        "co_await",
        "co_return",
        "co_yield",
        "decltype",
        "default",
        "delete",
        "do",
        "double",
        "dynamic_cast",
        "else",
        "enum",
        "explicit",
        "export",
        "extern",
        "false",
        "float",
        "for",
        "friend",
        "goto",
        "if",
        "inline",
        "int",
        "long",
        "mutable",
        "namespace",
        "new",
        "noexcept",
        "not",
        "not_eq",
        "nullptr",
        "operator",
        "or",
        "or_eq",
        "private",
        "protected",
        "public",
        "register",
        "reinterpret_cast",
        "requires",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "static_assert",
        "static_cast",
        "struct",
        "switch",
        "template",
        "this",
        "thread_local",
        "throw",
        "true",
        "try",
        "typedef",
        "typeid",
        "typename",
        "union",
        "unsigned",
        "using",
        "virtual",
        "void",
        "volatile",
        "wchar_t",
        "while",
        "xor",
        "xor_eq",
    ]
)

# CMSIS/HAL 的对象式宏；与之同名的 GPIO 对象名会被预处理器展开。
# Object-like CMSIS/HAL macros a GPIO object name would be expanded into.
_CMSIS_INSTANCE_MACRO = re.compile(
    r"GPIO[A-Z]|(?:ADC|DAC|TIM|LPTIM|HRTIM|SPI|I2S|I2C|I3C|USART|UART|LPUART|"
    r"CAN|FDCAN|DMA|BDMA|GPDMA|HPDMA|LPDMA|MDMA|DMAMUX|DMA2D|SAI|SDMMC|SDIO|"
    r"QUADSPI|OCTOSPI|OCTOSPIM|XSPI|FMC|FSMC|COMP|OPAMP|DFSDM|MDF|ADF|IWDG|"
    r"WWDG|RTC|TAMP|CRC|RNG|HASH|CRYP|AES|SAES|PKA|ETH|LTDC|DCMI|DCMIPP|PSSI|"
    r"USB_OTG_FS|USB_OTG_HS|USB|UCPD|TSC|LCD|CEC|SPDIFRX|SWPMI|MDIOS|RCC|PWR|"
    r"FLASH|EXTI|SYSCFG|DBGMCU|SCB|NVIC|SysTick|MPU|FPU|ITM|DWT|CoreDebug|TPI|"
    r"ICACHE|DCACHE|GTZC|VREFBUF|CORDIC|FMAC|JPEG|RAMCFG|OTFDEC|IPCC|HSEM)\d*"
)
_HAL_MACROS = frozenset({"NULL", "UNUSED", "UID_BASE"})


def _gpio_object_name(port: str, gpio_data: dict) -> str:
    """GPIO 对象的 C++ 名字：CubeMX 标签，没有标签时为 GPIO 段中的引脚键，经
    _sanitize_cpp_identifier() 处理。
    The C++ name of a GPIO object: its CubeMX label, or its pin key in the GPIO section without
    a label, passed through _sanitize_cpp_identifier().
    """
    return _sanitize_cpp_identifier(gpio_data.get("Label", "") or port)


def overused_names(names, generated_code: str, use_xrobot: bool) -> list[str]:
    """names 中在生成代码里出现次数超过声明（加 --xrobot 时的 XR_REGISTER 行）的名字：它们与
    生成代码用到的其他名字相同。注释和字符串里的不算；各平台的 GPIO 名字检查共用。
    The names among names that occur in the generated code more often than their declaration
    (plus the XR_REGISTER line with --xrobot): they equal another name the generated code uses.
    Comments and strings do not count; the GPIO name checks of every platform share this.
    """
    counts: dict[str, int] = {}
    for occurrence in identifier_occurrences(generated_code):
        counts[occurrence.text] = counts.get(occurrence.text, 0) + 1
    expected_uses = 2 if use_xrobot else 1
    return [name for name in names if counts.get(name, 0) > expected_uses]


def check_gpio_names(project_data: dict, generated_code: str, use_xrobot: bool) -> None:
    """拒绝生成的 app_main 无法声明的 GPIO 对象名。
    Reject GPIO object names that the generated app_main cannot declare.

    GPIO 对象以其 CubeMX 标签命名。标签若是 C++ 关键字或保留标识符、CMSIS/HAL 宏或 IRQ 名、
    CubeMX 由其他标签派生的宏，或生成代码中用到的其他名字，就会编译失败或在 app_main 中静默遮蔽
    该名字。
    A GPIO object is named after its CubeMX label. A label that is a C++
    keyword or reserved identifier, a CMSIS/HAL macro or IRQ name, a macro CubeMX derives
    from another label, or any other name the generated code uses would fail
    to compile or silently shadow that name inside app_main.

    Raises:
        ValueError: 至少一个 GPIO 名字有上述问题；信息列出全部问题。
            At least one GPIO name has one of these problems; the message lists all of them.
    """
    gpio = project_data.get("GPIO", {})
    label_macros = {}
    for data in gpio.values():
        label = data.get("Label", "")
        if label:
            for suffix in ("_Pin", "_GPIO_Port", "_EXTI_IRQn"):
                label_macros[f"{label}{suffix}"] = label
    counts = {}
    for occurrence in identifier_occurrences(generated_code):
        counts[occurrence.text] = counts.get(occurrence.text, 0) + 1
    # 声明处出现一次，启用 --xrobot 时 XR_REGISTER 行再出现一次。
    # Declaration, plus the XR_REGISTER line with --xrobot.
    expected_uses = 2 if use_xrobot else 1
    problems = []
    for port, data in gpio.items():
        name = _gpio_object_name(port, data)
        pin = port.split("-")[0]
        where = tr(f"GPIO object '{name}' (pin {pin})", f"GPIO 对象 '{name}'（引脚 {pin}）")
        if name in CPP_KEYWORDS:
            problems.append(tr(f"{where} is a C++ keyword", f"{where}是 C++ 关键字"))
        elif "__" in name or re.match(r"_[A-Z]", name):
            problems.append(
                tr(f"{where} is a reserved C++ identifier", f"{where}是 C++ 保留标识符")
            )
        elif name in label_macros:
            label = label_macros[name]
            problems.append(
                tr(
                    f"{where} is the CubeMX macro of GPIO label '{label}'",
                    f"{where}是 GPIO 标签 '{label}' 的 CubeMX 宏",
                )
            )
        elif name in _HAL_MACROS or name.endswith("_IRQn") or _CMSIS_INSTANCE_MACRO.fullmatch(name):
            problems.append(
                tr(f"{where} is a CMSIS/HAL macro or IRQ name", f"{where}是 CMSIS/HAL 宏或 IRQ 名")
            )
        elif counts.get(name, 0) > expected_uses:
            problems.append(
                tr(
                    f"{where} collides with a name the generated code uses",
                    f"{where}与生成代码使用的名字冲突",
                )
            )
    if problems:
        raise ValueError(
            tr(
                "rename these GPIO labels in CubeMX:\n  ",
                "请在 CubeMX 中重命名以下 GPIO 标签：\n  ",
            )
            + "\n  ".join(problems)
        )


def generate_gpio_declaration(
    port: str, gpio_data: dict, project_data: dict
) -> tuple[str, list[str]]:
    """生成一个 GPIO 对象的名字和构造参数 ``(port, pin[, irq])`` 并登记该对象。
    Generate the name and the constructor arguments ``(port, pin[, irq])`` of one GPIO object
    and register the object.

    有标签时使用 CubeMX 的 <label>_GPIO_Port 和 <label>_Pin 宏；配置为 EXTI 的引脚另带中断号。
    With a label the CubeMX macros <label>_GPIO_Port and <label>_Pin are used; a pin configured
    for EXTI also gets its IRQ number.
    """
    base_port = port.split("-")[0]
    port_define = f"GPIO{base_port[1]}"
    pin_num = int(base_port[2:])
    pin_define = f"GPIO_PIN_{pin_num}"
    label = gpio_data.get("Label", "")

    if label:
        port_define = f"{label}_GPIO_Port"
        pin_define = f"{label}_Pin"

    irq_define = _get_exti_irq(
        pin_num,
        base_port,
        gpio_data.get("GPXTI", False),
        project_data.get("Mcu", {}).get("Family", "STM32F4"),
        project_data.get("Mcu", {}).get("Type") or "",
    )
    arguments = [port_define, pin_define] + ([irq_define] if irq_define else [])

    var_name = _gpio_object_name(port, gpio_data)

    _register_device(
        var_name,
        "GPIO",
        tr(f"GPIO label {label} on {base_port}", f"{base_port} 上的 GPIO 标签 {label}")
        if label
        else f"GPIO {base_port}",
    )

    return var_name, arguments


# 各 CubeMX 系列（Mcu.Family）的 EXTI 中断向量，取自器件的向量表。
# 以下系列共用 EXTI0_1/EXTI2_3/EXTI4_15：
# EXTI interrupt vectors per CubeMX family (Mcu.Family), from the device
# vector tables. These families share EXTI0_1/EXTI2_3/EXTI4_15:
_EXTI_SHARED_LINE_FAMILIES = frozenset({"STM32F0", "STM32G0", "STM32L0", "STM32C0", "STM32U0"})
# 以下系列每条线一个向量，即 EXTI0_IRQn..EXTI15_IRQn（STM32H7 系列中的 STM32H7R/S 器件
# 也是如此）；其余系列共用 EXTI9_5 和 EXTI15_10。
# These have one vector per line, EXTI0_IRQn..EXTI15_IRQn (as do the STM32H7R/S
# parts of the STM32H7 family); the others share EXTI9_5 and EXTI15_10.
_EXTI_PER_LINE_FAMILIES = frozenset(
    {"STM32H5", "STM32U3", "STM32U5", "STM32L5", "STM32WBA", "STM32N6"}
)


def _get_exti_irq(
    pin_num: int, port: str, is_exti: bool, mcu_family: str, mcu_type: str = ""
) -> str:
    """按 MCU 系列返回引脚的 EXTI 中断号名字；不是 EXTI 引脚时为空字符串。
    The EXTI IRQ name of a pin for its MCU family; an empty string when the pin is not an EXTI
    pin.

    STM32WB0 的 PA/PB 引脚使用 GPIOA_IRQn/GPIOB_IRQn；共享向量的系列使用 EXTI0_1_IRQn、
    EXTI2_3_IRQn 和 EXTI4_15_IRQn；每线一个向量的系列以及 STM32H7R/S 使用 EXTI<n>_IRQn；其余系列
    的 0~4 线使用 EXTI<n>_IRQn，5~9 线和 10~15 线分别使用 EXTI9_5_IRQn 和 EXTI15_10_IRQn。
    STM32WB0 PA/PB pins use GPIOA_IRQn/GPIOB_IRQn; the shared-vector families use EXTI0_1_IRQn,
    EXTI2_3_IRQn and EXTI4_15_IRQn; the per-line families and STM32H7R/S use EXTI<n>_IRQn; the
    other families use EXTI<n>_IRQn for lines 0 to 4, EXTI9_5_IRQn for lines 5 to 9 and
    EXTI15_10_IRQn for lines 10 to 15.
    """
    if not is_exti:
        return ""

    if mcu_family.startswith("STM32WB0"):
        if port.startswith("PA"):
            return "GPIOA_IRQn"
        elif port.startswith("PB"):
            return "GPIOB_IRQn"

    if mcu_family in _EXTI_SHARED_LINE_FAMILIES:
        if pin_num <= 1:
            return "EXTI0_1_IRQn"
        if pin_num <= 3:
            return "EXTI2_3_IRQn"
        return "EXTI4_15_IRQn"
    elif mcu_family in _EXTI_PER_LINE_FAMILIES or mcu_type.startswith(("STM32H7R", "STM32H7S")):
        return f"EXTI{pin_num}_IRQn"
    else:
        if 5 <= pin_num <= 9:
            return "EXTI9_5_IRQn"
        if 10 <= pin_num <= 15:
            return "EXTI15_10_IRQn"
        return f"EXTI{pin_num}_IRQn"


# --------------------------
# DMA 配置 / DMA Configuration
# --------------------------
DMA_DEFAULT_SIZES = {
    "SPI": {"tx": 32, "rx": 32},
    "USART": {"tx": 128, "rx": 128},
    "I2C": {"buffer": 32},
    "ADC": {"buffer": 32},
}

# CubeMX 的 USB 实例名到规范名；USB 和 USB_DRD_FS（FSDEV）视为 USB_FS。
# CubeMX USB instance names to their normalized names; USB and USB_DRD_FS (FSDEV) are USB_FS.
_USB_INSTANCES = {
    "USB": "USB_FS",
    "USB_FS": "USB_FS",
    "USB_DRD_FS": "USB_FS",
    "USB_HS": "USB_HS",
    "USB_OTG_FS": "USB_OTG_FS",
    "USB_OTG_HS": "USB_OTG_HS",
}


def _pcd_handle(instance: str, config: dict) -> str | None:
    """USB 实例的 PCD 句柄名，例如 hpcd_USB_FS、hpcd_USB_OTG_HS；USB_DRD_FS 系列取 parse 记下的
    PCDHandle（hpcd_USB_DRD_FS）。中间件等其他实例名和主机模式（Role 为 Host）的实例为 None。
    The PCD handle name of a USB instance, such as hpcd_USB_FS or hpcd_USB_OTG_HS; families with
    USB_DRD_FS take the PCDHandle parse recorded (hpcd_USB_DRD_FS). Other instance names, such
    as middleware, and instances in host mode (Role Host) give None.
    """
    name = _USB_INSTANCES.get((instance or "").upper())
    if name is None or config.get("Role") == "Host":
        return None
    return config.get("PCDHandle") or f"hpcd_{name}"


def _invalid_setting(key: str, value, english: str, chinese: str) -> LibXRConfigError:
    """设置 key 的值 value 无效时的错误；信息写出 libxr_config_origin、key、value 和应有的值。
    The error for the invalid value of the setting key; the message names libxr_config_origin,
    key, value and what the value must be.
    """
    return LibXRConfigError(
        tr(
            f"{libxr_config_origin}: {key} {value!r} is not {english}",
            f"{libxr_config_origin}：{key} {value!r} 不是{chinese}",
        )
    )


def _settings(*keys: str) -> dict:
    """libxr_settings 中按 keys 逐层找到的设置段；缺少的段和空值（YAML 中只写了键）改为空映射。
    The settings section reached through keys in libxr_settings, level by level; a missing
    section or an empty value (only the key written in YAML) becomes an empty mapping.

    Raises:
        LibXRConfigError: 路径上的某个值不是映射；信息中写出到它为止的键。
            A value on the way is not a mapping; the message names the keys up to it.
    """
    section = libxr_settings
    for depth, key in enumerate(keys):
        value = section.get(key)
        if value is None:
            value = section[key] = {}
        elif not isinstance(value, dict):
            raise _invalid_setting(".".join(keys[: depth + 1]), value, "a mapping", "映射")
        section = value
    return section


def _flag(key: str, value) -> bool:
    """设置 key 的开关值：YAML 的 true/false、yes/no 或 on/off。
    The on/off value of the setting key: YAML true/false, yes/no or on/off.

    Raises:
        LibXRConfigError: value 不是布尔值，例如加了引号的 "false"；信息中写出 key。
            value is not a boolean, for example a quoted "false"; the message names key.
    """
    if isinstance(value, bool):
        return value
    raise _invalid_setting(key, value, "true or false", "布尔值（true 或 false）")


def _text(key: str, value) -> str:
    """设置 key 的文本值；空值（YAML 中只写了键）为空字符串。
    The text value of the setting key; an empty value (only the key written in YAML) is an
    empty string.

    Raises:
        LibXRConfigError: value 不是字符串；信息中写出 key。数字也不接受，例如 serial: 0001
            读出的是 1。
            value is not a string; the message names key. Numbers are refused as well:
            serial: 0001 reads as 1.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise _invalid_setting(key, value, "a string", "字符串")


def _integer(key: str, value, minimum: int = 1, maximum: int | None = None) -> int:
    """设置 key 的整数值；字符串按 Python 整数字面量解析（如 0x1D50）。
    The integer value of the setting key; a string is read as a Python integer literal, such
    as 0x1D50.

    数值写进生成的 C++ 代码，所以在这里检查，而不是等到编译时。
    The value goes into the generated C++ code, so it is checked here rather than at compile
    time.

    Raises:
        LibXRConfigError: value 不是整数，或不在 minimum 到 maximum 之间；信息中写出 key。
            value is not an integer, or lies outside minimum to maximum; the message names key.
    """
    number = None
    if not isinstance(value, bool):
        if isinstance(value, int):
            number = value
        else:
            try:
                number = int(str(value).strip(), 0)
            except ValueError:
                number = None
    if number is not None and number >= minimum and (maximum is None or number <= maximum):
        return number
    if maximum is not None:
        english, chinese = (
            f"an integer from {minimum} to {maximum}",
            f"{minimum} 到 {maximum} 的整数",
        )
    elif minimum == 1:
        english, chinese = "a positive integer", "正整数"
    else:
        english, chinese = "a non-negative integer", "非负整数"
    raise _invalid_setting(key, value, english, chinese)


def _number(key: str, value) -> float | int:
    """设置 key 的数值（整数或小数）；字符串按小数解析。
    The numeric value, integer or decimal, of the setting key; a string is read as a decimal.

    Raises:
        LibXRConfigError: value 不是有限的数；信息中写出 key。
            value is not a finite number; the message names key.
    """
    if not isinstance(value, bool):
        number = value
        if not isinstance(value, (int, float)):
            try:
                number = float(str(value).strip())
            except ValueError:
                number = None
        if number is not None and math.isfinite(number):
            return number
    raise _invalid_setting(key, value, "a number", "数字")


# CubeMX 的这些中间件自己使用 USB 外设的句柄，与 LibXR 的 USB 设备不能同时使用。
# These CubeMX middlewares use the handle of the USB peripheral themselves and cannot be used
# together with the LibXR USB device.
_USB_MIDDLEWARE = ("USB_DEVICE", "USB_HOST", "USBX")
# USB 段中不是 USB 外设实例的名字：CubeMX 的中间件和 USB PD（USBPD），不生成 USB 设备也不警告。
# The names in the USB section that are not USB peripheral instances: the CubeMX middleware and
# USB PD (USBPD); they get no USB device and no warning.
_NOT_USB_DEVICES = (*_USB_MIDDLEWARE, "USBPD")


def _default_usb_enables(peripherals: dict) -> None:
    """为 libxr_config 中还没有 enable 的 USB 实例写入默认值。
    Write the default enable of the USB instances that have none in libxr_config.

    CubeMX 中不是主机模式的 USB 实例默认启用；工程启用了 CubeMX 的 USB 中间件时都不启用，并记录
    一条说明。既不是 _USB_INSTANCES 中的实例、也不是中间件（_NOT_USB_DEVICES）的名字记录一条警告，
    这样的实例不生成对象。
    A USB instance that is not in host mode in CubeMX is enabled by default; when the project
    enables a CubeMX USB middleware, none is, and a notice says why. A name that is neither an
    instance of _USB_INSTANCES nor a middleware (_NOT_USB_DEVICES) is warned about; no object is
    generated for it.
    """
    usb = peripherals.get("USB", {})
    middleware = sorted(name for name in usb if name.upper() in _USB_MIDDLEWARE)
    for instance, config in usb.items():
        name = _USB_INSTANCES.get(instance.upper())
        if name is None:
            if instance.upper() not in _NOT_USB_DEVICES:
                known = ", ".join(_USB_INSTANCES)
                logging.warning(
                    tr(
                        f"USB instance '{instance}' is not one the generator knows ({known}); "
                        "no USB device is generated for it",
                        f"生成器不认识 USB 实例 '{instance}'（可识别的实例为 {known}），"
                        "不为它生成 USB 设备",
                    )
                )
            continue
        settings = _settings("USB", name.lower())
        if "enable" in settings:
            continue
        settings["enable"] = not middleware and config.get("Role") != "Host"
        if middleware:
            logging.info(
                tr(
                    f"USB.{name.lower()}.enable defaults to false: the CubeMX middleware "
                    f"{', '.join(middleware)} uses the USB peripheral",
                    f"USB.{name.lower()}.enable 默认为 false：CubeMX 的中间件 "
                    f"{'、'.join(middleware)} 使用了这个 USB 外设",
                )
            )


def _usb_settings(instance: str) -> tuple[str, dict] | None:
    """USB 实例的规范名和它在 libxr_settings["USB"] 中的设置；其他实例名（如 USB_DEVICE 中间件）
    为 None。
    The normalized name of a USB instance and its settings in libxr_settings["USB"]; None for
    any other instance name, such as the USB_DEVICE middleware.

    enable 的默认值由 _default_usb_enables() 先行写入，仍然缺少时为 false；未启用的实例不再补其他
    设置。已启用的实例按固定顺序补上缺少的设置：包大小、缓冲区和 FIFO 大小、dma_section、cdc 列表
    （每路 CDC 一项，含 tx_fifo_size、rx_fifo_size、queue_size 和可选的 interface，默认一路），
    以及描述符（默认
    1d50:6199 / 0x0100 / "XRUSB-DEMO-"，1d50:6199 的分配记录见
    https://github.com/openmoko/openmoko-usb-oui/commit/27f3846d77e0d0d10271b809b831f70040c6197a）。
    旧版的 cdc_tx_fifo_size、cdc_rx_fifo_size 和 cdc_queue_size 转换为 cdc 的一项，并在原来的位置
    写回。ep0_packet_size 不是 8、16、32、64 时给出警告并改为 8；其余大小必须是正整数，vid、pid、
    bcd 必须在 0 到 0xFFFF 之间。
    _default_usb_enables() writes the default enable first; when it is still missing it is
    false, and a disabled instance gets no other setting. An enabled instance gets its missing
    settings in a fixed order: packet size, buffer and FIFO sizes, dma_section, the cdc list (one
    item per CDC with tx_fifo_size, rx_fifo_size, queue_size and an optional interface, one CDC
    by default), and the
    descriptor (default 1d50:6199 / 0x0100 / "XRUSB-DEMO-"; see the link above for the
    allocation of 1d50:6199). The earlier cdc_tx_fifo_size, cdc_rx_fifo_size and
    cdc_queue_size become one item of cdc, written back where they were. An ep0_packet_size
    other than 8, 16, 32 or 64 is warned about and becomes 8; the other sizes must be positive
    integers, vid, pid and bcd must lie between 0 and 0xFFFF, and dma_section and the
    descriptor strings must be strings.

    Raises:
        LibXRConfigError: 实例的设置不是映射或 enable 不是布尔值；已启用的实例设置了 cdc_count
            （CDC 的路数由 cdc 列表给出），cdc 不是映射的非空列表，CDC 的路数超过端点号 EP1 到 EP15
            能容纳的 7 路，OTG 的 rx_fifo_size 容不下每路 CDC 的 OUT 端点，或某个大小、描述符数值或
            字符串无效。
            The settings of the instance are not a mapping or enable is not a boolean; an
            enabled instance sets cdc_count (the cdc list gives the number of CDCs), cdc is
            not a non-empty list of mappings, the number of CDCs exceeds the 7 that the endpoint
            numbers EP1 to EP15 hold, the rx_fifo_size of an OTG device cannot hold the OUT
            endpoint of each CDC, or a size, descriptor number or string is invalid.
    """
    name = _USB_INSTANCES.get((instance or "").upper())
    if name is None:
        return None
    prefix = f"USB.{name.lower()}"
    cfg = _settings("USB", name.lower())
    if not _flag(f"{prefix}.enable", cfg.setdefault("enable", False)):
        return name, cfg
    if "cdc_count" in cfg:
        raise LibXRConfigError(
            tr(
                f"{libxr_config_origin}: {prefix}.cdc_count is not a generator option; list the "
                f"CDCs under {prefix}.cdc",
                f"{libxr_config_origin}：{prefix}.cdc_count 不是生成器选项；请在 {prefix}.cdc 中"
                "列出各路 CDC",
            )
        )
    _convert_legacy_cdc(prefix, cfg)
    try:
        ep0 = _integer("ep0_packet_size", cfg.get("ep0_packet_size", 8))
    except ValueError:
        ep0 = None
    if ep0 in (8, 16, 32, 64):
        cfg.setdefault("ep0_packet_size", ep0)
    else:
        logging.warning(
            tr(
                f"USB {name.lower()}: ep0_packet_size {cfg['ep0_packet_size']} is not 8, 16, 32 "
                "or 64; using 8",
                f"USB {name.lower()}：ep0_packet_size {cfg['ep0_packet_size']} 不是 8、16、32 "
                "或 64，改用 8",
            )
        )
        cfg["ep0_packet_size"] = 8
    is_otg = name.startswith("USB_OTG_")
    defaults = {
        "tx_buffer_size": 128,
        "rx_buffer_size": 128,
        "rx_fifo_size": 256 if is_otg else 128,
        "tx_fifo_size": 128,
        "dma_section": "",
        "cdc": [dict(_CDC_DEFAULTS)],
        "vid": 0x1D50,
        "pid": 0x6199,
        "bcd": 0x0100,
        "manufacturer": "XRobot",
        "product": f"STM32 XRUSB {instance} CDC Demo",
        "serial": "XRUSB-DEMO-",
    }
    for key, value in defaults.items():
        cfg.setdefault(key, value)
    for key in (
        "tx_buffer_size",
        "rx_buffer_size",
        "rx_fifo_size",
        "tx_fifo_size",
    ):
        _integer(f"{prefix}.{key}", cfg[key])
    _check_cdc(prefix, cfg, is_otg)
    for key in ("vid", "pid", "bcd"):
        _integer(f"{prefix}.{key}", cfg[key], 0, 0xFFFF)
    for key in ("dma_section", "manufacturer", "product", "serial"):
        cfg[key] = _text(f"{prefix}.{key}", cfg[key])
    return name, cfg


# 一路 CDC 的设置及默认值：发送和接收队列的容量，以及发送请求队列的容量。
# The settings of one CDC and their defaults: the capacity of the transmit and receive queues
# and of the transmit request queue.
_CDC_DEFAULTS = {"tx_fifo_size": 128, "rx_fifo_size": 128, "queue_size": 3}
# 旧版单路 CDC 的设置对应 cdc 列表项中的键。
# The earlier settings of the single CDC and the keys of a cdc item they correspond to.
_LEGACY_CDC_KEYS = {
    "cdc_tx_fifo_size": "tx_fifo_size",
    "cdc_rx_fifo_size": "rx_fifo_size",
    "cdc_queue_size": "queue_size",
}
# 端点号 EP1 到 EP15 最多容纳的 CDC 路数：每路占两个 IN 端点号。
# The most CDCs the endpoint numbers EP1 to EP15 hold: each takes two IN endpoint numbers.
_MAX_CDC = 7


def _convert_legacy_cdc(prefix: str, cfg: dict) -> None:
    """把旧版的 cdc_tx_fifo_size、cdc_rx_fifo_size 和 cdc_queue_size 转换为 cdc 列表的一项。
    Convert the earlier cdc_tx_fifo_size, cdc_rx_fifo_size and cdc_queue_size into one item of
    the cdc list.

    新的 cdc 写在第一个旧键的位置，缺少的值取默认值并记录一条说明；已有 cdc 时旧键被忽略，记录
    一条警告。两种情况下旧键都被删除，写回的 libxr_config.yaml 只有新写法。
    The new cdc takes the place of the first old key, missing values take their defaults, and a
    notice is logged; with a cdc already present the old keys are ignored, with a warning. In
    both cases the old keys are removed, so the written libxr_config.yaml has only the new form.
    """
    legacy = [key for key in _LEGACY_CDC_KEYS if key in cfg]
    if not legacy:
        return
    names = ", ".join(f"{prefix}.{key}" for key in legacy)
    if "cdc" in cfg:
        logging.warning(
            tr(
                f"{libxr_config_origin}: {names} ignored and removed: {prefix}.cdc lists the CDCs",
                f"{libxr_config_origin}：已忽略并删除 {names}：各路 CDC 由 {prefix}.cdc 给出",
            )
        )
        for key in legacy:
            del cfg[key]
        return
    item = {new: cfg.get(old, _CDC_DEFAULTS[new]) for old, new in _LEGACY_CDC_KEYS.items()}
    entries = list(cfg.items())
    first = next(index for index, (key, _) in enumerate(entries) if key in _LEGACY_CDC_KEYS)
    cfg.clear()
    for index, (key, value) in enumerate(entries):
        if index == first:
            cfg["cdc"] = [item]
        if key not in _LEGACY_CDC_KEYS:
            cfg[key] = value
    logging.info(
        tr(
            f"{libxr_config_origin}: {names} became one item of {prefix}.cdc",
            f"{libxr_config_origin}：{names} 已改为 {prefix}.cdc 的一项",
        )
    )


def _check_cdc(prefix: str, cfg: dict, is_otg: bool) -> None:
    """检查 cdc 列表并给每项补上默认值：非空列表，每项是映射，各值是正整数。
    Check the cdc list and give each item its defaults: a non-empty list whose items are
    mappings with positive integer values.

    可选的 interface 是字符串，不补默认值。
    The optional interface is a string and gets no default.

    CDC 的路数不得超过端点号 EP1 到 EP15 能容纳的路数；OTG 设备的接收 FIFO 为 EP0 和每路 CDC 的
    OUT 端点各留 64 字节。
    The number of CDCs may not exceed what the endpoint numbers EP1 to EP15 hold; the receive
    FIFO of an OTG device keeps 64 bytes for EP0 and for the OUT endpoint of each CDC.

    Raises:
        LibXRConfigError: cdc 不是非空列表、某项不是映射或某个值无效，路数过多，或 OTG 设备的
            rx_fifo_size 太小。
            cdc is not a non-empty list, an item is not a mapping or a value is invalid, there
            are too many CDCs, or the rx_fifo_size of an OTG device is too small.
    """
    cdc = cfg["cdc"]
    key = f"{prefix}.cdc"
    if not isinstance(cdc, list) or not cdc:
        raise _invalid_setting(
            key, cdc, "a list with one mapping per CDC", "每路 CDC 一个映射的列表"
        )
    for index, item in enumerate(cdc):
        if not isinstance(item, dict):
            raise _invalid_setting(f"{key}[{index}]", item, "a mapping", "映射")
        for name, default in _CDC_DEFAULTS.items():
            _integer(f"{key}[{index}].{name}", item.setdefault(name, default))
        if "interface" in item:
            item["interface"] = _text(f"{key}[{index}].interface", item["interface"])
    if len(cdc) > _MAX_CDC:
        raise LibXRConfigError(
            tr(
                f"{libxr_config_origin}: {key} lists {len(cdc)} CDCs, but the endpoint numbers "
                f"hold {_MAX_CDC}",
                f"{libxr_config_origin}：{key} 列出了 {len(cdc)} 路 CDC，端点号最多容纳 {_MAX_CDC} 路",
            )
        )
    needed = 64 * (len(cdc) + 1)
    if is_otg and _integer(f"{prefix}.rx_fifo_size", cfg["rx_fifo_size"]) < needed:
        raise LibXRConfigError(
            tr(
                f"{libxr_config_origin}: {prefix}.rx_fifo_size {cfg['rx_fifo_size']!r} is too "
                f"small for {len(cdc)} CDCs; the receive FIFO needs 64 bytes for EP0 and for the "
                f"OUT endpoint of each CDC, {needed} in all",
                f"{libxr_config_origin}：{prefix}.rx_fifo_size {cfg['rx_fifo_size']!r} 容不下 "
                f"{len(cdc)} 路 CDC；接收 FIFO 为 EP0 和每路 CDC 的 OUT 端点各需要 64 字节，"
                f"共 {needed}",
            )
        )


# 有 L1 数据 cache 的 STM32 系列：Cortex-M7（F7、H7、H7RS）和 Cortex-M55（N6）。这些系列的 CMSIS
# 器件头文件定义 __DCACHE_PRESENT，LibXR 的驱动在 DMA 前后清理和失效 cache。
# The STM32 families with an L1 data cache: Cortex-M7 (F7, H7, H7RS) and Cortex-M55 (N6). Their
# CMSIS device headers define __DCACHE_PRESENT, and the LibXR drivers clean and invalidate the
# cache around DMA.
_DCACHE_FAMILIES = frozenset({"STM32F7", "STM32H7", "STM32H7RS", "STM32N6"})
# 没有数据 cache 的系列。
# The families without a data cache.
_NO_DCACHE_FAMILIES = frozenset(
    {
        "STM32C0",
        "STM32F0",
        "STM32F1",
        "STM32F2",
        "STM32F3",
        "STM32F4",
        "STM32G0",
        "STM32G4",
        "STM32H5",
        "STM32L0",
        "STM32L1",
        "STM32L4",
        "STM32L5",
        "STM32U0",
        "STM32U3",
        "STM32U5",
        "STM32WB",
        "STM32WB0",
        "STM32WBA",
        "STM32WL",
    }
)
# Cortex-M7 和 Cortex-M55 的数据 cache 行固定为 32 字节（CMSIS 的 __SCB_DCACHE_LINE_SIZE）。
# The data cache line of the Cortex-M7 and the Cortex-M55 is fixed at 32 bytes (the CMSIS
# __SCB_DCACHE_LINE_SIZE).
DCACHE_LINE_SIZE = 32
# DMA 缓冲区的元素类型及其字节数。
# The element types of the DMA buffers and their sizes in bytes.
_ELEMENT_BYTES = {"uint8_t": 1, "uint16_t": 2}


def _dcache_line_size(project_data: dict) -> int:
    """工程的 MCU 数据 cache 的行长度（字节）；没有数据 cache 时为 0。
    The line size in bytes of the data cache of the MCU of the project; 0 without a data cache.

    按 Mcu 中的系列判断，见 _DCACHE_FAMILIES 和 _NO_DCACHE_FAMILIES。系列不在这两个表中时按有
    数据 cache 处理并记录一条警告：多出的对齐只多占几个字节，缺少对齐会使 DMA 数据出错。
    The family in Mcu decides, see _DCACHE_FAMILIES and _NO_DCACHE_FAMILIES. A family in
    neither table counts as having a data cache, with a warning: extra alignment costs a few
    bytes, missing alignment corrupts DMA data.
    """
    mcu = project_data.get("Mcu", {})
    family = (mcu.get("Family") or "").upper()
    if family in _DCACHE_FAMILIES:
        return DCACHE_LINE_SIZE
    if family in _NO_DCACHE_FAMILIES:
        return 0
    logging.warning(
        tr(
            f"Cannot tell whether the MCU family '{family}' has a data cache; the DMA buffers "
            f"are aligned to {DCACHE_LINE_SIZE} bytes",
            f"无法判断 MCU 系列 '{family}' 有没有数据 cache；DMA 缓冲区按 {DCACHE_LINE_SIZE} 字节"
            "对齐",
        )
    )
    return DCACHE_LINE_SIZE


# STM32CubeMX 为这些系列生成的链接脚本把 .data 和 .bss 放在 DTCMRAM，DMA 不能访问这块 RAM。
# The linker scripts STM32CubeMX generates for these families place .data and .bss in DTCMRAM,
# which DMA cannot access.
_DTCM_BSS_FAMILIES = frozenset({"STM32H7"})


def _warn_dtcm_buffers(project_data: dict, keys: list[str]) -> None:
    """工程的系列在 _DTCM_BSS_FAMILIES 中且 keys 非空时记录一条警告：这些实例设置没有 dma_section，
    缓冲区随 .bss 落在 DTCMRAM。生成的代码不变。
    Warn when the family of the project is in _DTCM_BSS_FAMILIES and keys is not empty: these
    instance settings have no dma_section, so their buffers land in DTCMRAM with .bss. The
    generated code stays the same.
    """
    family = (project_data.get("Mcu", {}).get("Family") or "").upper()
    if family not in _DTCM_BSS_FAMILIES or not keys:
        return
    logging.warning(
        tr(
            f"{libxr_config_origin}: {', '.join(keys)} set no dma_section, so their DMA buffers "
            f"go to .bss, which the linker script STM32CubeMX generates for {family} places in "
            "DTCMRAM, out of reach of DMA; add a section in RAM that DMA can access (such as "
            "AXI SRAM) to the linker script and set it as dma_section of these instances",
            f"{libxr_config_origin}：{'、'.join(keys)} 没有设置 dma_section，它们的 DMA 缓冲区位于 "
            f".bss，而 STM32CubeMX 为 {family} 生成的链接脚本把 .bss 放在 DTCMRAM，DMA 不能访问；"
            "请在链接脚本中加入位于 DMA 可访问的 RAM（例如 AXI SRAM）中的段，并把它设为这些实例的 "
            "dma_section",
        )
    )


def _mcu_label(project_data: dict) -> str:
    """说明注释中的 MCU 名：型号去掉末尾的封装和温度等级，例如 STM32F407IGH6 写作 STM32F407IG。
    The MCU name for the comment: the part number without the trailing package and temperature
    grade, so STM32F407IGH6 is written STM32F407IG.
    """
    model = (project_data.get("Mcu", {}).get("Type") or "").strip()
    match = re.fullmatch(r"(STM32\w+?)[A-Z][0-9]", model)
    return match.group(1) if match else model or "MCU"


@dataclass(frozen=True)
class DmaBuffer:
    """一个 DMA 缓冲区：名字、元素类型、驱动使用的元素数、所在段和实际存储的元素数。
    One DMA buffer: its name, element type, the number of elements the driver uses, its section
    and the number of elements actually stored.

    有数据 cache 时存储的元素数向上取整到 cache 行的整数倍，使缓冲区两端不与其他数据共用
    cache 行；驱动使用的元素数不变。
    With a data cache the stored count is rounded up to a whole number of cache lines, so no
    other data shares a cache line with either end of the buffer; the count the driver uses
    stays.
    """

    name: str
    element: str
    count: int
    section: str
    stored: int

    def argument(self) -> "str | Braces":
        """传给驱动的参数：存储的元素数与驱动使用的相同时为缓冲区名，否则为 {名字, 字节数}。
        The argument passed to a driver: the buffer name when the stored count equals the count
        the driver uses, otherwise {name, bytes}.
        """
        if self.stored == self.count:
            return self.name
        return Braces(self.name, str(self.count * _ELEMENT_BYTES[self.element]))


def _natural_key(text: str) -> list:
    """自然排序的键：名字中的数字按数值比较，例如 usart2 排在 usart10 之前。
    The key of a natural sort: digits in the name compare as numbers, so usart2 sorts before
    usart10.
    """
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def _spi_uses_dma(config: dict) -> bool:
    """SPI 实例的配置 config 中两个方向都开启了 DMA 时为 True；只有一个方向开启时 SPI 也不用 DMA。
    True when the configuration config of an SPI instance enables DMA in both directions; with
    DMA in a single direction the SPI does not use DMA either.
    """
    return all(config.get(f"DMA_{direction}") == "ENABLE" for direction in ("RX", "TX"))


def dma_argument(name: str) -> "str | Braces":
    """名为 name 的 DMA 缓冲区传给驱动的参数；该缓冲区没有登记时直接用名字。
    The argument that passes the DMA buffer name to a driver; the name itself when no such
    buffer is registered.
    """
    buffer = dma_buffers.get(name)
    return buffer.argument() if buffer else name


def _usb_buffer_plan(is_otg: bool, cdc_count: int, ep0: int, tx: int, rx: int) -> list:
    """USB 设备的端点缓冲区：(名字后缀, 字节数) 的列表。
    The endpoint buffers of a USB device: a list of (name suffix, bytes).

    CDC 按顺序分配端点：第 i 个 CDC（从 0 起）的数据 IN 端点为 EP(2i+1)，通知端点为 EP(2i+2)；
    OTG 的数据 OUT 端点为 EP(i+1)，IN 和 OUT 的端点号各自编号；FSDEV 的数据 OUT 与数据 IN
    使用同一个端点号。通知端点的缓冲区为 16 字节。
    CDCs take endpoints in order: the data IN endpoint of CDC i (from 0) is EP(2i+1) and its
    notification endpoint EP(2i+2); on OTG the data OUT endpoint is EP(i+1), IN and OUT
    endpoints being numbered separately, while FSDEV uses one endpoint number for data OUT and
    data IN. The notification buffer is 16 bytes.
    """
    plan = [("ep0_in_buf", ep0), ("ep0_out_buf", ep0)]
    for index in range(cdc_count):
        data, notify = 2 * index + 1, 2 * index + 2
        plan.append((f"ep{data}_in_buf", tx))
        plan.append((f"ep{notify}_in_buf", 16))
        plan.append((f"ep{data if not is_otg else index + 1}_out_buf", rx))
    return plan


def _cdc_items(cfg: dict) -> list[dict]:
    """USB 设备的 CDC 串口设置列表：每个 CDC 一个映射，含整数 tx_fifo_size、rx_fifo_size 和
    queue_size，以及接口名 interface（未设置时为空字符串）。
    The list of CDC serial port settings of a USB device: one mapping per CDC with the integers
    tx_fifo_size, rx_fifo_size and queue_size and the interface name interface (an empty string
    when it is not set).
    """
    return [
        {name: _integer(f"cdc.{name}", item[name]) for name in _CDC_DEFAULTS}
        | {"interface": _text("cdc.interface", item.get("interface"))}
        for item in cfg["cdc"]
    ]


def generate_dma_resources(project_data: dict) -> list[str]:
    """登记外设的 DMA 缓冲区并返回它们的定义，每个缓冲区一行；没有缓冲区时为空列表。
    Register the DMA buffers of the peripherals and return their definitions, one line per
    buffer; an empty list without any buffer.

    USART（含 UART、LPUART）为开启 DMA 的方向各生成一个缓冲区；SPI 总是生成发送和接收缓冲区，
    不用 DMA（即两个方向不都有 DMA，见 _spi_uses_dma() 和 _generate_spi()）时缓冲区只由 CPU 访问；
    I2C 和 ADC 各生成一个缓冲区，ADC 的元素数为通道数乘以每通道元素数；已启用的 USB 实例生成端点
    缓冲区。缓冲区大小和 dma_section 取自 libxr_settings，缺少时写入默认值，大小必须是正整数；
    dma_section 非空时声明带 __attribute__((section("...")))。缓冲区按名字的自然顺序排列。
    A USART, UART and LPUART included, gets one buffer per direction with DMA enabled; an SPI
    always gets its transmit and receive buffers, accessed by the CPU alone when it does not use
    DMA, that is without DMA in both directions (see _spi_uses_dma() and _generate_spi()); I2C
    and ADC get one buffer each, the ADC one holding the channel count times the elements per
    channel; enabled USB instances get endpoint buffers. Buffer sizes and dma_section come from libxr_settings,
    which receives the defaults for missing values, and sizes must be positive integers; a
    non-empty dma_section adds __attribute__((section("..."))) to the declarations. The buffers
    are sorted by name in natural order.

    没有数据 cache 的 MCU 按 4 字节对齐；有数据 cache 的 MCU 按 cache 行对齐，数组长度向上取整到
    cache 行的整数倍，缓冲区两端不与其他数据共用 cache 行；驱动被告知的大小不变，见
    DmaBuffer.argument()。
    An MCU without a data cache aligns to 4 bytes; one with a data cache aligns to the cache
    line and rounds the array length up to a whole number of cache lines, so no other data
    shares a cache line with either end of a buffer; the size a driver is told stays, see
    DmaBuffer.argument().

    _DTCM_BSS_FAMILIES 中的系列上有 DMA 访问的缓冲区而 dma_section 为空的实例记录一条警告，见
    _warn_dtcm_buffers()；不用 DMA 的 SPI 的缓冲区只由 CPU 访问，不计入。
    On a family of _DTCM_BSS_FAMILIES, the instances with buffers that DMA accesses and an empty
    dma_section are warned about, see _warn_dtcm_buffers(); the buffers of an SPI that does not
    use DMA are accessed by the CPU only and do not count.

    Raises:
        LibXRConfigError: 某个设置段不是映射，或某个大小或 dma_section 无效。
            A settings section is not a mapping, or a size or dma_section is invalid.
    """
    # 有缓冲区而 dma_section 为空的实例设置的键，例如 SPI.spi1。
    # The keys of the instance settings with buffers and an empty dma_section, such as SPI.spi1.
    unplaced: list[str] = []

    def add(key: str, element: str, name: str, count: int, section: str) -> None:
        """登记实例设置 key 的一个缓冲区：count 个 element 类型的元素，位于 section 段。
        Register one buffer of the instance settings key: count elements of the type element in
        the section section.
        """
        dma_buffers[name] = DmaBuffer(name, element, count, section, count)
        if not section and key not in unplaced:
            unplaced.append(key)

    def section_of(key: str, instance_config: dict) -> str:
        """实例设置中 dma_section 的值；没有设置时为空字符串，并把 dma_section 记为空字符串，使
        libxr_config.yaml 列出这一项。key 是实例设置的键，例如 SPI.spi1。
        The value of dma_section in the instance settings; an empty string when it is not set,
        and dma_section is then recorded as an empty string so that libxr_config.yaml lists it.
        key is the key of the instance settings, such as SPI.spi1.
        """
        dma_section = _text(f"{key}.dma_section", instance_config.get("dma_section"))
        instance_config["dma_section"] = dma_section
        return dma_section

    # 遍历所有外设
    # Iterate all peripherals
    for p_type_raw, instances in project_data.get("Peripherals", {}).items():
        # 规范化外设类型（例如 "spi1" -> "SPI"）
        # Normalize peripheral type (e.g. "spi1" -> "SPI")
        match = re.match(r"([A-Za-z0-9]+?)(\d*)$", p_type_raw)
        p_type_base = match.group(1).upper() if match else p_type_raw.upper()

        # 确保该外设的设置段存在
        # Ensure the settings section of this peripheral exists
        _settings(p_type_base)

        # SPI 和 USART 外设；parse 把 USART、UART 和 LPUART 实例都放在 USART 下。
        # SPI and USART; parse puts USART, UART and LPUART instances all under USART.
        if p_type_base in ["SPI", "USART"]:
            for instance, config in instances.items():
                # 检查 DMA 使能标志
                # Check DMA enable flags
                tx_dma = config.get("DMA_TX", "DISABLE") == "ENABLE"
                rx_dma = config.get("DMA_RX", "DISABLE") == "ENABLE"
                instance_lower = instance.lower()
                instance_config = _settings(p_type_base, instance_lower)
                key = f"{p_type_base}.{instance_lower}"
                tx_size = _integer(
                    f"{key}.tx_buffer_size",
                    instance_config.setdefault(
                        "tx_buffer_size", DMA_DEFAULT_SIZES[p_type_base]["tx"]
                    ),
                )
                rx_size = _integer(
                    f"{key}.rx_buffer_size",
                    instance_config.setdefault(
                        "rx_buffer_size", DMA_DEFAULT_SIZES[p_type_base]["rx"]
                    ),
                )
                section = section_of(key, instance_config)

                if p_type_base == "SPI" and not _spi_uses_dma(config):
                    # 不用 DMA 的 SPI 走轮询路径，也经过这两个缓冲区，见 _generate_spi()；CPU
                    # 访问它们，所在的 RAM 不受 DMA 的限制。
                    # An SPI without DMA takes the polling path, which goes through these two
                    # buffers as well, see _generate_spi(); the CPU accesses them, so their RAM
                    # has no DMA limit.
                    for direction, size in (("tx", tx_size), ("rx", rx_size)):
                        name = f"{instance_lower}_{direction}_buf"
                        dma_buffers[name] = DmaBuffer(name, "uint8_t", size, section, size)
                    continue
                if tx_dma:
                    add(key, "uint8_t", f"{instance_lower}_tx_buf", tx_size, section)
                if rx_dma:
                    add(key, "uint8_t", f"{instance_lower}_rx_buf", rx_size, section)

        # I2C/ADC 外设
        # I2C/ADC
        elif p_type_base in ["I2C", "ADC"]:
            for instance, config in instances.items():
                instance_lower = instance.lower()
                instance_config = _settings(p_type_base, instance_lower)
                key = f"{p_type_base}.{instance_lower}"
                buf_size = _integer(
                    f"{key}.buffer_size",
                    instance_config.setdefault(
                        "buffer_size", DMA_DEFAULT_SIZES[p_type_base]["buffer"]
                    ),
                )
                section = section_of(key, instance_config)

                # ADC 缓冲区为 uint16_t，I2C 为 uint8_t
                # ADC buffer is uint16_t, I2C is uint8_t
                if p_type_base == "ADC":
                    # 通道选择规则：DMA 开启→RegularConversions，否则→Channels
                    # Channel selection: RegularConversions with DMA enabled, otherwise Channels
                    active_channels = (
                        config.get("RegularConversions", [])
                        if config.get("DMA") == "ENABLE"
                        else config.get("Channels", [])
                    )
                    # 至少保留 1 份缓冲
                    # Keep at least one channel's share of the buffer
                    ch_cnt = max(1, len(active_channels))
                    # 每通道的 uint16_t 元素数
                    # uint16_t elements per channel
                    elems_per_channel = max(1, int(buf_size // 2))
                    # 总元素数 = 通道数 × 每通道元素数
                    # Total elements = channel count × elements per channel
                    add(
                        key,
                        "uint16_t",
                        f"{instance_lower}_buf",
                        ch_cnt * elems_per_channel,
                        section,
                    )
                else:
                    add(key, "uint8_t", f"{instance_lower}_buf", buf_size, section)

        elif p_type_base == "USB":
            # 为每个已启用的 USB 实例生成端点缓冲区（所在段由 dma_section 决定）。
            # Endpoint buffers for each enabled USB instance, in the section dma_section names.
            for instance in instances:
                usb = _usb_settings(instance)
                if usb is None or not usb[1]["enable"]:
                    continue
                if _pcd_handle(instance, instances[instance]) is None:
                    continue
                name, usb_cfg = usb
                ep0 = _integer("ep0_packet_size", usb_cfg["ep0_packet_size"])
                tx_sz = _integer("tx_buffer_size", usb_cfg["tx_buffer_size"])
                rx_sz = _integer("rx_buffer_size", usb_cfg["rx_buffer_size"])
                plan = _usb_buffer_plan(
                    name.startswith("USB_OTG_"), len(_cdc_items(usb_cfg)), ep0, tx_sz, rx_sz
                )
                for suffix, size in plan:
                    section = usb_cfg["dma_section"]
                    add(f"USB.{name.lower()}", "uint8_t", f"{name.lower()}_{suffix}", size, section)

    if not dma_buffers:
        return []
    _warn_dtcm_buffers(project_data, unplaced)
    line = _dcache_line_size(project_data)
    if line:
        for name, buffer in list(dma_buffers.items()):
            size = _ELEMENT_BYTES[buffer.element]
            dma_buffers[name] = replace(
                buffer, stored=-(-buffer.count * size // line) * line // size
            )
        note = f"D-cache, {line}-byte lines"
    else:
        note = "no D-cache"
    lines = [f"// DMA buffers ({_mcu_label(project_data)}: {note})"]
    alignment = line or 4
    for name in sorted(dma_buffers, key=_natural_key):
        lines.extend(_dma_declaration(dma_buffers[name], alignment))
    return lines


def _dma_declaration(buffer: DmaBuffer, alignment: int) -> list[str]:
    """缓冲区的定义：一行；带段属性而超过列宽时，段属性另起一行并缩进 4 列。
    The definition of a buffer: one line; with a section attribute that passes the column
    limit, the attribute goes on its own line indented by 4 columns.
    """
    head = f"alignas({alignment}) static {buffer.element} {buffer.name}[{buffer.stored}]"
    if not buffer.section:
        return [f"{head};"]
    attribute = f'__attribute__((section("{buffer.section}")))'
    if len(f"{head} {attribute};") <= COLUMN_LIMIT:
        return [f"{head} {attribute};"]
    return [head, f"    {attribute};"]


# --------------------------
# 外设生成 / Peripheral Generation
# --------------------------
def _instance_settings(group: str, instance: str) -> dict:
    """libxr_settings[group] 中实例的设置，键为小写的实例名；不存在时创建。
    The settings of an instance in libxr_settings[group], keyed by the lower-case instance
    name; created when missing.

    旧版本按 CubeMX 的写法保存的键（如 CAN 下的 CAN1）改为小写，并记录一条提示。
    A key that older versions saved as CubeMX writes it, such as CAN1 under CAN, is renamed to
    lower case with a notice.

    Raises:
        LibXRConfigError: group 或实例的设置不是映射。
            The settings of group or of the instance are not a mapping.
    """
    settings = _settings(group)
    key = instance.lower()
    for old_key in [k for k in settings if k != key and str(k).lower() == key]:
        value = settings.pop(old_key)
        if key not in settings:
            settings[key] = value
        logging.info(
            tr(
                f"libxr_config.yaml: renamed {group}.{old_key} to {group}.{key}",
                f"libxr_config.yaml：已把 {group}.{old_key} 改为 {group}.{key}",
            )
        )
    return _settings(group, key)


class PeripheralFactory:
    """按外设类型生成 LibXR 外设对象的构造代码，并登记生成的对象。
    Generate the construction code of LibXR peripheral objects by peripheral type and register
    the generated objects.

    各生成方法返回 (种类, 各行)：种类为 "adc"、"dac"、"pwm"、"spi"、"uart"、"i2c"、"can"、
    "watchdog" 或 "usb"，决定代码在 app_main 中的位置；("", []) 表示不生成代码。各行已按
    clang-format 的风格排版并缩进。缺少的设置以默认值写入 libxr_settings。
    Each generator method returns (kind, lines): the kind, "adc", "dac", "pwm", "spi", "uart",
    "i2c", "can", "watchdog" or "usb", decides where the code goes in app_main, and ("", [])
    means no code. The lines are laid out and indented in the clang-format style. Missing
    settings are written to libxr_settings with their defaults.
    """

    @staticmethod
    def create(p_type: str, instance: str, config: dict) -> tuple:
        """调用 p_type 对应的生成方法并返回 (种类, 各行)；类型名不区分大小写，没有对应方法时
        为 ("", [])。
        Call the generator method of p_type, case-insensitively, and return (kind, lines);
        ("", []) when the type has none.
        """
        handler_map = {
            "ADC": PeripheralFactory._generate_adc,
            "DAC": PeripheralFactory._generate_dac,
            "TIM": PeripheralFactory._generate_tim,
            "FDCAN": PeripheralFactory._generate_canfd,
            "CAN": PeripheralFactory._generate_can,
            "SPI": PeripheralFactory._generate_spi,
            # parse 把 USART、UART 和 LPUART 实例都放在 USART 下。
            # parse puts USART, UART and LPUART instances all under USART.
            "USART": PeripheralFactory._generate_uart,
            "I2C": PeripheralFactory._generate_i2c,
            "IWDG": PeripheralFactory._generate_iwdg,
            "USB": PeripheralFactory._generate_usb,
        }
        generator = handler_map.get(p_type.upper())
        return generator(instance, config) if generator else ("", [])

    @staticmethod
    def _generate_adc(instance: str, config: dict) -> tuple:
        """生成 STM32ADC 对象和每个通道的引用；DMA 开启时用 RegularConversions，否则用 Channels。
        Generate the STM32ADC object and a reference per channel; RegularConversions with DMA
        enabled, Channels otherwise.

        参考电压取 libxr_settings 中的 vref（默认 3.3）；每个通道引用 <adc>_<channel> 登记为 ADC。
        同一通道排在多个 rank 时，第一次的引用沿用该名字，之后的加上 rank 后缀，例如
        adc1_adc_channel_8_rank12。不带 --xrobot 时，没有 XR_REGISTER 引用这些通道引用，各用
        UNUSED 标记。
        The reference voltage is vref from libxr_settings (default 3.3); each channel reference
        <adc>_<channel> is registered as ADC. When a channel is in several ranks, its first
        reference keeps that name and later ones get a rank suffix, for example
        adc1_adc_channel_8_rank12. Without --xrobot no XR_REGISTER refers to these channel
        references, so each is marked with UNUSED.
        """
        conversions = (
            config.get("RegularConversions", [])
            if config.get("DMA") == "ENABLE"
            else config.get("Channels", [])
        )
        name = instance.lower()
        adc_config = _settings("ADC", name)
        vref = _number(f"ADC.{name}.vref", adc_config.setdefault("vref", 3.3))

        _use_header("stm32_adc.hpp")
        _use_handle("ADC_HandleTypeDef", f"h{name}")
        lines = layout(
            f"static STM32ADC {name}",
            [f"&h{name}", dma_argument(f"{name}_buf"), Braces(*conversions), str(vref)],
        )

        names = set()
        for index, channel in enumerate(conversions):
            channel_name = f"{name}_{channel.lower()}"
            if channel_name in names:
                channel_name = f"{channel_name}_rank{index + 1}"
            names.add(channel_name)
            lines += layout(f"static auto& {channel_name} = {name}.GetChannel", [str(index)])
            if not generating_for_xrobot:
                lines.append(f"{INDENT}UNUSED({channel_name});")
            _register_device(channel_name, "ADC")

        return "adc", lines

    @staticmethod
    def _generate_dac(instance: str, config: dict) -> tuple:
        """为每个 DAC 输出通道生成一个 STM32DAC 对象，名字为 <instance>_<out_name>（如 dac1_out2）。
        Generate one STM32DAC object per DAC output channel, named <instance>_<out_name>, for
        example dac1_out2.

        DAC_OUT<n> 写作 DAC_CHANNEL_<n>，名字开头的 dac_dac_ 缩为 dac_。初始电压和参考电压取自
        libxr_settings（默认 0.0 和 3.3）。没有通道时不生成代码。
        DAC_OUT<n> is written as DAC_CHANNEL_<n>, and a name starting with dac_dac_ is shortened
        to dac_. The initial and reference voltages come from libxr_settings (defaults 0.0 and
        3.3). No channel means no code.
        """
        channels = config.get("Channels", {})
        if not channels:
            return "", []
        name = instance.lower()
        dac_config = _settings("DAC", name)
        init_voltage = _number(
            f"DAC.{name}.init_voltage", dac_config.setdefault("init_voltage", 0.0)
        )
        vref = _number(f"DAC.{name}.vref", dac_config.setdefault("vref", 3.3))
        _use_header("stm32_dac.hpp")
        _use_handle("DAC_HandleTypeDef", f"h{name}")
        lines = []
        for out_name, channel_id in channels.items():
            if channel_id.startswith("DAC_OUT"):
                m = re.search(r"DAC_OUT(\d+)", channel_id)
                channel_id = "DAC_CHANNEL_" + m.group(1)
            var_name = f"{name}_{out_name.lower()}"
            if var_name.startswith("dac_dac_"):
                var_name = var_name.replace("dac_dac_", "dac_")
            lines += layout(
                f"static STM32DAC {var_name}",
                [f"&h{name}", channel_id, str(init_voltage), str(vref)],
            )
            _register_device(var_name, "DAC")
        return "dac", lines

    @staticmethod
    def _generate_uart(instance: str, config: dict) -> tuple:
        """生成 STM32UART 对象；未开启 DMA 的方向使用空缓冲区 {nullptr, 0}。
        Generate the STM32UART object; a direction without DMA gets the empty buffer
        {nullptr, 0}.

        HAL 句柄名中的 usart 写作 uart；发送队列长度取 libxr_settings 中 USART 下的
        tx_queue_size（默认 5）。
        The HAL handle name writes usart as uart; the transmit queue length is tx_queue_size
        under USART in libxr_settings (default 5).
        """
        name = instance.lower()
        tx_dma = config.get("DMA_TX", "DISABLE") == "ENABLE"
        rx_dma = config.get("DMA_RX", "DISABLE") == "ENABLE"
        tx_buf = dma_argument(f"{name}_tx_buf") if tx_dma else Braces("nullptr", "0")
        rx_buf = dma_argument(f"{name}_rx_buf") if rx_dma else Braces("nullptr", "0")

        uart_config = _settings("USART", name)
        tx_queue = _integer(
            f"USART.{name}.tx_queue_size", uart_config.setdefault("tx_queue_size", 5)
        )

        handle = f"h{name.replace('usart', 'uart')}"
        _use_header("stm32_uart.hpp")
        _use_handle("UART_HandleTypeDef", handle)
        lines = layout(f"static STM32UART {name}", [f"&{handle}", rx_buf, tx_buf, str(tx_queue)])
        _register_device(name, "UART")
        return "uart", lines

    @staticmethod
    def _generate_i2c(instance: str, config: dict) -> tuple:
        """生成使用 <instance>_buf 缓冲区的 STM32I2C 对象；dma_enable_min_size 默认为 3。
        Generate the STM32I2C object with the <instance>_buf buffer; dma_enable_min_size
        defaults to 3.
        """
        name = instance.lower()
        i2c_config = _settings("I2C", name)
        dma_min_size = _integer(
            f"I2C.{name}.dma_enable_min_size",
            i2c_config.setdefault("dma_enable_min_size", 3),
            0,
        )
        _use_header("stm32_i2c.hpp")
        _use_handle("I2C_HandleTypeDef", f"h{name}")
        _register_device(name, "I2C")
        return "i2c", layout(
            f"static STM32I2C {name}",
            [f"&h{name}", dma_argument(f"{name}_buf"), str(dma_min_size)],
        )

    @staticmethod
    def _generate_tim(instance: str, config: dict) -> tuple:
        """为定时器的每个通道生成一个 STM32PWM 对象 pwm_<tim>_ch<n>；没有通道时不生成代码。
        Generate one STM32PWM object pwm_<tim>_ch<n> per timer channel; no channel means no
        code.

        互补通道使用去掉末尾 N 的 TIM_CHANNEL_<n>，并传入 true 表示互补输出。
        A complementary channel uses TIM_CHANNEL_<n> without the trailing N and passes true for
        the complementary output.
        """
        channels = config.get("Channels", {})
        if not channels:
            return "", []
        name = instance.lower()
        _use_header("stm32_pwm.hpp")
        _use_handle("TIM_HandleTypeDef", f"h{name}")
        lines = []
        for ch_name, ch_cfg in channels.items():
            ch_num = ch_name.replace("CH", "").lower()
            dev_name = f"pwm_{name}_ch{ch_num}"
            complementary = ch_cfg.get("Complementary", False)
            if complementary and ch_num.endswith("n"):
                ch_num = ch_num[:-1]
            lines += layout(
                f"static STM32PWM {dev_name}",
                [f"&h{name}", f"TIM_CHANNEL_{ch_num}", "true" if complementary else "false"],
            )
            _register_device(dev_name, "PWM")
        return "pwm", lines

    @staticmethod
    def _generate_canfd(instance: str, config: dict) -> tuple:
        """生成 STM32CANFD 对象；队列长度取 libxr_settings 中 FDCAN 下的 queue_size（默认 5）。
        Generate the STM32CANFD object; the queue length is queue_size under FDCAN in
        libxr_settings (default 5).
        """
        name = instance.lower()
        instance_cfg = _instance_settings("FDCAN", instance)
        queue_size = _integer(f"FDCAN.{name}.queue_size", instance_cfg.setdefault("queue_size", 5))

        _use_header("stm32_canfd.hpp")
        _use_handle("FDCAN_HandleTypeDef", f"h{name}")
        _register_device(name, "FDCAN")
        return "can", layout(f"static STM32CANFD {name}", [f"&h{name}", str(queue_size)])

    @staticmethod
    def _generate_can(instance: str, config: dict) -> tuple:
        """生成经典 CAN 外设的 STM32CAN 对象。
        Generate the STM32CAN object of a classic CAN peripheral.

        队列长度取 libxr_settings 中 CAN 下的 queue_size（默认 5）。
        The queue length is queue_size under CAN in libxr_settings (default 5).
        """
        name = instance.lower()
        instance_cfg = _instance_settings("CAN", instance)
        queue_size = _integer(f"CAN.{name}.queue_size", instance_cfg.setdefault("queue_size", 5))

        _use_header("stm32_can.hpp")
        _use_handle("CAN_HandleTypeDef", f"h{name}")
        _register_device(
            name,
            "CAN",
            tr(f"classic CAN peripheral {instance}", f"经典 CAN 外设 {instance}"),
        )
        return "can", layout(f"static STM32CAN {name}", [f"&h{name}", str(queue_size)])

    @staticmethod
    def _generate_spi(instance: str, config: dict) -> tuple:
        """生成 STM32SPI 对象。
        Generate the STM32SPI object.

        两个方向都开启了 DMA 的 SPI（见 _spi_uses_dma()）使用它的两个 DMA 缓冲区，
        dma_enable_min_size 取自 libxr_settings 中 SPI 下的设置，默认为 3。其他 SPI，包括只有一个
        方向开启了 DMA 的，使用 generate_dma_resources() 为它生成的发送和接收缓冲区（大小取自
        tx_buffer_size 和 rx_buffer_size），dma_enable_min_size 写成 UINT32_MAX：传输长度总不超过
        它，STM32SPI 总是走轮询路径，经这两个缓冲区收发，不使用 DMA 通道；这时不读取也不写入
        dma_enable_min_size 设置。只有一个方向开启了 DMA 时记录一条警告。
        An SPI with DMA in both directions (see _spi_uses_dma()) uses its two DMA buffers, and
        dma_enable_min_size comes from the SPI settings in libxr_settings and defaults to 3. Any
        other SPI, one with DMA in a single direction included, uses the transmit and receive
        buffers that generate_dma_resources() generates for it, sized by tx_buffer_size and
        rx_buffer_size, and dma_enable_min_size is written as UINT32_MAX: no transfer is longer,
        so STM32SPI always takes the polling path through these two buffers and uses no DMA
        channel; the dma_enable_min_size setting is then neither read nor written. DMA in a
        single direction is warned about.
        """
        name = instance.lower()
        if _spi_uses_dma(config):
            spi_config = _settings("SPI", name)
            dma_min_size = _integer(
                f"SPI.{name}.dma_enable_min_size",
                spi_config.setdefault("dma_enable_min_size", 3),
                0,
            )
        else:
            dma_min_size = "UINT32_MAX"
            directions = [d for d in ("RX", "TX") if config.get(f"DMA_{d}") == "ENABLE"]
            if directions:
                on, off = directions[0], ({"RX", "TX"} - set(directions)).pop()
                logging.warning(
                    tr(
                        f"{instance} has DMA for {on} only; an SPI uses DMA only with DMA for "
                        f"both RX and TX, so {name} takes the polling path and its {on} DMA "
                        f"channel is not used. Enable DMA for {off} in STM32CubeMX to use DMA",
                        f"{instance} 只有 {on} 方向开启了 DMA；SPI 只在 RX 和 TX 都有 DMA 时使用 "
                        f"DMA，{name} 走轮询路径，{on} 的 DMA 通道不被使用。需要 DMA 时请在 "
                        f"STM32CubeMX 中为 {off} 开启 DMA",
                    )
                )
        tx_buf = dma_argument(f"{name}_tx_buf")
        rx_buf = dma_argument(f"{name}_rx_buf")

        _use_header("stm32_spi.hpp")
        _use_handle("SPI_HandleTypeDef", f"h{name}")
        _register_device(name, "SPI")
        return "spi", layout(
            f"static STM32SPI {name}", [f"&h{name}", rx_buf, tx_buf, str(dma_min_size)]
        )

    @staticmethod
    def _generate_iwdg(instance: str, config: dict) -> tuple:
        """生成已启用 IWDG 的 STM32Watchdog 对象；未启用时不生成代码。
        Generate the STM32Watchdog object of an enabled IWDG; a disabled one produces no code.

        超时和喂狗间隔取自 libxr_settings，默认为 1000 ms 和 250 ms。
        Timeout and feed interval come from libxr_settings and default to 1000 ms and 250 ms.
        """
        if not config.get("Enabled"):
            return "", []
        name = instance.lower()
        iwdg_config = _settings("IWDG", name)
        key = f"IWDG.{name}"
        timeout_ms = _integer(f"{key}.timeout_ms", iwdg_config.setdefault("timeout_ms", 1000))
        feed_ms = _integer(
            f"{key}.feed_interval_ms", iwdg_config.setdefault("feed_interval_ms", 250)
        )
        _use_header("stm32_watchdog.hpp")
        _use_handle("IWDG_HandleTypeDef", f"h{name}")
        _register_device(name, "Watchdog")
        return "watchdog", layout(
            f"static STM32Watchdog {name}", [f"&h{name}", str(timeout_ms), str(feed_ms)]
        )

    @staticmethod
    def _generate_usb(instance: str, config: dict) -> tuple:
        """生成 USB 设备对象及其 CDC 串口，并把最终的 USB 设置写入 libxr_settings。
        Generate the USB device object with its CDC serial ports and write the final USB
        settings to libxr_settings.

        实例名和设置来自 _usb_settings()；其他实例名（如 USB_DEVICE 中间件）和未启用的实例不生成
        代码，启用了但在 CubeMX 中是主机模式的实例记录警告后不生成。设备对象以实例名命名（如
        usb_otg_fs），使用 _pcd_handle() 给出的句柄，引用 generate_dma_resources() 定义的端点
        缓冲区，本方法不定义缓冲区。CDC 串口 <实例>_cdc、<实例>_cdc2 …… 按顺序分配端点，规则见
        _usb_buffer_plan()，各登记为 UART。
        The instance name and settings come from _usb_settings(); other instance names, such as
        the USB_DEVICE middleware, and disabled instances produce no code, and an enabled
        instance in host mode in CubeMX is skipped with a warning. The device object is named
        after the instance, for example usb_otg_fs, uses the handle _pcd_handle() gives and
        references the endpoint buffers that generate_dma_resources() defines; this method
        defines no buffer. The CDC serial ports <instance>_cdc, <instance>_cdc2 ... take
        endpoints in order, see _usb_buffer_plan(), and are each registered as UART.

        Raises:
            LibXRConfigError: 设置了 cdc_count，或某个设置无效（见 _usb_settings()）。
                cdc_count is set, or a setting is invalid (see _usb_settings()).
        """
        usb = _usb_settings(instance)
        if usb is None:
            return "", []
        name, inst_cfg = usb
        inst_lower = name.lower()  # 例如 usb_fs、usb_otg_fs / e.g. usb_fs, usb_otg_fs
        if not inst_cfg["enable"]:
            logging.info(
                tr(
                    f"USB instance '{inst_lower}' is not generated: USB.{inst_lower}.enable is "
                    "false in libxr_config.yaml",
                    f"USB 实例 '{inst_lower}' 不生成：libxr_config.yaml 中 USB.{inst_lower}.enable "
                    "为 false",
                )
            )
            return "", []
        pcd_handle = _pcd_handle(instance, config)
        if pcd_handle is None:
            logging.warning(
                tr(
                    f"USB instance '{inst_lower}' is in host mode in CubeMX, and the LibXR USB "
                    "device needs device mode. Skipping generation.",
                    f"USB 实例 '{inst_lower}' 在 CubeMX 中是主机模式，LibXR 的 USB 设备需要设备"
                    "模式，跳过生成。",
                )
            )
            return "", []

        is_otg = name.startswith("USB_OTG_")
        speed = "HS" if name.endswith("_HS") else "FS"

        ep0_sz = _integer("ep0_packet_size", inst_cfg["ep0_packet_size"])
        # _usb_settings() 已检查这些数值，这里只取出整数。
        # _usb_settings() has checked these numbers; this only reads the integers.
        number = {
            key: _integer(key, inst_cfg[key], 0)
            for key in ("rx_buffer_size", "tx_fifo_size", "rx_fifo_size", "vid", "pid", "bcd")
        }
        rx_buf_sz = number["rx_buffer_size"]  # USB DMA 缓冲区 / USB DMA
        tx_fifo_size = number["tx_fifo_size"]  # EP1 硬件 FIFO / EP1 HW FIFO
        # OTG 共享的接收 FIFO / OTG shared RX FIFO
        rx_fifo_size = number["rx_fifo_size"]
        vid = number["vid"]
        pid = number["pid"]
        bcd = number["bcd"]
        manufacturer = inst_cfg["manufacturer"].replace('"', '\\"')
        product = inst_cfg["product"].replace('"', '\\"')
        serial = inst_cfg["serial"].replace('"', '\\"')
        cdcs = _cdc_items(inst_cfg)

        # EP0 包大小的枚举值
        # Size enum for EP0
        size_enum = {8: "SIZE_8", 16: "SIZE_16", 32: "SIZE_32", 64: "SIZE_64"}[ep0_sz]
        strings = f"{inst_lower}_strings"
        instance_type = (
            "STM32USBDeviceOtgFS"
            if (is_otg and speed == "FS")
            else "STM32USBDeviceOtgHS"
            if (is_otg and speed == "HS")
            else "STM32USBDeviceDevFs"
        )

        _use_header("stm32_usb_dev.hpp")
        _use_header("cdc_uart.hpp")
        _use_handle("PCD_HandleTypeDef", pcd_handle)

        def endpoint(number: int) -> str:
            """端点号 number 对应的 USB::Endpoint::EPNumber 枚举值。
            The USB::Endpoint::EPNumber enumerator of the endpoint number number.
            """
            return f"USB::Endpoint::EPNumber::EP{number}"

        title = name.replace("_", " ")
        lines = [f"{INDENT}// {title}: {len(cdcs)} CDC"]
        lines += layout(
            f"static constexpr auto {strings} = USB::DescriptorStrings::MakeLanguagePack",
            ["USB::DescriptorStrings::Language::EN_US", f'"{manufacturer}"', f'"{product}"']
            + [f'"{serial}"'],
        )
        # 以显式的端点号构造 CDC：第 i 个 CDC 的数据 IN 端点为 EP(2i+1)，通知端点为 EP(2i+2)，
        # OTG 的数据 OUT 端点为 EP(i+1)，FSDEV 的数据 OUT 与数据 IN 同号。
        # CDC construction with explicit endpoint numbers: CDC i has its data IN endpoint on
        # EP(2i+1) and its notification endpoint on EP(2i+2); on OTG the data OUT endpoint is
        # EP(i+1), on FSDEV it has the number of data IN.
        cdc_names = []
        for index, cdc in enumerate(cdcs):
            cdc_name = f"{inst_lower}_cdc" + (str(index + 1) if index else "")
            cdc_names.append(cdc_name)
            data_in = 2 * index + 1
            data_out = index + 1 if is_otg else data_in
            # 设置了 interface 时它同时作为控制接口和数据接口的名字；主机按它区分 VID:PID 相同的
            # 各路 CDC。
            # A set interface names both the control and the data interface; the host tells the
            # CDCs that share VID:PID apart by it.
            interface = cdc["interface"].replace('"', '\\"')
            lines += layout(
                f"static USB::CDCUart {cdc_name}",
                [
                    endpoint(data_in),
                    endpoint(data_out),
                    endpoint(2 * index + 2),
                    str(cdc["rx_fifo_size"]),
                    str(cdc["tx_fifo_size"]),
                    str(cdc["queue_size"]),
                ]
                + ([f'"{interface}"'] * 2 if interface else []),
            )

        def buffer(suffix: str) -> "str | Braces":
            """端点缓冲区 <实例>_<suffix> 传给驱动的参数。
            The argument that passes the endpoint buffer <instance>_<suffix> to the driver.
            """
            return dma_argument(f"{inst_lower}_{suffix}")

        if is_otg:
            out_buffers = [buffer("ep0_out_buf")]
            in_configs = [Braces(buffer("ep0_in_buf"), str(ep0_sz))]
            for index in range(len(cdcs)):
                out_buffers.append(buffer(f"ep{index + 1}_out_buf"))
                in_configs.append(Braces(buffer(f"ep{2 * index + 1}_in_buf"), str(tx_fifo_size)))
                in_configs.append(Braces(buffer(f"ep{2 * index + 2}_in_buf"), "16"))
            endpoints = [str(rx_fifo_size), Braces(*out_buffers), Braces(*in_configs)]
        else:
            configs = [
                Braces(buffer("ep0_in_buf"), buffer("ep0_out_buf"), str(ep0_sz), str(ep0_sz))
            ]
            for index in range(len(cdcs)):
                data = 2 * index + 1
                configs.append(
                    Braces(
                        buffer(f"ep{data}_in_buf"),
                        buffer(f"ep{data}_out_buf"),
                        str(tx_fifo_size),
                        str(rx_buf_sz),
                    )
                )
                configs.append(Braces(buffer(f"ep{data + 1}_in_buf"), "16", "true"))
            endpoints = [Braces(*configs)]
        lines += layout(
            f"static {instance_type} {inst_lower}",
            [f"&{pcd_handle}"]
            + endpoints
            + [
                f"USB::DeviceDescriptor::PacketSize0::{size_enum}",
                f"0x{vid:X}",
                f"0x{pid:X}",
                f"0x{bcd:X}",
                Braces(f"&{strings}"),
                Braces(Braces(*[f"&{cdc_name}" for cdc_name in cdc_names])),
                Braces("reinterpret_cast<void*>(UID_BASE)", "12"),
            ],
        )
        lines.append(f"{INDENT}{inst_lower}.Init(false);")
        lines.append(f"{INDENT}{inst_lower}.Start(false);")

        for cdc_name in cdc_names:
            _register_device(cdc_name, "UART")
        return "usb", lines


# 驱动头文件定义的名字：User Code 区域中的手写代码用到其中一个名字时，生成的文件仍然 include
# 这个头文件。
# The names the driver headers define: when hand-written code in a User Code region uses one of
# them, the generated file still includes that header.
_HEADER_NAMES = {
    "cdc_uart.hpp": ("CDCUart",),
    "flash_map.hpp": ("FLASH_REGIONS", "FLASH_REGION_NUMBER"),
    "stm32_adc.hpp": ("STM32ADC",),
    "stm32_can.hpp": ("STM32CAN",),
    "stm32_canfd.hpp": ("STM32CANFD",),
    "stm32_dac.hpp": ("STM32DAC",),
    "stm32_flash.hpp": ("STM32Flash",),
    "stm32_gpio.hpp": ("STM32GPIO",),
    "stm32_i2c.hpp": ("STM32I2C",),
    "stm32_pwm.hpp": ("STM32PWM",),
    "stm32_spi.hpp": ("STM32SPI",),
    "stm32_uart.hpp": ("STM32UART",),
    "stm32_usb_dev.hpp": ("STM32USBDeviceDevFs", "STM32USBDeviceOtgFS", "STM32USBDeviceOtgHS"),
    "stm32_watchdog.hpp": ("STM32Watchdog",),
}
# Peripherals 中的段与它们的 HAL 句柄类型；句柄名是 h 加小写的实例名。
# The sections of Peripherals and their HAL handle types; a handle is named h plus the instance
# name in lowercase.
_HANDLE_TYPES = {
    "ADC": "ADC_HandleTypeDef",
    "CAN": "CAN_HandleTypeDef",
    "DAC": "DAC_HandleTypeDef",
    "FDCAN": "FDCAN_HandleTypeDef",
    "I2C": "I2C_HandleTypeDef",
    "IWDG": "IWDG_HandleTypeDef",
    "SPI": "SPI_HandleTypeDef",
    "TIM": "TIM_HandleTypeDef",
}


# libxr 5.x 的 flash_map.hpp 和 LibXR 中改了名的名字及其新名字。
# Names of the flash_map.hpp of libxr 5.x and of LibXR that were renamed, with their new names.
_RENAMED_FLASH_NAMES = {
    "FLASH_SECTORS": "FLASH_REGIONS",
    "FLASH_SECTOR_NUMBER": "FLASH_REGION_NUMBER",
    "FlashSector": "FlashRegion",
}
# libxr 5.x 按速度命名的 USB OTG 设备对象及其现在按实例的名字。
# The USB OTG device objects that libxr 5.x named after their speed, with their names after the
# instance now.
_RENAMED_USB_OBJECTS = {"usb_fs": "usb_otg_fs", "usb_hs": "usb_otg_hs"}


def _report_renamed_names(project_data: dict, names: set[str]) -> None:
    """User Code 中的标识符 names 用到 libxr 5.x 之后改了名的名字时给出警告，写出新名字。
    Warn when the identifiers names of the User Code use a name that changed after libxr 5.x,
    giving the new name.

    Flash 的名字（_RENAMED_FLASH_NAMES）随 LibXR 的 FlashRegion 改名，STM32Flash 的第三个参数也由
    扇区序号改为存储区的起始地址。USB OTG 设备对象改为按实例命名（_RENAMED_USB_OBJECTS）：工程有
    这个 OTG 实例、没有同名的 USB 实例而 User Code 用到旧名字时才警告。生成的文件不变。
    The Flash names (_RENAMED_FLASH_NAMES) follow the renaming to FlashRegion in LibXR, and the
    third argument of STM32Flash changed from a sector index to the start address of the storage
    area. The USB OTG device objects are now named after the instance (_RENAMED_USB_OBJECTS): the
    warning comes only when the project has that OTG instance, no USB instance of the old name,
    and the User Code uses the old name. The generated files stay the same.
    """
    flash = [f"{old} -> {new}" for old, new in _RENAMED_FLASH_NAMES.items() if old in names]
    if flash:
        logging.warning(
            tr(
                "User Code uses names of libxr 5.x that LibXR renamed: "
                f"{', '.join(flash)}; the third argument of STM32Flash is now the start "
                "address of the storage area instead of a sector index",
                f"User Code 用到了 libxr 5.x 中 LibXR 已改名的名字：{'、'.join(flash)}；"
                "STM32Flash 的第三个参数由扇区序号改为存储区的起始地址",
            )
        )
    objects = {
        (_USB_INSTANCES.get(instance.upper()) or "").lower()
        for instance in project_data.get("Peripherals", {}).get("USB", {})
    }
    for old, new in _RENAMED_USB_OBJECTS.items():
        if old in names and old not in objects and new in objects:
            logging.warning(
                tr(
                    f"User Code uses {old}: the USB OTG device object is now named {new} after "
                    f"its instance (libxr 5.x named it {old} after its speed)",
                    f"User Code 用到了 {old}：USB OTG 设备对象现在按实例命名为 {new}"
                    f"（libxr 5.x 按速度命名为 {old}）",
                )
            )


def _use_names_of_user_code(project_data: dict, existing_code: str, flash_map: bool) -> None:
    """记录 User Code 区域中的手写代码用到的驱动头文件和 HAL 句柄，让生成的文件仍然 include
    和声明它们；用到 libxr 5.x 之后改了名的名字时给出警告（见 _report_renamed_names()）。
    Record the driver headers and HAL handles that hand-written code in the User Code regions
    uses, so that the generated file still includes and declares them; names that changed after
    libxr 5.x are warned about (see _report_renamed_names()).

    只看区域内的标识符，注释和字符串不算；工程中没有的外设不会得到句柄声明。标记有问题的
    已有代码在这里跳过，由 validate_user_regions() 报告。
    Only identifiers in the regions count, not comments or strings, and a peripheral that the
    project does not have gets no handle declaration. Existing code with a faulty marker is
    skipped here and reported by validate_user_regions().
    """
    if not existing_code.strip():
        return
    try:
        names = {
            occurrence.text
            for region in CppDocument.parse(existing_code).user_regions()
            for occurrence in identifier_occurrences(region.body_text)
        }
    except ValueError:
        return
    _report_renamed_names(project_data, names)
    for header, symbols in _HEADER_NAMES.items():
        if names.intersection(symbols) and (flash_map or header != "flash_map.hpp"):
            _use_header(header)
    peripherals = project_data.get("Peripherals", {})
    handles = {
        f"h{instance.lower()}": handle_type
        for section, handle_type in _HANDLE_TYPES.items()
        for instance in peripherals.get(section, {})
    }
    for instance, config in peripherals.get("USB", {}).items():
        handle = _pcd_handle(instance, config if isinstance(config, dict) else {})
        if handle:
            handles[handle] = "PCD_HandleTypeDef"
    for instance in peripherals.get("USART", {}):
        handles["h" + instance.lower().replace("usart", "uart")] = "UART_HandleTypeDef"
    for handle in sorted(names.intersection(handles)):
        _use_handle(handles[handle], handle)


def _generate_header_includes(use_xrobot: bool = False) -> list[str]:
    """生成 app_main 的 #include 行和 ``using namespace LibXR;``：只含用到的驱动头文件。
    Generate the #include lines of app_main and ``using namespace LibXR;``: only the driver
    headers the code uses.

    main header app_main.h 单独成块排在最前；其余头文件按文件名排序成一块，与 clang-format 的
    IncludeBlocks: Regroup 一致。启用 XRobot 时另外 include xrobot_main.hpp。
    The main header app_main.h comes first in a block of its own; the other headers form one
    block sorted by file name, as clang-format does with IncludeBlocks: Regroup. With XRobot
    xrobot_main.hpp is included as well.
    """
    headers = {"libxr.hpp", "main.h", "stm32_power.hpp", "stm32_timebase.hpp"} | used_headers
    if use_xrobot:
        headers.add("xrobot_main.hpp")
    lines = ['#include "app_main.h"', ""]
    lines += [f'#include "{header}"' for header in sorted(headers)]
    return lines + ["", "using namespace LibXR;"]


def _generate_extern_declarations() -> list[str]:
    """生成用到的 HAL 句柄的 extern 声明，按类型和句柄名的自然顺序排列且不重复。
    Generate the extern declarations of the HAL handles the code uses, sorted by type and by
    handle name in natural order, without duplicates.

    句柄由各生成方法在使用时记录：非 SysTick 时基使用的定时器、每个生成了对象的外设实例和
    设备模式的 USB 实例（PCD 句柄，见 _pcd_handle()）。
    The generator methods record the handles as they use them: the timer of a timebase other
    than SysTick, each peripheral instance that got an object and the USB instances in device
    mode (the PCD handle, see _pcd_handle()).
    """
    handles = sorted(used_handles, key=lambda item: (item[0], _natural_key(item[1])))
    return [f"extern {handle_type} {name};" for handle_type, name in handles]


_USER_MARKER_TEXT = re.compile(r"(?://|/\*)\s*User\s*Code\s*(?:Begin|End)\b", re.IGNORECASE)
_USER_MARKER_BEGIN = re.compile(r"/\*\s*User Code Begin(?:\s+(.+?))?\s*\*/")
_USER_MARKER_END = re.compile(r"/\*\s*User Code End(?:\s+(.+?))?\s*\*/")
_PREPROC_DIRECTIVE = re.compile(r"#\s*(\w+)")


def _source_line(source: bytes, offset: int) -> int:
    """source 中字节偏移 offset 所在的行号，从 1 开始。
    The 1-based line number of byte offset offset in source.
    """
    return source.count(b"\n", 0, offset) + 1


def validate_user_regions(existing_code: str, region_names) -> None:
    """若改写会丢掉用户放在标记附近的代码，则拒绝改写；只含空白的代码直接通过。
    Refuse a rewrite that would drop code the user placed around markers; code that is only
    whitespace passes.

    改写只保留生成器自己的 User Code 区域的内容。格式错误、改名、重复、不成对、缺失或位于预处理
    条件之内的标记会静默丢失代码或改变预处理器保留的内容，因此每个这样的标记都会被报告。
    Only the bodies of the generator's own User Code regions survive a
    rewrite. A marker that is malformed, renamed, duplicated, unpaired,
    missing or inside a preprocessor conditional would silently lose code or
    change what the preprocessor keeps, so every such marker is reported.

    Args:
        region_names: 生成器输出的 User Code 区域名。
            The names of the User Code regions the generator emits.

    Raises:
        ValueError: 标记有问题；信息逐条列出全部问题。
            A marker has a problem; the message lists every problem.
    """
    if not existing_code.strip():
        return
    document = CppDocument.parse(existing_code)
    source = document.render_bytes()
    expected = list(region_names)
    problems = []
    elements = sorted(
        (
            element
            for element in document.root.descendants(include_trivia=True)
            if element.kind == "comment" or element.kind.startswith("preproc_")
        ),
        key=lambda element: element.span.start,
    )
    depth = 0
    open_region = None
    seen = []
    for element in elements:
        line = _source_line(source, element.span.start)
        if element.kind != "comment":
            directive = _PREPROC_DIRECTIVE.match(element.text.strip())
            keyword = directive.group(1) if directive else ""
            if keyword in ("if", "ifdef", "ifndef"):
                depth += 1
            elif keyword == "endif":
                depth = max(0, depth - 1)
            continue
        text = element.text.strip()
        if not _USER_MARKER_TEXT.match(text):
            continue
        begin = _USER_MARKER_BEGIN.fullmatch(text)
        end = _USER_MARKER_END.fullmatch(text)
        if begin is None and end is None:
            problems.append(
                tr(
                    f"line {line}: malformed User Code marker {text}",
                    f"第 {line} 行：User Code 标记格式错误：{text}",
                )
            )
            continue
        name = ((begin or end).group(1) or "").strip()
        if depth:
            problems.append(
                tr(
                    f"line {line}: {text} is inside a preprocessor conditional",
                    f"第 {line} 行：{text} 位于预处理条件之内",
                )
            )
        if name not in expected:
            names = ", ".join(expected)
            problems.append(
                tr(
                    f"line {line}: {text} names a region the generator does not emit "
                    f"(expected {names})",
                    f"第 {line} 行：{text} 指定的区域不是生成器输出的区域（应为 {names}）",
                )
            )
        if begin is not None:
            if open_region is not None:
                problems.append(
                    tr(
                        f"line {line}: {text} opens before User Code End {open_region}",
                        f"第 {line} 行：{text} 出现在 User Code End {open_region} 之前",
                    )
                )
            if name in seen:
                problems.append(
                    tr(f"line {line}: {text} is duplicated", f"第 {line} 行：{text} 重复")
                )
            seen.append(name)
            open_region = name
        else:
            if open_region != name:
                problems.append(
                    tr(
                        f"line {line}: {text} has no matching Begin marker",
                        f"第 {line} 行：{text} 没有对应的 Begin 标记",
                    )
                )
            else:
                open_region = None
    if open_region is not None:
        problems.append(
            tr(
                f"User Code Begin {open_region} has no matching End marker",
                f"User Code Begin {open_region} 没有对应的 End 标记",
            )
        )
    for name in expected:
        if name not in seen:
            problems.append(
                tr(
                    f"User Code Begin {name} / End {name} markers are missing",
                    f"缺少 User Code Begin {name} / End {name} 标记",
                )
            )
    if problems:
        raise ValueError(
            tr(
                "existing User Code markers cannot be preserved safely; nothing was "
                "written. Fix the markers and regenerate:\n  ",
                "已有的 User Code 标记无法安全保留，未写入任何文件。请修正这些标记后重新生成：\n  ",
            )
            + "\n  ".join(problems)
        )


def _preserve_generated_regions(existing_code: str, generated_code: str) -> str:
    """把已有代码中 User Code 区域的内容填回新生成的代码并返回结果；其余代码全部重新生成，包括
    clang-format 和 NOLINT 保护的代码。
    Put the User Code bodies of the existing code into the newly generated code and return the
    result; everything else is regenerated, including code protected by clang-format and NOLINT
    markers.

    clang-format 和 NOLINT 控制的是工具，不表示生成代码的归属；嵌套在 User Code 中的标记仍属于保留
    的用户内容。
    clang-format and NOLINT control tooling, not ownership of generated code.
    Markers nested inside User Code remain part of the preserved user body.

    Raises:
        ValueError: validate_user_regions() 拒绝已有代码中的标记。
            validate_user_regions() rejects the markers of the existing code.
    """
    previous = CppDocument.parse(existing_code)
    current = CppDocument.parse(generated_code)
    validate_user_regions(existing_code, [region.name for region in current.user_regions()])
    used = set()
    for old_region in previous.user_regions():
        regions = list(current.user_regions())
        match = next(
            (
                (index, region)
                for index, region in enumerate(regions)
                if index not in used and region.name == old_region.name
            ),
            None,
        )
        if match is None:
            continue
        index, region = match
        current = current.replace_region_body(region, old_region.body_text)
        used.add(index)
    return current.render_bytes().decode("utf-8", errors="surrogateescape")


# LibXR 的线程优先级等级，下标即配置中的数值 0-4。
# The thread priority levels of LibXR; the index is the configuration value 0-4.
_PRIORITY_LEVELS = ("IDLE", "LOW", "MEDIUM", "HIGH", "REALTIME")


# RTOS 的优先级数所在的头文件（相对工程根目录）和宏，以及头文件中没有定义时的默认值。
# The header (relative to the project root) and macro of the RTOS priority count, and the
# default when the header does not define it.
_RTOS_PRIORITY_COUNTS = {
    "FreeRTOS": (os.path.join("Core", "Inc", "FreeRTOSConfig.h"), "configMAX_PRIORITIES", None),
    "ThreadX": (os.path.join("Core", "Inc", "tx_user.h"), "TX_MAX_PRIORITIES", 32),
}


def read_rtos_priorities(directories: list[str]) -> int | None:
    """工程的 RTOS 优先级数：FreeRTOS 的 configMAX_PRIORITIES 或 ThreadX 的 TX_MAX_PRIORITIES。
    The RTOS priority count of the project: configMAX_PRIORITIES of FreeRTOS or
    TX_MAX_PRIORITIES of ThreadX.

    依次在 directories 中找 Core/Inc 下的头文件（FreeRTOSConfig.h 或 tx_user.h），取第一个找到的
    文件中未注释的 #define；tx_user.h 没有定义时为 ThreadX 的默认值 32。裸机工程、找不到头文件或
    读不出数值时为 None。SYSTEM 取 load_configuration() 写入的值。
    The header under Core/Inc (FreeRTOSConfig.h or tx_user.h) is looked for in each of
    directories in turn, and the #define that is not commented out in the first one found is
    taken; a tx_user.h without it gives the ThreadX default 32. A bare-metal project, a missing
    header or an unreadable number gives None. SYSTEM is the value load_configuration() wrote.
    """
    entry = _RTOS_PRIORITY_COUNTS.get(libxr_settings["SYSTEM"])
    if entry is None:
        return None
    header, macro, default = entry
    for directory in directories:
        path = os.path.join(directory, header)
        try:
            with open(path, encoding="utf-8", errors="replace") as stream:
                text = stream.read()
        except OSError:
            continue
        text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
        match = re.search(rf"^[ \t]*#[ \t]*define[ \t]+{macro}[ \t]+\(?[ \t]*(\d+)", text, re.M)
        return int(match.group(1)) if match else default
    return None


def _rtos_priority(level: str) -> int | None:
    """等级 level 在本工程中对应的 RTOS 优先级（按 LibXR 的 LIBXR_PRIORITY_STEP 换算）；裸机工程
    或不知道 RTOS 的优先级数时为 None。
    The RTOS priority of the level level in this project, converted by LibXR's
    LIBXR_PRIORITY_STEP; None for a bare-metal project or when the RTOS priority count is unknown.

    LIBXR_PRIORITY_STEP 为 (优先级数 - 1) / 5。FreeRTOS 的等级 n 为 n 倍步长；ThreadX 数值越小越高，
    IDLE 到 HIGH 为 4 到 1 倍步长，REALTIME 为 1。
    LIBXR_PRIORITY_STEP is (priority count - 1) / 5. On FreeRTOS level n is n steps; on ThreadX,
    where lower numbers are higher, IDLE to HIGH are 4 to 1 steps and REALTIME is 1.
    """
    if rtos_priorities is None or libxr_settings["SYSTEM"] not in _RTOS_PRIORITY_COUNTS:
        return None
    step = (rtos_priorities - 1) // 5
    index = _PRIORITY_LEVELS.index(level)
    if libxr_settings["SYSTEM"] == "ThreadX":
        return 1 if level == "REALTIME" else step * (4 - index)
    return step * index


def _priority_level(key: str, value) -> str:
    """配置中的线程优先级 value 对应的 LibXR::Thread::Priority 枚举名。
    The LibXR::Thread::Priority enumerator of the thread priority value of the configuration.

    value 是 0-4 的整数或大小写不限的等级名。LibXR 按 RTOS 的优先级数把等级换算为 RTOS 优先级
    （FreeRTOS 数值越大越高，ThreadX 数值越小越高），所以生成的代码写枚举而不写数值。libxr 5.x
    把整数原样作为 RTOS 优先级；换算后的优先级与这个整数不同时记录一条说明（见 _rtos_priority()）。
    value is an integer 0-4 or a level name in any case. LibXR converts the levels to RTOS
    priorities by the RTOS priority count (higher numbers are higher on FreeRTOS, lower numbers
    on ThreadX), so the generated code names the enumerator instead of a number. libxr 5.x
    passed the integer to the RTOS as it was; when the converted priority differs from that
    integer, a notice is logged (see _rtos_priority()).

    Raises:
        LibXRConfigError: value 既不是 0-4 也不是等级名；key 是出错的设置。
            value is neither 0-4 nor a level name; key names the setting.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value < len(_PRIORITY_LEVELS):
            level = _PRIORITY_LEVELS[value]
            priority = _rtos_priority(level)
            if priority is not None and priority != value:
                system = libxr_settings["SYSTEM"]
                count = f"{_RTOS_PRIORITY_COUNTS[system][1]} {rtos_priorities}"
                logging.info(
                    tr(
                        f"{key} {value} is the priority level {level}, which is {system} "
                        f"priority {priority} with {count}; libxr 5.x passed {value} to "
                        f"{system} as it was",
                        f"{key} 的 {value} 表示优先级等级 {level}，按 {count} 即 {system} 优先级 "
                        f"{priority}；libxr 5.x 把 {value} 原样作为 {system} 的优先级",
                    )
                )
            return level
    elif isinstance(value, str) and value.strip().upper() in _PRIORITY_LEVELS:
        return value.strip().upper()
    raise _invalid_setting(
        key,
        value,
        f"a priority level; use 0-4 or {', '.join(_PRIORITY_LEVELS)}",
        f"优先级等级；请使用 0-4 或 {'、'.join(_PRIORITY_LEVELS)}",
    )


def _generate_core_system(project_data: dict) -> list[str]:
    """生成时基对象、PlatformInit() 调用和 power_manager 对象的代码，各行带缩进。
    Generate the code of the timebase object, the PlatformInit() call and the power_manager
    object, the lines indented.

    时基来源为 SysTick 时使用 STM32Timebase，否则使用该定时器的 STM32TimerTimebase。FreeRTOS 和
    ThreadX 下 PlatformInit() 取软件定时器的优先级等级（见 _priority_level()）和栈深度；不支持的
    SYSTEM 记录错误并以状态 1 退出。
    SysTick gives STM32Timebase and any other source gives STM32TimerTimebase on that timer.
    Under FreeRTOS and ThreadX PlatformInit() takes the priority level (see _priority_level())
    and stack depth of the software timer; an unsupported SYSTEM logs an error and exits with
    status 1.

    Raises:
        ValueError: 时基来源是 LPTIM 或 HRTIM；STM32TimerTimebase 只接受 TIM 的句柄
            （TIM_HandleTypeDef）。
            The timebase source is an LPTIM or HRTIM; STM32TimerTimebase takes only the handle
            of a TIM (TIM_HandleTypeDef).
    """
    timebase_cfg = project_data.get("Timebase", {"Source": "SysTick"})
    source = timebase_cfg.get("Source", "SysTick")

    if source.startswith(("LPTIM", "HRTIM")):
        raise ValueError(
            tr(
                f"the HAL timebase is {source}, but the LibXR timebase supports only TIM timers "
                "(STM32TimerTimebase takes a TIM_HandleTypeDef); nothing was written. In "
                "STM32CubeMX, set SYS > Timebase Source to a TIM timer (such as TIM6) and "
                "regenerate",
                f"HAL 时基是 {source}，而 LibXR 的时基只支持 TIM 定时器（STM32TimerTimebase 接受 "
                "TIM_HandleTypeDef），未写入任何文件。请在 STM32CubeMX 的 SYS 中把 Timebase Source "
                "改为 TIM 定时器（例如 TIM6）后重新生成",
            )
        )

    _use_header("stm32_timebase.hpp")
    _use_header("stm32_power.hpp")
    if source != "SysTick":
        handler = f"h{source.lower()}"
        if source.startswith("TIM"):
            _use_handle("TIM_HandleTypeDef", handler)
        timebase = layout("static STM32TimerTimebase timebase", [f"&{handler}"])
    else:
        # 默认使用 SysTick / Default to SysTick
        timebase = [f"{INDENT}static STM32Timebase timebase;"]

    system_type = libxr_settings["SYSTEM"]
    timer_cfg = libxr_settings["software_timer"]

    init_args = []
    if system_type == "None":  # 裸机 / Bare-metal
        init_args = []
    elif system_type == "FreeRTOS" or system_type == "ThreadX":
        level = _priority_level("software_timer.priority", timer_cfg["priority"])
        stack_depth = _integer("software_timer.stack_depth", timer_cfg["stack_depth"])
        init_args = [f"static_cast<uint32_t>(Thread::Priority::{level})", str(stack_depth)]
    else:
        logging.error(
            tr(f"Unsupported system type: {system_type}", f"不支持的系统类型：{system_type}")
        )
        sys.exit(1)

    return (
        [f"{INDENT}// Timebase and platform"]
        + timebase
        + layout("PlatformInit", init_args)
        + [f"{INDENT}static STM32PowerManager power_manager;"]
    )


def generate_gpio_config(project_data: dict) -> list[str]:
    """为 GPIO 段中的每个引脚生成一个 STM32GPIO 对象并登记；EXTI 引脚带中断号。
    Generate one STM32GPIO object per pin of the GPIO section and register it; EXTI pins get
    their IRQ number.

    返回带 ``// GPIO`` 说明注释的各行；没有引脚时为空列表。
    Returns the lines with the ``// GPIO`` comment; an empty list without any pin.
    """
    lines = []
    for port, config in project_data.get("GPIO", {}).items():
        name, arguments = generate_gpio_declaration(port, config, project_data)
        lines += layout(f"static STM32GPIO {name}", arguments)
    if not lines:
        return []
    _use_header("stm32_gpio.hpp")
    return [f"{INDENT}// GPIO"] + lines


# --------------------------
# Flash 与数据库 / Flash and Database
# --------------------------
# database.block_size 的默认值：由 LibXR 按芯片的 HAL 推出 Flash 的最小写入单元。
# The default of database.block_size: LibXR derives the minimum write unit of the Flash from
# the HAL of the chip.
AUTO_BLOCK_SIZE = "auto"
# block_size 为 auto 时 DatabaseRaw 的模板参数。
# The template argument of DatabaseRaw when block_size is auto.
MIN_WRITE_SIZE = "STM32Flash::MIN_WRITE_SIZE"


def _block_size(value) -> str:
    """database.block_size 的值 value 对应的 DatabaseRaw 模板参数：auto 为 MIN_WRITE_SIZE，即
    LibXR 按芯片的 HAL 推出的 Flash 最小写入单元；正整数（字节）原样写出，字符串按
    _integer() 解析。auto 不区分大小写。
    The DatabaseRaw template argument for value, the value of database.block_size: auto gives
    MIN_WRITE_SIZE, the minimum write unit of the Flash that LibXR derives from the HAL of the
    chip; a positive integer (bytes) is written as it is, a string read as in _integer(). auto
    is not case-sensitive.

    Raises:
        LibXRConfigError: value 既不是 auto 也不是正整数；信息中写出这两种写法。
            value is neither auto nor a positive integer; the message names both forms.
    """
    if isinstance(value, str) and value.strip().lower() == AUTO_BLOCK_SIZE:
        return MIN_WRITE_SIZE
    try:
        return str(_integer("database.block_size", value))
    except LibXRConfigError:
        raise _invalid_setting(
            "database.block_size",
            value,
            f"{AUTO_BLOCK_SIZE} or a positive integer",
            f"正整数或 {AUTO_BLOCK_SIZE}",
        ) from None


def generate_database(flash_map: bool) -> list[str]:
    """database.enable 为真时生成 STM32Flash 对象和 DatabaseRaw 数据库，并把数据库登记为
    database；否则为空列表。
    With database.enable, generate the STM32Flash object and the DatabaseRaw database and
    register the database as database; an empty list otherwise.

    STM32Flash 使用 flash_map.hpp 中的扇区表和末尾两个扇区，分别作主块和备份块。block_size 决定
    DatabaseRaw 的模板参数，即 Flash 的最小写入单元：默认的 auto 生成
    DatabaseRaw<STM32Flash::MIN_WRITE_SIZE>，正整数生成 DatabaseRaw<N>（见 _block_size()）。
    flash_map 为假表示没有 flash_map.hpp。
    STM32Flash uses the sector table of flash_map.hpp and its last two sectors, the main and the
    backup block. block_size gives the template argument of DatabaseRaw, the minimum write unit
    of the Flash: the default auto generates DatabaseRaw<STM32Flash::MIN_WRITE_SIZE>, and a
    positive integer generates DatabaseRaw<N> (see _block_size()). Without flash_map there is no
    flash_map.hpp.

    Raises:
        LibXRConfigError: enable 不是布尔值，block_size 既不是 auto 也不是正整数，或没有
            flash_map.hpp。
            enable is not a boolean, block_size is neither auto nor a positive integer, or there
            is no flash_map.hpp.
    """
    database = _settings("database")
    enable = _flag("database.enable", database.setdefault("enable", False))
    block_size = _block_size(database.setdefault("block_size", AUTO_BLOCK_SIZE))
    if not enable:
        return []
    if not flash_map:
        raise LibXRConfigError(
            tr(
                f"{libxr_config_origin}: database.enable is true, but no flash_map.hpp can be "
                "generated for this MCU, so the database has no Flash layout",
                f"{libxr_config_origin}：database.enable 为 true，但无法为这个 MCU 生成 "
                "flash_map.hpp，数据库没有 Flash 布局",
            )
        )
    _use_header("stm32_flash.hpp")
    _use_header("flash_map.hpp")
    _register_device("database", "Database")
    return [
        f"{INDENT}// Flash and database",
        f"{INDENT}static STM32Flash flash(FLASH_REGIONS, FLASH_REGION_NUMBER);",
        f"{INDENT}static DatabaseRaw<{block_size}> database(flash);",
    ]


# 看门狗
# Watchdog
def configure_watchdog(project_data: dict) -> list[str]:
    """为每个已启用的 IWDG 生成首次喂狗和周期喂狗的代码；没有已启用的 IWDG 时为空列表。
    Generate the first feed and the periodic feeding of every enabled IWDG; an empty list when
    no IWDG is enabled.

    libxr_settings 中 Watchdog 的 run_as_thread 为真时由独立线程喂狗，线程优先级见
    _priority_level()；否则由软件定时器任务每隔 feed_interval_ms（默认 250）喂狗一次。
    With run_as_thread of Watchdog in libxr_settings a thread of its own feeds the watchdog,
    its priority given as in _priority_level(); otherwise a software timer task feeds it every
    feed_interval_ms (default 250).
    """
    lines = []
    watchdog_instances = []
    for name, cfg in project_data.get("Peripherals", {}).get("IWDG", {}).items():
        if cfg.get("Enabled"):
            watchdog_instances.append(name.lower())
    if not watchdog_instances:
        return lines

    wdg_config = _settings("Watchdog")
    run_as_thread = _flag("Watchdog.run_as_thread", wdg_config.setdefault("run_as_thread", False))
    feed_interval = _integer(
        "Watchdog.feed_interval_ms", wdg_config.setdefault("feed_interval_ms", 250)
    )

    for name in watchdog_instances:
        watchdog = f"reinterpret_cast<LibXR::Watchdog*>(&{name})"
        lines.append(f"{INDENT}{name}.Feed();")
        if run_as_thread:
            thread_stack = _integer(
                "Watchdog.thread_stack_depth", wdg_config.setdefault("thread_stack_depth", 1024)
            )
            level = _priority_level(
                "Watchdog.thread_priority", wdg_config.setdefault("thread_priority", 3)
            )
            lines.append(f"{INDENT}static Thread {name}_thread;")
            lines += layout(
                f"{name}_thread.Create",
                [
                    watchdog,
                    f"{name}.ThreadFun",
                    f'"{name}_wdg"',
                    str(thread_stack),
                    f"Thread::Priority::{level}",
                ],
            )
        else:
            lines += layout(
                f"static auto {name}_task = Timer::CreateTask",
                [f"{name}.TaskFun", watchdog, str(feed_interval)],
            )
            lines.append(f"{INDENT}Timer::Add({name}_task);")
            lines.append(f"{INDENT}Timer::Start({name}_task);")
    return lines


# --------------------------
# 终端配置 / Terminal Configuration
# --------------------------
def configure_terminal(project_data: dict) -> list[str]:
    """把 terminal_source 指定的串口设为标准输入输出，并生成 RamFS、Terminal 对象及运行终端的代码。
    Make the UART named by terminal_source the standard I/O and generate the RamFS and Terminal
    objects and the code that runs the terminal.

    terminal_source 为空时返回空列表；它未登记为 UART 时记录警告，不初始化终端并返回空列表，它是
    enable 为 false 的 USB 实例的 CDC 串口（如 usb_fs_cdc、usb_fs_cdc2）时，警告写出要修改的键。
    Terminal 的 run_as_thread 为真时终端运行于独立线程（优先级见 _priority_level()），否则由软件
    定时器任务每 10 ms 运行一次。
    With an empty terminal_source an empty list is returned; when it is not registered as UART
    a warning is logged, the terminal is not initialized and the list is empty, and when it is
    the CDC serial port of a USB instance whose enable is false, such as usb_fs_cdc or
    usb_fs_cdc2, the warning names the key to change. With run_as_thread of Terminal the
    terminal runs in a thread of its own, its priority given as in _priority_level();
    otherwise a software timer task runs it every 10 ms.
    """
    terminal_source = _text("terminal_source", libxr_settings.get("terminal_source")).lower()
    if terminal_source == "":
        return []

    # 设备必须已登记且类型为 UART，否则记录警告并跳过
    # Device must be registered and of type UART, otherwise log a warning and skip
    if registered_devices.get(terminal_source) != "UART":
        cdc = re.fullmatch(r"(.+)_cdc\d*", terminal_source)
        usb = cdc.group(1) if cdc else terminal_source
        usb_settings = libxr_settings.get("USB", {}).get(usb)
        if cdc and isinstance(usb_settings, dict) and usb_settings.get("enable") is False:
            logging.warning(
                tr(
                    f"terminal_source '{terminal_source}' is the CDC serial port of USB "
                    f"instance {usb}, which is not generated; set USB.{usb}.enable to true in "
                    "libxr_config.yaml. The terminal is not initialized.",
                    f"terminal_source '{terminal_source}' 是 USB 实例 {usb} 的 CDC 串口，而这个"
                    f"实例没有生成；请在 libxr_config.yaml 中把 USB.{usb}.enable 设为 true。"
                    "终端不初始化。",
                )
            )
            return []
        logging.warning(
            tr(
                f"terminal_source '{terminal_source}' is not registered as UART, terminal "
                "will not be initialized.",
                f"terminal_source '{terminal_source}' 没有登记为 UART，不初始化终端。",
            )
        )
        return []

    term_config = _settings("Terminal")
    params = [
        _integer(f"Terminal.{key}", term_config.setdefault(key, default))
        for key, default in (
            ("read_buff_size", 32),
            ("max_line_size", 32),
            ("max_arg_number", 5),
            ("max_history_number", 5),
        )
    ]
    run_as_thread = _flag("Terminal.run_as_thread", term_config.setdefault("run_as_thread", False))
    if run_as_thread:
        thread_stack_depth = _integer(
            "Terminal.thread_stack_depth", term_config.setdefault("thread_stack_depth", 1024)
        )
        level = _priority_level(
            "Terminal.thread_priority", term_config.setdefault("thread_priority", 3)
        )

    terminal_type = f"Terminal<{', '.join(map(str, params))}>"
    lines = [
        f"{INDENT}// Terminal on {terminal_source}",
        f"{INDENT}STDIO::read_ = {terminal_source}.read_port_;",
        f"{INDENT}STDIO::write_ = {terminal_source}.write_port_;",
        f'{INDENT}static RamFS ramfs("XRobot");',
        f"{INDENT}static {terminal_type} terminal(ramfs);",
    ]
    if run_as_thread:
        lines.append(f"{INDENT}static Thread term_thread;")
        lines += layout(
            "term_thread.Create",
            [
                "&terminal",
                "terminal.ThreadFun",
                '"terminal"',
                str(thread_stack_depth),
                f"Thread::Priority::{level}",
            ],
        )
    else:
        lines += layout(
            "static auto terminal_task = Timer::CreateTask",
            ["terminal.TaskFun", "&terminal", "10"],
        )
        lines.append(f"{INDENT}Timer::Add(terminal_task);")
        lines.append(f"{INDENT}Timer::Start(terminal_task);")
    _register_device("ramfs", "RamFS")
    _register_device("terminal", terminal_type)
    return lines


# --------------------------
# XRobot 集成 / XRobot Integration
# --------------------------
# 登记行按种类分组，各组按这个顺序排列；表中没有的种类排在最后，保持登记的先后。
# Registration lines are grouped by kind in this order; kinds not in the table come last, in
# the order they were registered.
_REGISTRATION_ORDER = (
    "PowerManager",
    "GPIO",
    "ADC",
    "DAC",
    "PWM",
    "SPI",
    "UART",
    "I2C",
    "CAN",
    "FDCAN",
    "Watchdog",
    "RamFS",
    "Terminal",
    "Database",
)


def generate_xrobot_registrations() -> list[str]:
    """为每个登记的对象生成一行 XR_REGISTER，按种类分组，使静态入口无需运行时容器即可按名字取得
    BSP 对象；各组之间空一行。
    Generate one XR_REGISTER line per registered object, grouped by kind, exposing the named BSP
    objects to the static entry without a runtime container; groups are separated by a blank
    line.

    每个生成的设备对象以自己的 C++ 名字登记，类型缺少 LibXR:: 前缀时补上；YAML 配置按这些名字
    选择硬件。
    Every generated device object is registered under its own C++ name, with LibXR:: added to
    a type that lacks it; the YAML configuration selects hardware by these names.

    Raises:
        ValueError: 名字不是合法的 C++ 标识符，或缺少类型。
            A name is not a valid C++ identifier, or its type is missing.
    """
    groups: dict[str, list[str]] = {}
    for name, cpp_type in registered_devices.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
            raise ValueError(
                tr(
                    f"Static registration needs an existing C++ name: {name}",
                    f"静态登记需要已有的 C++ 名字：{name}",
                )
            )
        if not isinstance(cpp_type, str) or not cpp_type or cpp_type == "Unknown":
            raise ValueError(
                tr(
                    f"Explicit registration type is missing for {name}",
                    f"{name} 缺少显式的登记类型",
                )
            )
        if not cpp_type.startswith("LibXR::"):
            cpp_type = "LibXR::" + cpp_type
        kind = cpp_type.removeprefix("LibXR::").split("<")[0]
        groups.setdefault(kind, []).append(f"{INDENT}XR_REGISTER({name}, {cpp_type});")
    order = [kind for kind in _REGISTRATION_ORDER if kind in groups]
    order += [kind for kind in groups if kind not in _REGISTRATION_ORDER]
    return join_blocks([groups[kind] for kind in order])


# --------------------------
# 主生成流程 / Main Generator
# --------------------------
def reject_user_xrobot_main(existing_code: str) -> None:
    """拒绝在 User Code 区域中调用 XROBOT_MAIN() 的已有代码；该调用属于生成器，区域中的副本是遗留。
    Reject existing code that calls XROBOT_MAIN() inside a User Code region; the call belongs
    to the generator, and a User Code copy is a leftover.

    旧版生成器把该调用作为 User Code 3 的默认内容。保留这份副本会产生第二个入口调用，因此由用户
    删除。
    Older generators emitted the call as the default body of User Code 3.
    Keeping that copy would leave a second entry call, so the user deletes it.

    Raises:
        ValueError: 某个 User Code 区域调用了 XROBOT_MAIN()。
            A User Code region calls XROBOT_MAIN().
    """
    if not existing_code.strip():
        return
    document = CppDocument.parse(existing_code)
    for region in document.user_regions():
        for invocation in document.invocation_views("XROBOT_MAIN"):
            if region.body_span.start <= invocation.span.start < region.body_span.end:
                raise ValueError(
                    tr(
                        f"line {invocation.line}: User Code {region.name} still calls "
                        f"{invocation.text}. The generator now emits XROBOT_MAIN() after "
                        "the User Code regions of app_main; delete this call from the "
                        "User Code region and regenerate. Nothing was written.",
                        f"第 {invocation.line} 行：User Code {region.name} 仍然调用 "
                        f"{invocation.text}。生成器现在在 app_main 的 User Code 区域之后输出 "
                        "XROBOT_MAIN()；请从 User Code 区域中删除这一调用后重新生成。"
                        "未写入任何文件。",
                    )
                )


# libxr 5.x 带 --xrobot 生成时 User Code 3 的默认内容，去掉全部空白：以生成的 HardwareContainer
# 调用 XRobot 的主函数。
# The default body of User Code 3 that libxr 5.x generated with --xrobot, with all whitespace
# removed: it called the XRobot main function with the generated HardwareContainer.
_XROBOT_MAIN_3_V5 = "XRobotMain(peripherals);"


def _hardware_container_use(occurrence) -> str | None:
    """标识符 occurrence 是否用到 libxr 5.x 的 HardwareContainer：返回用法的写法（调用
    XRobotMain、访问 peripherals 的成员或写出 HardwareContainer），否则为 None。
    Whether the identifier occurrence uses the HardwareContainer of libxr 5.x: the spelling of
    the use (a call of XRobotMain, a member access of peripherals or the name HardwareContainer),
    None otherwise.
    """
    if occurrence.text == "XRobotMain" and occurrence.following in ("(", "<"):
        return "XRobotMain(...)"
    if occurrence.text == "peripherals" and occurrence.following == ".":
        return "peripherals."
    if occurrence.text == "HardwareContainer":
        return "HardwareContainer"
    return None


def reject_user_hardware_container(existing_code: str, use_xrobot: bool) -> None:
    """拒绝 User Code 区域中仍用到 libxr 5.x 的 HardwareContainer 的已有代码，逐条写出行号。
    Reject existing code whose User Code regions still use the HardwareContainer of libxr 5.x,
    naming the line of each use.

    5.x 带 --xrobot（或 --hw-cntr）时生成 LibXR::HardwareContainer peripherals，User Code 3 的默认
    内容是 XRobotMain(peripherals);。LibXR 已删除 HardwareContainer，这些代码无法编译，所以在写入
    任何文件之前停止。use_xrobot 为真时，内容恰好是这个默认值的 User Code 3 不算（由
    _without_default_loop() 清空）；注释和字符串中的名字不算。
    With --xrobot (or --hw-cntr) 5.x generated LibXR::HardwareContainer peripherals, and the
    default body of User Code 3 was XRobotMain(peripherals);. LibXR no longer has
    HardwareContainer and such code cannot compile, so generation stops before any file is
    written. With use_xrobot, a User Code 3 whose body is exactly that default does not count
    (_without_default_loop() empties it); names in comments and strings do not count.

    Raises:
        ValueError: User Code 调用了 XRobotMain、访问了 peripherals 的成员或写出 HardwareContainer；
            信息逐条列出这些行。
            User Code calls XRobotMain, accesses a member of peripherals or names
            HardwareContainer; the message lists each line.
    """
    if not existing_code.strip():
        return
    document = CppDocument.parse(existing_code)
    try:
        occurrences = document.identifier_occurrences()
    except ValueError:
        # 词法错误由 validate_user_regions() 报告。
        # validate_user_regions() reports lexical errors.
        return
    source = document.render_bytes()
    problems = []
    for region in document.user_regions():
        body = "".join(region.body_text.split())
        if use_xrobot and region.name == "3" and body == _XROBOT_MAIN_3_V5:
            continue
        for occurrence in occurrences:
            if not region.body_span.start <= occurrence.span.start < region.body_span.end:
                continue
            use = _hardware_container_use(occurrence)
            if use is not None:
                line = _source_line(source, occurrence.span.start)
                problems.append(
                    (
                        line,
                        tr(
                            f"line {line}: User Code {region.name} uses {use}",
                            f"第 {line} 行：User Code {region.name} 用到了 {use}",
                        ),
                    )
                )
    if problems:
        raise ValueError(
            tr(
                "User Code still uses the HardwareContainer of libxr 5.x, which LibXR no longer "
                "has; nothing was written. With --xrobot the generator registers the objects "
                "with XR_REGISTER and calls XROBOT_MAIN() after the User Code regions, which "
                "replaces XRobotMain(peripherals). Remove these uses and regenerate:\n  ",
                "User Code 仍然用到 libxr 5.x 的 HardwareContainer，LibXR 已删除它，未写入任何文件。"
                "带 --xrobot 时生成器以 XR_REGISTER 注册对象，并在 User Code 区域之后调用 "
                "XROBOT_MAIN()，取代 XRobotMain(peripherals)。请删除以下用法后重新生成：\n  ",
            )
            + "\n  ".join(text for _, text in sorted(problems))
        )


GENERATED_NOTICE = "// Generated by `libxr gen`; do not edit by hand."


def _app_main_notice(project_data: dict) -> list[str]:
    """app_main 源文件开头的两行说明：由哪些文件生成，以及只能改 User Code 区域。
    The two notice lines at the start of the app_main source: which files it is generated from
    and that only the User Code regions are for editing.

    工程 YAML 中有 Ioc（libxr parse 记下的 .ioc 文件名）时写出文件名，否则写 CubeMX 工程。
    With Ioc in the project YAML, the .ioc file name libxr parse recorded, the file name is
    written, otherwise the CubeMX project.
    """
    source = project_data.get("Ioc") or "the CubeMX project"
    return [
        f"// Generated by `libxr gen` from {source} and {libxr_config_label}.",
        '// Edit only between "User Code Begin" and "User Code End"; the rest is regenerated.',
    ]


# 不带 --xrobot 生成时 User Code 3 的默认循环，去掉全部空白；5.x 版本写成 while(true) { ... }，
# 只有空白不同。
# The default loop of User Code 3 generated without --xrobot, with all whitespace removed;
# versions 5.x wrote it as while(true) { ... }, which differs only in whitespace.
_DEFAULT_LOOP_3 = "while(true){Thread::Sleep(UINT32_MAX);}"


def _without_default_loop(existing_code: str) -> str:
    """User Code 3 仍是不带 --xrobot 生成的默认循环时清空它，使其后的 XROBOT_MAIN() 能够执行；
    仍是 libxr 5.x 带 --xrobot 生成的默认内容 XRobotMain(peripherals); 时同样清空，并记录一条说明。
    比较时忽略空白，其他内容不变。
    Empty User Code 3 when it still holds the default loop generated without --xrobot, so that
    the XROBOT_MAIN() after it runs; the default body XRobotMain(peripherals); that libxr 5.x
    generated with --xrobot is emptied too, with a notice. Whitespace is ignored in the
    comparison, and any other content is kept.
    """
    if not existing_code.strip():
        return existing_code
    document = CppDocument.parse(existing_code)
    for region in document.user_regions():
        if region.name != "3":
            continue
        body = "".join(region.body_text.split())
        if body not in (_DEFAULT_LOOP_3, _XROBOT_MAIN_3_V5):
            return existing_code
        if body == _XROBOT_MAIN_3_V5:
            logging.info(
                tr(
                    "User Code 3: removed XRobotMain(peripherals);, the default of libxr 5.x; "
                    "XROBOT_MAIN() after the User Code regions replaces it",
                    "User Code 3：已删除 libxr 5.x 的默认内容 XRobotMain(peripherals);，"
                    "User Code 区域之后的 XROBOT_MAIN() 取代了它",
                )
            )
        document = document.replace_region_body(region, "\n" + INDENT)
        return document.render_bytes().decode("utf-8", errors="surrogateescape")
    return existing_code


def generate_full_code(
    project_data: dict, use_xrobot: bool, existing_code: str, flash_map: bool = True
) -> str:
    """生成 app_main 源文件的完整内容，并填回已有代码中 User Code 区域的内容。
    Generate the full content of the app_main source file and put back the User Code bodies of
    the existing code.

    输出已按 LibXR 的 clang-format 风格排版（见 libxr.cpp_layout），不再用 clang-format 和
    NOLINT 标记保护。启用 XRobot 时为每个生成的对象输出 XR_REGISTER，并在 User Code 3 之后调用
    XROBOT_MAIN()；否则 User Code 3 的默认内容是一个无限休眠的循环。启用 XRobot 时，已有代码的
    User Code 3 若仍是这个循环（或 libxr 5.x 的默认内容 XRobotMain(peripherals);）则被清空，否则
    XROBOT_MAIN() 永远执行不到。User Code 中其他用到 HardwareContainer 的代码被拒绝（见
    reject_user_hardware_container()）。flash_map 为假表示没有 flash_map.hpp，这时不能启用数据库。
    The output is laid out in the clang-format style of LibXR (see libxr.cpp_layout) and is
    not protected by clang-format and NOLINT markers. With XRobot every generated object gets
    an XR_REGISTER line and XROBOT_MAIN() is called after User Code 3; otherwise the default
    body of User Code 3 is a loop that sleeps forever. With XRobot, a User Code 3 of the
    existing code that still holds this loop (or XRobotMain(peripherals);, the default of libxr
    5.x) is emptied, since XROBOT_MAIN() would never run after it. Any other use of
    HardwareContainer in the User Code is rejected (see reject_user_hardware_container()).
    Without flash_map there is no flash_map.hpp, and the database cannot be enabled.

    Raises:
        ValueError: 生成的对象名冲突，或 GPIO 名字、User Code 标记、遗留的 XROBOT_MAIN() 调用、
            遗留的 HardwareContainer 用法不合要求。
            Generated object names collide, or a GPIO name, a User Code marker, a leftover
            XROBOT_MAIN() call or a leftover use of HardwareContainer is rejected.
    """
    reject_user_hardware_container(existing_code, use_xrobot)
    if use_xrobot:
        reject_user_xrobot_main(existing_code)
        existing_code = _without_default_loop(existing_code)
    _default_usb_enables(project_data.get("Peripherals", {}))
    # DMA 缓冲区先登记，外设对象才能按缓冲区的存储方式引用它们。
    # The DMA buffers are registered first, so that the peripheral objects can refer to them
    # the way they are stored.
    dma = generate_dma_resources(project_data)
    core = _generate_core_system(project_data)
    gpio = generate_gpio_config(project_data)
    peripherals = generate_peripheral_instances(project_data, use_xrobot)
    terminal = configure_terminal(project_data)
    database = generate_database(flash_map)
    _use_names_of_user_code(project_data, existing_code, flash_map)
    watchdog = peripherals.get("watchdog", [])
    if watchdog:
        watchdog = watchdog + configure_watchdog(project_data)
    registrations = generate_xrobot_registrations() if use_xrobot else []
    if registrations:
        registrations = [f"{INDENT}// Hardware registration"] + registrations
    sections = [core, gpio]
    sections += [peripherals.get(name, []) for name in ("adc", "dac", "pwm", "comm", "usb")]
    sections += [terminal, database, watchdog, registrations]

    default_3 = (
        []
        if use_xrobot
        else [
            f"{INDENT}while (true)",
            f"{INDENT}{{",
            f"{INDENT * 2}Thread::Sleep(UINT32_MAX);",
            f"{INDENT}}}",
        ]
    )
    lines = _app_main_notice(project_data)
    lines += _generate_header_includes(use_xrobot)
    lines += ["", "/* User Code Begin 1 */", "/* User Code End 1 */"]
    externs = _generate_extern_declarations()
    if externs:
        lines += [""] + externs
    if dma:
        lines += [""] + dma
    lines += ["", 'extern "C" void app_main(void)', "{"]
    lines += [f"{INDENT}/* User Code Begin 2 */", f"{INDENT}/* User Code End 2 */", ""]
    lines += join_blocks([section for section in sections if section])
    lines += ["", f"{INDENT}/* User Code Begin 3 */"] + default_3
    lines.append(f"{INDENT}/* User Code End 3 */")
    if use_xrobot:
        lines.append(f"{INDENT}XROBOT_MAIN();")
    lines.append("}")
    generated = "\n".join(lines) + "\n"
    check_gpio_names(project_data, generated, use_xrobot)
    return _preserve_generated_regions(existing_code, generated)


APP_MAIN_HEADER = (
    "#pragma once\n"
    + GENERATED_NOTICE
    + "\n"
    + """
#ifdef __cplusplus
extern "C"
{
#endif

  void app_main(void);

#ifdef __cplusplus
}
#endif
"""
)


def generate_flash_map_cpp(flash_info: dict) -> str:
    """把 Flash 布局字典转换为 C++ 代码：constexpr 数组 FLASH_REGIONS 和段数 FLASH_REGION_NUMBER。
    Convert a Flash layout dictionary into C++ code: the constexpr array FLASH_REGIONS and the
    run count FLASH_REGION_NUMBER.

    地址相接、大小相同的扇区合成一段，写作 {起始地址, 扇区字节数, 扇区个数}。扇区大小按字节
    写出；STM32L0、L1 的页小于 1 KB（如 0.125 KB 即 128 字节）。数组每段一行并带末尾的逗号，
    排版与 clang-format 的结果相同。
    Adjacent sectors of equal size form one run, written as {start address, sector bytes,
    sector count}. Sector sizes are written in bytes; STM32L0 and L1 pages are below 1 KB,
    such as 0.125 KB, that is 128 bytes. The array has one run per line with a trailing comma,
    laid out as clang-format lays it out.

    Args:
        flash_info: flash_info_to_dict() 的输出；每个扇区有十六进制的 address 和 size_kb。
            The output of flash_info_to_dict(); each sector has a hexadecimal address and
            size_kb.
    """
    regions: list[list[int]] = []
    for s in flash_info["sectors"]:
        address = int(s["address"], 16)
        size = round(float(s["size_kb"]) * 1024)
        if regions:
            last_address, last_size, last_count = regions[-1]
            if last_size == size and last_address + last_size * last_count == address:
                regions[-1][2] += 1
                continue
        regions.append([address, size, 1])

    lines = ["constexpr LibXR::FlashRegion FLASH_REGIONS[] = {"]
    for address, size, count in regions:
        lines.append(f"    {{0x{address:08X}, 0x{size:08X}, {count}}},")

    lines.append("};\n")
    lines.append(
        "constexpr size_t FLASH_REGION_NUMBER = sizeof(FLASH_REGIONS) / sizeof(LibXR::FlashRegion);"
    )
    return "\n".join(lines)


def flash_layout(project_data: dict) -> dict | None:
    """project_data['Mcu']['Type'] 的 Flash 布局，即 flash_info_to_dict() 的输出。
    The Flash layout of project_data['Mcu']['Type'], as flash_info_to_dict() returns it.

    缺少 MCU 型号或推算不出布局时记录警告并返回 None；这时不生成 flash_map.hpp，app_main 也不
    include 它。
    A missing MCU type or a layout that cannot be derived logs a warning and gives None;
    flash_map.hpp is then not generated and app_main does not include it.
    """
    from libxr.stm32_flash_generator import flash_info_to_dict, layout_flash

    mcu_model = (project_data.get("Mcu", {}).get("Type") or "").strip()
    if not mcu_model:
        logging.warning(
            tr(
                "Cannot find the MCU type; flash_map.hpp is not generated",
                "找不到 MCU 型号；不生成 flash_map.hpp",
            )
        )
        return None
    try:
        return flash_info_to_dict(layout_flash(mcu_model))
    except ValueError as error:
        logging.warning(
            tr(
                f"{error}; flash_map.hpp is not generated",
                f"{error}；不生成 flash_map.hpp",
            )
        )
        return None


def flash_map_header(flash_info: dict, mcu_model: str) -> str:
    """flash_map.hpp 的内容：生成说明、MCU 型号、include 和 generate_flash_map_cpp() 的代码。
    The content of flash_map.hpp: the generated-file notice, the MCU type, the includes and the
    code of generate_flash_map_cpp().
    """
    return (
        f"#pragma once\n{GENERATED_NOTICE}\n// MCU: {mcu_model}\n\n"
        '#include "main.h"\n#include "stm32_flash.hpp"\n\n'
        + generate_flash_map_cpp(flash_info)
        + "\n"
    )


def _remove_generated(path: str) -> bool:
    """删除 libxr gen 生成的文件 path（第二行是生成说明）；不存在或不是生成的文件时不删。删除时
    为 True。
    Delete path when libxr gen generated it, its second line being the generated-file notice;
    a missing file or one written by hand stays. True when deleted.
    """
    try:
        with open(path, encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except (FileNotFoundError, UnicodeDecodeError):
        return False
    if GENERATED_NOTICE not in lines[:2]:
        return False
    os.remove(path)
    return True


def check_generator_pin() -> None:
    """libxr_config.yaml 没有固定 generator 版本，或固定的版本与已安装的 libxr 不同时给出警告。
    Warn when libxr_config.yaml pins no generator version, or one that differs from the
    installed libxr.

    BSP 的 CI 安装固定的版本重新生成并与提交的文件比较，用其他版本生成的文件可能与之不同。新建的
    libxr_config.yaml 已写入当前版本（见 load_libxr_config()）。固定为 commit 时不比较；包没有
    安装、无法得知版本时不检查。
    BSP CI installs the pinned version, regenerates and compares with the committed files, so
    files generated by another version may differ. A new libxr_config.yaml already holds the
    current version (see load_libxr_config()). A pin to a commit is not compared; nothing is
    checked when the package is not installed and its version is unknown.
    """
    pin = libxr_settings.get("generator")
    installed = update_notice.installed_version()
    if installed is None:
        return
    if pin is None:
        logging.warning(
            tr(
                f"libxr_config.yaml does not pin the generator; add `generator: {installed}` "
                "(the BSP CI installs the pinned version)",
                f"libxr_config.yaml 没有固定 generator 的版本；请添加 `generator: {installed}`"
                "（BSP 的 CI 安装固定的版本）",
            )
        )
        return
    pin = str(pin)
    if pin == installed or re.fullmatch(r"[0-9a-f]{40}", pin):
        return
    logging.warning(
        tr(
            f"libxr_config.yaml pins generator {pin}, but libxr {installed} is installed; "
            f"the BSP CI generates with {pin}",
            f"libxr_config.yaml 固定的 generator 是 {pin}，已安装的 libxr 是 {installed}；"
            f"BSP 的 CI 用 {pin} 生成",
        )
    )


def _name_libxr_config(output_dir: str, config_source: str) -> None:
    """记下说明注释中写出的 libxr_config.yaml 名字：相对工程根目录（输出目录的上一级）的路径，
    例如 User/libxr_config.yaml；配置来源在工程之外或是 URL 时为它的文件名。无论是否用
    --libxr-config 指定，同一个文件得到同一个名字。
    Record the name of libxr_config.yaml for the notice comment: its path relative to the
    project root (the parent of the output directory), such as User/libxr_config.yaml; the
    file name when the source lies outside the project or is a URL. The same file gets the
    same name whether or not --libxr-config names it.
    """
    global libxr_config_label
    root = os.path.dirname(os.path.abspath(output_dir))
    if config_source:
        path = config_source.split("?")[0].rstrip("/\\")
        if "://" in path:
            libxr_config_label = re.split(r"[\\/]", path)[-1] or "libxr_config.yaml"
            return
        path = os.path.abspath(path)
    else:
        path = os.path.join(os.path.abspath(output_dir), "libxr_config.yaml")
    relative = (
        os.path.relpath(path, root)
        if os.path.splitdrive(path)[0].lower() == (os.path.splitdrive(root)[0].lower())
        else ".."
    )
    if relative == ".." or relative.startswith(".." + os.sep):
        libxr_config_label = os.path.basename(path) or "libxr_config.yaml"
    else:
        libxr_config_label = relative.replace(os.sep, "/")


def generate(
    input_path: str, output_path: str, use_xrobot: bool = False, libxr_config: str = ""
) -> None:
    """生成 output_path 指定的 app_main 源文件，以及同一目录中的 app_main.h、flash_map.hpp 和
    libxr_config.yaml。
    Generate the app_main source file output_path, and app_main.h, flash_map.hpp and
    libxr_config.yaml in the same directory.

    每次从默认设置开始，合并 libxr_config（libxr_config.yaml 的路径或 URL，为空时读取输出目录中
    的文件）。全部文件先在内存中生成，没有错误时才写出，并且只写内容有变化的文件，其余文件的修改
    时间不变。Flash 布局只写在 flash_map.hpp 中，推算不出时删除以前生成的 flash_map.hpp；
    libxr_config.yaml 中以前版本写入的 FlashLayout 段被删除。已有输出文件中 User Code 区域的
    内容被保留。出错时记录错误（调试日志另记调用栈）并以状态 1 退出。
    Every run starts from the default settings and merges libxr_config, the path or URL of
    libxr_config.yaml, or the file in the output directory when empty. All files are generated
    in memory first and written only when nothing failed, and only the files whose content
    changed are written, so the others keep their modification time. The Flash layout goes
    only into flash_map.hpp, and a previously generated flash_map.hpp is deleted when no
    layout can be derived; a FlashLayout section that earlier versions wrote to
    libxr_config.yaml is removed. The User Code bodies of an existing output file are kept. An
    error is logged, with the traceback at debug level, and exits with status 1.
    """
    global rtos_priorities
    try:
        # 只给出文件名时写入当前目录。
        # A bare file name writes into the current directory.
        output_dir = os.path.dirname(output_path) or os.curdir

        reset_settings()
        project_data = load_configuration(input_path)
        # 工程根目录是工程 YAML 所在的目录，或输出目录（User）的上一级。
        # The project root is the directory of the project YAML or the parent of the output
        # directory (User).
        rtos_priorities = read_rtos_priorities(
            [
                os.path.dirname(os.path.abspath(input_path)),
                os.path.dirname(os.path.abspath(output_dir)),
            ]
        )
        load_libxr_config(output_dir, libxr_config)
        _name_libxr_config(output_dir, libxr_config)
        check_generator_pin()
        initialize_registry(use_xrobot)

        existing_code = ""
        if os.path.exists(output_path):
            with open(output_path, encoding="utf-8") as f:
                existing_code = f.read()

        flash_info = flash_layout(project_data)
        files = {
            os.path.basename(output_path): generate_full_code(
                project_data, use_xrobot, existing_code, flash_info is not None
            ),
            "app_main.h": APP_MAIN_HEADER,
        }
        if flash_info is None:
            files["flash_map.hpp"] = None
        else:
            files["flash_map.hpp"] = flash_map_header(
                flash_info, project_data["Mcu"]["Type"].strip()
            )
        if libxr_settings.pop("FlashLayout", None) is not None:
            logging.info(
                tr(
                    "libxr_config.yaml: removed FlashLayout, which is no longer used",
                    "libxr_config.yaml：已删除不再使用的 FlashLayout",
                )
            )
        files["libxr_config.yaml"] = libxr_config_text()

        os.makedirs(output_dir, exist_ok=True)
        written, unchanged, removed = [], [], []
        for name, text in files.items():
            path = os.path.join(output_dir, name)
            if text is None:
                if _remove_generated(path):
                    removed.append(name)
            elif _write_if_changed(path, text):
                written.append(name)
            else:
                unchanged.append(name)
        _report_files(output_dir, written, unchanged, removed)

    except Exception as e:
        logging.error(tr(f"Generation failed: {str(e)}", f"生成失败：{str(e)}"))
        logging.debug(tr("Traceback:", "调用栈："), exc_info=True)
        sys.exit(1)


def _report_files(output_dir: str, written: list, unchanged: list, removed: list) -> None:
    """用一行日志列出 output_dir 中写入、未变化和删除的文件。
    Log in one line which files of output_dir were written, unchanged or deleted.
    """
    english = []
    chinese = []
    if written:
        english.append("wrote " + ", ".join(written))
        chinese.append("已写入 " + "、".join(written))
    if unchanged:
        english.append("unchanged " + ", ".join(unchanged))
        chinese.append("未变化 " + "、".join(unchanged))
    if removed:
        english.append("removed " + ", ".join(removed))
        chinese.append("已删除 " + "、".join(removed))
    directory = os.path.normpath(output_dir)
    logging.info(
        tr(
            f"Generated {directory}: " + "; ".join(english),
            f"已生成 {directory}：" + "；".join(chinese),
        )
    )


if __name__ == "__main__":
    from libxr.legacy import run

    raise SystemExit(run("xr_gen_code_stm32"))
