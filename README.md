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
的 C++ 代码，以 pip 包 `libxr` 发布，命令为 `libxr`。目前支持 STM32：它读取 STM32CubeMX 工程的
`.ioc` 文件，生成外设对象和入口函数 `app_main`，并把 LibXR 接入工程的 CMake 构建。

LibXR_CppCodeGenerator generates C++ code that uses [LibXR](https://github.com/xrobot-org/libxr)
from the projects of each platform's development tool. It is published as the pip package
`libxr`, with the command `libxr`. STM32 is supported so far: it reads the `.ioc` file of an
STM32CubeMX project, generates the peripheral objects and the entry function `app_main`, and
adds LibXR to the project's CMake build.

---

## 🔧 安装 / Installation

需要 Python 3.10 或更高版本。

Requires Python 3.10 or later.

### 使用 pipx 安装 (Install via `pipx`)

Windows

```powershell
python -m pip install --user pipx
python -m pipx ensurepath
python -m pipx install libxr
# 重新打开终端 / Restart your terminal
```

Linux

```bash
sudo apt install pipx
pipx ensurepath
pipx install libxr
# 重新打开终端 / Restart your terminal
```

### 使用 pip 安装 (Install via `pip`)

pip 用于 Windows 或虚拟环境。Debian、Ubuntu 等发行版的系统 Python 由包管理器管理，直接运行
`pip install` 会报 `externally-managed-environment`；在这类系统上使用 pipx，或先建立虚拟环境再用 pip
安装。

pip is for Windows or a virtual environment. On Debian, Ubuntu and similar distributions the
package manager owns the system Python, and a plain `pip install` fails with
`externally-managed-environment`; there, use pipx, or create a virtual environment first and
install with pip.

Windows

```powershell
pip install libxr
```

Linux

```bash
sudo apt install python3-venv
python3 -m venv ~/.venvs/libxr
. ~/.venvs/libxr/bin/activate
pip install libxr
# 在激活了虚拟环境的终端中使用 libxr / Use libxr in a terminal with the environment activated
```

### 从源码安装 (Install from source)

```bash
git clone https://github.com/xrobot-org/LibXR_CppCodeGenerator.git
cd LibXR_CppCodeGenerator
python ./scripts/gen_libxr_version.py
pip install .
```

`scripts/gen_libxr_version.py` 记录 LibXR master 当前的提交，`libxr stm32 setup` 新加入 LibXR 时检出这个
提交。

`scripts/gen_libxr_version.py` records the current commit of LibXR master, which
`libxr stm32 setup` checks out when it adds LibXR.

以上三种方式只选其一，不要混用。系统中有多份安装时，命令行实际调用的版本可能与预期不同，而不同版本
生成的代码并不一致。当前使用的版本可通过 `libxr --version` 查看。BSP 所用的版本由
`User/libxr_config.yaml` 顶层的 `generator: 6.0.0` 固定：生成器新建这个文件时写入当前的版本，重新
生成时保留这个键；安装的版本应与之一致，例如 `pipx install libxr==6.0.0`。

Use only one of these methods. With several installations present, the command line may run a
different version than expected, and different versions generate different code.
`libxr --version` shows the version in use. The version a BSP uses is pinned by
`generator: 6.0.0` at the top level of `User/libxr_config.yaml`: the generator writes the
current version when it creates this file, and regeneration keeps the key; install the same
version, e.g. `pipx install libxr==6.0.0`.

---

## 📚 基本概念 / Concepts

以一个 STM32F103 的 CubeMX 工程为例：串口 USART1 用作终端，另有 SPI1、I2C1、ADC1、USB、一路 PWM 和
一个标签为 `LED` 的 GPIO，HAL 时基使用 TIM3，系统为 FreeRTOS。各个概念的对应关系如下：

Take an STM32CubeMX project for an STM32F103: USART1 serves as the terminal, and there are SPI1,
I2C1, ADC1, USB, a PWM channel and a GPIO labeled `LED`; the HAL timebase is TIM3 and the system
is FreeRTOS. The concepts correspond as follows:

| 概念 Concept | 在这个例子里 | In this example |
| --- | --- | --- |
| CubeMX 工程 CubeMX project | `STM32F103RC.ioc`、`Core/`、`Drivers/` 和 `CMakeLists.txt`（Toolchain / IDE 选择 CMake） | `STM32F103RC.ioc`, `Core/`, `Drivers/` and `CMakeLists.txt` (Toolchain / IDE set to CMake) |
| 工程 YAML Project YAML | `.config.yaml`，`libxr parse` 从 `.ioc` 读出的芯片、引脚、外设、DMA 和 RTOS | `.config.yaml`, the chip, pins, peripherals, DMA and RTOS that `libxr parse` reads from the `.ioc` file |
| 入口源文件 Entry source | `User/app_main.cpp`，`libxr gen` 生成的外设对象和 `app_main()`；User Code 区域中的代码在重新生成时保留 | `User/app_main.cpp`, the peripheral objects and `app_main()` generated by `libxr gen`; the code in its User Code regions is kept on regeneration |
| 生成设置 Generation settings | `User/libxr_config.yaml`，缓冲区大小、USB 的 CDC、数据库、终端设备、线程优先级等参数 | `User/libxr_config.yaml`, parameters such as buffer sizes, the CDCs of USB, the database, the terminal device and thread priorities |
| LibXR 子模块 LibXR submodule | `Middlewares/Third_Party/LibXR`，工程的 gitlink 固定所用的提交 | `Middlewares/Third_Party/LibXR`, pinned to a commit by the project's gitlink |
| CMake 接入 CMake integration | `cmake/LibXR.CMake`，固定结构，设置块供用户修改，由 `CMakeLists.txt` include | `cmake/LibXR.CMake`, a fixed structure with a settings block for the user to edit, included by `CMakeLists.txt` |
| 换行符规则 Line endings | `.gitattributes`，仓库内的文本文件为 LF，签出时按平台转换 | `.gitattributes`, text files are LF in the repository and converted on checkout by platform |

---

## 🚀 配置工程 / Setting Up a Project

`libxr stm32 setup` 依次加入 LibXR 子模块、解析 `.ioc`、生成代码，并接入 CMake。改动工程之前先检查
工程是否有 `Core/`、一个 `.ioc` 文件和 `CMakeLists.txt`，不满足时报错，工程保持原样。`-t` 指定终端使用的
设备：

`libxr stm32 setup` adds the LibXR submodule, parses the `.ioc` file, generates the code and
integrates CMake, in that order. Before changing anything it checks that the project has
`Core/`, one `.ioc` file and `CMakeLists.txt`, and otherwise stops with an error and leaves the
project as it is. `-t` names the device the terminal uses:

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
[INFO] LibXR.CMake included in CMakeLists.txt.
[INFO] [Pass] All tasks completed.
[INFO] Next: #include "app_main.h" and call app_main() in the default task StartDefaultTask (Core/Src/main.c), inside USER CODE sections, which CubeMX keeps when it regenerates the code.
[INFO] Build: cmake --preset debug && cmake --build --preset debug
```

克隆 LibXR 时，`libxr` 在 GitHub 和镜像中选择响应最快的源。工程中已有的 LibXR 检出保持在原来的提交，
切换版本时使用 `--commit`。最后两行是接下来的步骤：`Core/Src` 中还没有源文件调用 `app_main()` 时，
`Next` 写出调用的位置，FreeRTOS 工程为默认任务，裸机工程为 `main()`；`Build` 是 `CMakePresets.json`
中第一个 preset 的构建命令。工程要求和各项参数见 [STM32 代码生成](https://xrobot.work/docs/code_gen/stm32)。

`.gitattributes` 写入 `* text=auto`、`*.sh text eol=lf` 和 `*.bat text eol=crlf`：仓库内的文本文件为
LF，签出时按平台转换，所以在 Windows 和 Linux 上用 CubeMX 重新生成，提交的差异里都只有真实的改动。
已有的 `.gitattributes` 只补上缺少的行。

When LibXR is cloned, `libxr` picks the fastest of GitHub and the mirrors. A LibXR checkout
already in the project stays at its commit; `--commit` switches it. The last two lines are the
next steps: while no source file in `Core/Src` calls `app_main()`, `Next` names where to call
it, the default task in a FreeRTOS project and `main()` in a bare-metal one; `Build` is the
build command of the first preset in `CMakePresets.json`. Project requirements and all options
are described in [STM32 code generation](https://xrobot.work/en/docs/code_gen/stm32).

`.gitattributes` gets `* text=auto`, `*.sh text eol=lf` and `*.bat text eol=crlf`: text files are
LF in the repository and converted on checkout by platform, so regenerating with CubeMX on
Windows or on Linux shows only real changes in the commit. An existing `.gitattributes` only
gets the missing lines.

---

## 🧩 从 .ioc 到代码 / From .ioc to Code

`libxr parse` 读出 `.ioc` 中的外设并打印摘要：

`libxr parse` reads the peripherals from the `.ioc` file and prints a summary:

```bash
$ libxr parse
[INFO] Processing STM32F103RC.ioc...
[INFO] Configuration exported to: .\.config.yaml

===== [Configuration Summary] =====

MCU: STM32F1 STM32F103RCT6

GPIO (2 pins):
  Outputs: 1
  Inputs: 1
  External Interrupts: 0

Active Peripherals:
  TIM: 1 instance(s)
    TIM2: Channels=CH3
  ADC: 1 instance(s)
    ADC1: Channels=1
  SPI: 1 instance(s)
    SPI1: BaudRate=562.5 KBits/s
  USART: 1 instance(s)
    USART1
  I2C: 1 instance(s)
    I2C1
  USB: 1 instance(s)
    USB
```

`libxr gen` 为每个外设生成一个静态对象，CubeMX 中的引脚标签成为对象名。入口源文件按这些部分排列：
include 和 `extern` 声明只列出用到的驱动头文件和 HAL 句柄，DMA 缓冲区每个一行，`app_main()` 中每组
外设前有一行说明注释。串口为开启了 DMA 的方向分配缓冲区，例子中的 USART1 两个方向都开启了 DMA。
SPI 总是有发送和接收缓冲区，两个方向都开启 DMA 时才使用 DMA；其他情况下最后一个参数（DMA 切换阈值）
为 `UINT32_MAX`，传输总是走轮询路径。例子中的 SPI1 没有 DMA：

`libxr gen` generates one static object per peripheral, and the pin labels set in CubeMX become
object names. The entry source is arranged in these parts: the includes and the `extern`
declarations list only the driver headers and HAL handles that are used, each DMA buffer takes
one line, and every group of peripherals in `app_main()` has a comment line. UARTs get buffers
for the directions with DMA; in the example USART1 has DMA in both directions. An SPI always has
transmit and receive buffers and uses DMA only with DMA in both directions; otherwise the last
argument (the DMA threshold) is `UINT32_MAX` and transfers always take the polling path. SPI1 in
the example has no DMA:

```cpp
// User/app_main.cpp（节选 / excerpt）
// DMA buffers (STM32F103RC: no D-cache)
alignas(4) static uint16_t adc1_buf[16];
alignas(4) static uint8_t i2c1_buf[32];
alignas(4) static uint8_t spi1_rx_buf[32];
alignas(4) static uint8_t spi1_tx_buf[32];
alignas(4) static uint8_t usart1_rx_buf[128];
alignas(4) static uint8_t usart1_tx_buf[128];
// ...

extern "C" void app_main(void)
{
  /* User Code Begin 2 */
  /* User Code End 2 */

  // Timebase and platform
  static STM32TimerTimebase timebase(&htim3);
  PlatformInit(static_cast<uint32_t>(Thread::Priority::MEDIUM), 1024);
  static STM32PowerManager power_manager;

  // GPIO
  static STM32GPIO PA8(GPIOA, GPIO_PIN_8);
  static STM32GPIO LED(LED_GPIO_Port, LED_Pin);

  // ADC
  static STM32ADC adc1(&hadc1, adc1_buf, {ADC_CHANNEL_0}, 3.3);
  // ...

  // PWM
  static STM32PWM pwm_tim2_ch3(&htim2, TIM_CHANNEL_3, false);

  // SPI, UART, I2C
  static STM32SPI spi1(&hspi1, spi1_rx_buf, spi1_tx_buf, UINT32_MAX);
  static STM32UART usart1(&huart1, usart1_rx_buf, usart1_tx_buf, 5);
  static STM32I2C i2c1(&hi2c1, i2c1_buf, 3);

  // USB FS: 1 CDC
  // ...

  /* User Code Begin 3 */
  while (true)
  {
    Thread::Sleep(UINT32_MAX);
  }
  /* User Code End 3 */
}
```

生成的部分已按 LibXR 的 `.clang-format`（Google 风格、列宽 90、Allman 大括号）排版，clang-format 不会
改动它们。缓冲区的对齐取决于芯片：没有数据 cache 的芯片（F1、F4 等）为 `alignas(4)`；有数据 cache 的
F7、H7 和 N6 按 32 字节的 cache 行对齐，数组大小向上取整到整行，传给驱动的大小仍是配置的值。
`libxr_config.yaml` 给出 `dma_section` 时，缓冲区放在这个段：

The generated parts are laid out by the `.clang-format` of LibXR (Google style, column limit 90,
Allman braces), and clang-format leaves them as they are. The alignment of a buffer depends on
the chip: a chip without a data cache (F1, F4 and others) gets `alignas(4)`; F7, H7 and N6, which
have one, align to the 32-byte cache line and round the array up to a whole line, while the size
a driver is told stays the configured one. With `dma_section` in `libxr_config.yaml`, the buffers
go into that section:

```cpp
// DMA buffers (STM32H723VG: D-cache, 32-byte lines)
alignas(32) static uint8_t usart1_rx_buf[128] __attribute__((section(".axi_ram")));
```

`/* User Code Begin N */` 与 `/* User Code End N */` 之间的代码在重新生成时保留，生成结果没有变化的文件
保持原样。区域中的手写代码用到驱动类（如 `STM32Flash`）、`FLASH_REGIONS` 或工程的 HAL 句柄（如
`htim5`）时，生成的文件保留对应的 include 和 `extern` 声明：

The code between `/* User Code Begin N */` and `/* User Code End N */` is kept on regeneration,
and files whose content does not change are left as they are. When hand-written code in a region
uses a driver class such as `STM32Flash`, `FLASH_REGIONS` or a HAL handle of the project such as
`htim5`, the generated file keeps the include and the `extern` declaration for it:

```bash
$ libxr gen -i .config.yaml -o User/app_main.cpp
[INFO] System: FreeRTOS
[INFO] Generated User: unchanged app_main.cpp, app_main.h, flash_map.hpp, libxr_config.yaml
```

各外设的生成规则见 [STM32 代码生成](https://xrobot.work/docs/code_gen/stm32) 下的各个页面。

The generation rules of each peripheral are described in the pages under
[STM32 code generation](https://xrobot.work/en/docs/code_gen/stm32).

---

## ⚙️ 生成设置 / Generation Settings

`User/libxr_config.yaml` 在第一次生成时按默认值创建，记录各外设的缓冲区大小、终端设备、软件定时器和
终端的参数等。修改其中的值后，重新运行 `libxr gen` 即按新值生成，文件中的修改和注释在重新生成时保留：

`User/libxr_config.yaml` is created with the defaults on the first generation and records the
buffer sizes of each peripheral, the terminal device, and the parameters of the software timer
and the terminal, among others. After a value is changed, running `libxr gen` again generates
with the new value; the changes and comments in the file are kept on regeneration:

```yaml
# User/libxr_config.yaml（节选 / excerpt）
generator: 6.0.0
terminal_source: usart1
software_timer:
  priority: 2
  stack_depth: 1024
# ...
USART:
  usart1:
    tx_buffer_size: 128
    rx_buffer_size: 128
    dma_section: ''
    tx_queue_size: 5
# ...
USB:
  usb_fs:
    enable: true
    ep0_packet_size: 8
    cdc:
    - {tx_fifo_size: 128, rx_fifo_size: 128, queue_size: 3}
# ...
database:
  enable: false
  block_size: auto
```

`software_timer.priority` 以及终端和看门狗的 `thread_priority` 取 0 到 4 或等级名，0 到 4 依次为
`IDLE`、`LOW`、`MEDIUM`、`HIGH`、`REALTIME`。LibXR 按 RTOS 的优先级数把等级换算为 RTOS 优先级，步长
`LIBXR_PRIORITY_STEP` 为（优先级数 - 1）/ 5：FreeRTOS 的第 n 级为 n 倍步长，`configMAX_PRIORITIES` 为
56 时 `MEDIUM` 是 22；ThreadX 数值越小优先级越高，`TX_MAX_PRIORITIES` 为 32 时 `MEDIUM` 是 12。
libxr 5.x 把这个数原样作为 RTOS 优先级，步长为 1 的 FreeRTOS 工程两者相同。

`software_timer.priority` and the `thread_priority` of the terminal and the watchdog take 0 to 4
or a level name; 0 to 4 are `IDLE`, `LOW`, `MEDIUM`, `HIGH` and `REALTIME` in that order. LibXR
converts the levels to RTOS priorities by the RTOS priority count, with the step
`LIBXR_PRIORITY_STEP` being (priority count - 1) / 5: level n on FreeRTOS is n steps, so `MEDIUM`
is 22 with `configMAX_PRIORITIES` 56; on ThreadX lower numbers are higher priorities, and
`MEDIUM` is 12 with `TX_MAX_PRIORITIES` 32. libxr 5.x passed the number to the RTOS as it was,
which gives the same priority on a FreeRTOS project with a step of 1.

CubeMX 中处于设备模式的 USB 生成 USB 设备和 CDC 串口 `usb_fs_cdc`。`USB.usb_fs.enable` 为 false 时
不生成；工程启用了 CubeMX 的 USB 中间件（如 `USB_DEVICE`）时，`enable` 的默认值为 false。设备对象
以 USB 实例命名，例如 USB（FSDEV）为 `usb_fs`，OTG 为 `usb_otg_fs` 和 `usb_otg_hs`。libxr 5.x 按速度
把 OTG 的设备对象命名为 `usb_fs` 和 `usb_hs`，CDC 串口的名字没有变化。

A USB in device mode in CubeMX produces a USB device and the CDC serial port `usb_fs_cdc`. With
`USB.usb_fs.enable` set to false it is not generated; when the project enables a CubeMX USB
middleware such as `USB_DEVICE`, `enable` defaults to false. The device object is named after
the USB instance, such as `usb_fs` for USB (FSDEV) and `usb_otg_fs` and `usb_otg_hs` for OTG.
libxr 5.x named the OTG device objects `usb_fs` and `usb_hs` after their speed; the names of the
CDC serial ports are unchanged.

`cdc` 每路 CDC 写一项，一个 USB 设备可以有几路 CDC。第 N 路（N 从 1 起）生成 `usb_otg_hs_cdc`、
`usb_otg_hs_cdc2` 这样的 `USB::CDCUart` 对象，按顺序分配端点：数据 IN 端点为 EP(2N-1)，通知端点为
EP(2N)，OTG 的数据 OUT 端点为 EP(N)。端点缓冲区随路数增减，`terminal_source` 可以指向任意一路，例如
`usb_otg_hs_cdc2`。`cdc_tx_fifo_size`、`cdc_rx_fifo_size` 和 `cdc_queue_size` 转换为 `cdc` 的一项，
写回时使用 `cdc` 的形式：

`cdc` has one item per CDC, and a USB device can have several. The N-th CDC (N from 1) produces a
`USB::CDCUart` object such as `usb_otg_hs_cdc` or `usb_otg_hs_cdc2` and takes the endpoints in
order: the data IN endpoint is EP(2N-1), the notification endpoint EP(2N), and on OTG the data
OUT endpoint EP(N). The endpoint buffers follow the number of CDCs, and `terminal_source` can
name any of them, such as `usb_otg_hs_cdc2`. `cdc_tx_fifo_size`, `cdc_rx_fifo_size` and
`cdc_queue_size` convert to one item of `cdc` and are written back in the form of `cdc`:

```yaml
USB:
  usb_otg_hs:
    enable: true
    cdc:
    - {tx_fifo_size: 128, rx_fifo_size: 128, queue_size: 3}
    - {tx_fifo_size: 128, rx_fifo_size: 128, queue_size: 3}
```

`database.enable` 为 true 时，`app_main()` 生成 `STM32Flash flash(FLASH_REGIONS, FLASH_REGION_NUMBER)`
和 `DatabaseRaw<N> database(flash)`，数据库以 `database` 注册；`FLASH_REGIONS` 来自
`User/flash_map.hpp`，数据库使用 Flash 末尾的两个扇区。模板参数 `N` 是 Flash 的最小写入单元，单位为
字节，由 `block_size` 决定：写成正整数时使用这个数，例如 `block_size: 32` 生成 `DatabaseRaw<32>`；
默认值 `auto` 生成 `STM32Flash::MIN_WRITE_SIZE`，即 LibXR 按芯片的 HAL 得出的值：

With `database.enable` set to true, `app_main()` generates `STM32Flash flash(FLASH_REGIONS,
FLASH_REGION_NUMBER)` and `DatabaseRaw<N> database(flash)`, and the database is registered as
`database`; `FLASH_REGIONS` comes from `User/flash_map.hpp`, and the database uses the last two
sectors of the Flash. The template argument `N` is the minimum write unit of the Flash in bytes
and follows `block_size`: a positive integer is used as it is, e.g. `block_size: 32` generates
`DatabaseRaw<32>`, and the default `auto` generates `STM32Flash::MIN_WRITE_SIZE`, the value LibXR
derives from the HAL of the chip:

```cpp
  // Flash and database
  static STM32Flash flash(FLASH_REGIONS, FLASH_REGION_NUMBER);
  static DatabaseRaw<STM32Flash::MIN_WRITE_SIZE> database(flash);
```

数值在生成时检查，不合要求时生成停止，报错写出文件、键和值：

Values are checked during generation; an invalid one stops generation with an error naming the
file, the key and the value:

```bash
$ libxr gen -i .config.yaml -o User/app_main.cpp
[INFO] System: FreeRTOS
[ERROR] Generation failed: User\libxr_config.yaml: USART.usart1.tx_queue_size 'five' is not a positive integer
```

---

## 🤖 XRobot 集成 / XRobot Integration

`--xrobot` 在入口源文件中以 `XR_REGISTER` 注册生成的对象，并在 User Code 区域之后调用
`XROBOT_MAIN()`。[XRobot](https://github.com/xrobot-org/XRobot) 的模块按这些名字使用外设。之后的
`libxr gen` 和 `libxr stm32 setup` 沿用入口源文件现在的选择，`--no-xrobot` 取消注册：

With `--xrobot`, the entry source registers the generated objects with `XR_REGISTER` and calls
`XROBOT_MAIN()` after the User Code regions. The Modules of
[XRobot](https://github.com/xrobot-org/XRobot) use the peripherals by these names. Later runs of
`libxr gen` and `libxr stm32 setup` keep the entry source's current choice, and `--no-xrobot`
removes the registrations:

```bash
$ libxr gen -i .config.yaml -o User/app_main.cpp --xrobot
[INFO] System: FreeRTOS
[INFO] Generated User: wrote app_main.cpp; unchanged app_main.h, flash_map.hpp, libxr_config.yaml
```

`XR_REGISTER` 按种类分组，每组之间空一行；已注册的对象不再写 `UNUSED(...)`：

`XR_REGISTER` lines are grouped by kind with a blank line between groups, and registered
objects need no `UNUSED(...)`:

```cpp
// User/app_main.cpp（节选 / excerpt）
  // Hardware registration
  XR_REGISTER(power_manager, LibXR::PowerManager);

  XR_REGISTER(PA8, LibXR::GPIO);
  XR_REGISTER(LED, LibXR::GPIO);

  XR_REGISTER(adc1_adc_channel_0, LibXR::ADC);

  XR_REGISTER(pwm_tim2_ch3, LibXR::PWM);

  XR_REGISTER(spi1, LibXR::SPI);

  XR_REGISTER(usart1, LibXR::UART);
  XR_REGISTER(usb_fs_cdc, LibXR::UART);

  XR_REGISTER(i2c1, LibXR::I2C);
  // ...

  /* User Code Begin 3 */
  /* User Code End 3 */
  XROBOT_MAIN();
}
```

已有入口源文件的 User Code 3 仍是不带 `--xrobot` 时生成的默认循环时，加上 `--xrobot` 重新生成会清空
这个循环，使其后的 `XROBOT_MAIN()` 能够执行；User Code 3 的其他内容保持不变。

When User Code 3 of an existing entry source still holds the default loop generated without
`--xrobot`, regenerating with `--xrobot` empties the loop, so that the `XROBOT_MAIN()` after it
runs; any other content of User Code 3 is kept.

`libxr stm32 cmake` 和 `libxr stm32 setup` 按入口源文件的选择在 `cmake/LibXR.CMake` 的设置块中加入或
删除 `XROBOT_MODULES_DIR`，XRobot 的模块随之参与构建：

`libxr stm32 cmake` and `libxr stm32 setup` add or remove `XROBOT_MODULES_DIR` in the settings
block of `cmake/LibXR.CMake` according to the entry source's choice, so that the XRobot Modules
are built with the project:

```bash
$ libxr stm32 cmake
[INFO] LibXR.CMake: added set(XROBOT_MODULES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Modules"), as User/app_main.cpp uses XRobot
[INFO] Updated existing LibXR.CMake for system: FreeRTOS
[INFO] LibXR.CMake already included in CMakeLists.txt.
```

BSP 根目录还没有 `Modules/modules.yaml` 时，`libxr stm32 setup --xrobot` 在最后依次列出 XRobot 的设置
命令：`xrobot init`、`xrobot module add`、`xrobot setup` 和 `xrobot instance add`。

While the BSP root has no `Modules/modules.yaml` yet, `libxr stm32 setup --xrobot` ends by listing
the XRobot setup commands in order: `xrobot init`, `xrobot module add`, `xrobot setup` and
`xrobot instance add`.

详见 [与 XRobot 集成](https://xrobot.work/docs/code_gen/code-gen-xrobot-inter)。

See [XRobot integration](https://xrobot.work/en/docs/code_gen/code-gen-xrobot-inter).

---

## 🔨 CMake 与工具链 / CMake and Toolchains

`libxr stm32 cmake` 整体生成 `cmake/LibXR.CMake`，并在 `CMakeLists.txt` 中 include 它；`libxr stm32 setup`
已包含这一步。文件的结构固定，设置块在最前，其后依次是 LibXR、Application、库的优化设置和固件镜像：

`libxr stm32 cmake` generates `cmake/LibXR.CMake` as a whole and includes it from
`CMakeLists.txt`; `libxr stm32 setup` already does this. The structure of the file is fixed: the
settings block comes first, followed by LibXR, Application, the optimization of the libraries
and the firmware images:

```cmake
# Generated by `libxr stm32 setup`; edit the values in the "Project settings" block only.

# ---- Project settings --------------------------------------------------------
set(LIBXR_SYSTEM FreeRTOS)
set(LIBXR_DRIVER st)
set(XROBOT_MODULES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Modules")
# User sources of the application; "" when CMakeLists.txt adds them itself.
set(LIBXR_USER_SOURCES_GLOB "${CMAKE_CURRENT_SOURCE_DIR}/User/*.cpp")
# Optimization level of the application in Debug builds and of everything in Release builds;
# "" keeps the level of the toolchain file.
set(LIBXR_OPT_DEBUG "-Og")
set(LIBXR_OPT_RELEASE "")

# ---- LibXR -------------------------------------------------------------------
# ...
```

重写时设置块中用户改过的值和增加的内容保留，`LIBXR_SYSTEM` 取 `Core/Inc` 中的
`FreeRTOSConfig.h` 或 `app_threadx.h` 所决定的系统，`XROBOT_MODULES_DIR` 与入口源文件的选择一致，其余
各块重新生成。已有的 `LibXR.CMake` 按这个结构重写：设置的值保留，`LIBXR_OPT_DEBUG` 和
`LIBXR_OPT_RELEASE` 取已用的优化级别（工具链文件中的 `CMAKE_CXX_FLAGS_DEBUG` 和
`CMAKE_CXX_FLAGS_RELEASE`，两个工具链文件不同时留空），重新生成的语句之外的内容保留在
`Kept from the earlier LibXR.CMake` 块中；旧文件 Debug 块中用户改过的库优化选项也原样放在这里，
包在同样的 `if(CMAKE_BUILD_TYPE STREQUAL "Debug")` 中，排在库的 `-O2` 之后，因此仍然生效。
`LIBXR_OPT_DEBUG` 是应用的 Debug 优化级别，`LIBXR_OPT_RELEASE` 是应用、`xr` 和 CubeMX 生成的库在
Release 构建中的优化级别；Debug 构建中 `xr` 和 CubeMX 生成的全部库目标使用 `-O2`（libxr 5.x 只包括
`FreeRTOS`、`STM32_Drivers` 和 `USB_Device_Library`，`ThreadX` 等其他库目标使用工具链的 Debug 级别）。
目标的编译选项排在工具链的 `CMAKE_<LANG>_FLAGS_<CONFIG>` 之后，所以最后一个 `-O` 就是这里设置的级别。
值为 `""` 时沿用工具链文件中的级别，CubeMX 写出的 GCC 为 `-Os`、ST Arm Clang 为 `-Oz`；新工程的
`LIBXR_OPT_RELEASE` 为 `""`。

On a rewrite the values the user changed in the settings block, and what the user added there,
stay; `LIBXR_SYSTEM` takes the system that `FreeRTOSConfig.h` or `app_threadx.h` in `Core/Inc`
decides, `XROBOT_MODULES_DIR` agrees with the entry source's choice, and the other blocks are
generated again. An existing `LibXR.CMake` is rewritten in this structure: the values of the
settings stay, `LIBXR_OPT_DEBUG` and `LIBXR_OPT_RELEASE` take the optimization levels in use (the
`CMAKE_CXX_FLAGS_DEBUG` and `CMAKE_CXX_FLAGS_RELEASE` of the toolchain files, empty when the two
toolchain files differ), and what is not among the regenerated statements stays in the
`Kept from the earlier LibXR.CMake` block; the library optimization options the user changed
in the Debug block of the old file go there as written too, inside the same
`if(CMAKE_BUILD_TYPE STREQUAL "Debug")` and after the `-O2` of the libraries, so they still apply.
`LIBXR_OPT_DEBUG` is the Debug optimization level of the application, and `LIBXR_OPT_RELEASE`
the Release level of the application, `xr` and the libraries CubeMX generates; in Debug builds
`xr` and every library target CubeMX generates use `-O2` (libxr 5.x covered only `FreeRTOS`,
`STM32_Drivers` and `USB_Device_Library`, and other library targets such as `ThreadX` used the
Debug level of the toolchain). Target compile options come after the toolchain's
`CMAKE_<LANG>_FLAGS_<CONFIG>`, so the last `-O` is the level set here. With `""` the level of the
toolchain file applies, `-Os` for GCC and `-Oz` for ST Arm Clang as CubeMX writes them; a new
project has `LIBXR_OPT_RELEASE` set to `""`.

应用每次链接之后，固件镜像块用工具链文件中的 `CMAKE_OBJCOPY` 由 ELF 生成 `<工程名>.hex`（Intel HEX）
和 `<工程名>.bin`（二进制镜像），三个文件位于同一目录，用 CubeMX 的 preset 构建时为 `build/<preset>/`。

After each link of the application, the firmware image block makes `<project>.hex` (Intel HEX)
and `<project>.bin` (binary image) from the ELF with the `CMAKE_OBJCOPY` of the toolchain file;
the three files are in the same directory, `build/<preset>/` when built with a CubeMX preset.

`cmake/starm-clang.cmake` 和 `cmake/gcc-arm-none-eabi.cmake` 保持 CubeMX 写出的内容。`libxr stm32 toolchain`
切换 `CMakePresets.json` 中默认的工具链，在 GCC 与 ST Arm Clang 之间切换时删除以前的构建目录，下次
构建重新配置；ST Arm Clang 还可以选择标准库，这时只改写 CubeMX 在 `starm-clang.cmake` 中写出的
`set(STARM_TOOLCHAIN_CONFIG ...)` 一行：

`cmake/starm-clang.cmake` and `cmake/gcc-arm-none-eabi.cmake` keep what CubeMX wrote.
`libxr stm32 toolchain` switches the default toolchain in `CMakePresets.json`; switching between
GCC and ST Arm Clang removes the old build directories, and the next build configures again. ST
Arm Clang can also select its standard library, which rewrites only the
`set(STARM_TOOLCHAIN_CONFIG ...)` line that CubeMX wrote in `starm-clang.cmake`:

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

---

## 💾 Flash 布局 / Flash Layout

`libxr stm32 flash-info` 按型号推算 Flash 扇区表，`libxr gen` 据此生成 `User/flash_map.hpp`：

`libxr stm32 flash-info` derives the flash sector table of a model, from which `libxr gen`
generates `User/flash_map.hpp`:

```bash
$ libxr stm32 flash-info STM32F103RCT6
model: STM32F103RCT6
flash_base: '0x08000000'
flash_size_kb: 256
sectors:
- index: 0
  address: '0x08000000'
  size_kb: 2.0
- index: 1
  address: '0x08000800'
  size_kb: 2.0
# ...
- index: 127
  address: '0x0803F800'
  size_kb: 2.0
```

---

## 📌 引脚布局 / Pin Layout

`libxr pins` 按型号给出芯片的封装和引脚布局：每个引脚的封装位置、名称、类型和全部可选信号。型号的前缀决定平台，目前支持 STM32 和 MSPM0；输出为 YAML，`--format json` 输出 JSON。

`libxr pins` prints the package and pin layout of a chip model: for each pin its position on the package, name, type and all selectable signals. The prefix of the model chooses the platform; STM32 and MSPM0 are supported. The output is YAML, or JSON with `--format json`.

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
  # ...
  iomux_pincm: 1
  modes:
    PA0: 1
    UART0.TX: 2
    I2C0.SDA: 3
    # ...
```

- STM32 的封装由型号决定（`STM32H723VGT6` 为 LQFP100）；MSPM0 的封装取自型号后缀中的代码（`MSPM0G3507SPMR` 的 `PM`），型号中没有时用 `--package` 给出（`LQFP-64`、`PM` 或 `LQFP-64(PM)`）。
- `pin_count` 是封装上的位置数。一个位置可以有多个条目：STM32G0 的 PA9 和 PA11 可以互换，共用一个位置。
- MSPM0 的引脚还有 `iomux_pincm` 和每个信号的模式号 `modes`。
- 数据来自厂商：STM32 来自 ST 的 [STM32_open_pin_data](https://github.com/STMicroelectronics/STM32_open_pin_data)（BSD-3-Clause），MSPM0 来自 TI SysConfig 的器件数据（TI 有限许可，只可用于 TI 器件）。数据文件和两份许可证文本在 `src/libxr/pin_data/`，随包分发；用 `scripts/build_pin_data.py` 重新生成。

- The package of an STM32 is part of the model (`STM32H723VGT6` is LQFP100); that of an MSPM0 comes from the code in the model suffix (`PM` of `MSPM0G3507SPMR`), or from `--package` when the model has none (`LQFP-64`, `PM` or `LQFP-64(PM)`).
- `pin_count` is the number of positions on the package. A position can have several entries: PA9 and PA11 of an STM32G0 can be swapped and share one position.
- An MSPM0 pin also has `iomux_pincm` and `modes`, the mode of each signal.
- The data comes from the vendors: STM32 from ST's [STM32_open_pin_data](https://github.com/STMicroelectronics/STM32_open_pin_data) (BSD-3-Clause), MSPM0 from the device data of TI SysConfig (TI limited license, for TI devices only). The data files and both license texts are in `src/libxr/pin_data/` and are distributed with the package; `scripts/build_pin_data.py` rebuilds them.

---

## 🚀 命令一览 / Commands

`parse` 和 `gen` 按工程所属的平台选择解析器和生成器，只属于某个平台的命令放在平台名之下。

`parse` and `gen` choose the parser and the generator by the project's platform; commands that
belong to one platform sit under its name.

| 命令 Command | 说明 | Description |
| --- | --- | --- |
| `libxr parse` | 解析工程，写出工程 YAML | Parse a project into the project YAML |
| `libxr gen` | 由工程 YAML 生成入口源文件 | Generate the entry source from the project YAML |
| `libxr pins` | 打印某个型号的封装和引脚布局 | Print the package and pin layout of a model |
| `libxr stm32 setup` | 为 CubeMX 工程加入 LibXR，生成代码并接入 CMake | Add LibXR to a CubeMX project, generate the code and integrate CMake |
| `libxr stm32 cubemx-gen` | 以脚本模式运行 STM32CubeMX，由 `.ioc` 重新生成 CubeMX 工程 | Run STM32CubeMX in script mode to regenerate the CubeMX project from the `.ioc` file |
| `libxr stm32 cmake` | 把 LibXR 接入 CubeMX 的 CMake 工程 | Integrate LibXR into the CubeMX CMake project |
| `libxr stm32 flash-info` | 打印某个型号的 Flash 布局 | Print the flash layout of a model |
| `libxr stm32 toolchain` | 切换工具链和 ST Arm Clang 的标准库 | Switch the toolchain and the ST Arm Clang standard library |

入门教程和完整说明见文档 / Tutorials and reference: <https://xrobot.work/docs/code_gen> ·
<https://xrobot.work/en/docs/code_gen>

### 旧命令 / Old commands

6.0.0 之前的 `xr_*` 命令仍可使用，参数与原来相同，运行时提示对应的新命令，将在 7.0.0 删除。以下几处
行为与 libxr 5.2.4 不同：`xr_gen_code` 和 `xr_gen_code_stm32` 收到 `--hw-cntr` 时报错退出，它生成的
`HardwareContainer` 已从 LibXR 删除，XRobot 工程改用 `--xrobot`；`xr_parse` 和 `xr_parse_ioc` 在含有
多个 `.ioc` 文件的目录中报错退出，不再逐个解析；`xr_stm32_toolchain_switch clang` 不带 `-g`、`-n` 或
`-p` 时保持当前的标准库，不再报错；`xr_stm32_toolchain_switch` 在 GCC 与 ST Arm Clang 之间切换时删除
`build/` 和 `cmake-build*`，`xr_stm32_cmake` 则不再删除它们，CMake 在下次构建时重新配置。

The `xr_*` commands of versions before 6.0.0 still work with the same arguments; they name their
new command when run and are removed in 7.0.0. A few behaviors differ from libxr 5.2.4:
`xr_gen_code` and `xr_gen_code_stm32` stop with an error on `--hw-cntr`, as LibXR no longer has
the `HardwareContainer` it generated, and XRobot projects use `--xrobot` instead; `xr_parse` and
`xr_parse_ioc` stop with an error in a directory with several `.ioc` files instead of parsing
each; `xr_stm32_toolchain_switch clang` without `-g`, `-n` or `-p` keeps the current standard
library instead of failing; `xr_stm32_toolchain_switch` removes `build/` and `cmake-build*` when
it switches between GCC and ST Arm Clang, while `xr_stm32_cmake` no longer removes them, and
CMake configures again on the next build.

| 旧命令 Old | 新命令 New |
| --- | --- |
| `xr_parse`、`xr_parse_ioc` | `libxr parse` |
| `xr_gen_code`、`xr_gen_code_stm32` | `libxr gen` |
| `xr_cubemx_cfg` | `libxr stm32 setup` |
| `xr_cubemx_generate` | `libxr stm32 cubemx-gen` |
| `xr_stm32_cmake` | `libxr stm32 cmake` |
| `xr_stm32_flash` | `libxr stm32 flash-info` |
| `xr_stm32_toolchain_switch` | `libxr stm32 toolchain` |

---

## 🧪 测试 / Tests

```bash
pip install clang-format==21.1.8
python -m unittest discover -s tests -v
```

生成的 C++ 文件按 LibXR 的风格排版，测试用固定版本 21.1.8 的 clang-format 核对 DevC、MC02 和随机工程的
输出；没有安装这个版本时，这些测试被跳过。

The generated C++ files follow the style of LibXR, and the tests check the output for DevC, MC02
and random projects against clang-format 21.1.8; they are skipped without that version.

---

## 📖 更多信息 / More Information

- [GitHub Repository](https://github.com/xrobot-org/LibXR_CppCodeGenerator)
- [Documentation](https://xrobot.work/docs/code_gen)
- [PyPI](https://pypi.org/project/libxr/)
- [Issue Tracker](https://github.com/xrobot-org/LibXR_CppCodeGenerator/issues)
- [LibXR](https://github.com/xrobot-org/libxr) · [XRobot](https://github.com/xrobot-org/XRobot)
