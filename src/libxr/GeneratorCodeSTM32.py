#!/usr/bin/env python
"""STM32 Peripheral Code Generator - Core Module (Optimized)"""

import logging
import os
import re
import sys
import urllib.request
import argparse
import yaml

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

PIN_DERIVED_GPIO_ALIAS_RE = re.compile(r"^(P[A-K]\d+)(?:_|$)")

# --------------------------
# Global Configuration
# --------------------------
device_aliases = {"power_manager": {"type": "PowerManager", "aliases": ["power_manager"]}}
libxr_settings = {
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
        "max_history_number": 5
    },
    "SYSTEM": "None"
}


# --------------------------
# Configuration Initialization
# --------------------------
def _normalize_alias_list(aliases) -> list:
    if aliases is None:
        return []
    if isinstance(aliases, (list, tuple, set)):
        return [str(alias) for alias in aliases if alias is not None]
    return [str(aliases)]


def _normalize_device_alias_entry(dev: str, entry) -> dict:
    if isinstance(entry, dict):
        dev_type = entry.get("type", "Unknown") or "Unknown"
        aliases = _normalize_alias_list(entry.get("aliases", []))
    elif isinstance(entry, (list, tuple, set, str)):
        dev_type = "Unknown"
        aliases = _normalize_alias_list(entry)
    else:
        logging.warning(f"Ignoring invalid device alias entry for '{dev}'")
        return None

    return {
        "type": str(dev_type),
        "aliases": aliases,
    }


def _normalize_device_aliases(raw_aliases) -> dict:
    if raw_aliases is None:
        return {}
    if not isinstance(raw_aliases, dict):
        logging.warning("Ignoring invalid device_aliases config, expected a mapping")
        return {}

    normalized = {}
    for dev, entry in raw_aliases.items():
        dev_name = str(dev)
        meta = _normalize_device_alias_entry(dev_name, entry)
        if meta is not None:
            normalized[dev_name] = meta

    return normalized


def initialize_device_aliases(use_xrobot: bool) -> None:
    global device_aliases
    device_aliases.clear()

    if not use_xrobot:
        return

    saved_aliases = _normalize_device_aliases(libxr_settings.get("device_aliases", {}))

    # 插入默认设备
    if "power_manager" not in saved_aliases:
        saved_aliases["power_manager"] = {
            "type": "PowerManager",
            "aliases": ["power_manager"]
        }

    device_aliases.update(saved_aliases)


# --------------------------
# CLI Arguments
# --------------------------
def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Generate STM32 Peripheral Initialization Code",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-i", "--input", required=True,
                        help="Input YAML configuration file path")
    parser.add_argument("-o", "--output", required=True,
                        help="Output C++ file path")
    parser.add_argument("--xrobot", action="store_true",
                        help="Enable XRobot framework integration")
    parser.add_argument("--libxr-config", default="",
                        help="Optional path or URL to libxr_config.yaml")
    return parser.parse_args()


# --------------------------
# Device Registration
# --------------------------
def _register_device(name: str, dev_type: str):
    global device_aliases
    if name not in device_aliases:
        device_aliases[name] = {
            "type": dev_type,
            "aliases": [name]
        }
        return

    meta = _normalize_device_alias_entry(name, device_aliases[name])
    if meta is None:
        meta = {"type": dev_type, "aliases": [name]}
    device_aliases[name] = meta
    meta["type"] = dev_type
    aliases = meta.setdefault("aliases", [])
    if name not in aliases:
        aliases.append(name)


# --------------------------
# Peripheral Instance Generation
# --------------------------
def generate_peripheral_instances(project_data: dict) -> str:
    """Generate initialization code for all peripherals with topological sorting."""
    code_sections = {
        "adc": [],
        "pwm": [],
        "main": []
    }

    for p_type, instances in project_data.get("Peripherals", {}).items():
        for instance_name, config in instances.items():
            section, code = PeripheralFactory.create(p_type, instance_name, config)
            if section in code_sections:
                code_sections[section].append(code)

    # Assemble code in correct order: ADC config -> PWM -> Main peripherals
    return "\n".join([
        "\n".join(code_sections["adc"]),
        "\n".join(code_sections["pwm"]),
        "\n".join(code_sections["main"])
    ])


# --------------------------
# Configuration Loading
# --------------------------
def load_configuration(file_path: str, use_xrobot: bool) -> dict:
    """Load and validate project YAML configuration with enhanced error reporting."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

            if use_xrobot:
                if 'device_aliases' in config:
                    libxr_settings['device_aliases'] = _normalize_device_aliases(
                        config['device_aliases']
                    )

            # Basic schema validation
            required_sections = ["Mcu", "GPIO", "Peripherals"]
            for section in required_sections:
                if section not in config:
                    raise ValueError(f"Missing required section: {section}")

            # Detect RTOS
            if 'FreeRTOS' in config:
                libxr_settings['SYSTEM'] = 'FreeRTOS'
                logging.info("Detected FreeRTOS configuration")
            elif 'ThreadX' in config:
                libxr_settings['SYSTEM'] = 'ThreadX'
            else:
                libxr_settings['SYSTEM'] = 'None'

            # Software timer config
            if 'software_timer' in config:
                libxr_settings['software_timer'].update(config['software_timer'])

            # Terminal source
            if 'terminal_source' in config:
                libxr_settings['terminal_source'] = config['terminal_source']

            if "Peripherals" in config:
                empty_keys = [k for k, v in config["Peripherals"].items() if not v or v == {}]
                for k in empty_keys:
                    logging.info(f"Skipping empty peripheral config: {k}")
                    del config["Peripherals"][k]

            return config
    except FileNotFoundError:
        logging.error(f"Configuration file not found: {file_path}")
        sys.exit(1)
    except yaml.YAMLError as e:
        logging.error(f"YAML syntax error: {str(e)}")
        sys.exit(1)
    except ValueError as e:
        logging.error(f"Configuration validation failed: {str(e)}")
        sys.exit(1)


# --------------------------
# Library Configuration
# --------------------------
def load_libxr_config(output_dir: str, config_source: str) -> None:
    """Load or create library configuration with version compatibility check."""
    global libxr_settings
    config_path = os.path.join(output_dir, "libxr_config.yaml")

    if config_source:
        try:
            external_cfg = {}
            if config_source.startswith("http://") or config_source.startswith("https://"):
                logging.info(f"Downloading libxr_config.yaml from {config_source}")
                with urllib.request.urlopen(config_source) as response:
                    external_cfg = yaml.safe_load(response.read().decode()) or {}
            elif os.path.exists(config_source):
                logging.info(f"Using external libxr_config.yaml from {config_source}")
                with open(config_source, "r", encoding="utf-8") as f:
                    external_cfg = yaml.safe_load(f) or {}
            else:
                logging.warning(f"Cannot locate config source: {config_source}")
                return

            external_cfg.pop("SYSTEM", None)

            libxr_settings = _deep_merge(libxr_settings, external_cfg)
        except Exception as e:
            logging.warning(f"Failed to load external config: {e}")
        return

    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                saved_config = yaml.safe_load(f) or {}

                if saved_config.get("config_version", 1) > 1:
                    logging.warning("Config file format is newer than expected")

                saved_config.pop("SYSTEM", None)

                libxr_settings = _deep_merge(libxr_settings, saved_config)
        else:
            logging.info("Creating new library configuration file")
            os.makedirs(output_dir, exist_ok=True)
            with open(config_path, "w", encoding="utf-8", newline="\n") as f:
                yaml.dump(libxr_settings, f, allow_unicode=True, sort_keys=False)

    except Exception as e:
        logging.warning(f"Failed to process library config: {str(e)}")


def _deep_merge(base: dict, update: dict) -> dict:
    """Recursively merge nested dictionaries with type checking."""
    for key, value in update.items():
        if isinstance(value, dict):
            node = base.setdefault(key, {})
            if isinstance(node, dict):
                _deep_merge(node, value)
            else:
                logging.warning(f"Config type conflict for key '{key}', expected dict")
        else:
            base[key] = value
    return base


# --------------------------
# GPIO Configuration
# --------------------------
def _sanitize_cpp_identifier(name: str) -> str:
    return re.sub(r'\W|^(?=\d)', '_', name)


def generate_gpio_alias(port: str, gpio_data: dict, project_data: dict) -> str:
    base_port = port.split("-")[0]
    port_define = f"GPIO{base_port[1]}"
    pin_num = int(base_port[2:])
    pin_define = f"GPIO_PIN_{pin_num}"
    label = gpio_data.get("Label", "")

    if label:
        port_define = f"{label}_GPIO_Port"
        pin_define = f"{label}_Pin"

    irq_define = _get_exti_irq(pin_num, base_port, gpio_data.get("GPXTI", False),
                               project_data.get("Mcu", {}).get("Family", "STM32F4"))
    irq_str = f", {irq_define}" if irq_define else ""

    var_name = _sanitize_cpp_identifier(label or port)

    _register_device(var_name, "GPIO")

    return f"{var_name}({port_define}, {pin_define}{irq_str})"


def _merge_pin_derived_gpio_aliases() -> None:
    """Attach stale pin-derived GPIO aliases to the currently generated pin object.

    Older ``libxr_config.yaml`` files may retain aliases such as
    ``PC14_OSC32_IN`` from CubeMX pin tokens. When the current IOC has no
    ``GPIO_Label`` for that pin, the generator creates the C++ object as the
    physical pin name, for example ``PC14``. In that case the old name should
    remain a hardware alias, not a separate C++ variable reference.
    """
    global device_aliases
    device_aliases = _normalize_device_aliases(device_aliases)

    for dev, meta in list(device_aliases.items()):
        if meta.get("type") not in ("GPIO", "Unknown"):
            continue

        candidates = [dev]
        candidates.extend(_normalize_alias_list(meta.get("aliases", [])))
        target = None
        for candidate in candidates:
            match = PIN_DERIVED_GPIO_ALIAS_RE.match(str(candidate))
            if match:
                pin_name = match.group(1)
                pin_meta = device_aliases.get(pin_name)
                if isinstance(pin_meta, dict) and pin_meta.get("type") == "GPIO":
                    target = pin_name
                    break

        if target is None or target == dev:
            continue

        target_meta = device_aliases[target]
        aliases = set(_normalize_alias_list(target_meta.get("aliases", [])))
        aliases.add(target)
        aliases.add(dev)
        aliases.update(_normalize_alias_list(meta.get("aliases", [])))
        target_meta["aliases"] = sorted(aliases)
        del device_aliases[dev]


def _get_exti_irq(pin_num: int, port: str, is_exti: bool, mcu_family: str) -> str:
    if not is_exti:
        return ""

    if mcu_family.startswith("STM32WB0"):
        if port.startswith("PA"):
            return "GPIOA_IRQn"
        elif port.startswith("PB"):
            return "GPIOB_IRQn"

    if mcu_family == "STM32F0" or mcu_family == 'STM32G0' or mcu_family == 'STM32L0':
        if pin_num <= 1: return "EXTI0_1_IRQn"
        if pin_num <= 3: return "EXTI2_3_IRQn"
        return "EXTI4_15_IRQn"
    else:
        if 5 <= pin_num <= 9: return "EXTI9_5_IRQn"
        if 10 <= pin_num <= 15: return "EXTI15_10_IRQn"
        return f"EXTI{pin_num}_IRQn"


# --------------------------
# DMA Configuration
# --------------------------
DMA_DEFAULT_SIZES = {
    "SPI": {"tx": 32, "rx": 32},
    "USART": {"tx": 128, "rx": 128},
    "I2C": {"buffer": 32},
    "ADC": {"buffer": 32}
}


def generate_dma_resources(project_data: dict) -> str:
    """
    Generate DMA buffer definitions for all relevant peripherals,
    using per-buffer 'dma_section' config.
    - Reads libxr_settings['SPI'/'USART'/...][instance]['dma_section']
    - If section is empty, no attribute is added; if not, __attribute__((section("..."))) is added
    Returns generated C code as string.
    Cache-equipped targets use padded storage with the original array extent.
    Both ends are isolated without changing DMA lengths or endpoint capacities.
    """
    dma_code = []
    # Default section settings
    DEFAULT_SECTIONS = {
        "DMA": "",
        "BDMA": "",
    }

    def get_buf_section(user_section: str, dma_type: str) -> str:
        """
        Return buffer section name:
        - If user config is set, use it.
        - Otherwise, use default by dma_type.
        """
        if user_section:  # User configuration takes priority
            return user_section
        return DEFAULT_SECTIONS.get(dma_type, "")

    def buffer_declaration(data_type: str, name: str, count, section: str) -> str:
        # Align the storage type, not only its object: sizeof then includes tail
        # padding. Keep the array extent so RawData and split buffers are unchanged.
        return "\n".join([
            "#if defined(__DCACHE_PRESENT) && (__DCACHE_PRESENT == 1U)",
            "static struct alignas(__SCB_DCACHE_LINE_SIZE)",
            "{",
            f"  {data_type} data[{count}];",
            f"}} {name}_storage{section};",
            f"static constexpr auto& {name} = {name}_storage.data;",
            "#else",
            f"alignas(4) static {data_type} {name}[{count}]{section};",
            "#endif",
        ])

    # Iterate all peripherals
    for p_type_raw, instances in project_data.get("Peripherals", {}).items():
        # Normalize peripheral type (e.g. "spi1" -> "SPI")
        match = re.match(r'([A-Za-z0-9]+?)(\d*)$', p_type_raw)
        p_type_base = match.group(1).upper() if match else p_type_raw.upper()

        # Ensure settings dict exists for this peripheral
        if p_type_base not in libxr_settings:
            libxr_settings[p_type_base] = {}

        # SPI/USART/UART/LPUART
        if p_type_base in ["SPI", "USART", "UART", "LPUART"]:
            for instance, config in instances.items():
                # Check DMA enable flags
                tx_dma = config.get("DMA_TX", "DISABLE") == "ENABLE"
                rx_dma = config.get("DMA_RX", "DISABLE") == "ENABLE"
                # Use configured DMA type if available, fallback to "DMA"
                dma_type = config.get("DMA_TX_TYPE", config.get("DMA_RX_TYPE", "DMA"))
                # Instance name for variable
                instance_lower = instance.lower()
                instance_config = libxr_settings[p_type_base].setdefault(instance_lower, {})
                tx_size = instance_config.setdefault(
                    "tx_buffer_size",
                    DMA_DEFAULT_SIZES.get(p_type_base, {}).get("tx", 32)
                )
                rx_size = instance_config.setdefault(
                    "rx_buffer_size",
                    DMA_DEFAULT_SIZES.get(p_type_base, {}).get("rx", 32)
                )
                # Get dma_section config or assign default
                dma_section = instance_config.get("dma_section", None)
                if not dma_section:
                    dma_section = get_buf_section("", dma_type)
                    instance_config["dma_section"] = dma_section
                sec_str = f' __attribute__((section("{dma_section}")))' if dma_section else ""

                buf_code = []
                if tx_dma:
                    buf_code.append(buffer_declaration("uint8_t", f"{instance_lower}_tx_buf", tx_size, sec_str))
                if rx_dma:
                    buf_code.append(buffer_declaration("uint8_t", f"{instance_lower}_rx_buf", rx_size, sec_str))
                if buf_code:
                    dma_code.append("\n".join(buf_code))

        # I2C/ADC
        elif p_type_base in ["I2C", "ADC"]:
            for instance, config in instances.items():
                dma_type = config.get("DMA_RX_TYPE", "DMA")  # type tag for section picking
                instance_lower = instance.lower()
                instance_config = libxr_settings[p_type_base].setdefault(instance_lower, {})
                buf_size = instance_config.setdefault(
                    "buffer_size",
                    DMA_DEFAULT_SIZES[p_type_base]["buffer"]
                )
                dma_section = instance_config.get("dma_section", None)
                if not dma_section:
                    dma_section = get_buf_section("", dma_type)
                    instance_config["dma_section"] = dma_section
                sec_str = f' __attribute__((section("{dma_section}")))' if dma_section else ""

                # ADC buffer is uint16_t, I2C is uint8_t
                if p_type_base == "ADC":
                    # 通道选择规则：DMA 开启→RegularConversions，否则→Channels
                    active_channels = (
                        config.get("RegularConversions", [])
                        if config.get("DMA") == "ENABLE"
                        else config.get("Channels", [])
                    )
                    ch_cnt = max(1, len(active_channels))               # 至少保留 1 份缓冲
                    elems_per_channel = max(1, int(buf_size // 2))      # 每通道的 uint16_t 元素数
                    total_elems = ch_cnt * elems_per_channel            # 总元素数 = 通道数 × 每通道元素数
                    dma_code.append(buffer_declaration("uint16_t", f"{instance_lower}_buf", total_elems, sec_str))
                else:
                    dma_code.append(buffer_declaration("uint8_t", f"{instance_lower}_buf", buf_size, sec_str))

        elif p_type_base == "USB":
            # Generate buffer variables for each USB EP (controlled by dma_section)
            for instance, cfg in instances.items():
                # Normalize instance name
                inst_u = (instance or "USB_FS").upper()
                inst_u = (inst_u
                          .replace("USBOTG", "USB_OTG_")
                          .replace("OTGFS", "OTG_FS")
                          .replace("OTGHS", "OTG_HS"))
                if inst_u == "USB":
                    inst_u = "USB_FS"
                is_otg = inst_u.startswith("USB_OTG_")
                inst_lower = inst_u.lower()

                # Read or set default USB config from libxr_settings (consistent with _generate_usb)
                usb_cfg = libxr_settings.setdefault("USB", {}).setdefault(inst_lower, {})

                def _as_int(v, d):
                    try:
                        return int(str(v), 0)
                    except Exception:
                        return d

                enable = usb_cfg.setdefault("enable", cfg.get("enable", False))
                if not enable:
                    logging.info(f"Skipping disabled USB instance: {instance}")
                    continue

                if 'cdc_count' in usb_cfg or 'cdc_count' in cfg:
                    raise ValueError("USB cdc_count is not a generator option; define composite USB in BSP user code")

                # EP0 packet size, fallback to defaults if needed
                ep0 = _as_int(usb_cfg.get("ep0_packet_size", cfg.get("ep0_packet_size", cfg.get("packet_size", 8))), 8)
                if ep0 not in (8, 16, 32, 64):
                    ep0 = 8
                usb_cfg.setdefault("ep0_packet_size", ep0)

                tx_sz = usb_cfg.setdefault("tx_buffer_size", _as_int(cfg.get("tx_buffer_size", 128), 128))
                rx_sz = usb_cfg.setdefault("rx_buffer_size", _as_int(cfg.get("rx_buffer_size", 128), 128))
                usb_cfg.setdefault("rx_fifo_size", _as_int(cfg.get("rx_fifo_size", 256 if is_otg else 128), 256 if is_otg else 128))
                usb_cfg.setdefault("tx_fifo_size", _as_int(cfg.get("tx_fifo_size", 128), 128))

                # Section name (same as UART)
                dma_section = usb_cfg.get("dma_section", cfg.get("dma_section", ""))
                if "dma_section" not in usb_cfg:
                    usb_cfg["dma_section"] = dma_section
                sec_str = f' __attribute__((section("{dma_section}")))' if dma_section else ""

                # One line per variable to avoid attribute only on the last one
                dma_code.append(buffer_declaration("uint8_t", f"{inst_lower}_ep0_in_buf", ep0, sec_str))
                dma_code.append(buffer_declaration("uint8_t", f"{inst_lower}_ep0_out_buf", ep0, sec_str))
                dma_code.append(buffer_declaration("uint8_t", f"{inst_lower}_ep1_in_buf", tx_sz, sec_str))
                dma_code.append(buffer_declaration("uint8_t", f"{inst_lower}_ep1_out_buf", rx_sz, sec_str))
                dma_code.append(buffer_declaration("uint8_t", f"{inst_lower}_ep2_in_buf", 16, sec_str))

    # Final output with section header if any code generated
    if dma_code:
        output = "/* DMA Resources */\n"
        output += "\n".join(dma_code)
    else:
        output = "/* No DMA Resources generated. */"
    return output

# --------------------------
# Peripheral Generation
# --------------------------
class PeripheralFactory:
    @staticmethod
    def create(p_type: str, instance: str, config: dict) -> str:
        handler_map = {
            "ADC": PeripheralFactory._generate_adc,
            "DAC": PeripheralFactory._generate_dac,
            "TIM": PeripheralFactory._generate_tim,
            "FDCAN": PeripheralFactory._generate_canfd,
            "CAN": PeripheralFactory._generate_can,
            "SPI": PeripheralFactory._generate_spi,
            "USART": PeripheralFactory._generate_uart,
            "UART": PeripheralFactory._generate_uart,
            "LPUART": PeripheralFactory._generate_uart,
            "I2C": PeripheralFactory._generate_i2c,
            "IWDG": PeripheralFactory._generate_iwdg,
            "USB": PeripheralFactory._generate_usb,
        }
        generator = handler_map.get(p_type.upper())
        return generator(instance, config) if generator else ("", "")

    @staticmethod
    def _generate_adc(instance: str, config: dict) -> tuple:
        """Generate ADC initialization with configurable queue size."""
        conversions = config.get("RegularConversions", []) if config.get("DMA") == "ENABLE" else config.get("Channels",
                                                                                                            [])
        adc_config = libxr_settings['ADC'].setdefault(instance.lower(), {})
        vref = adc_config.setdefault('vref', 3.3)

        channels_code = f"  static STM32ADC {instance.lower()}(&h{instance.lower()}, {instance.lower()}_buf, {{{', '.join(conversions)}}}, {vref});\n"

        index = 0

        for channel in conversions:
            channels_code += f"  static auto& {instance.lower()}_{channel.lower()} = {instance.lower()}.GetChannel({index});\n"
            channels_code += f"  UNUSED({instance.lower()}_{channel.lower()});\n"
            _register_device(f"{instance.lower()}_{channel.lower()}", "ADC")
            index = index + 1

        return "adc", channels_code

    @staticmethod
    def _generate_dac(instance: str, config: dict) -> tuple:
        """
        Generate DAC initialization code.
        Always use variable name as <instance>_<out_name> (e.g., dac1_out2).
        """
        channels = config.get("Channels", {})
        if not channels:
            return "", ""
        dac_config = libxr_settings['DAC'].setdefault(instance.lower(), {})
        init_voltage = dac_config.setdefault('init_voltage', 0.0)
        vref = dac_config.setdefault('vref', 3.3)
        codes = []
        for out_name, channel_id in channels.items():
            if channel_id.startswith("DAC_OUT"):
                m = re.search(r'DAC_OUT(\d+)', channel_id)
                channel_id = "DAC_CHANNEL_" + m.group(1)
            var_name = f"{instance.lower()}_{out_name.lower()}"
            if var_name.startswith("dac_dac_"):
                var_name = var_name.replace("dac_dac_", "dac_")
            codes.append(
                f"  static STM32DAC {var_name}(&h{instance.lower()}, {channel_id}, {init_voltage}, {vref});"
            )
            _register_device(var_name, "DAC")
        return "main", "\n".join(codes) + "\n"

    @staticmethod
    def _generate_uart(instance: str, config: dict) -> tuple:
        tx_dma = config.get("DMA_TX", "DISABLE") == "ENABLE"
        rx_dma = config.get("DMA_RX", "DISABLE") == "ENABLE"
        tx_buf = f"{instance.lower()}_tx_buf" if tx_dma else "{nullptr, 0}"
        rx_buf = f"{instance.lower()}_rx_buf" if rx_dma else "{nullptr, 0}"

        uart_config = libxr_settings['USART'].setdefault(instance.lower(), {})
        tx_queue = uart_config.setdefault("tx_queue_size", 5)

        code = f"  static STM32UART {instance.lower()}(&h{instance.lower().replace('usart', 'uart')},\n" \
               f"              {rx_buf}, {tx_buf}, {tx_queue});\n"
        _register_device(f"{instance.lower()}", "UART")
        return "main", code

    @staticmethod
    def _generate_i2c(instance: str, config: dict) -> tuple:
        """Generate I2C initialization code with dynamic buffer configuration."""
        i2c_config = libxr_settings['I2C'].setdefault(instance.lower(), {})
        dma_min_size = i2c_config.setdefault('dma_enable_min_size', 3)
        _register_device(f"{instance.lower()}", "I2C")
        return ("main",
                f"  static STM32I2C {instance.lower()}(&h{instance.lower()}, {instance.lower()}_buf, {dma_min_size});\n")

    @staticmethod
    def _generate_tim(instance: str, config: dict) -> tuple:
        channels = config.get('Channels', {})
        if not channels:
            return "", ""
        code = ""
        for ch_name, ch_cfg in channels.items():
            ch_num = ch_name.replace('CH', '').lower()
            dev_name = f"pwm_{instance.lower()}_ch{ch_num}"
            complementary = ch_cfg.get("Complementary", False)
            if complementary:
                if ch_num.endswith("N") or ch_num.endswith("n"):
                    ch_num = ch_num[:-1]
                code += f"  static STM32PWM {dev_name}(&h{instance.lower()}, TIM_CHANNEL_{ch_num}, true);\n"
            else:
                code += f"  static STM32PWM {dev_name}(&h{instance.lower()}, TIM_CHANNEL_{ch_num}, false);\n"
            _register_device(dev_name, "PWM")
        return "pwm", code

    @staticmethod
    def _generate_canfd(instance: str, config: dict) -> tuple:
        """Generate CAN FD initialization with configurable queue size."""
        instance_cfg = libxr_settings['FDCAN'].setdefault(instance, {})
        queue_size = instance_cfg.setdefault('queue_size', 5)

        _register_device(f"{instance.lower()}", "FDCAN")
        return ("main",
                f'  static STM32CANFD {instance.lower()}(&h{instance.lower()}, {queue_size});\n')

    @staticmethod
    def _generate_can(instance: str, config: dict) -> tuple:
        """Generate classic CAN initialization with queue configuration."""
        instance_cfg = libxr_settings['CAN'].setdefault(instance, {})
        queue_size = instance_cfg.setdefault('queue_size', 5)

        _register_device(f"{instance.lower()}", "CAN")
        return ("main",
                f'  static STM32CAN {instance.lower()}(&h{instance.lower()}, {queue_size});\n')

    @staticmethod
    def _generate_spi(instance: str, config: dict) -> tuple:
        """Generate SPI initialization with DMA buffer configuration."""
        tx_enabled = config.get('DMA_TX', 'DISABLE') == 'ENABLE'
        rx_enabled = config.get('DMA_RX', 'DISABLE') == 'ENABLE'

        spi_config = libxr_settings['SPI'].setdefault(instance.lower(), {})
        dma_min_size = spi_config.setdefault('dma_enable_min_size', 3)

        tx_buf = f"{instance.lower()}_tx_buf" if tx_enabled else "{nullptr, 0}"
        rx_buf = f"{instance.lower()}_rx_buf" if rx_enabled else "{nullptr, 0}"

        _register_device(f"{instance.lower()}", "SPI")

        return ("main",
                f'  static STM32SPI {instance.lower()}(&h{instance.lower()}, {rx_buf}, {tx_buf}, {dma_min_size});\n')

    @staticmethod
    def _generate_iwdg(instance: str, config: dict) -> tuple:
        if not config.get("Enabled"):
            return "", ""
        iwdg_config = libxr_settings['IWDG'].setdefault(instance.lower(), {})
        timeout_ms = iwdg_config.setdefault("timeout_ms", config.get("Configuration", {}).get("timeout_ms", 1000))
        feed_ms = iwdg_config.setdefault("feed_interval_ms",
                                         config.get("Configuration", {}).get("feed_interval_ms", 250))
        code = (
            f"  static STM32Watchdog {instance.lower()}(&h{instance.lower()}, "
            f"{timeout_ms}, {feed_ms});\n"
        )
        _register_device(instance.lower(), "Watchdog")
        return "main", code

    @staticmethod
    def _generate_usb(instance: str, config: dict) -> tuple:
        """
        Simple version:
        - Only writes/updates the final value of libxr_settings['USB'][instance_lower]
        - Only generates device construction code (references *_buf), does not create any buffer arrays
        """
        cfg_in = config or {}

        # Normalize instance name (consistent with extern declarations)
        inst_u = (instance or "USB_FS").upper()
        inst_u = (inst_u
                .replace("USBOTG", "USB_OTG_")
                .replace("OTGFS", "OTG_FS")
                .replace("OTGHS", "OTG_HS"))
        if inst_u == "USB":
            inst_u = "USB_FS"
        if inst_u not in {"USB_FS", "USB_HS", "USB_OTG_FS", "USB_OTG_HS"}:
            inst_u = "USB_FS"

        is_otg = inst_u.startswith("USB_OTG_")
        speed = "HS" if inst_u.endswith("_HS") else "FS"
        inst_lower = inst_u.lower()      # Example: usb_fs / usb_otg_fs
        obj = f"usb_{speed.lower()}"     # Example: usb_fs / usb_hs

        # Update settings (consistent with other modules)
        usb_root = libxr_settings.setdefault("USB", {})
        inst_cfg = usb_root.setdefault(inst_lower, {})

        def _as_int(v, d):
            try:
                return int(str(v), 0)  # Support 0x (hex) style
            except Exception:
                return d

        # Enable switch
        inst_cfg.setdefault("enable", cfg_in.get("enable", False))
        if not inst_cfg["enable"]:
            logging.info(f"USB instance '{inst_lower}' is disabled. Skipping generation.")
            return "", ""

        # Packet size and FIFO setup
        ep0 = _as_int(cfg_in.get("ep0_packet_size", cfg_in.get("packet_size", inst_cfg.get("ep0_packet_size", 8))), 8)
        if ep0 not in (8, 16, 32, 64):
            ep0 = 8
        inst_cfg.setdefault("ep0_packet_size", ep0)

        # DMA buffer sizes
        inst_cfg.setdefault("tx_buffer_size", _as_int(cfg_in.get("tx_buffer_size", inst_cfg.get("tx_buffer_size", 128)), 128))
        inst_cfg.setdefault("rx_buffer_size", _as_int(cfg_in.get("rx_buffer_size", inst_cfg.get("rx_buffer_size", 128)), 128))

        # USB HW FIFO sizes
        inst_cfg.setdefault("tx_fifo_size", _as_int(cfg_in.get("tx_fifo_size", inst_cfg.get("tx_fifo_size", 128)), 128))
        inst_cfg.setdefault("rx_fifo_size", _as_int(cfg_in.get("rx_fifo_size", inst_cfg.get("rx_fifo_size", 256 if is_otg else 128)), 256 if is_otg else 128))
        # CDC FIFO
        inst_cfg.setdefault("cdc_tx_fifo_size", _as_int(cfg_in.get("cdc_tx_fifo_size", inst_cfg.get("cdc_tx_fifo_size", 128)), 128))
        inst_cfg.setdefault("cdc_rx_fifo_size", _as_int(cfg_in.get("cdc_rx_fifo_size", inst_cfg.get("cdc_rx_fifo_size", 128)), 128))
        inst_cfg.setdefault("cdc_queue_size", _as_int(cfg_in.get("cdc_queue_size", inst_cfg.get("cdc_queue_size", 3)), 3))
        if 'cdc_count' in cfg_in or 'cdc_count' in inst_cfg:
            raise ValueError("USB cdc_count is not a generator option; define composite USB in BSP user code")
        # DMA section name
        inst_cfg.setdefault("dma_section", cfg_in.get("dma_section", inst_cfg.get("dma_section", "")))

        # https://github.com/openmoko/openmoko-usb-oui/commit/27f3846d77e0d0d10271b809b831f70040c6197a
        # Descriptor information — 默认 1d50:6199 / 0x0100 / "XRUSB-DEMO-"
        inst_cfg.setdefault(
            "vid",
            _as_int(cfg_in.get("vid", inst_cfg.get("vid", 0x1d50)), 0x1d50)
        )
        inst_cfg.setdefault(
            "pid",
            _as_int(cfg_in.get("pid", inst_cfg.get("pid", 0x6199)), 0x6199)
        )
        inst_cfg.setdefault(
            "bcd",
            _as_int(cfg_in.get("bcd", inst_cfg.get("bcd", 0x0100)), 0x0100)
        )
        inst_cfg.setdefault(
            "manufacturer",
            cfg_in.get("manufacturer", inst_cfg.get("manufacturer", "XRobot"))
        )
        inst_cfg.setdefault(
            "product",
            cfg_in.get("product", inst_cfg.get("product", f"STM32 XRUSB {instance} CDC Demo"))
        )
        inst_cfg.setdefault(
            "serial",
            cfg_in.get("serial", inst_cfg.get("serial", "XRUSB-DEMO-"))
        )

        # Get the final value from settings for code generation
        ep0_sz = int(inst_cfg["ep0_packet_size"])
        tx_buf_sz = int(inst_cfg["tx_buffer_size"])   # USB DMA
        rx_buf_sz = int(inst_cfg["rx_buffer_size"])   # USB DMA
        tx_fifo_size = int(inst_cfg["tx_fifo_size"])  # EP1 HW FIFO
        rx_fifo_size = int(inst_cfg["rx_fifo_size"])  # EP1 HW FIFO
        cdc_tx_fifo_size = int(inst_cfg["cdc_tx_fifo_size"])
        cdc_rx_fifo_size = int(inst_cfg["cdc_rx_fifo_size"])
        cdc_queue_size = int(inst_cfg["cdc_queue_size"])
        vid = int(inst_cfg["vid"])
        pid = int(inst_cfg["pid"])
        bcd = int(inst_cfg["bcd"])
        manufacturer = str(inst_cfg["manufacturer"]).replace('"', '\\"')
        product = str(inst_cfg["product"]).replace('"', '\\"')
        serial = str(inst_cfg["serial"]).replace('"', '\\"')

        # Size enum for EP0
        size_enum = {8: "SIZE_8", 16: "SIZE_16", 32: "SIZE_32", 64: "SIZE_64"}[ep0_sz]
        lang_var = f"{inst_lower}_lang_pack".upper()
        cdc_var = f"{inst_lower}_cdc"
        pcd_handle = f"hpcd_USB_OTG_{speed}" if is_otg else f"hpcd_USB_{speed}"
        instance_type = "STM32USBDeviceOtgFS" if (is_otg and speed == "FS") else \
            "STM32USBDeviceOtgHS" if (is_otg and speed == "HS") else \
            "STM32USBDeviceDevFs"

        # Generate device construction code (buffer variables are defined elsewhere)
        code = []
        code.append(
            f"  static constexpr auto {lang_var} = "
            "LibXR::USB::DescriptorStrings::MakeLanguagePack("
            "LibXR::USB::DescriptorStrings::Language::EN_US, "
            f"\"{manufacturer}\", \"{product}\", \"{serial}\");"
        )
        # CDC construction with explicit endpoint numbers.
        # CDC1: EP1 IN/OUT data, EP2 IN notification.
        code.append(
            f"  static LibXR::USB::CDCUart {cdc_var}("
            "LibXR::USB::Endpoint::EPNumber::EP1, "
            "LibXR::USB::Endpoint::EPNumber::EP1, "
            "LibXR::USB::Endpoint::EPNumber::EP2, "
            f"{cdc_rx_fifo_size}, {cdc_tx_fifo_size}, {cdc_queue_size});")
        code.append("")

        if is_otg:
            out_buffers = [f"{inst_lower}_ep0_out_buf", f"{inst_lower}_ep1_out_buf"]
            in_buffers = [
                f"{{{inst_lower}_ep0_in_buf, {ep0_sz}}}",
                f"{{{inst_lower}_ep1_in_buf, {tx_fifo_size}}}",
                f"{{{inst_lower}_ep2_in_buf, 16}}",
            ]
            code.append(f"  static {instance_type} {obj}(")
            code.append(f"      &{pcd_handle},")
            code.append(f"      {rx_fifo_size},")
            code.append("      {" + ", ".join(out_buffers) + "},")
            code.append("      {" + ", ".join(in_buffers) + "},")
            code.append(f"      USB::DeviceDescriptor::PacketSize0::{size_enum},")
            code.append(f"      0x{vid:X}, 0x{pid:X}, 0x{bcd:X},")
            code.append(f"      {{&{lang_var}}},")
            code.append(f"      {{{{&{cdc_var}}}}},")
            code.append("      {reinterpret_cast<void *>(UID_BASE), 12}")
            code.append("  );")
        else:
            code.append(f"  static {instance_type} {obj}(")
            code.append(f"      &{pcd_handle},")
            code.append("      {")
            code.append(f"          {{{inst_lower}_ep0_in_buf, {inst_lower}_ep0_out_buf, {ep0_sz}, {ep0_sz}}},")
            code.append(f"          {{{inst_lower}_ep1_in_buf, {inst_lower}_ep1_out_buf, {tx_fifo_size}, {rx_buf_sz}}},")
            code.append(f"          {{{inst_lower}_ep2_in_buf, 16, true}}")
            code.append("      },")
            code.append(f"      USB::DeviceDescriptor::PacketSize0::{size_enum},")
            code.append(f"      0x{vid:X}, 0x{pid:X}, 0x{bcd:X},")
            code.append(f"      {{&{lang_var}}},")
            code.append(f"      {{{{&{cdc_var}}}}},")
            code.append("      {reinterpret_cast<void *>(UID_BASE), 12}")
            code.append("  );")

        code.append(f"  {obj}.Init(false);")
        code.append(f"  {obj}.Start(false);\n")

        _register_device(cdc_var, "UART")
        return "main", "\n".join(code)


def _generate_header_includes(use_xrobot: bool = False) -> str:
    """Generate essential header inclusions with optional XRobot components."""
    headers = [
        '#include "app_main.h"\n',
        '#include "cdc_uart.hpp"',
        '#include "libxr.hpp"',
        '#include "main.h"',
        '#include "stm32_adc.hpp"',
        '#include "stm32_can.hpp"',
        '#include "stm32_canfd.hpp"',
        '#include "stm32_dac.hpp"',
        '#include "stm32_flash.hpp"',
        '#include "stm32_gpio.hpp"',
        '#include "stm32_i2c.hpp"',
        '#include "stm32_power.hpp"',
        '#include "stm32_pwm.hpp"',
        '#include "stm32_spi.hpp"',
        '#include "stm32_timebase.hpp"',
        '#include "stm32_uart.hpp"',
        '#include "stm32_usb_dev.hpp"',
        '#include "stm32_watchdog.hpp"',
        '#include "flash_map.hpp"'
    ]

    if use_xrobot:
        headers.append('#include "xrobot_main.hpp"')

    return '\n'.join(headers) + '\n\nusing namespace LibXR;\n'


def _generate_extern_declarations(project_data: dict) -> str:
    """Generate external declarations for HAL handlers with comprehensive checks."""
    externs = set()

    # Timebase source declaration
    timebase_cfg = project_data.get('Timebase', {})
    if timebase_cfg.get('Source', 'SysTick') != 'SysTick':
        src = timebase_cfg['Source']
        if src.startswith('TIM'):
            externs.add(f'extern TIM_HandleTypeDef h{src.lower()};')
        elif src.startswith('LPTIM'):
            externs.add(f'extern LPTIM_HandleTypeDef h{src.lower()};')
        elif src.startswith('HRTIM'):
            externs.add(f'extern HRTIM_HandleTypeDef h{src.lower()};')

    # Peripheral declarations
    peripherals = project_data.get('Peripherals', {})
    for p_type, instances in peripherals.items():
        for instance in instances:
            if p_type == 'USB':
                # New USB stack uses PCD handle (e.g., hpcd_USB_FS / hpcd_USB_HS)
                if instance == 'USB':
                    instance = "USB_FS"
                externs.add(f"extern PCD_HandleTypeDef hpcd_{instance};")
            elif p_type == 'DAC':
                externs.add(f'extern DAC_HandleTypeDef h{instance.lower()};')
            else:
                handle_type = 'UART_HandleTypeDef' if p_type in ['USART', 'UART',
                                                                 'LPUART'] else f'{p_type}_HandleTypeDef'
                if p_type in ['USART', 'UART', 'LPUART']:
                    externs.add(f'extern {handle_type} h{instance.lower().replace("usart", "uart")};')
                else:
                    externs.add(f'extern {handle_type} h{instance.lower()};')

    return '/* External HAL Declarations */\n' + '\n'.join(sorted(externs)) + '\n'


def preserve_user_blocks(existing_code: str, section: int) -> str:
    """Preserve user code between protection markers with enhanced pattern matching."""
    patterns = {
        1: (r'/\* User Code Begin 1 \*/(.*?)/\* User Code End 1 \*/', ''),
        2: (r'/\* User Code Begin 2 \*/(.*?)/\* User Code End 2 \*/', ''),
        3: (r'/\* User Code Begin 3 \*/(.*?)/\* User Code End 3 \*/', ''),
    }

    if section not in patterns:
        return ''

    pattern, default = patterns[section]
    match = re.search(pattern, existing_code, re.DOTALL)
    content = match.group(1).strip() if match else default
    return '  ' + content if section != 1 and content else content


def _generate_core_system(project_data: dict) -> str:
    """Generate core system initialization with timebase configuration."""
    timebase_cfg = project_data.get('Timebase', {'Source': 'SysTick'})
    source = timebase_cfg.get('Source', 'SysTick')

    timebase_init = '  static STM32Timebase timebase;'  # Default to SysTick

    if source != 'SysTick':
        timer_type = 'TIM' if source.startswith('TIM') else \
            'LPTIM' if source.startswith('LPTIM') else 'HRTIM'
        handler = f'h{source.lower()}'
        timebase_init = f'  static STM32TimerTimebase timebase(&{handler});'

    system_type = libxr_settings['SYSTEM']
    timer_cfg = libxr_settings['software_timer']

    init_args = ""
    if system_type == 'None':  # Bare-metal
        init_args = ""
    elif system_type == 'FreeRTOS' or system_type == 'ThreadX':
        init_args = f"{timer_cfg['priority']}, {timer_cfg['stack_depth']}"
    else:
        logging.error(f'Unsupported system type: {system_type}')
        sys.exit(1)

    return f"""{timebase_init}
  PlatformInit({init_args});
  static STM32PowerManager power_manager;"""


def generate_gpio_config(project_data: dict) -> str:
    """Generate GPIO initialization code with EXTI support."""
    code = '\n  /* GPIO Configuration */\n'
    for port, config in project_data.get('GPIO', {}).items():
        alias = generate_gpio_alias(port, config, project_data)
        code += f'  static STM32GPIO {alias};\n'
    return code


# Watchdog
def configure_watchdog(project_data: dict) -> str:
    code = ""
    watchdog_instances = []
    for name, cfg in project_data.get("Peripherals", {}).get("IWDG", {}).items():
        if cfg.get("Enabled"):
            watchdog_instances.append(name.lower())
    if not watchdog_instances:
        return code

    wdg_config = libxr_settings.setdefault("Watchdog", {})
    run_as_thread = wdg_config.setdefault("run_as_thread", False)
    feed_interval = wdg_config.setdefault("feed_interval_ms", 250)

    for name in watchdog_instances:
        code += f"""  {name}.Feed();
"""
        if run_as_thread:
            thread_stack = wdg_config.setdefault("thread_stack_depth", 1024)
            thread_priority = wdg_config.setdefault("thread_priority", 3)
            code += f"""  static LibXR::Thread {name}_thread;
  {name}_thread.Create(reinterpret_cast<LibXR::Watchdog *>(&{name}), {name}.ThreadFun, "{name}_wdg", {thread_stack},
                      static_cast<LibXR::Thread::Priority>({thread_priority}));
"""
        else:
            code += f"""  static auto {name}_task = Timer::CreateTask({name}.TaskFun, reinterpret_cast<LibXR::Watchdog *>(&{name}), {feed_interval});
  Timer::Add({name}_task);
  Timer::Start({name}_task);
"""
    return code


# --------------------------
# Terminal Configuration
# --------------------------
def configure_terminal(project_data: dict) -> str:
    code = "  /* Terminal Configuration */\n"
    terminal_source = libxr_settings.get("terminal_source", "").lower()

    # User-specified terminal source
    if terminal_source != "":
        dev = terminal_source.lower()
        # Device must be registered and of type UART, otherwise log a warning and skip
        info = device_aliases.get(dev)
        if not info or info.get("type") != "UART":
            logging.warning(f"terminal_source '{terminal_source}' is not registered as UART, terminal will not be initialized!")
            return code
        dev = terminal_source.upper()
        code += (
            f"  STDIO::read_ = {dev.lower()}.read_port_;\n"
            f"  STDIO::write_ = {dev.lower()}.write_port_;\n"
        )

    if terminal_source != "":
        term_config = libxr_settings.setdefault("Terminal", {})
        params = [
            term_config.setdefault("read_buff_size", 32),
            term_config.setdefault("max_line_size", 32),
            term_config.setdefault("max_arg_number", 5),
            term_config.setdefault("max_history_number", 5)
        ]

        run_as_thread = term_config.setdefault("run_as_thread", False)

        if run_as_thread:
            thread_stack_depth = term_config.setdefault("thread_stack_depth", 1024)
            thread_priority = term_config.setdefault("thread_priority", 3)

        code += f"""
  static RamFS ramfs("XRobot");
  static Terminal<{', '.join(map(str, params))}> terminal(ramfs);
"""
        if run_as_thread:
            code += f"""\
  static LibXR::Thread term_thread;
  term_thread.Create(&terminal, terminal.ThreadFun, "terminal", {thread_stack_depth},
                     static_cast<LibXR::Thread::Priority>({thread_priority}));
"""
        else:
            code += f"""\
  static auto terminal_task = Timer::CreateTask(terminal.TaskFun, &terminal, 10);
  Timer::Add(terminal_task);
  Timer::Start(terminal_task);
"""
        _register_device("ramfs", "RamFS")
        _register_device("terminal", f"Terminal<{', '.join(map(str, params))}>")
    return code


# --------------------------
# XRobot Integration
# --------------------------
def generate_xrobot_registrations() -> str:
    """Expose named BSP objects to the static entry without a runtime container.

    Peripheral generation already materializes ADC/PWM channels as named C++
    references. Saved string aliases remain optional generator configuration;
    they are not emitted as a second runtime lookup system.
    """
    _merge_pin_derived_gpio_aliases()
    libxr_settings["device_aliases"] = {
        name: {"type": meta["type"], "aliases": sorted(set(meta.get("aliases", [])))}
        for name, meta in device_aliases.items()
    }
    lines = []
    for name, meta in device_aliases.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
            raise ValueError(f"Static registration needs an existing C++ name: {name}")
        cpp_type = meta["type"]
        if not isinstance(cpp_type, str) or not cpp_type or cpp_type == "Unknown":
            raise ValueError(f"Explicit registration type is missing for {name}")
        if not cpp_type.startswith("LibXR::"):
            cpp_type = "LibXR::" + cpp_type
        lines.append(f"  XR_REGISTER({name}, {cpp_type});")
    return "\n".join(lines) + "\n"


# --------------------------
# Main Generator
# --------------------------
def generate_full_code(project_data: dict, use_xrobot: bool, existing_code: str) -> str:
    user_code_def_3 = '  XROBOT_MAIN();\n' if use_xrobot else f"  while(true) {{\n    Thread::Sleep(UINT32_MAX);\n  }}\n"
    components = [
        _generate_header_includes(use_xrobot),
        '/* User Code Begin 1 */',
        preserve_user_blocks(existing_code, 1),
        '/* User Code End 1 */',
        '// NOLINTBEGIN',
        '// clang-format off',
        _generate_extern_declarations(project_data),

        generate_dma_resources(project_data),

        '\nextern "C" void app_main(void) {',
        '  // clang-format on',
        '  // NOLINTEND',
        '  /* User Code Begin 2 */',
        preserve_user_blocks(existing_code, 2),
        '  /* User Code End 2 */',
        '  // clang-format off',
        '  // NOLINTBEGIN',
        _generate_core_system(project_data),
        generate_gpio_config(project_data),
        generate_peripheral_instances(project_data),
        configure_terminal(project_data),
        configure_watchdog(project_data),
        generate_xrobot_registrations() if use_xrobot else '',
        '  // clang-format on',
        '  // NOLINTEND',
        '  /* User Code Begin 3 */',
        user_code_def_3.rstrip('\n') if preserve_user_blocks(existing_code, 3) == '' else '',
        preserve_user_blocks(existing_code, 3),
        '  /* User Code End 3 */',
        '}'
    ]
    return '\n'.join(filter(None, components))


def generate_app_main_header(output_dir: str) -> None:
    """Generate app_main.h header file."""
    header_path = os.path.join(output_dir, "app_main.h")
    content = """#ifdef __cplusplus
extern "C" {
#endif

void app_main(void);

#ifdef __cplusplus
}
#endif
"""

    if not os.path.exists(header_path) or open(header_path).read() != content:
        with open(header_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        logging.info(f"Generated header: {header_path}")


def generate_flash_map_cpp(flash_info: dict) -> str:
    """
    Convert flash_info dictionary to a C++ constexpr struct array.

    :param flash_info: Output from flash_info_to_dict

    :return: C++ code as a string
    """
    lines = [
        "#include \"stm32_flash.hpp\"",
        "",
        "constexpr LibXR::FlashSector FLASH_SECTORS[] = {",
    ]

    for s in flash_info["sectors"]:
        index = s["index"]
        address = int(s["address"], 16)
        size_kb = int(s["size_kb"])
        lines.append(f"  {{0x{address:08X}, 0x{(size_kb * 1024):08X}}},")

    lines.append("};\n")
    lines.append("constexpr size_t FLASH_SECTOR_NUMBER = sizeof(FLASH_SECTORS) / sizeof(LibXR::FlashSector);")
    return "\n".join(lines)


def inject_flash_layout(project_data: dict, output_dir: str) -> None:
    """
    Automatically generate FlashLayout from project_data['Mcu']['Type']
    and inject it into libxr_settings. Also generates flash_map.hpp.

    :param project_data: Project configuration containing MCU type

    :param output_dir: Output directory for generated flash_map.hpp
    """
    try:
        from libxr.STM32FlashGenerator import layout_flash, flash_info_to_dict
        mcu_model = project_data.get("Mcu", {}).get("Type", "").strip()
        if not mcu_model:
            logging.warning("Cannot find MCU name, skipping FlashLayout generation")
            return

        flash_info = layout_flash(mcu_model)
        flash_dict = flash_info_to_dict(flash_info)
        libxr_settings["FlashLayout"] = flash_dict
        logging.info(f"FlashLayout is generated and injected, MCU: {mcu_model}")

        cpp_code = generate_flash_map_cpp(flash_dict)
        if output_dir:
            hpp_path = os.path.join(output_dir, "flash_map.hpp")
            with open(hpp_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(f"""#pragma once
// Auto-generated Flash Layout Map
// MCU: {mcu_model}

#include "main.h"

""")
                f.write(cpp_code)
            logging.info(f"Flash layout map written to: {hpp_path}")
    except ImportError as e:
        logging.warning(f"Cannot import FlashLayout generator: {e}")
    except Exception as e:
        logging.warning(f"Cannot generate FlashLayout: {e}")


def main():
    from libxr.PackageInfo import LibXRPackageInfo

    LibXRPackageInfo.check_and_print()

    try:
        # Parse arguments
        args = parse_arguments()

        use_xrobot = args.xrobot

        # Load configurations
        project_data = load_configuration(args.input, use_xrobot)
        load_libxr_config(os.path.dirname(args.output), args.libxr_config)
        initialize_device_aliases(use_xrobot)

        output_dir = os.path.dirname(args.output)
        os.makedirs(output_dir, exist_ok=True)

        # Generate code
        existing_code = ""
        if os.path.exists(args.output):
            with open(args.output, "r", encoding="utf-8") as f:
                existing_code = f.read()

        output_code = generate_full_code(project_data, use_xrobot, existing_code)

        # Write output
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(output_code)

        inject_flash_layout(project_data, output_dir)

        config_path = os.path.join(output_dir, "libxr_config.yaml")

        with open(config_path, "w", encoding="utf-8", newline="\n") as f:
            cleaned_config = {
                k: v for k, v in libxr_settings.items()
                if not (isinstance(v, dict) and len(v) == 0)
                   and not (k == "device_aliases" and not args.xrobot)
            }
            yaml.dump(cleaned_config, f, allow_unicode=True, sort_keys=False)

        logging.info(f"Successfully generated: {output_dir}")

        generate_app_main_header(output_dir)
        logging.info("Generated header file: app_main.h")

    except Exception as e:
        logging.error(f"Generation failed: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
