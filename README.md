# LibXR_CppCodeGenerator

LibXR 代码生成工具 / Code generator for LibXR

<h1 align="center">
<img src="https://github.com/xrobot-org/LibXR_CppCodeGenerator/raw/master/imgs/XRobot.jpeg" width="300">
</h1><br>

[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/xrobot-org/LibXR_CppCodeGenerator/blob/master/LICENSE)
[![GitHub Repo](https://img.shields.io/github/stars/xrobot-org/LibXR_CppCodeGenerator?style=social)](https://github.com/xrobot-org/LibXR_CppCodeGenerator)
[![Documentation](https://img.shields.io/badge/docs-online-brightgreen)](https://xrobot.work/docs/code_gen)
[![GitHub Issues](https://img.shields.io/github/issues/xrobot-org/LibXR_CppCodeGenerator)](https://github.com/xrobot-org/LibXR_CppCodeGenerator/issues)
[![CI/CD - Python Package](https://github.com/xrobot-org/LibXR_CppCodeGenerator/actions/workflows/python-publish.yml/badge.svg)](https://github.com/xrobot-org/LibXR_CppCodeGenerator/actions/workflows/python-publish.yml)
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2FJiu-xiao%2FLibXR_CppCodeGenerator.svg?type=shield)](https://app.fossa.com/projects/git%2Bgithub.com%2FJiu-xiao%2FLibXR_CppCodeGenerator?ref=badge_shield)

LibXR_CppCodeGenerator 由各平台开发工具的工程生成使用 [LibXR](https://github.com/xrobot-org/libxr)
的 C++ 代码，以 pip 包 `libxr` 发布，命令为 `libxr`。目前支持三个平台：STM32 读取 STM32CubeMX 工程的
`.ioc` 文件，MSPM0 读取 SysConfig 工程的 `.syscfg`，HPM 读取 HPM Pinmux Tool 工程的 `.hpmpc`；三者都
生成外设对象和入口函数 `app_main`，STM32 的命令另外把 LibXR 接入工程的 CMake 构建。

LibXR_CppCodeGenerator generates C++ code that uses [LibXR](https://github.com/xrobot-org/libxr)
from the projects of each platform's development tool. It is published as the pip package
`libxr`, with the command `libxr`. Three platforms are supported: STM32 reads the `.ioc` file of
an STM32CubeMX project, MSPM0 reads the `.syscfg` of a SysConfig project, and HPM reads the
`.hpmpc` of an HPM Pinmux Tool project; all three generate the peripheral objects and the entry
function `app_main`, and the STM32 commands also add LibXR to the project's CMake build.

---

## 🔧 安装 / Installation

需要 Python 3.10 或更高版本。

Requires Python 3.10 or later.

### 使用 pipx 安装 / Install via `pipx`

```powershell
# Windows
python -m pip install --user pipx
python -m pipx ensurepath
python -m pipx install libxr
# 重新打开终端 / Restart your terminal
```

```bash
# Linux
sudo apt install pipx
pipx ensurepath
pipx install libxr
# 重新打开终端 / Restart your terminal
```

### 使用 pip 安装 / Install via `pip`

```powershell
# Windows
pip install libxr
```

```bash
# Linux (venv)
sudo apt install python3-venv
python3 -m venv ~/.venvs/libxr
. ~/.venvs/libxr/bin/activate
pip install libxr
```

### 从源码安装 / Install from source

```bash
git clone https://github.com/xrobot-org/LibXR_CppCodeGenerator.git
cd LibXR_CppCodeGenerator
python ./scripts/gen_libxr_version.py
pip install .
```

以上三种方式只选其一。当前使用的版本可通过 `libxr --version` 查看，BSP 所用的版本记录在 `User/libxr_config.yaml` 顶层的 `generator:` 字段中，安装的版本应与之一致，例如 `pipx install libxr==6.0.1`。

Use only one of these methods. `libxr --version` shows the installed version; the version a BSP uses is recorded under `generator:` at the top of `User/libxr_config.yaml`, and the installed version should match it (for example `pipx install libxr==6.0.1`).

---

## 📚 基本概念 / Concepts

以一个 STM32F103 的 CubeMX 工程为例：串口 USART1 用作终端，另有 SPI1、I2C1、ADC1、USB、一路 PWM 和
一个标签为 `LED` 的 GPIO，HAL 时基使用 TIM3，系统为 FreeRTOS。各个概念的对应关系如下：

Take an STM32CubeMX project for an STM32F103: USART1 serves as the terminal, and there are SPI1,
I2C1, ADC1, USB, a PWM channel and a GPIO labeled `LED`; the HAL timebase is TIM3 and the system
is FreeRTOS. The concepts correspond as follows:

| 概念 Concept | 在这个例子里 | In this example |
| --- | --- | --- |
| CubeMX 工程 CubeMX project | `STM32F103RC.ioc`、`Core/`、`Drivers/` 和 `CMakeLists.txt` | `STM32F103RC.ioc`, `Core/`, `Drivers/` and `CMakeLists.txt` |
| 工程 YAML Project YAML | `.config.yaml`，`libxr parse` 解析得到的芯片、引脚、外设与 RTOS | `.config.yaml`, chip, pins, peripherals and RTOS parsed by `libxr parse` |
| 入口源文件 Entry source | `User/app_main.cpp`，`libxr gen` 生成的外设对象与 `app_main()` | `User/app_main.cpp`, generated peripheral objects and `app_main()` |
| 生成设置 Generation settings | `User/libxr_config.yaml`，缓冲区大小、USB CDC、数据库与线程优先级 | `User/libxr_config.yaml`, buffer sizes, USB CDCs, database and priorities |
| LibXR 子模块 LibXR submodule | `Middlewares/Third_Party/LibXR`，固定到对应提交的子模块 | `Middlewares/Third_Party/LibXR`, submodule pinned to its commit |
| CMake 接入 CMake integration | `cmake/LibXR.CMake`，包含设置块、LibXR 与固件镜像构建规则 | `cmake/LibXR.CMake`, settings block, LibXR and image rules |

---

## 🚀 配置工程 / Setting Up a Project

`libxr stm32 setup` 依次加入 LibXR 子模块、解析 `.ioc`、生成代码，并接入 CMake，可通过 `-t` 指定终端使用的设备：

`libxr stm32 setup` adds the LibXR submodule, parses the `.ioc` file, generates the code and integrates CMake, with `-t` selecting the terminal device:

```bash
$ libxr stm32 setup -t usart1
[INFO] Default LibXR commit: 4e9670164541b6af6b600a6d544115a9b3e49d98
[INFO] Cloning LibXR from https://github.com/xrobot-org/libxr.git
[INFO] Added the LibXR submodule (https://github.com/xrobot-org/libxr.git).
[INFO] Initializing new LibXR submodule to default commit 4e9670164541b6af6b600a6d544115a9b3e49d98
[INFO] Found .ioc file: .\STM32F103RC.ioc
[INFO] Creating .gitignore file...
[INFO] Creating .gitattributes file...
[INFO] Set terminal_source to usart1 in .\User\libxr_config.yaml
[INFO] Parsing .ioc file...
[INFO] Processing STM32F103RC.ioc...
[INFO] Configuration exported to: .\.config.yaml
[INFO] Generating C++ code...
[INFO] System: FreeRTOS
[INFO] Generated User: wrote app_main.cpp, app_main.h, flash_map.hpp, libxr_config.yaml
[INFO] Generated LibXR.CMake at: cmake\LibXR.CMake
[INFO] Placed the GOT in FLASH after .fini_array in STM32F103xx_FLASH.ld
[INFO] LibXR.CMake included in CMakeLists.txt.
[INFO] [Pass] All tasks completed.
[INFO] Next: #include "app_main.h" and call app_main() in the default task StartDefaultTask (Core/Src/main.c), inside USER CODE sections, which CubeMX keeps when it regenerates the code.
[INFO] Build: cmake --preset debug && cmake --build --preset debug
```

工程要求、`.gitattributes` 规则、参数说明及 CMake 工具链切换见 [STM32 代码生成](https://xrobot.work/docs/code_gen/stm32)。

Project requirements, `.gitattributes` rules, options and CMake toolchain switching are described in [STM32 code generation](https://xrobot.work/en/docs/code_gen/stm32).

---

## 🧩 从工程到代码 / From Project to Code

`libxr parse` 读出工程配置到 `.config.yaml`，`libxr gen` 在 `User/app_main.cpp` 中生成静态外设对象与 `app_main()`（保留 `/* User Code Begin N */` 区域），并由 `User/libxr_config.yaml` 控制缓冲区大小、USB CDC、Flash 数据库与线程优先级；加入 `--xrobot` 时同时生成 `XR_REGISTER` 硬件注册与 `XROBOT_MAIN()` 调用。

`libxr parse` exports the project configuration to `.config.yaml`, and `libxr gen` emits static peripheral objects and `app_main()` in `User/app_main.cpp` (preserving `/* User Code Begin N */` regions), controlled by `User/libxr_config.yaml` for buffer sizes, USB CDCs, Flash database and thread priorities; adding `--xrobot` also emits `XR_REGISTER` registrations and the `XROBOT_MAIN()` call.

```cpp
// User/app_main.cpp（节选 / excerpt）
extern "C" void app_main(void)
{
  /* User Code Begin 2 */
  /* User Code End 2 */

  // Timebase and platform
  static STM32TimerTimebase timebase(&htim3);
  PlatformInit(static_cast<uint32_t>(Thread::Priority::MEDIUM), 1024);
  static STM32PowerManager power_manager;

  // GPIO
  static STM32GPIO LED(LED_GPIO_Port, LED_Pin);

  // SPI, UART, I2C
  static STM32SPI spi1(&hspi1, spi1_rx_buf, spi1_tx_buf, UINT32_MAX);
  static STM32UART usart1(&huart1, usart1_rx_buf, usart1_tx_buf, 5);
  static STM32I2C i2c1(&hi2c1, i2c1_buf, 3);
  // ...
}
```

对于 TI SysConfig（`.syscfg`）和 HPM Pinmux Tool（`.hpmpc`）工程，`libxr mspm0 setup` 与 `libxr hpm setup` 采用同样的工作流生成 `User/app_main.cpp`、`app_main.h` 和 `libxr_config.yaml`：

For TI SysConfig (`.syscfg`) and HPM Pinmux Tool (`.hpmpc`) projects, `libxr mspm0 setup` and `libxr hpm setup` follow the same workflow to generate `User/app_main.cpp`, `app_main.h` and `libxr_config.yaml`:

```bash
$ libxr mspm0 setup
[INFO] Processing mspm0g3507_minidb48.syscfg...
[INFO] Running SysConfig on mspm0g3507_minidb48.syscfg
[INFO] Configuration exported to: .\.config.yaml
[INFO] System: bare metal
[INFO] .\User\libxr_config.yaml does not exist; creating it with the default settings
[INFO] terminal_source is not set; using uart0
[INFO] Generated User: wrote app_main.cpp, app_main.h, libxr_config.yaml
[INFO] Next: run `xrobot gen` to bring User/xrobot_main.hpp up to date; libxr setup does not update it
```

各外设生成规则、XRobot 集成以及 MSPM0 / HPM 完整说明见 [CodeGenerator 文档](https://xrobot.work/docs/code_gen)。

Peripheral generation rules, XRobot integration and full MSPM0 / HPM references are in the [CodeGenerator documentation](https://xrobot.work/en/docs/code_gen).

---

## 🔨 CMake 与工具链 / CMake and Toolchains

`libxr stm32 cmake` 在 `cmake/LibXR.CMake` 中生成 LibXR、应用与固件镜像（`.hex` / `.bin`）的构建规则；`libxr stm32 toolchain` 切换 `CMakePresets.json` 的默认工具链和 ST Arm Clang 标准库：

`libxr stm32 cmake` writes `cmake/LibXR.CMake` with the build rules for LibXR, the application and firmware images (`.hex` / `.bin`), and `libxr stm32 toolchain` switches the default toolchain in `CMakePresets.json` and the ST Arm Clang standard library:

```bash
$ libxr stm32 toolchain gcc
[INFO] Switched the default preset to cmake/gcc-arm-none-eabi.cmake
[INFO] Removed build, configured with the previous toolchain
[INFO] Done.

$ libxr stm32 toolchain clang --newlib
[INFO] Switched the default preset to cmake/starm-clang.cmake
[INFO] Set STARM_TOOLCHAIN_CONFIG to "STARM_NEWLIB" in cmake\starm-clang.cmake
[INFO] Done.
```

`libxr stm32 cmake` 还会检查工程根目录的链接脚本（`*.ld`），在缺少 `.got` 输出段时将其紧跟 `.fini_array` 放入 Flash（已存在但位置不连续时仅告警，不改动），避免 ST Arm Clang（`ld.lld`）将孤立 `.got` 放进 RAM 导致 `.bin` 膨胀或 RELRO 链接失败；GCC 构建不受影响。

`libxr stm32 cmake` also checks root linker scripts (`*.ld`) and inserts a `.got` output section directly after `.fini_array` in Flash when missing (warning without modifying if `.got` exists elsewhere), preventing ST Arm Clang (`ld.lld`) from placing an orphan `.got` in RAM or failing the contiguous RELRO check; GCC builds are unaffected.

---

## 📌 引脚布局 / Pin Layout

`libxr pins` 按芯片型号输出封装与每个引脚的可选信号；加上 `-d <工程目录>` 时叠加工程的当前引脚分配和 `libxr_config.yaml` 配置条目。

`libxr pins` prints the package and selectable signals per pin for a chip model; with `-d <project directory>`, it overlays the project's current pin assignments and `libxr_config.yaml` entries.

```bash
$ libxr pins MSPM0G3507SPMR
model: MSPM0G3507SPMR
platform: mspm0
part: MSPM0G3507
package: LQFP-64(PM)
pin_count: 64
# ...
- position: '33'
  name: PA0
  type: Default
  signals:
  - PA0
  - UART0.TX
  - I2C0.SDA
```

详细用法和数据来源见 [引脚布局文档](https://xrobot.work/docs/code_gen/code-gen-pins)。

Full usage and vendor pin data sources are in the [pin layout documentation](https://xrobot.work/en/docs/code_gen/code-gen-pins).

---

## 🚀 命令一览 / Commands

`parse` 和 `gen` 按工程所属的平台选择解析器和生成器，平台专用命令放在对应子命令下。

`parse` and `gen` dispatch by project platform, while platform-specific commands sit under each platform subcommand.

| 命令 Command | 说明 | Description |
| --- | --- | --- |
| `libxr parse` | 解析工程，写出工程 YAML | Parse a project into the project YAML |
| `libxr gen` | 由工程 YAML 生成入口源文件 | Generate the entry source from the project YAML |
| `libxr pins` | 打印某个型号的封装和引脚布局 | Print the package and pin layout of a model |
| `libxr stm32 setup` | 为 CubeMX 工程加入 LibXR，生成代码并接入 CMake | Add LibXR to a CubeMX project, generate code and integrate CMake |
| `libxr mspm0 setup` | 为 SysConfig 工程生成 LibXR 代码 | Generate LibXR code for a SysConfig project |
| `libxr hpm setup` | 为 HPM 工程生成 LibXR 代码 | Generate LibXR code for an HPM project |
| `libxr stm32 cubemx-gen` | 以脚本模式运行 STM32CubeMX 重新生成工程 | Run STM32CubeMX in script mode to regenerate the project |
| `libxr stm32 cmake` | 把 LibXR 接入 CubeMX 的 CMake 工程 | Integrate LibXR into the CubeMX CMake project |
| `libxr stm32 flash-info` | 打印某个型号的 Flash 布局 | Print the flash layout of a model |
| `libxr stm32 toolchain` | 切换工具链和 ST Arm Clang 的标准库 | Switch the toolchain and the ST Arm Clang standard library |

入门教程、参数参考及 `xr_*` 兼容命令对照见 <https://xrobot.work/docs/code_gen> · <https://xrobot.work/en/docs/code_gen>。

---

## 🧪 测试 / Tests

```bash
pip install clang-format==21.1.8
python -m unittest discover -s tests -v
```

生成的 C++ 文件按 LibXR 的 `.clang-format` 排版，测试核对 DevC、MC02 和随机工程的输出。

The generated C++ files follow LibXR's `.clang-format`, and the tests verify outputs across DevC, MC02 and random projects.

---

## 📖 更多信息 / More Information

- [GitHub Repository](https://github.com/xrobot-org/LibXR_CppCodeGenerator)
- [Documentation](https://xrobot.work/docs/code_gen)
- [PyPI](https://pypi.org/project/libxr/)
- [Issue Tracker](https://github.com/xrobot-org/LibXR_CppCodeGenerator/issues)
- [LibXR](https://github.com/xrobot-org/libxr) · [XRobot](https://github.com/xrobot-org/XRobot)
