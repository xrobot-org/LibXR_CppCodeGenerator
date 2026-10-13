"""libxr stm32 cmake 写入的 CMake 集成（libxr.generator_stm32_cmake）：固定结构的 LibXR.CMake、
旧格式的迁移，以及 ST Arm Clang 工具链文件的恢复。
The CMake integration written by libxr stm32 cmake (libxr.generator_stm32_cmake): LibXR.CMake
in its fixed structure, the migration of earlier formats, and the restoring of the ST Arm Clang
toolchain file.
"""

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixtures import CUBEMX_STARM, GeneratorTestCase, TestCase
from xr_syntax.cmake import CMakeDocument

from libxr import generator_stm32_cmake as stm32_cmake

# DevC 在迁移之前的 LibXR.CMake：旧模板加上 LIBXR_SOURCE_DIR 和两段编译器警告设置。
# The LibXR.CMake of DevC before the migration: the earlier template plus LIBXR_SOURCE_DIR and
# two compiler warning settings.
DEVC_LIBXR_CMAKE = """\
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# LibXR
set(LIBXR_SYSTEM FreeRTOS)
set(LIBXR_DRIVER st)
set(XROBOT_MODULES_DIR ${CMAKE_CURRENT_SOURCE_DIR}/Modules)
set(LIBXR_SOURCE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Middlewares/Third_Party/LibXR"
    CACHE PATH "Existing LibXR source checkout")
add_subdirectory("${LIBXR_SOURCE_DIR}" "${CMAKE_CURRENT_BINARY_DIR}/LibXR")
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU")
    target_compile_options(xr PRIVATE -Wno-tautological-compare -Wno-maybe-uninitialized)
endif()
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    target_compile_options(xr PRIVATE -Wno-deprecated-volatile)
endif()
target_link_libraries(xr
    PUBLIC stm32cubemx
)


target_compile_features(xr PUBLIC cxx_std_20)
target_include_directories(xr
    PUBLIC $<TARGET_PROPERTY:stm32cubemx,INTERFACE_INCLUDE_DIRECTORIES>
    PUBLIC Core/Inc
    PUBLIC User
)

# Add include paths
set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES
    CXX_STANDARD 20
    CXX_STANDARD_REQUIRED ON
)

target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user defined include paths
    PUBLIC $<TARGET_PROPERTY:xr,INTERFACE_INCLUDE_DIRECTORIES>
    PUBLIC User
)

# Add linked libraries
target_link_libraries(${CMAKE_PROJECT_NAME}
    stm32cubemx

    # Add user defined libraries
    xr
)

file(
    GLOB LIBXR_USER_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/User/*.cpp")


target_sources(${CMAKE_PROJECT_NAME}
    PRIVATE ${LIBXR_USER_SOURCES}
)

if(CMAKE_BUILD_TYPE STREQUAL "Debug")
    target_compile_options(${CMAKE_PROJECT_NAME} PRIVATE -Og)
    target_compile_options(xr PRIVATE -O2)
    if(TARGET FreeRTOS)
        target_compile_options(FreeRTOS PRIVATE -O2)
    endif()

    if(TARGET STM32_Drivers)
        target_compile_options(STM32_Drivers PRIVATE -O2)
    endif()

    if(TARGET USB_Device_Library)
        target_compile_options(USB_Device_Library PRIVATE -O2)
    endif()
endif()
"""

# OpenCR 的 LibXR.CMake：按镜像选择系统，没有 User 源文件的 GLOB，也没有优化选项。
# The LibXR.CMake of OpenCR: the system chosen by image, no glob of the User sources, and no
# optimization options.
OPENCR_LIBXR_CMAKE = """\
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(OPENCR_IMAGE STREQUAL "bootloader")
    set(LIBXR_SYSTEM FreeRTOS)
else()
    set(LIBXR_SYSTEM None)
endif()
set(LIBXR_DRIVER st)
set(XROBOT_MODULES_DIR ${CMAKE_CURRENT_SOURCE_DIR}/Modules)

add_subdirectory(Middlewares/Third_Party/LibXR)

target_compile_features(xr PUBLIC cxx_std_20)
target_link_libraries(xr PUBLIC stm32cubemx)

target_link_libraries(${CMAKE_PROJECT_NAME} stm32cubemx xr)
"""

# 新工程的 “Project settings” 块中 LIBXR_SYSTEM 和 LIBXR_DRIVER 之后的行。
# The lines of the "Project settings" block of a new project after LIBXR_SYSTEM and
# LIBXR_DRIVER.
SOURCES_AND_LEVELS = [
    '# User sources of the application; "" when CMakeLists.txt adds them itself.',
    'set(LIBXR_USER_SOURCES_GLOB "${CMAKE_CURRENT_SOURCE_DIR}/User/*.cpp")',
    "# Optimization level of the application in Debug builds and of everything in Release builds;",
    '# "" keeps the level of the toolchain file.',
    'set(LIBXR_OPT_DEBUG "-Og")',
    'set(LIBXR_OPT_RELEASE "")',
]

# CubeMX 的工具链文件中 CMAKE_<LANG>_FLAGS_<配置> 的设置。
# The CMAKE_<LANG>_FLAGS_<configuration> settings of a CubeMX toolchain file.
GCC_TOOLCHAIN = """\
set(CMAKE_C_FLAGS_DEBUG "-Og -g3")
set(CMAKE_C_FLAGS_RELEASE "-O3 -g0")
set(CMAKE_CXX_FLAGS_DEBUG "-Og -g3")
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -g0")
"""
CLANG_TOOLCHAIN = GCC_TOOLCHAIN.replace("-O3", "-Oz")

# 旧版本加进 starm-clang.cmake 的内容：默认行之后的选择块、multilib 块之前的初始化和块中的检查。
# What earlier versions added to starm-clang.cmake: the selection block after the default line,
# the initialization before the multilib block and the check inside the block.
EARLIER_STARM = CUBEMX_STARM.replace(
    'set(STARM_TOOLCHAIN_CONFIG "STARM_PICOLIBC")\n',
    'set(STARM_TOOLCHAIN_CONFIG "STARM_PICOLIBC")\n'
    "# LibXR: -DSTARM_TOOLCHAIN_CONFIG=<profile> selects the profile of a build\n"
    "# directory; a default changed by libxr stm32 toolchain also reaches\n"
    "# existing build directories.\n"
    "set(_xr_starm_default ${STARM_TOOLCHAIN_CONFIG})\n"
    "unset(STARM_TOOLCHAIN_CONFIG)\n"
    "if(NOT DEFINED CACHE{STARM_TOOLCHAIN_CONFIG} OR\n"
    "   (DEFINED CACHE{XR_STARM_TOOLCHAIN_DEFAULT} AND\n"
    "    NOT XR_STARM_TOOLCHAIN_DEFAULT STREQUAL _xr_starm_default))\n"
    "  set(STARM_TOOLCHAIN_CONFIG ${_xr_starm_default} CACHE STRING "
    '"ST Arm Clang runtime profile" FORCE)\n'
    "endif()\n"
    "set(XR_STARM_TOOLCHAIN_DEFAULT ${_xr_starm_default} CACHE INTERNAL "
    '"Default STARM_TOOLCHAIN_CONFIG of this file")\n'
    "set_property(CACHE STARM_TOOLCHAIN_CONFIG PROPERTY STRINGS STARM_HYBRID STARM_NEWLIB "
    "STARM_PICOLIBC)\n"
    '\nset(TOOLCHAIN_MULTILIBS "")\n',
    1,
).replace(
    '--config=newlib.cfg")\nendif()\n',
    '--config=newlib.cfg")\n'
    'elseif(NOT STARM_TOOLCHAIN_CONFIG STREQUAL "STARM_PICOLIBC")\n'
    '  message(FATAL_ERROR "Unknown STARM_TOOLCHAIN_CONFIG: ${STARM_TOOLCHAIN_CONFIG}")\n'
    "endif()\n",
    1,
)


class Integration(GeneratorTestCase):
    """把 LibXR 接入工程：固定结构的 LibXR.CMake（XRobot 工程另设 XROBOT_MODULES_DIR）、
    CMakeLists.txt 的 include 和 starm-clang.cmake 的恢复。
    Integrating LibXR into a project: LibXR.CMake in its fixed structure (XRobot projects also
    set XROBOT_MODULES_DIR), the include in CMakeLists.txt, and the restoring of
    starm-clang.cmake.
    """

    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "User").mkdir()
        (self.root / "cmake").mkdir()
        (self.root / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
        self.libxr_cmake = self.root / "cmake" / "LibXR.CMake"

    def write_app_main(self, use_xrobot):
        """生成并写入 User/app_main.cpp，返回其文本。
        Generate and write User/app_main.cpp; return its text.
        """
        code = self.generate(use_xrobot=use_xrobot)
        (self.root / "User" / "app_main.cpp").write_text(code, encoding="utf-8")
        return code

    def run_cmake_generator(self):
        """把 LibXR 接入工程，返回 cmake/LibXR.CMake 的文本。
        Integrate LibXR into the project and return the text of cmake/LibXR.CMake.
        """
        stm32_cmake.integrate(str(self.root))
        return self.libxr_cmake.read_text(encoding="utf-8")

    def settings(self, text):
        """LibXR.CMake 中 “Project settings” 块的内容。
        The content of the "Project settings" block of LibXR.CMake.
        """
        return stm32_cmake.split_blocks(text)[stm32_cmake.SETTINGS_TITLE]

    def test_a_new_file_has_the_fixed_structure(self):
        text = self.run_cmake_generator()
        self.assertEqual(
            text.splitlines()[:3],
            [
                stm32_cmake.GENERATED_LINE,
                "",
                "# ---- Project settings " + "-" * 56,
            ],
        )
        self.assertEqual(
            list(stm32_cmake.split_blocks(text)),
            [
                "Project settings",
                "Compile options",
                "LibXR",
                "Application",
                "Library optimization",
                "Firmware images",
                "Application settings",
            ],
        )
        self.assertEqual(
            self.settings(text).splitlines(),
            [
                "set(LIBXR_SYSTEM None)",
                "set(LIBXR_DRIVER st)",
                *SOURCES_AND_LEVELS,
            ],
        )
        self.assertTrue(all(len(line) <= 100 for line in text.splitlines()))

    def test_the_optimization_options_follow_the_toolchain_flags(self):
        # 目标的编译选项排在 CMAKE_<LANG>_FLAGS_<CONFIG> 之后，最后一个 -O 决定优化级别。
        # Target compile options come after CMAKE_<LANG>_FLAGS_<CONFIG>, and the last -O decides.
        text = self.run_cmake_generator()
        self.assertIn(
            "target_compile_options(${CMAKE_PROJECT_NAME} PRIVATE\n"
            "    $<$<CONFIG:Debug>:${LIBXR_OPT_DEBUG}> $<$<CONFIG:Release>:${LIBXR_OPT_RELEASE}>)",
            text,
        )
        self.assertIn(
            "    target_compile_options(${library} PRIVATE\n"
            "        $<$<CONFIG:Debug>:-O2> $<$<CONFIG:Release>:${LIBXR_OPT_RELEASE}>)",
            text,
        )
        self.assertIn("BUILDSYSTEM_TARGETS", text)

    def test_the_hex_and_bin_images_are_made_next_to_the_elf(self):
        # 共享的 BSP CI 打包 <工程名>.elf、.hex 和 .bin；以前 CubeMX 工程只有 ELF，官方 BSP 在
        # CMakeLists.txt 中手写 objcopy。
        # The shared BSP CI packages <project>.elf, .hex and .bin; CubeMX projects used to have
        # only the ELF, and the official BSPs added objcopy to CMakeLists.txt by hand.
        text = self.run_cmake_generator()
        self.assertEqual(
            stm32_cmake.split_blocks(text)[stm32_cmake.FIRMWARE_TITLE],
            "# Each link of the application also writes <project>.hex and <project>.bin next "
            "to the ELF.\n"
            "add_custom_command(TARGET ${CMAKE_PROJECT_NAME} POST_BUILD\n"
            "    WORKING_DIRECTORY $<TARGET_FILE_DIR:${CMAKE_PROJECT_NAME}>\n"
            "    COMMAND ${CMAKE_OBJCOPY} --output-target ihex\n"
            "        $<TARGET_FILE_NAME:${CMAKE_PROJECT_NAME}> ${CMAKE_PROJECT_NAME}.hex\n"
            "    COMMAND ${CMAKE_OBJCOPY} --output-target binary --strip-all\n"
            "        $<TARGET_FILE_NAME:${CMAKE_PROJECT_NAME}> ${CMAKE_PROJECT_NAME}.bin\n"
            "    VERBATIM)",
        )

    def test_an_existing_file_gains_the_firmware_images(self):
        # 由没有固件镜像块的版本写出的文件：设置块、Kept 块和两个 User Code 区域保留，固件镜像块
        # 排在 Kept 块之前，应用设置区域排在最后。
        # A file written by a version without the firmware image block: the settings block, the
        # Kept block and the two User Code regions stay, the firmware image block comes before the
        # Kept block, and the application settings region comes last.
        text = self.run_cmake_generator()
        blocks = stm32_cmake.split_blocks(text)
        settings = blocks[stm32_cmake.SETTINGS_TITLE].replace('"-Og"', '"-O1"')
        kept = "target_compile_options(xr PRIVATE -Wno-deprecated-volatile)"
        earlier = text[: text.index(stm32_cmake._rule(stm32_cmake.FIRMWARE_TITLE))].rstrip("\n")
        earlier = earlier.replace(blocks[stm32_cmake.SETTINGS_TITLE], settings)
        earlier += "\n\n" + stm32_cmake._rule(stm32_cmake.KEPT_TITLE) + "\n" + kept + "\n"
        self.libxr_cmake.write_text(earlier, encoding="utf-8")
        with self.assertLogs(level="INFO") as logs:
            rewritten = self.run_cmake_generator()
        self.assertEqual(rewritten, stm32_cmake.render_libxr_cmake(settings, kept))
        self.assertEqual(
            list(stm32_cmake.split_blocks(rewritten))[-3:],
            [
                stm32_cmake.FIRMWARE_TITLE,
                stm32_cmake.KEPT_TITLE,
                stm32_cmake.APPLICATION_SETTINGS_TITLE,
            ],
        )
        self.assertIn("INFO:root:Updated existing LibXR.CMake for system: bare metal", logs.output)

    def test_the_modules_directory_follows_app_main(self):
        # 还没有生成 app_main 的工程按纯 LibXR 工程处理。
        # A project without a generated app_main counts as a LibXR-only project.
        line = 'set(XROBOT_MODULES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Modules")'
        for use_xrobot in (True, False, None):
            with self.subTest(use_xrobot=use_xrobot):
                self.setUp()
                if use_xrobot is not None:
                    self.write_app_main(use_xrobot)
                text = self.run_cmake_generator()
                self.assertEqual(line in self.settings(text), use_xrobot is True)
                self.assertNotIn("XROBOT_MODULES_DIR", text.replace(self.settings(text), ""))

    def test_include_inside_user_code_is_not_the_xrobot_choice(self):
        code = self.write_app_main(False).replace(
            "/* User Code Begin 1 */", '/* User Code Begin 1 */\n#include "xrobot_main.hpp"', 1
        )
        (self.root / "User" / "app_main.cpp").write_text(code, encoding="utf-8")
        self.assertFalse(stm32_cmake.project_uses_xrobot(str(self.root)))

    def test_the_system_follows_the_cubemx_project(self):
        for header, system in (("FreeRTOSConfig.h", "FreeRTOS"), ("app_threadx.h", "ThreadX")):
            with self.subTest(header=header):
                self.setUp()
                (self.root / "Core" / "Inc").mkdir(parents=True)
                (self.root / "Core" / "Inc" / header).write_text("", encoding="utf-8")
                self.assertIn(f"set(LIBXR_SYSTEM {system})", self.run_cmake_generator())

    def test_the_settings_block_keeps_what_the_user_edited(self):
        text = self.run_cmake_generator()
        edited = (
            text.replace("set(LIBXR_DRIVER st)", "set(LIBXR_DRIVER st)  # my driver")
            .replace('"-Og"', '"-O1"')
            .replace('set(LIBXR_OPT_RELEASE "")', 'set(LIBXR_OPT_RELEASE "-O2")')
            .replace("set(LIBXR_SYSTEM None)", "set(LIBXR_SYSTEM None)\nset(MY_FLAG ON)")
            # 其他块中的改动在重写时恢复。
            # Edits in the other blocks are undone on a rewrite.
            .replace("target_compile_features(xr PUBLIC cxx_std_20)", "# edited")
        )
        self.libxr_cmake.write_text(edited, encoding="utf-8")
        (self.root / "Core" / "Inc").mkdir(parents=True)
        (self.root / "Core" / "Inc" / "FreeRTOSConfig.h").write_text("", encoding="utf-8")
        rewritten = self.run_cmake_generator()
        self.assertEqual(
            self.settings(rewritten).splitlines()[:8],
            [
                "set(LIBXR_SYSTEM FreeRTOS)",
                "set(MY_FLAG ON)",
                "set(LIBXR_DRIVER st)  # my driver",
                *SOURCES_AND_LEVELS[:4],
                SOURCES_AND_LEVELS[4].replace('"-Og"', '"-O1"'),
            ],
        )
        self.assertIn('set(LIBXR_OPT_RELEASE "-O2")', rewritten)
        self.assertIn("target_compile_features(xr PUBLIC cxx_std_20)", rewritten)
        self.assertNotIn("# edited", rewritten)

    def test_an_unchanged_file_is_not_written(self):
        self.run_cmake_generator()
        stamp = self.libxr_cmake.stat().st_mtime_ns
        with self.assertLogs(level="INFO") as logs:
            logging.info("marker")
            self.run_cmake_generator()
        self.assertEqual(self.libxr_cmake.stat().st_mtime_ns, stamp)
        self.assertIn("INFO:root:LibXR.CMake already up to date, no changes needed.", logs.output)

    def test_missing_settings_are_added(self):
        text = self.run_cmake_generator()
        self.libxr_cmake.write_text(
            text.replace('set(LIBXR_OPT_DEBUG "-Og")\n', "").replace("set(LIBXR_DRIVER st)\n", ""),
            encoding="utf-8",
        )
        rewritten = self.run_cmake_generator()
        self.assertIn("set(LIBXR_DRIVER st)\n", rewritten)
        self.assertIn('set(LIBXR_OPT_DEBUG "-Og")\n', rewritten)

    def test_an_existing_file_follows_the_xrobot_choice(self):
        # 以前只给出警告，切换到 XRobot 后要手动加这一行，否则模块不参与构建。
        # Only a warning used to be given; after switching to XRobot the line had to be added
        # by hand, or the Modules were not built.
        line = 'set(XROBOT_MODULES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Modules")'
        self.run_cmake_generator()
        for use_xrobot, before, after, messages in (
            (
                False,
                True,
                False,
                [
                    "INFO:root:LibXR.CMake: removed XROBOT_MODULES_DIR, as User/app_main.cpp "
                    "does not use XRobot"
                ],
            ),
            (False, False, False, []),
            (
                True,
                False,
                True,
                [f"INFO:root:LibXR.CMake: added {line}, as User/app_main.cpp uses XRobot"],
            ),
        ):
            with self.subTest(use_xrobot=use_xrobot, before=before):
                self.setUp()
                self.write_app_main(use_xrobot)
                self.run_cmake_generator()
                text = self.libxr_cmake.read_text(encoding="utf-8")
                settings = self.settings(text)
                if use_xrobot:
                    settings = settings.replace(line + "\n", "")
                elif before:
                    settings = settings.replace(
                        "set(LIBXR_DRIVER st)\n", f"set(LIBXR_DRIVER st)\n{line}\n"
                    )
                self.libxr_cmake.write_text(
                    text.replace(self.settings(text), settings), encoding="utf-8"
                )
                with self.assertLogs(level="INFO") as logs:
                    logging.info("marker")
                    rewritten = self.run_cmake_generator()
                self.assertEqual(line in self.settings(rewritten), after)
                self.assertEqual(
                    [entry for entry in logs.output if "XROBOT_MODULES_DIR" in entry], messages
                )

    def test_the_update_notice_names_bare_metal_in_words(self):
        # 以前中文输出为“已按系统 None 更新现有的 LibXR.CMake”。
        # The Chinese output used to read "已按系统 None 更新现有的 LibXR.CMake".
        text = self.run_cmake_generator()
        for language, message in (
            ("en", "INFO:root:Updated existing LibXR.CMake for system: bare metal"),
            ("zh", "INFO:root:已更新现有的 LibXR.CMake，系统：裸机"),
        ):
            with self.subTest(language=language), mock.patch.dict(os.environ, XR_LANG=language):
                edited = text.replace("target_compile_features(xr PUBLIC cxx_std_20)", "# edited")
                self.libxr_cmake.write_text(edited, encoding="utf-8")
                with self.assertLogs(level="INFO") as logs:
                    self.run_cmake_generator()
                self.assertIn(message, logs.output)

    def test_build_directories_are_kept(self):
        (self.root / "build" / "debug").mkdir(parents=True)
        self.run_cmake_generator()
        self.assertTrue((self.root / "build" / "debug").is_dir())

    def test_new_user_sources_are_globbed_at_build_time(self):
        text = self.run_cmake_generator()
        self.assertIn(
            'file(GLOB LIBXR_USER_SOURCES CONFIGURE_DEPENDS "${LIBXR_USER_SOURCES_GLOB}")', text
        )

    def test_an_include_in_any_spelling_is_not_appended_again(self):
        # 以前只比较整行文字，下面前两种写法会再追加一行 include，CMake 配置失败。
        # Comparing whole lines used to append a second include for the first two, which fails
        # the CMake configure.
        cmakelists = self.root / "CMakeLists.txt"
        for text, appended in (
            ("project(demo)\ninclude(${CMAKE_CURRENT_LIST_DIR}/cmake/LibXR.CMake)", False),
            ("project(demo)\nINCLUDE( cmake/LibXR.CMake )\n", False),
            ("project(demo)\n# include(${CMAKE_CURRENT_LIST_DIR}/cmake/LibXR.CMake)\n", True),
        ):
            with self.subTest(text=text):
                cmakelists.write_text(text, encoding="utf-8")
                self.run_cmake_generator()
                expected = text + (
                    "\n# Add LibXR\n" + stm32_cmake.include_cmake_cmd if appended else ""
                )
                self.assertEqual(cmakelists.read_text(encoding="utf-8"), expected)

    def test_the_st_arm_clang_toolchain_is_restored(self):
        toolchain = self.root / "cmake" / "starm-clang.cmake"
        toolchain.write_text(EARLIER_STARM, encoding="utf-8")
        self.run_cmake_generator()
        self.assertEqual(toolchain.read_text(encoding="utf-8"), CUBEMX_STARM)

    def test_a_directory_without_cmakelists_is_left_untouched(self):
        (self.root / "CMakeLists.txt").unlink()
        (self.root / "cmake").rmdir()
        (self.root / "build").mkdir()
        with self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit) as exit:
            self.run_cmake_generator()
        self.assertEqual(exit.exception.code, 1)
        self.assertEqual(logs.output, [f"ERROR:root:{self.root / 'CMakeLists.txt'} not found."])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["User", "build"])


class Migration(GeneratorTestCase):
    """旧格式的 LibXR.CMake 迁移到固定结构，设置的值和用到的优化级别保留。
    A LibXR.CMake in an earlier format migrates to the fixed structure, keeping the values of its
    settings and the optimization levels it uses.
    """

    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "User").mkdir()
        (self.root / "cmake").mkdir()
        (self.root / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
        (self.root / "Core" / "Inc").mkdir(parents=True)
        (self.root / "Core" / "Inc" / "FreeRTOSConfig.h").write_text("", encoding="utf-8")
        self.libxr_cmake = self.root / "cmake" / "LibXR.CMake"
        code = self.generate(use_xrobot=True)
        (self.root / "User" / "app_main.cpp").write_text(code, encoding="utf-8")

    def migrate(self, old, gcc="", clang=""):
        """用旧文件 old 和给定的工具链文件迁移，返回新的 LibXR.CMake，并记下日志。
        Migrate the old file old with the given toolchain files and return the new LibXR.CMake,
        keeping the log.
        """
        self.libxr_cmake.write_text(old, encoding="utf-8")
        for name, text in (("gcc-arm-none-eabi.cmake", gcc), ("starm-clang.cmake", clang)):
            if text:
                (self.root / "cmake" / name).write_text(text, encoding="utf-8")
        with self.assertLogs(level="INFO") as logs:
            logging.info("marker")
            stm32_cmake.integrate(str(self.root))
        self.log = [entry.removeprefix("INFO:root:") for entry in logs.output[1:]]
        return self.libxr_cmake.read_text(encoding="utf-8")

    def test_devc_keeps_its_settings_and_warning_options(self):
        text = self.migrate(DEVC_LIBXR_CMAKE, GCC_TOOLCHAIN, CLANG_TOOLCHAIN)
        blocks = stm32_cmake.split_blocks(text)
        self.assertEqual(
            blocks["Project settings"].splitlines(),
            [
                "set(LIBXR_SYSTEM FreeRTOS)",
                "set(LIBXR_DRIVER st)",
                'set(XROBOT_MODULES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Modules")',
                *SOURCES_AND_LEVELS,
            ],
        )
        self.assertEqual(
            blocks["Kept from the earlier LibXR.CMake"],
            'if(CMAKE_CXX_COMPILER_ID MATCHES "GNU")\n'
            "    target_compile_options(xr PRIVATE -Wno-tautological-compare "
            "-Wno-maybe-uninitialized)\n"
            "endif()\n\n"
            'if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")\n'
            "    target_compile_options(xr PRIVATE -Wno-deprecated-volatile)\n"
            "endif()",
        )
        # 其余块与新文件相同。
        # The other blocks equal those of a new file.
        for title in ("LibXR", "Application", "Library optimization", "Firmware images"):
            self.assertEqual(
                blocks[title], stm32_cmake.split_blocks(stm32_cmake.render_libxr_cmake(""))[title]
            )
        self.assertIn(
            'LibXR.CMake: kept if(CMAKE_CXX_COMPILER_ID MATCHES "GNU") of the earlier file in '
            "the Kept block",
            self.log,
        )

    def test_different_release_levels_of_the_toolchain_files_stay_with_each_toolchain(self):
        text = self.migrate(DEVC_LIBXR_CMAKE, GCC_TOOLCHAIN, CLANG_TOOLCHAIN)
        self.assertIn('set(LIBXR_OPT_RELEASE "")', text)
        self.assertIn(
            "The Release levels of the toolchain files differ (gcc-arm-none-eabi.cmake -O3, "
            "starm-clang.cmake -Oz); LIBXR_OPT_RELEASE stays empty and each toolchain keeps its "
            "own level",
            self.log,
        )
        text = self.migrate(DEVC_LIBXR_CMAKE, GCC_TOOLCHAIN, GCC_TOOLCHAIN)
        self.assertIn('set(LIBXR_OPT_RELEASE "-O3")', text)
        self.assertFalse([entry for entry in self.log if "differ" in entry])

    def test_levels_come_from_the_toolchain_files_when_the_old_file_sets_none(self):
        old = OPENCR_LIBXR_CMAKE
        text = self.migrate(
            old,
            GCC_TOOLCHAIN.replace("-Og", "-O0").replace("-O3", "-Os"),
        )
        settings = stm32_cmake.split_blocks(text)["Project settings"]
        self.assertIn('set(LIBXR_OPT_DEBUG "-O0")', settings)
        self.assertIn('set(LIBXR_OPT_RELEASE "-Os")', settings)

    def test_cubemx_toolchain_files_keep_their_own_levels(self):
        # CubeMX 写出的 gcc 为 -O0 / -Os，ST Arm Clang 为 -Og / -Oz。
        # CubeMX writes -O0 / -Os for gcc and -Og / -Oz for ST Arm Clang.
        text = self.migrate(
            OPENCR_LIBXR_CMAKE,
            GCC_TOOLCHAIN.replace("-Og", "-O0").replace("-O3", "-Os"),
            CLANG_TOOLCHAIN,
        )
        settings = stm32_cmake.split_blocks(text)["Project settings"]
        self.assertIn('set(LIBXR_OPT_DEBUG "")', settings)
        self.assertIn('set(LIBXR_OPT_RELEASE "")', settings)
        self.assertIn(
            "The Debug levels of the toolchain files differ (gcc-arm-none-eabi.cmake -O0, "
            "starm-clang.cmake -Og); LIBXR_OPT_DEBUG stays empty and each toolchain keeps its "
            "own level",
            self.log,
        )

    def test_the_old_application_level_wins_over_the_toolchain_debug_level(self):
        text = self.migrate(
            DEVC_LIBXR_CMAKE.replace("PRIVATE -Og", "PRIVATE -O1"),
            GCC_TOOLCHAIN.replace("-Og", "-O0"),
        )
        self.assertIn('set(LIBXR_OPT_DEBUG "-O1")', text)

    def test_the_levels_stay_empty_without_a_level_to_take(self):
        text = self.migrate(
            DEVC_LIBXR_CMAKE.replace(
                "target_compile_options(${CMAKE_PROJECT_NAME} PRIVATE -Og)", ""
            )
        )
        self.assertIn('set(LIBXR_OPT_DEBUG "")', text)
        self.assertIn('set(LIBXR_OPT_RELEASE "")', text)

    def test_a_file_without_a_glob_keeps_adding_the_sources_in_cmakelists(self):
        text = self.migrate(OPENCR_LIBXR_CMAKE)
        settings = stm32_cmake.split_blocks(text)["Project settings"]
        self.assertIn('set(LIBXR_USER_SOURCES_GLOB "")', settings)
        self.assertIn('set(XROBOT_MODULES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Modules")', settings)

    def test_statements_before_libxr_stay_in_the_settings_and_the_rest_is_kept_after(self):
        text = self.migrate(OPENCR_LIBXR_CMAKE)
        blocks = stm32_cmake.split_blocks(text)
        self.assertTrue(
            blocks["Project settings"].endswith(
                'if(OPENCR_IMAGE STREQUAL "bootloader")\n'
                "    set(LIBXR_SYSTEM FreeRTOS)\n"
                "else()\n"
                "    set(LIBXR_SYSTEM FreeRTOS)\n"
                "endif()"
            )
        )
        self.assertNotIn("Kept from the earlier LibXR.CMake", blocks)

    def test_a_custom_user_glob_and_extra_libraries_are_kept(self):
        old = DEVC_LIBXR_CMAKE.replace("User/*.cpp", "Src/*.cpp").replace(
            "    stm32cubemx\n\n    # Add user defined libraries\n    xr\n",
            "    stm32cubemx\n\n    # Add user defined libraries\n    xr\n    my_lib\n",
        )
        text = self.migrate(old, GCC_TOOLCHAIN)
        blocks = stm32_cmake.split_blocks(text)
        self.assertIn(
            'set(LIBXR_USER_SOURCES_GLOB "${CMAKE_CURRENT_SOURCE_DIR}/Src/*.cpp")',
            blocks["Project settings"],
        )
        self.assertIn(
            "target_link_libraries(${CMAKE_PROJECT_NAME}\n    stm32cubemx\n\n"
            "    # Add user defined libraries\n    xr\n    my_lib\n)",
            blocks["Kept from the earlier LibXR.CMake"],
        )

    def test_a_recursive_or_multi_pattern_glob_is_kept_with_its_target_sources(self):
        # 以前 GLOB_RECURSE 的 file() 留在 Kept 块中而 target_sources 被删掉，多个模式只取最后
        # 一个，User 源文件不再编译，工程链接失败（undefined reference to app_main）。
        # A GLOB_RECURSE file() used to stay in the Kept block while its target_sources was
        # dropped, and of several patterns only the last was taken, so the User sources were no
        # longer compiled and the project failed to link (undefined reference to app_main).
        glob = (
            "file(\n    GLOB LIBXR_USER_SOURCES CONFIGURE_DEPENDS "
            '"${CMAKE_CURRENT_SOURCE_DIR}/User/*.cpp")'
        )
        target = "target_sources(${CMAKE_PROJECT_NAME}\n    PRIVATE ${LIBXR_USER_SOURCES}\n)"
        for kept in (
            glob.replace("GLOB ", "GLOB_RECURSE "),
            glob.replace('*.cpp")', '*.cpp" "${CMAKE_CURRENT_SOURCE_DIR}/User/*.c")'),
        ):
            with self.subTest(kept=kept):
                text = self.migrate(DEVC_LIBXR_CMAKE.replace(glob, kept), GCC_TOOLCHAIN)
                blocks = stm32_cmake.split_blocks(text)
                self.assertIn('set(LIBXR_USER_SOURCES_GLOB "")', blocks["Project settings"])
                self.assertTrue(
                    blocks["Kept from the earlier LibXR.CMake"].endswith(f"{kept}\n\n{target}")
                )
                self.assertIn(
                    "LibXR.CMake: the earlier file(... LIBXR_USER_SOURCES ...) is not a GLOB with "
                    "a single pattern; it stays in the Kept block with its target_sources, and "
                    'LIBXR_USER_SOURCES_GLOB is ""',
                    self.log,
                )
        # 单一模式仍由 LIBXR_USER_SOURCES_GLOB 接管。
        # A single pattern is still taken over by LIBXR_USER_SOURCES_GLOB.
        text = self.migrate(DEVC_LIBXR_CMAKE, GCC_TOOLCHAIN)
        kept = stm32_cmake.split_blocks(text)["Kept from the earlier LibXR.CMake"]
        self.assertNotIn("LIBXR_USER_SOURCES", kept)
        self.assertFalse([entry for entry in self.log if "single pattern" in entry])

    def test_debug_options_the_user_changed_are_kept(self):
        # 以前 Debug 块整块丢弃，用户改过的 xr 级别静默变回 -O2。
        # The Debug block used to be dropped as a whole, and a level the user changed for xr
        # silently went back to -O2.
        old = DEVC_LIBXR_CMAKE.replace(
            "target_compile_options(xr PRIVATE -O2)", "target_compile_options(xr PRIVATE -O1)"
        ).replace(
            "    if(TARGET USB_Device_Library)",
            "    if(TARGET ThreadX)\n        target_compile_options(ThreadX PRIVATE -O0)\n"
            "    endif()\n\n    if(TARGET USB_Device_Library)",
        )
        text = self.migrate(old, GCC_TOOLCHAIN)
        blocks = stm32_cmake.split_blocks(text)
        # 保留的选项排在 “Library optimization” 块的 -O2 之后，所以仍然生效。
        # The kept options come after the -O2 of the "Library optimization" block, so they still
        # apply.
        self.assertTrue(
            blocks["Kept from the earlier LibXR.CMake"].endswith(
                "endif()\n\n"
                'if(CMAKE_BUILD_TYPE STREQUAL "Debug")\n'
                "    target_compile_options(xr PRIVATE -O1)\n"
                "    if(TARGET ThreadX)\n"
                "        target_compile_options(ThreadX PRIVATE -O0)\n"
                "    endif()\n"
                "endif()"
            ),
            blocks["Kept from the earlier LibXR.CMake"],
        )
        self.assertLess(text.index("Library optimization"), text.index("PRIVATE -O1"))
        self.assertIn('set(LIBXR_OPT_DEBUG "-Og")', blocks["Project settings"])
        self.assertEqual(
            [entry for entry in self.log if "of the Debug block" in entry],
            [
                f"LibXR.CMake: kept target_compile_options({target} PRIVATE {level}) of the "
                "Debug block in the earlier file in the Kept block, after the -O2 that Debug "
                "builds now give xr and the CubeMX libraries"
                for target, level in (("xr", "-O1"), ("ThreadX", "-O0"))
            ],
        )
        # 旧模板的默认选项由新结构重新生成，不保留也不提示。
        # The default options of the earlier template are generated again, neither kept nor
        # logged.
        text = self.migrate(DEVC_LIBXR_CMAKE, GCC_TOOLCHAIN)
        self.assertNotIn("CMAKE_BUILD_TYPE", text)
        self.assertFalse([entry for entry in self.log if "of the Debug block" in entry])
        # 迁移后的文件再次生成时保持不变。
        # The migrated file stays the same when generated again.
        text = self.migrate(old, GCC_TOOLCHAIN)
        with self.assertLogs(level="INFO") as logs:
            stm32_cmake.integrate(str(self.root))
        self.assertEqual(self.libxr_cmake.read_text(encoding="utf-8"), text)
        self.assertIn("INFO:root:LibXR.CMake already up to date, no changes needed.", logs.output)

    def test_the_migrated_file_is_stable(self):
        text = self.migrate(DEVC_LIBXR_CMAKE, GCC_TOOLCHAIN, CLANG_TOOLCHAIN)
        with self.assertLogs(level="INFO") as logs:
            logging.info("marker")
            stm32_cmake.integrate(str(self.root))
        self.assertEqual(self.libxr_cmake.read_text(encoding="utf-8"), text)
        self.assertIn("INFO:root:LibXR.CMake already up to date, no changes needed.", logs.output)


class RestoredToolchain(TestCase):
    """cmake/starm-clang.cmake 恢复为 CubeMX 写出的原文。
    cmake/starm-clang.cmake is restored to what CubeMX wrote.
    """

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "starm-clang.cmake"

    def restore(self, text):
        """写入工具链文件并恢复，返回 (是否写回, 结果)。
        Write the toolchain file, restore it and return (whether it was written, the result).
        """
        self.path.write_bytes(text.encode("utf-8"))
        changed = stm32_cmake.restore_starm_clang_toolchain(self.path)
        return changed, self.path.read_bytes().decode("utf-8")

    def test_what_earlier_versions_added_is_removed(self):
        self.assertEqual(self.restore(EARLIER_STARM), (True, CUBEMX_STARM))

    def test_a_file_that_cubemx_wrote_is_left_alone(self):
        self.assertEqual(self.restore(CUBEMX_STARM), (False, CUBEMX_STARM))
        self.assertFalse(stm32_cmake.restore_starm_clang_toolchain(self.path.with_name("none")))

    def test_the_cache_form_of_still_earlier_versions_becomes_the_plain_line(self):
        cached = CUBEMX_STARM.replace(
            'set(STARM_TOOLCHAIN_CONFIG "STARM_PICOLIBC")\n\n',
            'set(STARM_TOOLCHAIN_CONFIG "STARM_PICOLIBC" CACHE STRING "ST Arm Clang runtime '
            'profile")\n'
            "set_property(CACHE STARM_TOOLCHAIN_CONFIG PROPERTY STRINGS\n"
            "             STARM_HYBRID STARM_NEWLIB STARM_PICOLIBC)\n",
        )
        _, text = self.restore(cached)
        self.assertEqual(
            [line for line in text.splitlines() if line.strip()],
            [line for line in CUBEMX_STARM.splitlines() if line.strip()],
        )

    def test_the_users_own_edits_stay(self):
        edited = EARLIER_STARM.replace('"STARM_PICOLIBC")\n#', '"STARM_NEWLIB")\n#', 1).replace(
            "set(CMAKE_SYSTEM_PROCESSOR          arm)", "set(CMAKE_SYSTEM_PROCESSOR arm)"
        )
        _, text = self.restore(edited)
        self.assertEqual(
            text,
            CUBEMX_STARM.replace('"STARM_PICOLIBC"', '"STARM_NEWLIB"').replace(
                "set(CMAKE_SYSTEM_PROCESSOR          arm)", "set(CMAKE_SYSTEM_PROCESSOR arm)"
            ),
        )

    def test_line_endings_stay_as_the_file_has_them(self):
        crlf = EARLIER_STARM.replace("\n", "\r\n")
        _, text = self.restore(crlf)
        self.assertEqual(text, CUBEMX_STARM.replace("\n", "\r\n"))


class UserRegions(Integration):
    """LibXR.CMake 的两个 User Code 区域：重新生成时保留内容，缺失时补上空的区域。
    The two User Code regions of LibXR.CMake: their contents survive a rewrite, and a missing
    region is added empty.
    """

    def regions(self, text):
        """text 中的 User Code 区域，按 (名字, 内容) 排列。
        The User Code regions of text as (name, content) pairs.
        """
        return [
            (region.name, region.body_text) for region in CMakeDocument.parse(text).user_regions()
        ]

    def without_regions(self, text):
        """去掉两个 User Code 区域所在的节，得到加上区域之前的那种文件。
        The text without the two User Code sections, as it was before the regions were added.
        """
        for title, name in (
            (stm32_cmake.COMPILE_OPTIONS_TITLE, stm32_cmake.USER_REGION_COMPILE_OPTIONS),
            (stm32_cmake.APPLICATION_SETTINGS_TITLE, stm32_cmake.USER_REGION_APPLICATION_SETTINGS),
        ):
            section = (
                "\n"
                + stm32_cmake._rule(title)
                + f"\n# User Code Begin {name}\n# User Code End {name}"
            )
            self.assertIn(section, text)
            text = text.replace(section, "")
        return text

    def test_a_new_file_has_both_regions_empty(self):
        text = self.run_cmake_generator()
        self.assertEqual(
            self.regions(text),
            [
                (stm32_cmake.USER_REGION_COMPILE_OPTIONS, "\n"),
                (stm32_cmake.USER_REGION_APPLICATION_SETTINGS, "\n"),
            ],
        )

    def test_the_content_of_the_regions_survives_a_rewrite(self):
        text = self.run_cmake_generator()
        edited = text.replace(
            "# User Code Begin CompileOptions\n# User Code End CompileOptions",
            "# User Code Begin CompileOptions\n"
            "add_compile_options(-Wall)\n"
            "# User Code End CompileOptions",
        ).replace(
            "# User Code Begin ApplicationSettings\n# User Code End ApplicationSettings",
            "# User Code Begin ApplicationSettings\n"
            "set_target_properties(xr PROPERTIES POSITION_INDEPENDENT_CODE ON)\n"
            "# User Code End ApplicationSettings",
        )
        self.libxr_cmake.write_text(edited, encoding="utf-8")
        self.assertEqual(self.run_cmake_generator(), edited)

    def test_the_regions_are_not_part_of_the_settings_or_kept_blocks(self):
        text = self.run_cmake_generator()
        blocks = stm32_cmake.split_blocks(text)
        self.assertNotIn("User Code", blocks[stm32_cmake.SETTINGS_TITLE])
        self.assertIn("# User Code Begin CompileOptions", blocks[stm32_cmake.COMPILE_OPTIONS_TITLE])
        self.assertIn(
            "# User Code Begin ApplicationSettings",
            blocks[stm32_cmake.APPLICATION_SETTINGS_TITLE],
        )

    def test_a_file_with_the_earlier_first_line_is_rewritten_not_migrated(self):
        # 加上 User Code 区域之前写出的文件：首行仍是新格式的标志，因此走重写；只有首行变化、补上两个
        # 空的区域，设置块和 Kept 块原样保留。
        # A file written before the User Code regions were added: its first line still marks the
        # current format, so it is rewritten; only the first line changes and the two empty regions
        # are added, and the settings and Kept blocks stay as they are.
        text = self.run_cmake_generator()
        earlier = text.replace(stm32_cmake.GENERATED_LINE, stm32_cmake.EARLIER_GENERATED_LINE)
        self.libxr_cmake.write_text(earlier, encoding="utf-8")
        rewritten = self.run_cmake_generator()
        self.assertEqual(rewritten, text)
        self.assertEqual(
            stm32_cmake.split_blocks(rewritten)[stm32_cmake.SETTINGS_TITLE],
            stm32_cmake.split_blocks(earlier)[stm32_cmake.SETTINGS_TITLE],
        )
        # 迁移会把不认识的语句丢进 Kept 块；这里没有 Kept 块。
        # A migration would collect unrecognized statements in the Kept block; there is none here.
        self.assertNotIn(stm32_cmake.KEPT_TITLE, stm32_cmake.split_blocks(rewritten))

    def test_the_modules_directory_inside_a_block_is_removed_like_before(self):
        # 块内的 set(XROBOT_MODULES_DIR ...) 同样删掉，只删那一行，块的其余部分不变——与加上区域之前
        # 的实现逐字节相同。
        # A set(XROBOT_MODULES_DIR ...) inside a block goes too: only its line is removed and the rest
        # of the block stays, byte for byte as the implementation before the regions did it.
        text = self.run_cmake_generator()
        inside = text.replace(
            "set(LIBXR_SYSTEM None)\n",
            "set(LIBXR_SYSTEM None)\n"
            "if(FOO)\n"
            '    set(XROBOT_MODULES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Modules")\n'
            "    set(KEEP 1)\n"
            "endif()\n",
        )
        self.libxr_cmake.write_text(inside, encoding="utf-8")
        # 只少了那一行：块本身和其他设置都留在原处。
        # Only that line is gone: the block itself and the other settings stay where they were.
        self.assertEqual(
            self.run_cmake_generator(),
            inside.replace(
                '    set(XROBOT_MODULES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Modules")\n', ""
            ),
        )

    def test_a_file_without_regions_gains_them_empty(self):
        text = self.run_cmake_generator()
        self.libxr_cmake.write_text(self.without_regions(text), encoding="utf-8")
        self.assertEqual(self.run_cmake_generator(), text)

    def test_a_region_inside_the_kept_block_survives(self):
        # Kept 块里的内容与区域互不影响：区域排在最后，Kept 块的内容原样保留。
        # The Kept block and the regions do not disturb each other: the regions come last and the
        # Kept block stays as written.
        text = self.run_cmake_generator()
        kept = "target_compile_options(xr PRIVATE -Wno-deprecated-volatile)"
        with_kept = text.replace(
            "\n" + stm32_cmake._rule(stm32_cmake.APPLICATION_SETTINGS_TITLE),
            "\n"
            + stm32_cmake._rule(stm32_cmake.KEPT_TITLE)
            + "\n"
            + kept
            + "\n\n"
            + stm32_cmake._rule(stm32_cmake.APPLICATION_SETTINGS_TITLE),
        )
        self.libxr_cmake.write_text(with_kept, encoding="utf-8")
        self.assertEqual(self.run_cmake_generator(), with_kept)
        self.assertIn(kept, stm32_cmake.split_blocks(with_kept)[stm32_cmake.KEPT_TITLE])

    def test_a_begin_without_its_end_is_refused(self):
        text = self.run_cmake_generator()
        broken = text.replace("# User Code End CompileOptions\n", "")
        self.libxr_cmake.write_text(broken, encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            self.run_cmake_generator()
        self.assertIn("1 Begin and 0 End", str(raised.exception))
        # 拒绝时什么都不写。
        # Nothing is written when it refuses.
        self.assertEqual(self.libxr_cmake.read_text(encoding="utf-8"), broken)

    def test_a_marker_of_an_unknown_region_is_refused(self):
        text = self.run_cmake_generator()
        broken = text.replace(
            "# User Code Begin CompileOptions",
            "# User Code Begin CompileOptions\n# User Code Begin Other",
        )
        self.libxr_cmake.write_text(broken, encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            self.run_cmake_generator()
        self.assertIn(
            "not a region this file does not have".replace("not a region", "names a region"),
            str(raised.exception),
        )
        self.assertEqual(self.libxr_cmake.read_text(encoding="utf-8"), broken)

    def test_a_missing_region_is_added_next_to_the_kept_content(self):
        text = self.run_cmake_generator()
        broken = text.replace(
            "\n"
            + stm32_cmake._rule(stm32_cmake.COMPILE_OPTIONS_TITLE)
            + "\n# User Code Begin CompileOptions\n# User Code End CompileOptions",
            "",
        )
        self.libxr_cmake.write_text(broken, encoding="utf-8")
        self.assertEqual(self.run_cmake_generator(), text)


if __name__ == "__main__":
    unittest.main()


class RefusedFiles(Integration):
    """写文件之前的校验：生成器不写自己下一次会拒绝的文件。
    The check before writing: the generator does not write a file it would refuse on the next read.
    """

    def test_a_paired_region_in_the_settings_block_is_refused(self):
        # 设置块里成对出现的 CompileOptions 标记：重新生成会写出第二对，因此拒绝写入。
        # A paired CompileOptions marker in the settings block: a rewrite would write a second pair,
        # so it is refused.
        text = self.run_cmake_generator()
        with_regions = text.replace(
            "set(LIBXR_OPT_RELEASE",
            "# User Code Begin CompileOptions\nadd_compile_options(-Wall)\n"
            "# User Code End CompileOptions\nset(LIBXR_OPT_RELEASE",
        )
        self.libxr_cmake.write_text(with_regions, encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            self.run_cmake_generator()
        self.assertIn("CompileOptions", str(raised.exception))
        # 拒绝时什么都不写。
        # Nothing is written when it refuses.
        self.assertEqual(self.libxr_cmake.read_text(encoding="utf-8"), with_regions)

    def test_an_unparsable_file_is_refused_with_its_position(self):
        # 引号没有闭合：报出诊断的位置，不写文件。旧版对这种文件照常输出。
        # An unterminated quote: the position of the diagnostic is reported and nothing is written.
        # The earlier versions wrote such a file as well.
        text = self.run_cmake_generator()
        broken = text.replace(
            'set(LIBXR_USER_SOURCES_GLOB "${CMAKE_CURRENT_SOURCE_DIR}/User/*.cpp")',
            'set(LIBXR_USER_SOURCES_GLOB "${CMAKE_CURRENT_SOURCE_DIR}/User/*.cpp)',
        )
        self.libxr_cmake.write_text(broken, encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            self.run_cmake_generator()
        self.assertIn("unterminated quoted argument", str(raised.exception))
        self.assertIn("row", str(raised.exception))
        # 拒绝时什么都不写。
        # Nothing is written when it refuses.
        self.assertEqual(self.libxr_cmake.read_text(encoding="utf-8"), broken)


class SettingValues(Integration):
    """设置值的写入：只有值本身变化，没有值的那一行就地补上。
    The value written into a setting: only the value changes and a line without one gets it in place.
    """

    def test_a_setting_without_a_value_is_written_in_place(self):
        # set(LIBXR_SYSTEM ) 没有值：就地写入值。在它前面另插一行会让 CMake 最后读到空值。
        # set(LIBXR_SYSTEM ) has no value: the value is written in place. Inserting a line before it
        # would leave CMake reading the empty value at the end.
        text = self.run_cmake_generator()
        empty = text.replace("set(LIBXR_SYSTEM None)", "set(LIBXR_SYSTEM )")
        self.libxr_cmake.write_text(empty, encoding="utf-8")
        written = self.run_cmake_generator()
        self.assertEqual(written, text)

    def test_a_value_is_replaced_without_touching_the_rest_of_the_line(self):
        text = self.run_cmake_generator()
        commented = text.replace(
            "set(LIBXR_DRIVER st)",
            "set(LIBXR_DRIVER st) # driver",
        )
        self.libxr_cmake.write_text(commented, encoding="utf-8")
        written = self.run_cmake_generator()
        # 行尾注释留在原行，值本身换了。
        # The comment at the end of the line stays and only the value changes.
        self.assertEqual(
            written,
            commented.replace("set(LIBXR_DRIVER st) # driver", "set(LIBXR_DRIVER st) # driver"),
        )
        self.assertIn("set(LIBXR_DRIVER st) # driver", written)


MODULES_DIR_SETTING = 'set(XROBOT_MODULES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/Modules")'


def assert_cmake_parses(text):
    """让真正的 cmake 解析这份 LibXR.CMake；没有安装 cmake 时跳过。
    Let the real cmake parse this LibXR.CMake; skipped when cmake is not installed.

    这份文件里有 add_subdirectory 之类不能在 -P 模式执行的命令，因此只看有没有解析错误。
    The file holds commands such as add_subdirectory that -P mode cannot run, so only a parse error
    counts.
    """
    import shutil
    import subprocess

    executable = shutil.which("cmake")
    if executable is None:
        raise unittest.SkipTest("cmake is not installed")
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "LibXR.CMake"
        script.write_text(text, encoding="utf-8", newline="")
        completed = subprocess.run(
            [executable, "-P", str(script)], capture_output=True, text=True, check=False
        )
    if "Parse error" in completed.stderr:
        raise AssertionError(completed.stderr)


class InsertedSettings(Integration):
    """补写的设置另起一行，排在同一组设置里，不挤进上一条设置那一行的行尾。
    A setting added later gets a line of its own next to the settings it belongs with, instead of
    being glued to the end of the line of the setting before it.

    插入点是原命令所在那一行的换行之后：后面有几个空行、有没有行尾注释、有没有行尾空格、是 LF 还是
    CRLF 都不改变结果。写出的文件因此总是 CMake 能读的。
    The insertion point is right after the line ending of the line that holds the command: how many
    blank lines follow, whether the line carries a trailing comment or trailing spaces, and whether
    the file uses LF or CRLF change nothing. The file written is therefore always one CMake can read.
    """

    def standard_file(self):
        """生成一个不使用 XRobot 的工程，返回 cmake/LibXR.CMake 的文本。
        Generate the project without XRobot and return the text of cmake/LibXR.CMake.
        """
        self.write_app_main(False)
        return self.run_cmake_generator()

    def regenerate_with(self, text, settings, use_xrobot, newline="\n"):
        """把设置块换成 settings，用给定的入口选择再生成一次，返回新的文本。
        Replace the settings block with settings, generate again with the given entry choice and
        return the new text.

        newline 给出写回文件时的换行符，用来构造 CRLF 的输入文件。
        newline is the line ending the file is written with, used to build a CRLF input file.
        """
        current = self.settings(text)
        self.libxr_cmake.write_text(
            text.replace(current, settings, 1), encoding="utf-8", newline=newline
        )
        self.write_app_main(use_xrobot)
        written = self.run_cmake_generator()
        # 写出的文件必须是 CMake 能读的，也必须是本解析器能读的。
        # The file written must be one CMake can read and one this parser can read.
        self.assertEqual(CMakeDocument.parse(written).diagnostics, ())
        assert_cmake_parses(written)
        return written

    def test_a_setting_added_after_a_blank_line_stays_on_its_own_line(self):
        text = self.standard_file()
        driver = "set(LIBXR_DRIVER st)\n"
        written = self.regenerate_with(
            text, self.settings(text).replace(driver, driver + "\n"), True
        )
        self.assertEqual(written, text.replace(driver, driver + MODULES_DIR_SETTING + "\n\n", 1))

    def test_a_setting_added_after_a_line_that_ends_with_a_comment(self):
        text = self.standard_file()
        driver = "set(LIBXR_DRIVER st)\n"
        commented = "set(LIBXR_DRIVER st) # d\n"
        written = self.regenerate_with(
            text, self.settings(text).replace(driver, commented + "\n"), True
        )
        self.assertEqual(written, text.replace(driver, commented + MODULES_DIR_SETTING + "\n\n", 1))

    def test_a_missing_driver_is_added_after_the_line_of_the_system(self):
        text = self.standard_file()
        driver = "set(LIBXR_DRIVER st)\n"
        written = self.regenerate_with(text, self.settings(text).replace(driver, "\n"), False)
        self.assertEqual(written, text.replace(driver, driver + "\n", 1))

    def test_a_missing_release_level_goes_before_a_blank_line_at_the_end(self):
        text = self.standard_file()
        debug = 'set(LIBXR_OPT_DEBUG "-Og")\n'
        release = 'set(LIBXR_OPT_RELEASE "")\n'
        without_release = text.replace(release, "", 1)
        written = self.regenerate_with(text, self.settings(without_release) + "\n", False)
        self.assertEqual(written, without_release.replace(debug, debug + release, 1))

    def test_a_missing_release_level_goes_before_a_blank_line_and_a_comment(self):
        text = self.standard_file()
        debug = 'set(LIBXR_OPT_DEBUG "-Og")\n'
        release = 'set(LIBXR_OPT_RELEASE "")\n'
        without_release = text.replace(release, "", 1)
        written = self.regenerate_with(
            text, self.settings(without_release) + "\n\n# my note\n", False
        )
        self.assertEqual(
            written,
            without_release.replace(debug, debug + release + "\n# my note\n", 1),
        )

    def test_trailing_spaces_on_the_setting_line_are_kept(self):
        text = self.standard_file()
        driver = "set(LIBXR_DRIVER st)\n"
        padded = "set(LIBXR_DRIVER st)  \n"
        written = self.regenerate_with(
            text, self.settings(text).replace(driver, padded + "\n"), True
        )
        self.assertEqual(written, text.replace(driver, padded + MODULES_DIR_SETTING + "\n\n", 1))

    def test_a_file_with_crlf_is_rewritten_like_one_with_lf(self):
        text = self.standard_file()
        driver = "set(LIBXR_DRIVER st)\n"
        # 读文件时 CRLF 归一为 LF，因此 CRLF 的文件得到与 LF 相同的输出。
        # Reading a file folds CRLF into LF, so a CRLF file gives the same output as an LF one.
        written = self.regenerate_with(
            text, self.settings(text).replace(driver, driver + "\n"), True, newline="\r\n"
        )
        self.assertEqual(written, text.replace(driver, driver + MODULES_DIR_SETTING + "\n\n", 1))


if __name__ == "__main__":
    unittest.main()
