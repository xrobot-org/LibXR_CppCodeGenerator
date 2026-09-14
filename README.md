<h1 align="center">
<img src="https://github.com/Jiu-xiao/LibXR_CppCodeGenerator/raw/main/imgs/XRobot.jpeg" width="300">
</h1><br>

[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![GitHub Repo](https://img.shields.io/github/stars/Jiu-xiao/libxr?style=social)](https://github.com/Jiu-xiao/libxr)
[![Documentation](https://img.shields.io/badge/docs-online-brightgreen)](https://xrobot.work/libxr/)
[![GitHub Issues](https://img.shields.io/github/issues/Jiu-xiao/LibXR_CppCodeGenerator)](https://github.com/Jiu-xiao/LibXR_CppCodeGenerator/issues)
[![CI/CD - Python Package](https://github.com/Jiu-xiao/LibXR_CppCodeGenerator/actions/workflows/python-publish.yml/badge.svg)](https://github.com/Jiu-xiao/LibXR_CppCodeGenerator/actions/workflows/python-publish.yml)
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2FJiu-xiao%2FLibXR_CppCodeGenerator.svg?type=shield)](https://app.fossa.com/projects/git%2Bgithub.com%2FJiu-xiao%2FLibXR_CppCodeGenerator?ref=badge_shield)

`libxr` 是一个 Python 包，用于自动化嵌入式系统开发。它通过解析硬件配置文件并生成对应的 C++ 工程代码，显著降低嵌入式开发中的重复性工作。目前默认支持 STM32 平台，后续将扩展至更多硬件体系结构。

`libxr` is a Python package for automating embedded system development. It parses hardware configuration files and generates corresponding C++ project code, significantly reducing repetitive manual work. STM32 is supported by default, with more hardware architectures planned.

## 🌟 Features 功能亮点

- 🧠 自动生成设备驱动和应用程序框架。
  Automatically generates device drivers and application scaffolding.

- ⚙️ 支持多种后端架构，默认支持 STM32 平台。
  Supports multiple backends; STM32 is the default.

- 🔌 支持多重别名注册与查找。
  Supports multi-alias registration and lookup.

- 📦 可与 XRobot 框架集成，实现模块自动注册与调度管理。
  Enables integration with the XRobot application framework.

## 📥 Installation 安装

### 使用pipx安装 (Install via `pipx`)

windows

```ps
python -m pip install --user pipx
python -m pipx ensurepath
pipx install libxr
pipx ensurepath
# Restart your terminal
```

linux

```bash
sudo apt install pipx
pipx install libxr
pipx ensurepath
# Restart your terminal
```

### 使用 pip 安装 (Install via `pip`)

```bash
pip install libxr
```

### 从源码安装 (Install from source)

```bash
git clone https://github.com/Jiu-xiao/LibXR_CppCodeGenerator.git
cd LibXR_CppCodeGenerator
python3 ./scripts/gen_libxr_version.py
pip install -e .
```

---

## 🔧 General 通用命令(跨平台支持)

以下命令适用于所有平台(如 STM32 及未来支持的架构)。
These commands work across platforms (STM32 and others):

### `xr_parse`

```bash
xr_parse -i config.yaml
```

解析通用的 YAML 硬件配置文件，提取外设定义。
Parses a generic YAML hardware configuration and extracts peripheral definitions.

### `xr_gen_code`

```bash
xr_gen_code -i config.yaml [--xrobot]
```

根据 YAML 配置生成平台无关的 C++ 硬件抽象层代码，可选生成 XRobot 集成代码。
Generates platform-agnostic C++ hardware abstraction code from YAML.

---

## STM32 工程工具 (STM32 Project Tools)

### `xr_cubemx_cfg`

自动配置 STM32CubeMX 工程
Automatically configures an STM32CubeMX project.

```bash
usage: xr_cubemx_cfg [-h] -d DIRECTORY [-t TERMINAL] [--xrobot] [--commit COMMIT] [--git-source GIT_SOURCE]
                     [--git-mirrors GIT_MIRRORS]
```

解析 `.ioc` 文件，生成 YAML 和 C++ 驱动代码，补丁中断处理函数，并初始化项目结构
Parses `.ioc`, generates YAML and C++ code, patches interrupt handlers, and initializes the project structure.

#### 🔧 必选参数 (Required)

- `-d, --directory <DIRECTORY>`：

  STM32CubeMX 工程路径
  Path to the STM32CubeMX project.

#### ⚙️ 可选参数 (Optional)

- `-t, --terminal <TERMINAL>`：

  串口设备名称(如 `usart1` `usb_fs_cdc`)
  Terminal device name (e.g. `usart1` `usb_fs_cdc`).

- `--xrobot`：

  生成 XRobot Glue 代码
  Enable XRobot glue code generation.

- `--commit <COMMIT>`

  显式指定 LibXR 仓库 commit 版本；提供该参数时工具会切到这个 commit。
  Explicitly select the LibXR repository commit; when provided, the tool checks out this commit.

  不提供 `--commit` 时，如果 LibXR checkout 干净且比包内默认 commit 旧，工具会将其升级到默认 commit；
  即使工程 gitlink 已记录默认 commit、实际 checkout 仍停在旧版本，也会自动修正。checkout 已是默认版本、
  比默认版本新、与默认版本分叉或包含本地修改时均保持不变。如需精确切换版本，请显式使用 `--commit`。
  When `--commit` is omitted, a clean LibXR checkout older than the package default commit is upgraded to that
  default, even if the project gitlink already records the default while the checkout itself is stale. A
  checkout that is already current, newer than the package default, has diverged from it, or contains local
  changes is preserved. Use `--commit` to request an exact revision explicitly.

- `--git-source`

  Git 源的 base URL 或完整仓库 URL，或使用 `auto`/`github`（默认：`auto`）。
  A Git source base URL or a full repository URL, or `auto`/`github` (default: `auto`).

  示例 / Examples:
  ```bash
  --git-source https://gitee.com/jiu-xiao/libxr
  ```

- `--git-mirrors`

  逗号分隔的镜像 base/完整仓库 URL 列表，仅在 --git-source=auto 时作为候选参与测速。
  Comma-separated mirror base/full repo URLs; used as candidates when --git-source=auto.

  示例 / Examples:
  ```bash
  --git-mirrors "https://gitee.com/jiu-xiao/libxr"
  ```

  或通过环境变量追加 / Or via environment variable:
  ```bash
  export XR_GIT_MIRRORS="https://gitee.com/jiu-xiao/libxr"
  ```

#### 🌐 网络与镜像说明 (Networking & Mirrors)

工具会在 GitHub 与内置/自定义镜像间测速并选择最快源。
The tool benchmarks GitHub and built-in/custom mirrors, then picks the fastest.

选中的源会作为子模块的 origin 远程地址。
The chosen source becomes the submodule’s origin remote.

#### 🤖 CubeMX 自动生成说明 (CubeMX Automation Notes)

新的 `xr_cubemx_generate` 入口可以单独使用，只做 CubeMX 脚本模式生成，不执行后续 LibXR 解析和代码生成。
The new `xr_cubemx_generate` entrypoint can be used standalone when you only want CubeMX script-mode generation.

最小脚本模式命令示例 / Minimal example:

```bash
xr_cubemx_generate -d . --cubemx-cmd /path/to/STM32CubeMX
```

Windows 上推荐直接启动 STM32CubeMX.exe / Windows example:

```powershell
xr_cubemx_generate -d . `
  --cubemx-cmd "$env:LOCALAPPDATA\Programs\STM32CubeMX\STM32CubeMX.exe" `
  --launch-mode direct `
  --auto-confirm `
  --log-dir .cubemx-logs
```

如果显式传入 STM32CubeMX `.jar`，才使用 `--launch-mode java` 和 `--java-cmd`。
Use `--launch-mode java` and `--java-cmd` only when `--cubemx-cmd` points to an STM32CubeMX `.jar` file.

注意：CubeMX 脚本模式仍可能启动 GUI。`--auto-confirm` 是对 GUI 弹窗的 best-effort 兜底，不是可靠的官方 headless CI 模式。
Note: CubeMX script mode may still launch the GUI. `--auto-confirm` is a best-effort fallback for GUI dialogs, not a reliable official headless CI mode.

#### 📦 输出内容 (Outputs)

- `.config.yaml`:

  自动生成的 C++ 驱动代码(如 `app_main.cpp`)
  Generated C++ driver code (e.g. `app_main.cpp`)

- `CMakeLists.txt`、`.gitignore`

- 初始化的 Git 仓库及 LibXR 子模块
  Initialized Git repository and LibXR submodule

---

### `xr_cubemx_generate`

独立运行 STM32CubeMX 脚本模式生成。
Run STM32CubeMX script-mode generation as a standalone step.

```bash
usage: xr_cubemx_generate [-h] -d DIRECTORY [--ioc IOC] [--cubemx-cmd CUBEMX_CMD]
                          [--java-cmd JAVA_CMD] [--launch-mode {auto,direct,java}]
                          [--generate-code-dir GENERATE_CODE_DIR] [--expect-path EXPECT_PATH]
                          [--log-dir LOG_DIR] [--script-path SCRIPT_PATH] [--keep-script]
                          [--silent] [--auto-confirm] [--timeout TIMEOUT]
```

用途 / Purpose:

- 只做 CubeMX 工程生成，不做 LibXR 解析或代码生成
  Run CubeMX generation only, without LibXR parsing or code generation
- 为 CI 产出命令、脚本和日志工件
  Produce command/script/log artifacts for CI
- 在已有 `.ioc` 上做预生成或复现 GUI 迁移流程
  Pre-generate projects or reproduce GUI migration flows from an existing `.ioc`

默认会校验 `Core/Inc` 和 `Drivers` 已生成；如需覆盖默认值，可重复传入 `--expect-path`。
By default, the runner verifies that `Core/Inc` and `Drivers` were generated; pass `--expect-path` one or more times to override the defaults.

#### CubeMX 前置条件 / CubeMX Prerequisites

该工具不登录 ST 账号，也不恢复 CubeMX 用户状态或固件缓存。运行前必须确保当前电脑上的 CubeMX 已经能手动打开并生成目标 `.ioc`：已登录、已安装对应 `STM32Cube_FW_*` 包、已接受协议、旧工程迁移已保存。
This tool does not sign in to ST accounts and does not restore CubeMX user state or firmware caches. Before running it, make sure CubeMX on this same machine can manually open and generate the target `.ioc`: signed in, required `STM32Cube_FW_*` packages installed, licenses accepted, and old-project migration saved.

CI 如果要运行 CubeMX，也必须运行在这样的已配置桌面环境中。
If CI runs CubeMX, it must run in such an already configured desktop environment.

#### 📦 输出内容 (Outputs)

- `cubemx_generate.txt` 或显式指定的脚本文件
  Generated CubeMX script file

- 可选日志目录中的命令、stdout、stderr
  Optional command/stdout/stderr logs in `--log-dir`

- 默认校验的 `Core/Inc`、`Drivers`，或由 `--expect-path` 覆盖的生成目录/文件
  Default `Core/Inc` and `Drivers` checks, or generated directories/files overridden through `--expect-path`

---

### `xr_parse_ioc`

自动解析 STM32CubeMX 工程配置
Parses `.ioc` files from STM32CubeMX projects and exports structured YAML.

```bash
usage: xr_parse_ioc [-h] -d DIRECTORY [-o OUTPUT] [--verbose]
```

解析 `.ioc` 文件为 `.config.yaml`，并在终端输出解析摘要
Parses `.ioc` files and creates `.config.yaml` with a readable summary.

#### 🔧 必选参数 (Required)

- `-d, --directory <DIRECTORY>`
  `.ioc` 文件所在目录路径
  Path to the input directory containing `.ioc` files.

#### ⚙️ 可选参数 (Optional)

- `-o, --output <FILE>`
  自定义 YAML 输出路径(默认与 `.ioc` 同名)
  Custom YAML output path (default: `<input_file>.yaml`).

- `--verbose`
  启用调试日志，输出详细解析过程
  Enable verbose logging.

#### 📦 输出内容 (Outputs)

- `.config.yaml`：

  包含 GPIO、外设、DMA、FreeRTOS、MCU 等配置
  YAML file containing GPIO, peripheral, DMA, FreeRTOS, and MCU configurations.

- 控制台摘要：MCU 信息、GPIO 数量、外设统计等
  Console summary: MCU information, GPIO count, peripheral statistics, etc.

---

### `xr_gen_code_stm32`

根据 YAML 配置生成 STM32 硬件抽象层代码，可选生成 XRobot 集成代码。
Generates STM32 application code from YAML.

```bash
usage: xr_gen_code_stm32 [-h] -i INPUT -o OUTPUT [--xrobot] [--hw-cntr] [--libxr-config LIBXR_CONFIG]
```

#### 🔧 Required

- `-i`：

  `.config.yaml` 配置文件路径
  Path to `.config.yaml`

- `-o`：

  生成代码输出目录
  Output directory

#### ⚙️ Optional

- `--xrobot`：

  启用 XRobot glue 代码生成
  Enable XRobot glue generation

- `--hw-cntr`
  生成 LibXR HardwareContainer 定义及 app_framework.hpp 头文件（可用于非 XRobot 项目）
  Generate LibXR HardwareContainer definition and include app_framework.hpp header (can be used without XRobot)

- `--libxr-config`：

  自定义 libxr_config.yaml 路径(可为本地或远程)
  Path or URL to runtime config YAML

#### 📦 Outputs

- `app_main.cpp`：
  主入口文件，包含所有初始化逻辑
  Main entry point with all initialization logic

- `libxr_config.yaml`：
  运行时配置文件，可自定义缓冲区大小、队列等参数
  Runtime config YAML, can be customized with buffer size, queue, etc.

- `flash_map.hpp`：
  自动生成的 Flash 扇区表，供 Flash 抽象层使用
  Auto-generated flash sector layout for use with Flash abstraction layer

---

### `xr_stm32_flash`

解析 STM32 型号，生成 Flash 扇区信息表（YAML 格式输出）。
Parses STM32 model name and generates flash layout info (YAML output).

```bash
usage: xr_stm32_flash <STM32_MODEL>
```

### 🧠 功能说明 (Functionality)

- 根据 STM32 型号名称自动推导 Flash 大小
  Automatically infers flash size from the STM32 model string

- 根据芯片系列（如 F1/F4/H7/U5 等）生成对应的扇区布局
  Generates sector layout depending on the chip series (e.g., F1/F4/H7/U5)

- 输出包括每个扇区的地址、大小和索引
  Output includes address, size, and index of each sector

### 📦 输出内容 (Outputs)

- YAML 格式的 Flash 信息
  Flash info in YAML format:

```yaml
model: STM32F103C8
flash_base: '0x08000000'
flash_size_kb: 64
sectors:
- index: 0
  address: '0x08000000'
  size_kb: 1.0
- index: 1
  address: '0x08000400'
  size_kb: 1.0
  ...
```

---

### `xr_libxr_cmake`

为 STM32CubeMX 工程生成 `LibXR.CMake` 配置，并自动集成至 `CMakeLists.txt`。
Generates `LibXR.CMake` file and injects it into the STM32CubeMX CMake project.

```bash
usage: xr_libxr_cmake [-h] input_dir
```

#### 🔧 必选参数 (Required)

- `input_dir`：

  指定 CubeMX 生成的 CMake 工程根目录
  Path to the CubeMX-generated CMake project root

#### ⚙️ 功能说明 (Functionality)

- 自动生成 `cmake/LibXR.CMake` 文件，内容包括：
  Generate `cmake/LibXR.CMake` containing:

  - 添加 `LibXR` 子目录
    Add `LibXR` as a subdirectory

  - 链接 `xr` 静态库
    Link the `xr` static library

  - 添加 `Core/Inc`、`User` 目录为包含路径
    Include `Core/Inc` and `User` directories

  - 添加 `User/*.cpp` 为源文件
    Add `User/*.cpp` to project sources

- 自动检测是否启用 FreeRTOS：
  Auto-detect FreeRTOS configuration:

  - 存在 `Core/Inc/FreeRTOSConfig.h` → `LIBXR_SYSTEM=FreeRTOS`
  - 否则设置为 `None`

- 自动删除旧的 `build/` 目录(如存在)
  Automatically deletes existing `build/` directory if found

- 自动向主 `CMakeLists.txt` 添加以下指令(若尚未包含)：
  Auto-appends the following line to `CMakeLists.txt` if missing:

  ```cmake
  include(${CMAKE_CURRENT_LIST_DIR}/cmake/LibXR.CMake)
  ```

#### 📦 输出内容 (Outputs)

- 生成 `cmake/LibXR.CMake` 文件
  Generates `cmake/LibXR.CMake` file

- 修改主工程的 `CMakeLists.txt`，插入 `include(...)`
  Updates `CMakeLists.txt` to include `LibXR.CMake`

- 删除原有构建缓存目录 `build/`(如存在)
  Deletes the old `build/` directory if present

---

### STM32 工程要求  (STM32 Project Requirements)

#### 📁 项目结构要求(Project Structure)

- 必须为 **STM32CubeMX 导出的 CMake 工程**
  Must be a CMake project exported from STM32CubeMX

- 项目应包含以下路径：
  Project should contain the following directories:

  - `xx.ioc`
  - `CMakeLists.txt`
  - `Core/Inc`, `Core/Src`

#### ⚙️ 配置要求(Peripheral & Middleware)

- 所有 **UART / SPI / I2C** 外设必须启用 **DMA**
  All **UART / SPI / I2C** peripherals must have **DMA** enabled

- 如果ADC启用了DMA，请开启连续转换模式
  If ADC has DMA enabled, enable continuous mode

- 推荐启用 **FreeRTOS**，自动生成 `FreeRTOSConfig.h`
  Recommended to enable **FreeRTOS** and generate `FreeRTOSConfig.h`

  - 关闭 `USB_DEVICE` 或 `USBX` 中间件
    Disable `USB_DEVICE` or `USBX` middleware.

#### ⏱️ Timebase 配置建议(Timebase Configuration)

> ✅ 强烈推荐使用 `TIM6`/`TIM7` 等 Timer 作为 Timebase
    Strongly recommended to use `TIM6`/`TIM7` Timers as Timebase
> ✅ 并将该中断优先级设置为 **最高(0)**
    And set the interrupt priority to **highest (0)**

---

### `xr_stm32_toolchain_switch`

自动切换 STM32 CMake 工程的工具链及 Clang 标准库配置。
Automatically switches STM32 CMake toolchain and Clang standard library configuration.

```bash
usage: xr_stm32_toolchain_switch {gcc,clang} [-g | --gnu | --hybrid | -n | --newlib | -p | --picolibc]
```

#### 🔧 必选参数 (Required)

- `gcc`
  切换为 GCC ARM 工具链
  Switch to GCC ARM toolchain

- `clang`
  切换为 Clang 工具链（需额外指定标准库）
  Switch to Clang toolchain (requires a standard library selection below)

#### ⚙️ 可选参数 (Standard library for `clang` only)

- `-g, --gnu, --hybrid`
  使用 GNU 标准库
  Use GNU standard library

- `-n, --newlib`
  使用 newlib 标准库
  Use newlib standard library

- `-p, --picolibc`
  使用 picolibc 标准库
  Use picolibc standard library

#### 📝 示例 (Examples)

```bash
xr_stm32_toolchain_switch gcc
xr_stm32_toolchain_switch clang -g
xr_stm32_toolchain_switch clang --newlib
xr_stm32_toolchain_switch clang --picolibc
```

#### 📦 功能说明 (Functionality)

- 自动修改 `CMakePresets.json`，切换默认工具链
  Automatically modify `CMakePresets.json` to switch the default toolchain

- 如使用 Clang，同步修改 `cmake/starm-clang.cmake` 的标准库类型
  If using Clang, synchronize the standard library type in `cmake/starm-clang.cmake`

---

## 🧩 代码生成后操作 (After Code Generation)

生成代码后，你需要**手动添加**以下内容：
After generating code, you must **manually add** the following:

```cpp
#include "app_main.h"
```

并在合适位置调用 `app_main();`：
And call `app_main();` in the appropriate location:

| 场景 (Scenario)       | 添加位置        |Where to add|
|-----------------------|------------------------------------| -----------|
| 🟢 Bare metal 裸机工程 | `main()` 函数末尾   | End of `main()` |
| 🔵 FreeRTOS 工程       | 线程入口       | Thread entry function |

---

## LibXR / LibXR_CppCodeGenerator / XRobot Relationship

LibXR、LibXR_CppCodeGenerator 与 XRobot 三者形成了一套完整的嵌入式与机器人软件开发体系，分工明确，协同紧密。
LibXR, LibXR_CppCodeGenerator and XRobot together form a complete software ecosystem for embedded and robotics development, with clear separation of concerns and tight integration.

---

### 🧠 LibXR

**LibXR 是跨平台的驱动抽象与工具库**，支持 STM32、Linux 等平台，包含：
LibXR is a cross-platform driver abstraction and utility library supporting STM32, Linux, and more. It provides:

- 通用外设接口封装
  Unified peripheral interface abstraction
- 嵌入式组件（如 Terminal、PowerManager、Database 等）
  Embedded modules like Terminal, PowerManager, Database, etc.
- FreeRTOS / bare-metal 支持
  FreeRTOS and bare-metal support
- 机器人运动学与导航
  Kinematics and navigation libraries for robotics
- 自动代码生成支持
  Code generation support

#### 🔗 Links

- **Repository**: [libxr](https://github.com/Jiu-xiao/libxr)
- **API Documentation**: [API](https://xrobot.work/libxr/)
- **Issues**: [Issue Tracker](https://github.com/Jiu-xiao/libxr/issues)

---

### 🔧 LibXR_CppCodeGenerator

**LibXR_CppCodeGenerator 是用于 LibXR 的代码生成工具链**，当前支持 STM32 + CubeMX，未来将扩展至 Zephyr、ESP-IDF 等平台。
LibXR_CppCodeGenerator is a code generation toolchain for LibXR. It currently supports STM32 with CubeMX, and is planned to support Zephyr, ESP-IDF, and more.

- 从不同平台的工程文件生成 `.yaml` 配置
  Parse project files from different platforms to generate `.yaml` configurations
- 基于 `.yaml` 自动生成 `app_main.cpp`、中断、CMake 等
  Generate `app_main.cpp`, interrupt handlers, and CMake integration
- 支持 `XRobot` glue 层集成
  Supports optional integration with XRobot framework
- 支持用户代码保留与多文件结构
  Preserves user code blocks and supports modular output

#### 🔗 Links

- **Repository**: [LibXR_CppCodeGenerator](https://github.com/Jiu-xiao/LibXR_CppCodeGenerator)
- **Documentation and Releases**: [PyPI](https://pypi.org/project/libxr/)
- **Issues**: [Issue Tracker](https://github.com/Jiu-xiao/LibXR_CppCodeGenerator/issues)

---

### 🤖 XRobot

XRobot 是一个轻量级的模块化应用管理框架，专为嵌入式设备而设计。它本身不包含任何驱动或业务代码，专注于模块的注册、调度、生命周期管理、事件处理与参数配置。
**XRobot is a lightweight modular application management framework designed for embedded systems.**
It does not include any drivers or business logic by itself. Instead, it focuses on module registration, scheduling, lifecycle management, event handling, and parameter configuration.

- 模块注册与生命周期管理
  Module registration and lifecycle management
- 参数管理 / 配置系统 / 事件系统
  Parameter management, configuration system, and event system
- ApplicationRunner / ThreadManager 等应用调度器
  ApplicationRunner and ThreadManager for runtime coordination
- 不直接访问硬件，依赖 LibXR 的 PeripheralManager
  Does not access hardware directly, relies on LibXR's PeripheralManager

---

#### ✅ Recommended For 推荐使用场景

- 拥有多个子模块（如传感器、通信、控制器）且希望统一管理初始化、调度与资源依赖
  For projects with multiple submodules (e.g., sensors, communication, controllers) needing unified lifecycle and dependency management.

- 希望构建平台无关的应用层逻辑，与底层驱动解耦
  For building platform-independent application logic decoupled from hardware drivers.

- 与 **LibXR** 结合使用，实现自动注册硬件对象（通过 `HardwareContainer`）
  When used with **LibXR**, supports automatic hardware registration via `HardwareContainer`.

- 支持生成模块入口代码、配置逻辑名与硬件名的映射，便于快速适配不同硬件配置
  Supports generating module entry code and logical-to-physical hardware name mapping for quick adaptation to different platforms.

#### 🔗 Links

- **Repository**: [XRobot](https://github.com/xrobot-org/XRobot)
- **Documentation**: [GitHub Pages](https://xrobot.work)
- **Releases**: [PyPI](https://pypi.org/project/xrobot)
- **Issues**: [Issue Tracker](https://github.com/xrobot-org/XRobot/issues)

---

## 📄 License

Licensed under **Apache-2.0**. See [LICENSE](LICENSE).


[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2FJiu-xiao%2FLibXR_CppCodeGenerator.svg?type=large)](https://app.fossa.com/projects/git%2Bgithub.com%2FJiu-xiao%2FLibXR_CppCodeGenerator?ref=badge_large)
