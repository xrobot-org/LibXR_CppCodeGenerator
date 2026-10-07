"""libxr 命令：为不同平台的工程生成基于 LibXR 的 C++ 代码。
The libxr command: generate LibXR-based C++ code for projects of different platforms.

parse 和 gen 按工程所属的平台选择解析器和生成器（PLATFORMS）；只属于一个平台的命令在平台名
之下，例如 libxr stm32 setup。旧的 xr_* 命令见 libxr.legacy。
parse and gen choose the parser and the generator by the platform of the project (PLATFORMS);
commands that belong to one platform sit under its name, such as libxr stm32 setup. The old
xr_* commands are in libxr.legacy.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import yaml
from xr_syntax.i18n import localize_argparse, tr

from libxr import update_notice
from libxr.output import configure_output


@dataclass(frozen=True)
class Platform:
    """一个平台：名字、如何识别它的工程，以及它的解析器和生成器。
    A platform: its name, how its projects are recognized, and its parser and generator.
    """

    name: str
    # 识别方式的说明，用于无法识别平台时的报错。
    # How a project is recognized, for the error when no platform matches.
    project: Callable[[], str]
    # 目录是否属于这个平台的工程。
    # Whether a directory holds a project of this platform.
    detect: Callable[[str], bool]
    parse: Callable[[argparse.Namespace], None]
    gen: Callable[[argparse.Namespace], None]


def _has_ioc(directory: str) -> bool:
    """directory 中有 .ioc 文件（STM32CubeMX 工程）时为 True；目录无法列出时为 False。
    True when directory holds an .ioc file (an STM32CubeMX project); False when the directory
    cannot be listed.
    """
    try:
        return any(name.endswith(".ioc") for name in os.listdir(directory))
    except OSError:
        return False


def _has_syscfg(directory: str) -> bool:
    """directory 的根目录中有 .syscfg 文件（SysConfig 工程）时为 True；目录无法列出时为 False。
    True when the root of directory holds a .syscfg file (a SysConfig project); False when the
    directory cannot be listed.
    """
    try:
        return any(name.endswith(".syscfg") for name in os.listdir(directory))
    except OSError:
        return False


def _has_hpmpc(directory: str) -> bool:
    """directory 是 HPM 工程（根目录有 app.yaml 且 boards/ 的某个板子目录里有 .hpmpc）时为
    True；目录无法列出时为 False。
    True when directory holds an HPM project (an app.yaml in the root and a .hpmpc in a board
    directory under boards/); False when the directory cannot be listed.
    """
    if not os.path.isfile(os.path.join(directory, "app.yaml")):
        return False
    try:
        boards = os.listdir(os.path.join(directory, "boards"))
    except OSError:
        return False
    for name in boards:
        board = os.path.join(directory, "boards", name)
        if os.path.isdir(board):
            try:
                if any(file.endswith(".hpmpc") for file in os.listdir(board)):
                    return True
            except OSError:
                continue
    return False


def _stm32_parse(args: argparse.Namespace) -> None:
    """用 STM32 解析器运行 libxr parse。
    Run libxr parse with the STM32 parser.
    """
    from libxr.peripheral_analyzer_stm32 import parse_project

    parse_project(args.directory, args.output)


def _stm32_gen(args: argparse.Namespace) -> None:
    """用 STM32 生成器运行 libxr gen。
    Run libxr gen with the STM32 generator.

    args.xrobot 为 None 时沿用输出文件现在的选择：它由 --xrobot 生成时继续生成 XRobot 代码。
    With args.xrobot None the output file keeps its choice: XRobot code is generated again when
    it was generated with --xrobot.
    """
    from libxr.generator_code_stm32 import generate
    from libxr.generator_stm32_cmake import uses_xrobot

    use_xrobot = args.xrobot
    if use_xrobot is None:
        use_xrobot = uses_xrobot(args.output)
        if use_xrobot:
            logging.info(
                tr(
                    f"{args.output} uses XRobot; generating with --xrobot "
                    "(--no-xrobot turns it off).",
                    f"{args.output} 使用了 XRobot，继续按 --xrobot 生成（--no-xrobot 可关闭）。",
                )
            )
    generate(args.input, args.output, use_xrobot, args.libxr_config)


def _mspm0_parse(args: argparse.Namespace) -> None:
    """用 MSPM0 解析器运行 libxr parse。
    Run libxr parse with the MSPM0 parser.
    """
    from libxr.peripheral_analyzer_mspm0 import parse_project

    parse_project(args.directory, args.output)


def _mspm0_gen(args: argparse.Namespace) -> None:
    """用 MSPM0 生成器运行 libxr gen。
    Run libxr gen with the MSPM0 generator.

    args.xrobot 为 None 时沿用输出文件现在的选择：它由 --xrobot 生成时继续生成 XRobot 代码。
    With args.xrobot None the output file keeps its choice: XRobot code is generated again when
    it was generated with --xrobot.
    """
    from libxr.generator_code_mspm0 import generate
    from libxr.generator_stm32_cmake import uses_xrobot

    use_xrobot = args.xrobot
    if use_xrobot is None:
        use_xrobot = uses_xrobot(args.output)
        if use_xrobot:
            logging.info(
                tr(
                    f"{args.output} uses XRobot; generating with --xrobot "
                    "(--no-xrobot turns it off).",
                    f"{args.output} 使用了 XRobot，继续按 --xrobot 生成（--no-xrobot 可关闭）。",
                )
            )
    generate(args.input, args.output, use_xrobot, args.libxr_config)


def _hpm_parse(args: argparse.Namespace) -> None:
    """用 HPM 解析器运行 libxr parse。
    Run libxr parse with the HPM parser.
    """
    from libxr.peripheral_analyzer_hpm import parse_project

    parse_project(args.directory, args.output)


def _hpm_gen(args: argparse.Namespace) -> None:
    """用 HPM 生成器运行 libxr gen。
    Run libxr gen with the HPM generator.

    args.xrobot 为 None 时沿用输出文件现在的选择：它由 --xrobot 生成时继续生成 XRobot 代码。
    With args.xrobot None the output file keeps its choice: XRobot code is generated again when
    it was generated with --xrobot.
    """
    from libxr.generator_code_hpm import generate
    from libxr.generator_stm32_cmake import uses_xrobot

    use_xrobot = args.xrobot
    if use_xrobot is None:
        use_xrobot = uses_xrobot(args.output)
        if use_xrobot:
            logging.info(
                tr(
                    f"{args.output} uses XRobot; generating with --xrobot "
                    "(--no-xrobot turns it off).",
                    f"{args.output} 使用了 XRobot，继续按 --xrobot 生成（--no-xrobot 可关闭）。",
                )
            )
    generate(args.input, args.output, use_xrobot, args.libxr_config)


PLATFORMS = (
    Platform(
        "stm32",
        lambda: tr("a directory with an STM32CubeMX .ioc file", "含有 STM32CubeMX .ioc 文件的目录"),
        _has_ioc,
        _stm32_parse,
        _stm32_gen,
    ),
    Platform(
        "mspm0",
        lambda: tr(
            "a directory with a SysConfig .syscfg file in its root",
            "根目录中含有 SysConfig .syscfg 文件的目录",
        ),
        _has_syscfg,
        _mspm0_parse,
        _mspm0_gen,
    ),
    Platform(
        "hpm",
        lambda: tr(
            "a directory with an app.yaml and a .hpmpc under boards/",
            "根目录有 app.yaml 且 boards/ 下有 .hpmpc 文件的目录",
        ),
        _has_hpmpc,
        _hpm_parse,
        _hpm_gen,
    ),
)


def _supported() -> str:
    """支持的平台及其工程的识别方式，用于报错。
    The supported platforms and how their projects are recognized, for error messages.
    """
    return tr("; ", "；").join(f"{p.name}: {p.project()}" for p in PLATFORMS)


def platform_of(directory: str) -> Platform:
    """directory 中的工程所属的平台；无法识别时记录错误（列出支持的平台）并以状态 1 退出。
    The platform of the project in directory; when none matches, log an error that lists the
    supported platforms and exit with status 1.
    """
    for platform in PLATFORMS:
        if platform.detect(directory):
            return platform
    logging.error(
        tr(
            f"{directory}: no supported platform recognized ({_supported()})",
            f"{directory}：无法识别工程所属的平台（支持 {_supported()}）",
        )
    )
    sys.exit(1)


def recorded_platform(config: str) -> Platform | None:
    """工程 YAML config 中 parse 记录的平台（Platform 键）；没有记录或文件无法读取时为 None。
    The platform that parse recorded in the project YAML config (the Platform key); None
    when none is recorded or the file cannot be read.

    文件无法读取或解析时由生成器报告详细的错误。记录的平台不受支持时记录错误并以状态 1 退出。
    A file that cannot be read or parsed is reported in detail by the generator. An unsupported
    recorded platform logs an error and exits with status 1.
    """
    try:
        with open(config, encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    name = data.get("Platform") if isinstance(data, dict) else None
    if name is None:
        return None
    for platform in PLATFORMS:
        if platform.name == name:
            return platform
    logging.error(
        tr(
            f"{config}: platform {name!r} is not supported ({_supported()})",
            f"{config}：不支持记录的平台 {name!r}（支持 {_supported()}）",
        )
    )
    sys.exit(1)


def cmd_parse(args: argparse.Namespace) -> None:
    """libxr parse：识别 -d 目录中工程的平台，解析工程并写出工程 YAML。
    libxr parse: recognize the platform of the project in the -d directory, parse the project
    and write the project YAML.
    """
    if not os.path.isdir(args.directory):
        logging.error(
            tr(f"Directory does not exist: {args.directory}", f"目录不存在：{args.directory}")
        )
        sys.exit(1)
    platform_of(args.directory).parse(args)


def cmd_gen(args: argparse.Namespace) -> None:
    """libxr gen：按工程 YAML 记录的平台由它生成代码。
    libxr gen: generate code from the project YAML by the platform it records.

    YAML 没有记录平台时（旧版 parse 写出的文件），按 -d 工程目录（默认当前目录）识别平台。
    When the YAML records no platform (a file written by an older parse), the platform of the
    -d project directory, the current directory by default, is used.
    """
    if not os.path.isfile(args.input):
        logging.error(
            tr(
                f"YAML configuration file not found: {args.input}",
                f"找不到 YAML 配置文件：{args.input}",
            )
        )
        sys.exit(1)
    platform = recorded_platform(args.input)
    if platform is None:
        if not os.path.isdir(args.directory):
            logging.error(
                tr(f"Directory does not exist: {args.directory}", f"目录不存在：{args.directory}")
            )
            sys.exit(1)
        platform = platform_of(args.directory)
    platform.gen(args)


def cmd_stm32_setup(args: argparse.Namespace) -> None:
    """libxr stm32 setup：把 STM32CubeMX 工程配置为使用 LibXR 的工程。
    libxr stm32 setup: set up an STM32CubeMX project to use LibXR.
    """
    from libxr.config_cubemx_project import setup_project

    setup_project(
        args.directory,
        terminal_source=args.terminal,
        xrobot_enable=args.xrobot,
        commit=args.commit,
        git_source=args.git_source,
        git_mirrors=args.git_mirrors,
    )


def _setup_without_cubemx(args: argparse.Namespace, parse, generate) -> None:
    """没有 CubeMX 步骤的平台（HPM、MSPM0）的 setup，规则与 libxr stm32 setup 相同：写
    .gitignore（已有的不动），解析工程到 .config.yaml，生成 User/app_main.cpp（或 -o 给出的
    文件）。--xrobot 和 --no-xrobot 都不给时沿用输出文件现在的选择，新文件不用 XRobot。结束时，
    XRobot 工程还没有 Modules/modules.yaml 就给出 XRobot 的设置步骤；与 stm32 setup 一样不改
    User/xrobot_main.hpp，它由 xrobot setup / xrobot gen 生成。
    The setup of a platform without a CubeMX step (HPM, MSPM0), with the rules of libxr stm32
    setup: write .gitignore (an existing one stays), parse the project into .config.yaml and
    generate User/app_main.cpp (or the file -o names). With neither --xrobot nor --no-xrobot
    the output file keeps its choice, and a new file does not use XRobot. At the end an XRobot
    project without Modules/modules.yaml gets the XRobot setup steps; like stm32 setup it
    leaves User/xrobot_main.hpp alone, which xrobot setup / xrobot gen generates.
    """
    from libxr.config_cubemx_project import create_gitignore_file, report_xrobot_steps
    from libxr.generator_stm32_cmake import uses_xrobot

    output = args.output or os.path.join(args.directory, "User", "app_main.cpp")
    use_xrobot = args.xrobot
    if use_xrobot is None:
        use_xrobot = uses_xrobot(output)
        if use_xrobot:
            logging.info(
                tr(
                    f"{output} uses XRobot; generating with --xrobot (--no-xrobot turns it off).",
                    f"{output} 使用了 XRobot，继续按 --xrobot 生成（--no-xrobot 可关闭）。",
                )
            )
    create_gitignore_file(args.directory)
    parse(args.directory, None)
    generate(os.path.join(args.directory, ".config.yaml"), output, use_xrobot, args.libxr_config)
    report_xrobot_steps(args.directory, use_xrobot)


def cmd_hpm_setup(args: argparse.Namespace) -> None:
    """libxr hpm setup：解析 HPM 工程并生成 User/app_main.cpp；没有 CubeMX 步骤（见
    _setup_without_cubemx()）。
    libxr hpm setup: parse an HPM project and generate User/app_main.cpp; there is no CubeMX
    step (see _setup_without_cubemx()).
    """
    from libxr.generator_code_hpm import generate
    from libxr.peripheral_analyzer_hpm import parse_project

    _setup_without_cubemx(args, parse_project, generate)


def cmd_mspm0_setup(args: argparse.Namespace) -> None:
    """libxr mspm0 setup：解析 SysConfig 工程并生成 User/app_main.cpp；没有 CubeMX 步骤（见
    _setup_without_cubemx()）。
    libxr mspm0 setup: parse a SysConfig project and generate User/app_main.cpp; there is no
    CubeMX step (see _setup_without_cubemx()).
    """
    from libxr.generator_code_mspm0 import generate
    from libxr.peripheral_analyzer_mspm0 import parse_project

    _setup_without_cubemx(args, parse_project, generate)


def cmd_stm32_cubemx_gen(args: argparse.Namespace) -> None:
    """libxr stm32 cubemx-gen：以脚本模式运行 STM32CubeMX 生成工程；出错时记录错误（调试日志另记
    调用栈）并以状态 1 退出。
    libxr stm32 cubemx-gen: run STM32CubeMX in script mode to generate the project; an error is
    logged, with the traceback at debug level, and exits with status 1.
    """
    from libxr.cubemx_generator import generate_cubemx_project

    try:
        generate_cubemx_project(
            project_dir=args.directory,
            ioc_file=args.ioc,
            cubemx_cmd=args.cubemx_cmd,
            java_cmd=args.java_cmd,
            launch_mode=args.launch_mode,
            generate_code_dir=args.generate_code_dir,
            expect_paths=args.expect_path,
            log_dir=args.log_dir,
            script_path=args.script_path,
            keep_script=args.keep_script,
            silent=args.silent,
            firmware=args.firmware,
            download=args.download,
            timeout=args.timeout,
        )
    except Exception as error:
        logging.error(error)
        logging.debug(tr("Traceback:", "调用栈："), exc_info=True)
        sys.exit(1)


def cmd_stm32_cmake(args: argparse.Namespace) -> None:
    """libxr stm32 cmake：把 LibXR 接入 CubeMX 生成的 CMake 工程。
    libxr stm32 cmake: integrate LibXR into a CMake project generated by CubeMX.
    """
    from libxr.generator_stm32_cmake import integrate

    integrate(args.directory)


def cmd_stm32_flash_info(args: argparse.Namespace) -> None:
    """libxr stm32 flash-info：以 YAML 打印一个 STM32 型号的 Flash 布局。
    libxr stm32 flash-info: print the flash layout of an STM32 model as YAML.
    """
    from libxr.stm32_flash_generator import print_flash_info

    print_flash_info(args.model)


def cmd_pins(args: argparse.Namespace) -> None:
    """libxr pins：打印一个型号的封装和引脚布局。
    libxr pins: print the package and pin layout of a model.
    """
    from libxr.pin_layout import print_pin_layout

    print_pin_layout(args.model, args.package, args.format, args.directory, args.libxr_config)


def cmd_stm32_toolchain(args: argparse.Namespace) -> None:
    """libxr stm32 toolchain：切换默认 preset 的工具链和 clang 的标准库。
    libxr stm32 toolchain: switch the toolchain of the default preset and the clang standard
    library.
    """
    from libxr.stm32_toolchain_switch import switch_toolchain

    switch_toolchain(args.directory, args.compiler, args.std)


def _command(group, name: str, text: str, run: Callable[[argparse.Namespace], None], **options):
    """在 group 中加入子命令 name，帮助和说明都是 text，运行 run。
    Add the subcommand name to group, with text as its help and description, running run.
    """
    parser = group.add_parser(name, help=text, description=text, **options)
    parser.set_defaults(run=run)
    return parser


def _add_xrobot_choice(parser, default_help: str) -> None:
    """加入互斥的 --xrobot 和 --no-xrobot；都不给时为 None，default_help 说明这时沿用什么。
    Add the mutually exclusive --xrobot and --no-xrobot; with neither the value is None, and
    default_help says what is kept then.
    """
    xrobot = parser.add_mutually_exclusive_group()
    xrobot.add_argument(
        "--xrobot",
        dest="xrobot",
        action="store_const",
        const=True,
        default=None,
        help=tr("generate XRobot registrations", "生成 XRobot 注册代码") + default_help,
    )
    xrobot.add_argument(
        "--no-xrobot",
        dest="xrobot",
        action="store_const",
        const=False,
        help=tr("generate LibXR code without XRobot", "生成不含 XRobot 的 LibXR 代码"),
    )


def _add_parse(commands) -> None:
    """加入 parse 子命令。
    Add the parse subcommand.
    """
    parser = _command(
        commands,
        "parse",
        tr(
            "parse a project into the project YAML that gen reads",
            "解析工程，写出 gen 读取的工程 YAML",
        ),
        cmd_parse,
    )
    parser.add_argument(
        "-d",
        "--directory",
        default=".",
        help=tr(
            "project directory: an STM32CubeMX project holds one .ioc file, an MSPM0 project "
            "one .syscfg file in its root and an HPM project an app.yaml plus a .hpmpc under "
            "boards/ (default: current directory)",
            "工程目录：STM32CubeMX 工程含有一个 .ioc 文件，MSPM0 工程的根目录含有一个 .syscfg "
            "文件，HPM 工程有 app.yaml 且 boards/ 下有一个 .hpmpc（默认：当前目录）",
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        help=tr(
            "output YAML file (default: .config.yaml in DIRECTORY)",
            "输出的 YAML 文件（默认：DIRECTORY 中的 .config.yaml）",
        ),
    )


def _add_gen(commands) -> None:
    """加入 gen 子命令。
    Add the gen subcommand.
    """
    parser = _command(
        commands,
        "gen",
        tr(
            "generate the LibXR code of a project from its project YAML",
            "由工程 YAML 生成 LibXR 代码",
        ),
        cmd_gen,
    )
    parser.add_argument(
        "-i",
        "--input",
        required=True,
        help=tr(
            "project YAML written by parse; it records the platform",
            "parse 写出的工程 YAML，其中记录了平台",
        ),
    )
    parser.add_argument(
        "-d",
        "--directory",
        default=".",
        help=tr(
            "project directory whose platform is used when the YAML records none (default: "
            "current directory)",
            "工程目录，YAML 没有记录平台时按它的平台选择生成器（默认：当前目录）",
        ),
    )
    parser.add_argument(
        "-o", "--output", required=True, help=tr("output C++ file", "输出的 C++ 文件")
    )
    _add_xrobot_choice(
        parser,
        tr(
            " (default: keep the choice of the existing output file)",
            "（默认：沿用已有输出文件的选择）",
        ),
    )
    parser.add_argument(
        "--libxr-config",
        default="",
        help=tr(
            "path or URL of libxr_config.yaml (default: libxr_config.yaml next to the output)",
            "libxr_config.yaml 的路径或 URL（默认：输出文件所在目录中的 libxr_config.yaml）",
        ),
    )


def _add_stm32_setup(commands) -> None:
    """加入 stm32 setup 子命令。
    Add the stm32 setup subcommand.
    """
    parser = _command(
        commands,
        "setup",
        tr(
            "set up an STM32CubeMX project for LibXR: add LibXR, then parse, generate and "
            "integrate CMake",
            "把 STM32CubeMX 工程配置为使用 LibXR：加入 LibXR，再解析、生成代码并接入 CMake",
        ),
        cmd_stm32_setup,
    )
    _add_project_directory(parser)
    parser.add_argument(
        "-t",
        "--terminal",
        default="",
        help=tr(
            "terminal device, such as usart1 or usb_fs_cdc; stored as terminal_source in "
            "User/libxr_config.yaml",
            "终端设备，例如 usart1、usb_fs_cdc；记录为 User/libxr_config.yaml 中的 terminal_source",
        ),
    )
    _add_xrobot_choice(
        parser,
        tr(" (default: keep the project's current choice)", "（默认：沿用工程现在的选择）"),
    )
    parser.add_argument(
        "--commit",
        default="",
        help=tr(
            "check out LibXR at this commit (default: keep the checkout; a new submodule "
            "starts at the commit this libxr release pins)",
            "把 LibXR 检出到这个提交（默认：保持现有检出；新加入的子模块检出到本版本锁定的提交）",
        ),
    )
    parser.add_argument(
        "--git-source",
        default="auto",
        help=tr(
            "where a missing LibXR is cloned from: auto, github, a base or repository URL, or a "
            "local repository (default: auto); .gitmodules always records "
            "https://github.com/xrobot-org/libxr.git",
            "缺少 LibXR 时从哪里克隆：auto、github、基础地址、仓库地址或本地仓库（默认：auto）；"
            ".gitmodules 始终记录 https://github.com/xrobot-org/libxr.git",
        ),
    )
    parser.add_argument(
        "--git-mirrors",
        default="",
        help=tr(
            "comma-separated mirror base or repository URLs, tried with --git-source auto",
            "以逗号分隔的镜像基础地址或仓库地址，--git-source 为 auto 时参与选择",
        ),
    )


def _add_hpm_setup(commands) -> None:
    """加入 hpm setup 子命令。
    Add the hpm setup subcommand.
    """
    parser = _command(
        commands,
        "setup",
        tr(
            "set up an HPM project for LibXR: parse the Pinmux Tool project and generate "
            "User/app_main.cpp (there is no CubeMX step)",
            "把 HPM 工程配置为使用 LibXR：解析 Pinmux Tool 工程并生成 User/app_main.cpp"
            "（没有 CubeMX 步骤）",
        ),
        cmd_hpm_setup,
    )
    parser.add_argument(
        "-d",
        "--directory",
        default=".",
        help=tr(
            "project directory: an app.yaml in the root and one .hpmpc under boards/ "
            "(default: current directory)",
            "工程目录：根目录有 app.yaml，boards/ 下有一个 .hpmpc（默认：当前目录）",
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default="",
        help=tr(
            "output C++ file (default: User/app_main.cpp in DIRECTORY)",
            "输出的 C++ 文件（默认：DIRECTORY 中的 User/app_main.cpp）",
        ),
    )
    _add_xrobot_choice(
        parser,
        tr(
            " (default: keep the choice of the existing output file; a new file uses no XRobot)",
            "（默认：沿用已有输出文件的选择；新文件不使用 XRobot）",
        ),
    )
    parser.add_argument(
        "--libxr-config",
        default="",
        help=tr(
            "path or URL of libxr_config.yaml (default: libxr_config.yaml next to the output)",
            "libxr_config.yaml 的路径或 URL（默认：输出文件所在目录中的 libxr_config.yaml）",
        ),
    )


def _add_mspm0_setup(commands) -> None:
    """加入 mspm0 setup 子命令。
    Add the mspm0 setup subcommand.
    """
    parser = _command(
        commands,
        "setup",
        tr(
            "set up an MSPM0 project for LibXR: parse the SysConfig project and generate "
            "User/app_main.cpp (there is no CubeMX step)",
            "把 MSPM0 工程配置为使用 LibXR：解析 SysConfig 工程并生成 User/app_main.cpp"
            "（没有 CubeMX 步骤）",
        ),
        cmd_mspm0_setup,
    )
    parser.add_argument(
        "-d",
        "--directory",
        default=".",
        help=tr(
            "project directory: a .syscfg file in its root (default: current directory)",
            "工程目录：根目录有一个 .syscfg 文件（默认：当前目录）",
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default="",
        help=tr(
            "output C++ file (default: User/app_main.cpp in DIRECTORY)",
            "输出的 C++ 文件（默认：DIRECTORY 中的 User/app_main.cpp）",
        ),
    )
    _add_xrobot_choice(
        parser,
        tr(
            " (default: keep the choice of the existing output file; a new file uses no XRobot)",
            "（默认：沿用已有输出文件的选择；新文件不使用 XRobot）",
        ),
    )
    parser.add_argument(
        "--libxr-config",
        default="",
        help=tr(
            "path or URL of libxr_config.yaml (default: libxr_config.yaml next to the output)",
            "libxr_config.yaml 的路径或 URL（默认：输出文件所在目录中的 libxr_config.yaml）",
        ),
    )


def _add_stm32_cubemx_gen(commands) -> None:
    """加入 stm32 cubemx-gen 子命令。
    Add the stm32 cubemx-gen subcommand.
    """
    parser = _command(
        commands,
        "cubemx-gen",
        tr(
            "run STM32CubeMX in script mode to generate the project",
            "以脚本模式运行 STM32CubeMX 生成工程",
        ),
        cmd_stm32_cubemx_gen,
    )
    parser.add_argument(
        "-d",
        "--directory",
        default=".",
        help=tr(
            "directory holding the CubeMX .ioc file (default: current directory)",
            "含有 CubeMX .ioc 文件的目录（默认：当前目录）",
        ),
    )
    parser.add_argument(
        "--ioc",
        default="",
        help=tr(
            ".ioc file (default: the only .ioc in DIRECTORY)",
            ".ioc 文件（默认：DIRECTORY 中唯一的 .ioc 文件）",
        ),
    )
    parser.add_argument(
        "--cubemx-cmd",
        default="",
        help=tr("STM32CubeMX executable", "STM32CubeMX 可执行文件"),
    )
    parser.add_argument(
        "--java-cmd",
        default="",
        help=tr(
            "Java executable for the java launch mode",
            "java 启动方式使用的 Java 可执行文件",
        ),
    )
    parser.add_argument(
        "--launch-mode",
        choices=("auto", "direct", "java"),
        default="auto",
        help=tr(
            "how CubeMX is started (default: auto: java -jar for a .jar or an installation "
            "with its jre, else direct)",
            "CubeMX 的启动方式（默认 auto：.jar 或带 jre 的安装用 java -jar 启动，其余直接启动）",
        ),
    )
    parser.add_argument(
        "--generate-code-dir",
        default="",
        help=tr(
            "run 'generate code <dir>' instead of 'project generate'",
            "运行 'generate code <dir>'，代替 'project generate'",
        ),
    )
    parser.add_argument(
        "--expect-path",
        action="append",
        default=None,
        help=tr(
            "path that must exist after generation; may be repeated (default: Core/Inc and "
            "Drivers)",
            "生成后必须存在的路径，可重复给出（默认：Core/Inc 和 Drivers）",
        ),
    )
    parser.add_argument(
        "--log-dir",
        default="",
        help=tr(
            "directory for the command, script, stdout and stderr logs (default: none)",
            "存放命令、脚本、标准输出和标准错误日志的目录（默认：不保存）",
        ),
    )
    parser.add_argument(
        "--script-path",
        default="",
        help=tr(
            "path of the generated CubeMX script (default: a temporary file)",
            "生成的 CubeMX 脚本的路径（默认：临时文件）",
        ),
    )
    parser.add_argument(
        "--keep-script",
        action="store_true",
        help=tr(
            "keep the generated CubeMX script in the project directory",
            "在工程目录中保留生成的 CubeMX 脚本",
        ),
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help=tr("pass -s to STM32CubeMX", "向 STM32CubeMX 传入 -s"),
    )
    parser.add_argument(
        "--firmware",
        choices=("keep", "migrate"),
        default=None,
        help=tr(
            "answer when the project was saved by another STM32CubeMX version: keep its "
            "firmware package (Continue) or migrate the project (default: stop)",
            "工程由另一版本的 STM32CubeMX 保存时的回答：keep 沿用它的固件包（Continue），"
            "migrate 迁移工程（默认：停止）",
        ),
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help=tr(
            "let STM32CubeMX download a missing firmware package and accept its license "
            "(default: stop)",
            "允许 STM32CubeMX 下载缺少的固件包并接受其许可协议（默认：停止）",
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1200,
        help=tr(
            "time limit of the CubeMX process in seconds (default: 1200)",
            "CubeMX 进程的时限，单位为秒（默认：1200）",
        ),
    )


def _add_stm32_cmake(commands) -> None:
    """加入 stm32 cmake 子命令。
    Add the stm32 cmake subcommand.
    """
    parser = _command(
        commands,
        "cmake",
        tr(
            "integrate LibXR into the CMake project generated by CubeMX",
            "把 LibXR 接入 CubeMX 生成的 CMake 工程",
        ),
        cmd_stm32_cmake,
    )
    _add_project_directory(parser)


def _add_project_directory(parser) -> None:
    """加入 -d/--directory：CubeMX 工程目录，默认当前目录。
    Add -d/--directory: the CubeMX project directory, the current directory by default.
    """
    parser.add_argument(
        "-d",
        "--directory",
        default=".",
        help=tr(
            "STM32CubeMX project directory (default: current directory)",
            "STM32CubeMX 工程目录（默认：当前目录）",
        ),
    )


def _add_pins(commands) -> None:
    """加入 pins 子命令。
    Add the pins subcommand.
    """
    parser = _command(
        commands,
        "pins",
        tr(
            "print the package and pin layout of a chip model (STM32, MSPM0)",
            "打印芯片型号的封装和引脚布局（STM32、MSPM0）",
        ),
        cmd_pins,
        epilog=tr("examples:", "示例：")
        + "\n  libxr pins STM32H723VGT6\n  libxr pins MSPM0G3507SPMR"
        + "\n  libxr pins MSPM0G3507 --package LQFP-64 --format json"
        + "\n  libxr pins -d path/to/project",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "model",
        nargs="?",
        help=tr(
            "chip model (default: the one of the project given with -d)",
            "芯片型号（默认：-d 给出的工程的型号）",
        ),
    )
    parser.add_argument(
        "-d",
        "--directory",
        help=tr(
            "project directory: overlay the signals it has selected (an STM32CubeMX .ioc, or a "
            "SysConfig ti_msp_dl_config.h) and their libxr_config.yaml settings",
            "工程目录：叠加其中已选的信号（STM32CubeMX 的 .ioc 或 SysConfig 的 "
            "ti_msp_dl_config.h）和它们在 libxr_config.yaml 中的设置",
        ),
    )
    parser.add_argument(
        "-c",
        "--libxr-config",
        help=tr(
            "libxr_config.yaml to read the settings from (default: User/libxr_config.yaml in "
            "the project)",
            "读取设置的 libxr_config.yaml（默认：工程中的 User/libxr_config.yaml）",
        ),
    )
    parser.add_argument(
        "-p",
        "--package",
        help=tr(
            "package, for a model that does not name it (MSPM0: LQFP-64, PM, ...); with -d an "
            "MSPM0 takes it from the SysConfig project",
            "封装，用于型号中没有封装的情况（MSPM0：LQFP-64、PM 等）；用 -d 时 MSPM0 取自 "
            "SysConfig 工程",
        ),
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("yaml", "json"),
        default="yaml",
        help=tr("output format (default: yaml)", "输出格式（默认：yaml）"),
    )


def _add_stm32_flash_info(commands) -> None:
    """加入 stm32 flash-info 子命令。
    Add the stm32 flash-info subcommand.
    """
    parser = _command(
        commands,
        "flash-info",
        tr(
            "print the internal flash layout of an STM32 model as YAML",
            "以 YAML 打印 STM32 型号的内部 Flash 布局",
        ),
        cmd_stm32_flash_info,
        epilog=tr("examples:", "示例：")
        + "\n  libxr stm32 flash-info STM32F103C8T6\n  libxr stm32 flash-info STM32L476RG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("model", help=tr("STM32 model", "STM32 型号"))


def _add_stm32_toolchain(commands) -> None:
    """加入 stm32 toolchain 子命令。
    Add the stm32 toolchain subcommand.
    """
    parser = _command(
        commands,
        "toolchain",
        tr(
            "switch the toolchain of the default preset and the clang standard library",
            "切换默认 preset 的工具链和 clang 的标准库",
        ),
        cmd_stm32_toolchain,
        epilog=tr(
            "A changed toolchain removes build/ and cmake-build*: CMake keeps an existing build "
            "directory on its old compiler.\n\n",
            "工具链改变时删除 build/ 和 cmake-build*：CMake 不会更换已有构建目录的编译器。\n\n",
        )
        + tr("examples:", "示例：")
        + "\n  libxr stm32 toolchain gcc\n  libxr stm32 toolchain clang -g"
        + "\n  libxr stm32 toolchain clang --newlib\n  libxr stm32 toolchain clang --picolibc"
        + "\n  libxr stm32 toolchain clang"
        + tr(
            "    (keeps the current standard library)",
            "    （沿用现在的标准库）",
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_project_directory(parser)
    parser.add_argument(
        "compiler",
        choices=["gcc", "clang"],
        help=tr("compiler", "编译器"),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-g",
        "--gnu",
        "--hybrid",
        dest="std",
        action="store_const",
        const="hybrid",
        help=tr("use the GNU (hybrid) standard library", "使用 GNU（hybrid）标准库"),
    )
    group.add_argument(
        "-n",
        "--newlib",
        dest="std",
        action="store_const",
        const="newlib",
        help=tr("use the newlib standard library", "使用 newlib 标准库"),
    )
    group.add_argument(
        "-p",
        "--picolibc",
        dest="std",
        action="store_const",
        const="picolibc",
        help=tr("use the picolibc standard library", "使用 picolibc 标准库"),
    )


def build_parser() -> argparse.ArgumentParser:
    """libxr 命令的参数解析器。
    The argument parser of the libxr command.
    """
    localize_argparse()
    parser = argparse.ArgumentParser(
        prog="libxr",
        description=tr(
            "Generate LibXR-based C++ code for projects of different platforms.",
            "为不同平台的工程生成基于 LibXR 的 C++ 代码。",
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"libxr {update_notice.installed_version() or tr('unknown', '未知')}",
        help=tr("show the version and exit", "显示版本号并退出"),
    )
    commands = parser.add_subparsers(metavar="<command>", required=True)
    _add_parse(commands)
    _add_gen(commands)
    _add_pins(commands)
    stm32 = commands.add_parser(
        "stm32",
        help=tr("commands for STM32CubeMX projects", "STM32CubeMX 工程的命令"),
        description=tr("Commands for STM32CubeMX projects.", "STM32CubeMX 工程的命令。"),
    )
    stm32_commands = stm32.add_subparsers(metavar="<command>", required=True)
    _add_stm32_setup(stm32_commands)
    _add_stm32_cubemx_gen(stm32_commands)
    _add_stm32_cmake(stm32_commands)
    _add_stm32_flash_info(stm32_commands)
    _add_stm32_toolchain(stm32_commands)
    hpm = commands.add_parser(
        "hpm",
        help=tr("commands for HPM Pinmux Tool projects", "HPM Pinmux Tool 工程的命令"),
        description=tr("Commands for HPM Pinmux Tool projects.", "HPM Pinmux Tool 工程的命令。"),
    )
    hpm_commands = hpm.add_subparsers(metavar="<command>", required=True)
    _add_hpm_setup(hpm_commands)
    mspm0 = commands.add_parser(
        "mspm0",
        help=tr("commands for TI SysConfig projects", "TI SysConfig 工程的命令"),
        description=tr("Commands for TI SysConfig projects.", "TI SysConfig 工程的命令。"),
    )
    mspm0_commands = mspm0.add_subparsers(metavar="<command>", required=True)
    _add_mspm0_setup(mspm0_commands)
    # 每个子命令最后都有 --verbose。
    # Every subcommand ends with --verbose.
    for group in (commands, stm32_commands, hpm_commands, mspm0_commands):
        for name, command in group.choices.items():
            if name not in ("stm32", "hpm", "mspm0"):
                command.add_argument(
                    "--verbose",
                    action="store_true",
                    help=tr("enable debug logging", "输出调试日志"),
                )
    return parser


def run_command(run: Callable[[], None]) -> None:
    """运行一个命令 run；运行期间在后台检查新版本，结束时（失败也一样）提示。
    Run a command, run; a new version is checked in the background meanwhile and reported at
    the end, after a failure too.
    """
    report = update_notice.start()
    try:
        run()
    finally:
        report()


def main(argv: Sequence[str] | None = None) -> int:
    """libxr 命令入口：解析参数并运行子命令；--verbose 时输出调试日志。运行期间在后台检查新版本，
    结束时提示。
    Entry of the libxr command: parse the arguments and run the subcommand, with debug logging
    under --verbose; a new version is checked in the background meanwhile and reported at the
    end.
    """
    configure_output()
    args = build_parser().parse_args(argv)
    if args.verbose:
        configure_output(logging.DEBUG)
    run_command(lambda: args.run(args))
    return 0
