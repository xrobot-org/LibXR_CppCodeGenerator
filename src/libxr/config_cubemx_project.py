#!/usr/bin/env python

"""libxr stm32 setup：把 STM32CubeMX 工程配置为使用 LibXR 的工程。
libxr stm32 setup: set up an STM32CubeMX project to use LibXR.

依次加入 LibXR 子模块（Middlewares/Third_Party/LibXR），创建 .gitignore、.gitattributes 和 User
目录，记录终端设备，再像 libxr parse、libxr gen 和 libxr stm32 cmake 一样生成配置、C++ 代码和
CMake 集成。
In order it adds the LibXR submodule (Middlewares/Third_Party/LibXR), creates .gitignore,
.gitattributes and the User directory, records the terminal device, then produces the
configuration, the C++ code and the CMake integration as libxr parse, libxr gen and libxr stm32
cmake do.
"""

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys

from xr_syntax.i18n import tr

DEFAULT_MIRRORS = [
    "https://gitee.com/jiu-xiao/libxr",
]

# LibXR 的正式地址，工程的 .gitmodules 记录的就是它。
# The canonical LibXR URL, which the project's .gitmodules records.
LIBXR_URL = "https://github.com/xrobot-org/libxr.git"
# LibXR 在 GitHub 上迁移前后的地址；镜像只替换这些地址。
# LibXR's GitHub URLs before and after the transfer; a mirror only stands in for these.
LIBXR_URL_PATTERN = re.compile(
    r"^https://github\.com/(xrobot-org|jiu-xiao)/libxr(\.git)?/?$", re.IGNORECASE
)
SUBMODULE_PATH = "Middlewares/Third_Party/LibXR"


def is_git_repo(path):
    """path 位于 Git 工作树中时为 True。
    True when path is inside a Git work tree.
    """
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return result.stdout.strip() == "true"
    except subprocess.CalledProcessError:
        return False


def is_git_worktree_root(path):
    """path 本身是 Git 工作树的顶层目录时为 True（按真实路径比较），位于上层仓库之中时为 False。
    True when path itself is the top level of a Git work tree, compared by real path; False
    when it lies inside an enclosing repository.
    """
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return os.path.realpath(result.stdout.strip()) == os.path.realpath(path)
    except subprocess.CalledProcessError:
        return False


def run_command(cmd: list[str], ignore_error=False):
    """不经 shell 运行命令 cmd（参数列表）并返回标准输出；日志中各参数按 shell 规则引用。
    Run the command cmd, a list of arguments, without a shell and return its stdout; the log
    shows the arguments shell-quoted.

    成功的命令只记入调试日志。命令失败时，ignore_error 为 True 则记录警告并仍返回标准输出，否则
    记录错误并以退出码 1 结束进程。
    A successful command goes to the debug log only. On failure, ignore_error logs a warning
    and still returns stdout; otherwise the error is logged and the process exits with code 1.
    """
    result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    command_line = " ".join(shlex.quote(str(argument)) for argument in cmd)
    if result.returncode == 0:
        logging.debug(tr(f"[OK] {command_line}", f"[完成] {command_line}"))
        return result.stdout
    if ignore_error:
        logging.warning(
            tr(
                f"[IGNORED FAILURE] {command_line}\n{result.stderr}",
                f"[已忽略的失败] {command_line}\n{result.stderr}",
            )
        )
        return result.stdout
    logging.error(
        tr(f"[FAILED] {command_line}\n{result.stderr}", f"[失败] {command_line}\n{result.stderr}")
    )
    sys.exit(1)


def _probe_environment() -> dict:
    """测速用的环境：git 不提示输入账号密码，地址填错时直接失败。
    The environment for probing: git asks for no credentials, so a wrong address just fails.
    """
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")


def pick_git_base(default_base="https://github.com", mirrors=None, timeout=5.0):
    """在默认源和镜像中选出 git ls-remote 响应最快的 LibXR Git 源。
    Pick the LibXR Git source whose git ls-remote answers fastest, among the default and the
    mirrors.

    default_base 和 mirrors 中的每一项可以是基础地址（如 https://github.com，探测
    <基础地址>/xrobot-org/libxr.git），也可以是以 .git 或 libxr 结尾的完整仓库地址。探测时 git
    不提示输入账号密码。
    default_base and each mirror are either a base URL such as https://github.com, probed as
    <base>/xrobot-org/libxr.git, or a full repository URL ending in .git or libxr. Probes never
    ask for credentials.

    Returns:
        最快的候选项，保持传入时的形式；全部失败或超过 timeout 秒时为 default_base。
        The fastest candidate as it was given; default_base when every probe fails or takes
        longer than timeout seconds.
    """
    import time

    candidates = [default_base] + [m.strip() for m in (mirrors or []) if m.strip()]
    scores = []
    for item in candidates:
        start = time.monotonic()
        try:
            r = subprocess.run(
                ["git", "ls-remote", "-h", make_repo_url(item)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                env=_probe_environment(),
            )
            if r.returncode == 0:
                scores.append((time.monotonic() - start, item))
        except subprocess.TimeoutExpired:
            pass
    return min(scores)[1] if scores else default_base


def make_repo_url(base_or_repo: str, owner="xrobot-org", repo="libxr"):
    """完整的仓库地址：base_or_repo 以 .git 或仓库名结尾时原样返回，否则拼成
    <base_or_repo>/<owner>/<repo>.git。
    The full repository URL: base_or_repo as is when it ends in .git or the repository name,
    else <base_or_repo>/<owner>/<repo>.git.
    """
    if (
        base_or_repo.endswith(".git")
        or base_or_repo.rstrip("/").split("/")[-1].lower() == repo.lower()
    ):
        return base_or_repo
    return f"{base_or_repo.rstrip('/')}/{owner}/{repo}.git"


class LibXRSource:
    """克隆 LibXR 时实际使用的源。只有需要克隆时才测速选择，工程的 .gitmodules 始终记录 LIBXR_URL。
    The source LibXR is actually cloned from. It is chosen, by probing, only when a clone is
    needed; the project's .gitmodules always records LIBXR_URL.

    git_source 为 auto 时在 GitHub 和 mirrors 中选最快的，为 github 时用 GitHub，否则是给定的
    基础地址或仓库地址。
    With git_source auto the fastest of GitHub and the mirrors is used, with github GitHub, and
    otherwise the given base or repository URL.
    """

    def __init__(self, git_source: str = "auto", mirrors=()):
        """记录源的选择方式；此时还不测速。
        Record how the source is chosen; nothing is probed yet.
        """
        self.git_source = git_source
        self.mirrors = list(mirrors)
        self._url = None

    def url(self) -> str:
        """选中的仓库地址；第一次调用时选择并记录日志。
        The chosen repository URL; chosen and logged on the first call.
        """
        if self._url is None:
            if self.git_source == "auto":
                base = pick_git_base("https://github.com", self.mirrors, timeout=5.0)
            elif self.git_source == "github":
                base = "https://github.com"
            else:
                base = self.git_source
            self._url = make_repo_url(base)
            logging.info(tr(f"Cloning LibXR from {self._url}", f"从 {self._url} 克隆 LibXR"))
        return self._url

    def config_for(self, recorded_url: str) -> list:
        """让 recorded_url 改从选中的源获取的 git -c 参数；recorded_url 不是 LibXR 的 GitHub 地址
        （例如用户自己的分叉）或与选中的源相同时为空。
        git -c options that fetch recorded_url from the chosen source instead; empty when
        recorded_url is not a GitHub address of LibXR (a user's fork, say) or is the chosen source.

        git 2.38 起子模块默认不能从本地路径克隆；选中的源是本地仓库（路径或 file: 地址）时，参数中
        另外放行 file 协议，只作用于这一条命令。
        Since git 2.38 a submodule cannot be cloned from a local path by default; when the chosen
        source is a local repository, a path or a file: URL, the options also allow the file
        protocol for this one command.
        """
        if not LIBXR_URL_PATTERN.match(recorded_url):
            return []
        url = self.url()
        if url == recorded_url:
            return []
        options = ["-c", f"url.{url}.insteadOf={recorded_url}"]
        if url.startswith("file:") or os.path.isdir(url):
            options += ["-c", "protocol.file.allow=always"]
        return options


def create_gitignore_file(project_dir):
    """工程目录没有 .gitignore 时创建一个，忽略 build、.history、.cache、CMakeFiles 和
    .config.yaml；已有的 .gitignore 不改动。
    Create a .gitignore in the project directory that ignores build, .history, .cache,
    CMakeFiles and .config.yaml; an existing .gitignore is left unchanged.
    """
    gitignore_path = os.path.join(project_dir, ".gitignore")
    if not os.path.exists(gitignore_path):
        logging.info(tr("Creating .gitignore file...", "正在创建 .gitignore 文件……"))
        with open(gitignore_path, "w", encoding="utf-8", newline="\n") as gitignore_file:
            gitignore_file.write("""build/**
.history/**
.cache/**
.config.yaml
CMakeFiles/**
""")


# .gitattributes 中的行：仓库内的文本文件统一为 LF，签出时按平台转换；脚本的换行与平台无关，
# .bat 为 CRLF。后面的行覆盖前面的，所以 * text=auto 在最前。
# The lines of .gitattributes: text files are LF in the repository and converted on checkout by
# platform; shell scripts are LF everywhere and .bat files CRLF. Later lines override earlier
# ones, so * text=auto comes first.
GITATTRIBUTES_COMMENT = (
    "# Text files are LF in the repository; the working tree follows the platform."
)
GITATTRIBUTES_LINES = ("* text=auto", "*.sh text eol=lf", "*.bat text eol=crlf")


def create_gitattributes_file(project_dir):
    """工程目录没有 .gitattributes 时创建一个，写入 GITATTRIBUTES_LINES；已有的文件只补上缺少
    的行，其余内容不动。
    Create a .gitattributes in the project directory with GITATTRIBUTES_LINES; an existing file
    only gets the missing lines appended and keeps everything else.

    第一次提交就是 LF，之后无论谁在哪个平台用 CubeMX 重新生成，提交的差异里都只有真实的改动，
    没有换行符。已有文件的换行符（LF 或 CRLF）沿用到补上的行。
    The first commit is LF, so whoever regenerates with CubeMX on whatever platform afterwards,
    the commit shows only real changes and no line endings. The line ending of an existing file,
    LF or CRLF, carries over to the appended lines.
    """
    path = os.path.join(project_dir, ".gitattributes")
    if not os.path.exists(path):
        logging.info(tr("Creating .gitattributes file...", "正在创建 .gitattributes 文件……"))
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join((GITATTRIBUTES_COMMENT, *GITATTRIBUTES_LINES)) + "\n")
        return
    with open(path, encoding="utf-8", newline="") as stream:
        text = stream.read()
    present = {line.strip() for line in text.splitlines()}
    missing = [line for line in GITATTRIBUTES_LINES if line not in present]
    if not missing:
        return
    eol = "\r\n" if "\r\n" in text else "\n"
    separator = "" if not text or text.endswith("\n") else eol
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(text + separator + eol.join(missing) + eol)
    logging.info(
        tr(
            f"Added {', '.join(missing)} to .gitattributes",
            f"已在 .gitattributes 中补上 {'、'.join(missing)}",
        )
    )


def get_git_head(path):
    """path 处仓库的 HEAD commit；git 执行失败时为空字符串。
    The HEAD commit of the repository at path; an empty string when git fails.
    """
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def is_commit_ancestor(repo_path, older_commit, newer_commit):
    """older_commit 是 newer_commit 的祖先或与其相同时为 True；任一为空或 git 执行失败时为 False。
    True when older_commit is an ancestor of, or the same as, newer_commit; False when either is
    empty or git fails.
    """
    if not older_commit or not newer_commit:
        return False
    result = subprocess.run(
        ["git", "-C", repo_path, "merge-base", "--is-ancestor", older_commit, newer_commit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def is_empty_directory(path):
    """path 是空目录且不是符号链接时为 True。
    True when path is an empty directory and not a symbolic link.
    """
    return os.path.isdir(path) and not os.path.islink(path) and not os.listdir(path)


def _recorded_url(project_dir) -> str:
    """工程 .gitmodules 中 LibXR 子模块记录的地址；没有时为空字符串。
    The URL that the project's .gitmodules records for the LibXR submodule; an empty string
    when there is none.
    """
    result = subprocess.run(
        ["git", "-C", project_dir, "config", "-f", ".gitmodules", "--get-regexp", r"\.path$"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in result.stdout.splitlines():
        key, _, path = line.partition(" ")
        if path.strip() == SUBMODULE_PATH:
            name = key[: -len(".path")]
            url = subprocess.run(
                ["git", "-C", project_dir, "config", "-f", ".gitmodules", f"{name}.url"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
            return url.stdout.strip()
    return ""


def _has_commit(repo_path, commit) -> bool:
    """repo_path 中已有 commit 时为 True。
    True when repo_path already has commit.
    """
    result = subprocess.run(
        ["git", "-C", repo_path, "cat-file", "-e", f"{commit}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def add_libxr(project_dir, libxr_commit=None, source=None, default_libxr_commit=None):
    """把 LibXR 作为 Git 子模块放到 Middlewares/Third_Party/LibXR，并决定检出哪个 commit。
    Put LibXR at Middlewares/Third_Party/LibXR as a Git submodule and decide which commit is
    checked out.

    工程还不是 Git 仓库时先执行 git init。已登记的子模块会同步地址，没有检出时按 gitlink 初始化；
    未登记时以 LIBXR_URL 加入子模块。需要克隆时才从 source（默认 LibXRSource()）选出的源获取，
    .gitmodules 仍记录 LIBXR_URL。已有的 LibXR 目录不会被删除、移动或重新克隆。
    A project that is not yet a Git repository gets git init. A registered submodule has its URL
    synced and, without a checkout, is initialized to its gitlink; an unregistered one is added
    as LIBXR_URL. Only a clone fetches from the source that source (LibXRSource() by default)
    chooses, and .gitmodules still records LIBXR_URL. An existing LibXR directory is never
    deleted, moved or re-cloned.

    只有给出 libxr_commit，或本次新加入且原来没有检出的子模块（此时用 default_libxr_commit）才会
    切换检出；新加入的子模块的 gitlink 随之暂存。其他情况保持现有检出，与 default_libxr_commit
    不同且不比它新时记录警告。
    Only libxr_commit, or default_libxr_commit for a submodule added by this run without an earlier
    checkout, moves the checkout; the gitlink of a newly added submodule is staged with it.
    Otherwise the existing checkout is kept, with a warning when it differs from
    default_libxr_commit and is not newer than it.

    Raises:
        SystemExit: LibXR 目录既不是 Git 检出也不是空目录，或必需的 git 命令失败。
            The LibXR directory is neither a Git checkout nor empty, or a required git command
            failed.
    """
    source = source or LibXRSource()
    libxr_path = os.path.join(project_dir, *SUBMODULE_PATH.split("/"))
    os.makedirs(os.path.dirname(libxr_path), exist_ok=True)

    def has_registered_submodule(repo_root, rel_path):
        """rel_path 已登记为子模块时为 True：索引中该路径是 gitlink（模式 160000）。只在
        .gitmodules 中出现（例如解压 ZIP 后 git init 的工程）不算登记，因为没有记录 commit。
        True when rel_path is registered as a submodule: the index holds a gitlink (mode 160000)
        there. A path only named in .gitmodules, as in a project unpacked from a ZIP and then
        given git init, does not count: no commit is recorded for it.
        """
        result = subprocess.run(
            ["git", "-C", repo_root, "ls-files", "--stage", "--", rel_path],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return False
        return any(line.split()[:1] == ["160000"] for line in result.stdout.splitlines())

    if not is_git_repo(project_dir):
        logging.warning(
            tr(
                f"{project_dir} is not a Git repository. Initializing...",
                f"{project_dir}：不是 Git 仓库，正在初始化……",
            )
        )
        run_command(["git", "init", project_dir])

    registered = has_registered_submodule(project_dir, SUBMODULE_PATH)
    checkout_path_present = os.path.lexists(libxr_path)
    existing_checkout = checkout_path_present and is_git_worktree_root(libxr_path)
    added_submodule = False

    # 已有的 LibXR 目录可能含有用户自己的提交或源码，不会被删除、移动或重新克隆。
    # An existing LibXR directory may hold the user's own commits or sources;
    # it is never deleted, moved or re-cloned.
    if checkout_path_present and not existing_checkout and not is_empty_directory(libxr_path):
        logging.error(
            tr(
                f"{libxr_path} exists but is not a valid Git checkout; it was left untouched. "
                "Move it away or turn it into a LibXR checkout, then run again.",
                f"{libxr_path}：已存在，但不是有效的 Git 检出，未做改动。"
                "请把它移走或改成 LibXR 的检出，然后重新运行。",
            )
        )
        sys.exit(1)

    if registered:
        run_command(["git", "-C", project_dir, "submodule", "sync", "--", SUBMODULE_PATH])
        if not existing_checkout:
            options = source.config_for(_recorded_url(project_dir))
            run_command(
                ["git", *options, "-C", project_dir, "submodule", "update", "--init"]
                + ["--recursive", "--", SUBMODULE_PATH]
            )
    else:
        if checkout_path_present and not existing_checkout:
            # 空目录（例如 ZIP 中的子模块目录）挡住 submodule add；它是空的，可以删除。
            # An empty directory, such as a submodule folder from a ZIP, blocks submodule add;
            # it is empty, so it can go.
            os.rmdir(libxr_path)
        options = [] if existing_checkout else source.config_for(LIBXR_URL)
        run_command(
            ["git", *options, "-C", project_dir, "submodule", "add", LIBXR_URL, SUBMODULE_PATH]
        )
        logging.info(
            tr(f"Added the LibXR submodule ({LIBXR_URL}).", f"已加入 LibXR 子模块（{LIBXR_URL}）。")
        )
        added_submodule = True

    if not os.path.exists(libxr_path):
        return
    current_commit = get_git_head(libxr_path)
    target_commit = ""

    # LibXR 由工程的 gitlink 锁定。只有显式的 --commit 或本次新加入的子模块才会切换检出；
    # 其他情况下检出保持不变，与包内默认提交不同时只报告。
    # The project's gitlink pins LibXR. Only an explicit --commit or a
    # submodule added by this run moves the checkout; otherwise the
    # checkout stays where it is and a different package default is only
    # reported.
    if libxr_commit:
        target_commit = libxr_commit
        logging.info(
            tr(
                f"Checking out LibXR to requested commit {target_commit}",
                f"把 LibXR 检出到指定的提交 {target_commit}",
            )
        )
    elif added_submodule and not existing_checkout and default_libxr_commit:
        target_commit = default_libxr_commit
        logging.info(
            tr(
                f"Initializing new LibXR submodule to default commit {target_commit}",
                f"把新加入的 LibXR 子模块初始化到默认提交 {target_commit}",
            )
        )
    elif default_libxr_commit and current_commit != default_libxr_commit:
        if is_commit_ancestor(libxr_path, default_libxr_commit, current_commit):
            logging.info(
                tr(
                    f"LibXR checkout {current_commit[:12]} is newer than this generator's "
                    "default; keeping it.",
                    f"LibXR 的检出 {current_commit[:12]} 比本生成器的默认提交新，保留不变。",
                )
            )
        else:
            relation = (
                tr("older than", "早于")
                if is_commit_ancestor(libxr_path, current_commit, default_libxr_commit)
                else tr("different from", "不同于")
            )
            logging.warning(
                tr(
                    f"LibXR checkout {current_commit[:12]} is {relation} this generator's "
                    f"default {default_libxr_commit[:12]}; it was left unchanged. To switch, "
                    f"run `libxr stm32 setup` with --commit {default_libxr_commit} (or check out "
                    "the commit in Middlewares/Third_Party/LibXR) and commit the gitlink.",
                    f"LibXR 的检出 {current_commit[:12]} {relation}本生成器的默认提交 "
                    f"{default_libxr_commit[:12]}，未做改动。如需切换，请用 --commit "
                    f"{default_libxr_commit} 运行 `libxr stm32 setup`（或在 "
                    "Middlewares/Third_Party/LibXR 中检出该提交），然后提交 gitlink。",
                )
            )
    else:
        logging.info(
            tr(
                f"Keeping the LibXR checkout {current_commit[:12]}.",
                f"保留现有的 LibXR 检出 {current_commit[:12]}。",
            )
        )

    if target_commit:
        if not _has_commit(libxr_path, target_commit):
            origin = subprocess.run(
                ["git", "-C", libxr_path, "remote", "get-url", "origin"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            ).stdout.strip()
            options = source.config_for(origin)
            run_command(["git", *options, "-C", libxr_path, "fetch", "origin"], ignore_error=True)
        run_command(["git", "-C", libxr_path, "checkout", target_commit])
        if added_submodule:
            # submodule add 暂存的是克隆时的 HEAD；暂存检出后的 commit。
            # submodule add staged the HEAD of the clone; stage the checked-out commit.
            run_command(["git", "-C", project_dir, "add", "--", SUBMODULE_PATH])


def create_user_directory(project_dir):
    """确保工程中有 User 目录，并返回其路径。
    Make sure the project has a User directory and return its path.
    """
    user_path = os.path.join(project_dir, "User")
    if not os.path.exists(user_path):
        os.makedirs(user_path)
    return user_path


def set_terminal_source(user_path, terminal_source):
    """把 -t/--terminal 指定的终端设备写入 User/libxr_config.yaml 的 terminal_source，
    文件不存在时新建。
    Record the -t/--terminal device as terminal_source in User/libxr_config.yaml, creating the
    file when it does not exist.

    libxr gen 从这个文件读取终端设备，所以之后重新生成时沿用该设置；文件中的其他键和
    注释保持不变。新建的文件先写入固定为已安装 libxr 版本的 generator，与 libxr gen 新建的
    配置相同。文件无法按 LibXR 配置读取时记录错误并以退出码 1 结束。
    libxr gen reads the terminal device from this file, so the choice persists for later
    regenerations. Other keys and comments are kept. A new file first gets generator pinned to
    the installed libxr version, like a configuration libxr gen creates. A file that cannot be
    read as a LibXR configuration logs an error and exits with code 1.
    """
    from libxr import libxr_config_file, update_notice

    config_path = os.path.join(user_path, "libxr_config.yaml")
    try:
        if os.path.exists(config_path):
            document, _ = libxr_config_file.read(config_path)
        else:
            document = libxr_config_file.new_document(update_notice.installed_version())
    except libxr_config_file.LibXRConfigError as error:
        logging.error(str(error))
        sys.exit(1)
    libxr_config_file.set_value(document, "terminal_source", terminal_source)
    libxr_config_file.write(config_path, document)
    logging.info(
        tr(
            f"Set terminal_source to {terminal_source} in {config_path}",
            f"已在 {config_path} 中把 terminal_source 设为 {terminal_source}",
        )
    )


def _friendly_path_name(path: str) -> str:
    """路径的末级名称，用于提示信息（'.' 显示为当前目录名）；没有末级名称（如根目录）时为绝对路径。
    The last component of a path for messages, so '.' shows the current folder name; the
    absolute path when there is none, as for a root directory.
    """
    abs_path = os.path.abspath(path)
    base = os.path.basename(abs_path.rstrip(os.sep))
    return base or abs_path


def _stop(english: str, chinese: str) -> None:
    """按当前语言记录错误，并以退出码 1 结束。
    Log the error in the current language and exit with code 1.
    """
    logging.error(tr(english, chinese))
    sys.exit(1)


def check_project(project_dir: str) -> str:
    """检查 project_dir 是 CMake 形式的 STM32CubeMX 工程，返回其中唯一的 .ioc 文件的路径。
    Check that project_dir is an STM32CubeMX project in CMake form and return the path of its
    only .ioc file.

    setup 在改动工程之前调用它。目录不存在、没有 Core/、.ioc 文件不是恰好一个，或者没有
    CMakeLists.txt（CubeMX 生成的不是 CMake 工程）时记录错误并以退出码 1 结束；提示中用目录名
    代替 '.'。
    setup calls it before it changes the project. A missing directory, no Core/, other than
    exactly one .ioc file, or no CMakeLists.txt (CubeMX generated something other than a CMake
    project) logs an error and exits with code 1; the messages show the folder name instead
    of '.'.
    """
    name = _friendly_path_name(project_dir)
    if not os.path.isdir(project_dir):
        _stop(f"Directory {name} does not exist", f"目录 {name} 不存在")
    if not os.path.isdir(os.path.join(project_dir, "Core")):
        _stop(
            f"{name} is not a valid STM32CubeMX project: missing Core/ directory; generate the "
            "code with STM32CubeMX, or run `libxr stm32 cubemx-gen`",
            f"{name}：不是有效的 STM32CubeMX 工程，缺少 Core/ 目录；请先用 STM32CubeMX 生成代码，"
            "或运行 `libxr stm32 cubemx-gen`",
        )
    ioc_files = sorted(entry for entry in os.listdir(project_dir) if entry.endswith(".ioc"))
    if not ioc_files:
        _stop(f"{name} holds no .ioc file", f"{name} 中没有 .ioc 文件")
    if len(ioc_files) > 1:
        _stop(
            f"{name} holds several .ioc files ({', '.join(ioc_files)}); a directory holds one "
            "CubeMX project",
            f"{name} 中有多个 .ioc 文件（{'、'.join(ioc_files)}）；一个目录只放一个 CubeMX 工程",
        )
    if not os.path.isfile(os.path.join(project_dir, "CMakeLists.txt")):
        _stop(
            f"{name} has no CMakeLists.txt; set Toolchain / IDE to CMake in the Project Manager "
            "of STM32CubeMX and generate the project again",
            f"{name} 中没有 CMakeLists.txt；请在 STM32CubeMX 的 Project Manager 中把 "
            "Toolchain / IDE 设为 CMake，然后重新生成工程",
        )
    return os.path.join(project_dir, ioc_files[0])


def setup_project(
    project_dir: str,
    terminal_source: str = "",
    xrobot_enable: bool | None = None,
    commit: str = "",
    git_source: str = "auto",
    git_mirrors: str = "",
) -> None:
    """加入 LibXR 子模块，写 .gitignore 和 .gitattributes，再生成配置、C++ 代码和 CMake 集成。
    Add the LibXR submodule, write .gitignore and .gitattributes, then generate the
    configuration, the C++ code and the CMake integration.

    改动工程之前先用 check_project() 检查工程。commit 为空时以 libxr_version.py 中锁定的 commit
    为默认值。需要克隆 LibXR 时，git_source 为 auto 则在 GitHub、内置镜像、XR_GIT_MIRRORS 和
    git_mirrors（逗号分隔）中选出响应最快的源。xrobot_enable 为 None 时沿用工程现在的选择：
    User/app_main.cpp 由 --xrobot 生成时继续生成 XRobot 代码。结束时说明还需手动完成的步骤（见
    _report_next_steps()）。
    check_project() checks the project before anything changes. With an empty commit, the
    commit locked in libxr_version.py is the default. When LibXR has to
    be cloned, git_source auto picks the fastest of GitHub, the built-in mirror,
    XR_GIT_MIRRORS and git_mirrors (comma-separated). With xrobot_enable None the project keeps
    its choice: XRobot code is generated again when User/app_main.cpp was generated with
    --xrobot. At the end it describes what is left to do by hand (see _report_next_steps()).
    """
    from libxr.generator_code_stm32 import generate
    from libxr.generator_stm32_cmake import integrate, project_uses_xrobot
    from libxr.peripheral_analyzer_stm32 import parse_project

    if shutil.which("git") is None:
        logging.error(
            tr(
                "git was not found on PATH; LibXR is added to the project as a Git submodule",
                "PATH 中找不到 git；LibXR 以 Git 子模块的形式加入工程",
            )
        )
        sys.exit(1)

    project_dir = project_dir.rstrip("/")
    # 先检查完工程再改动它。
    # Check the whole project before changing anything.
    ioc_file = check_project(project_dir)

    libxr_commit = commit.strip()
    default_libxr_commit = ""
    if not libxr_commit:
        try:
            from libxr.libxr_version import LibXRInfo

            default_libxr_commit = LibXRInfo.COMMIT
        except ImportError:
            logging.info(
                tr(
                    "No default LibXR commit: src/libxr/libxr_version.py is missing "
                    "(scripts/gen_libxr_version.py creates it); a new submodule stays at the "
                    "commit it is cloned at.",
                    "没有默认的 LibXR 提交：缺少 src/libxr/libxr_version.py"
                    "（由 scripts/gen_libxr_version.py 生成）；新加入的子模块停在克隆时的提交。",
                )
            )

    if libxr_commit:
        logging.info(
            tr(f"Requested LibXR commit: {libxr_commit}", f"指定的 LibXR 提交：{libxr_commit}")
        )
    elif default_libxr_commit:
        logging.info(
            tr(
                f"Default LibXR commit: {default_libxr_commit}",
                f"默认的 LibXR 提交：{default_libxr_commit}",
            )
        )

    if xrobot_enable is None:
        xrobot_enable = project_uses_xrobot(project_dir)
        if xrobot_enable:
            logging.info(
                tr(
                    "User/app_main.cpp uses XRobot; generating with --xrobot "
                    "(--no-xrobot turns it off).",
                    "User/app_main.cpp 使用了 XRobot，继续按 --xrobot 生成（--no-xrobot 可关闭）。",
                )
            )

    # 克隆用的源只在需要克隆时选择（auto 时对默认源和镜像测速）。
    # The source for cloning is chosen only when a clone is needed (auto probes the default and
    # the mirrors).
    env_mirrors = os.environ.get("XR_GIT_MIRRORS", "")
    cli_mirrors = [m for m in git_mirrors.split(",") if m.strip()]
    all_mirrors = (
        DEFAULT_MIRRORS
        + [m.strip() for m in (env_mirrors.split(",") if env_mirrors else []) if m.strip()]
        + cli_mirrors
    )
    add_libxr(
        project_dir,
        libxr_commit if libxr_commit else None,
        source=LibXRSource(git_source, all_mirrors),
        default_libxr_commit=default_libxr_commit if default_libxr_commit else None,
    )
    if xrobot_enable:
        check_xrobot_support(project_dir, default_libxr_commit)

    logging.info(tr(f"Found .ioc file: {ioc_file}", f"找到 .ioc 文件：{ioc_file}"))

    create_gitignore_file(project_dir)
    create_gitattributes_file(project_dir)

    # 创建 User 目录。
    # Create user directory
    user_path = create_user_directory(project_dir)

    # 确定输出路径。
    # Define paths
    yaml_output = os.path.join(project_dir, ".config.yaml")
    cpp_output = os.path.join(user_path, "app_main.cpp")

    # 为代码生成器记录终端设备。
    # Record the terminal device for the code generator
    if terminal_source:
        set_terminal_source(user_path, terminal_source)

    logging.info(tr("Parsing .ioc file...", "正在解析 .ioc 文件……"))
    parse_project(project_dir, yaml_output, summary=False)

    logging.info(tr("Generating C++ code...", "正在生成 C++ 代码……"))
    generate(yaml_output, cpp_output, xrobot_enable)

    integrate(project_dir)

    logging.info(tr("[Pass] All tasks completed.", "[通过] 全部任务已完成。"))
    _report_next_steps(project_dir, xrobot_enable)


# LibXR 中 XRobot 工程构建其模块所需的 CMake 文件，相对 LibXR 检出的路径。
# The CMake file of LibXR that an XRobot project needs to build its Modules, relative to the
# LibXR checkout.
LIBXR_XROBOT_CMAKE = "cmake/XRobot.cmake"
# XRobot 工程还没有 Modules/modules.yaml 时依次运行的命令：(命令, 英文说明, 中文说明)。
# The commands an XRobot project without Modules/modules.yaml runs in order: (command, English
# description, Chinese description).
XROBOT_STEPS = (
    (
        "xrobot init",
        "create Modules/modules.yaml, Modules/sources.yaml and User/xrobot.yaml",
        "创建 Modules/modules.yaml、Modules/sources.yaml 和 User/xrobot.yaml",
    ),
    ("xrobot module add <owner>/<Module>", "add a Module", "加入模块"),
    (
        "xrobot setup",
        "fetch the Modules and generate User/xrobot_main.hpp",
        "拉取模块并生成 User/xrobot_main.hpp",
    ),
    ("xrobot instance add <owner>/<Module>", "add an instance of the Module", "新增模块的实例"),
)


def check_xrobot_support(project_dir: str, default_libxr_commit: str = "") -> None:
    """LibXR 检出中没有 LIBXR_XROBOT_CMAKE 时记录错误并以退出码 1 结束，这时还没有生成任何文件。
    Log an error and exit with code 1 when the LibXR checkout has no LIBXR_XROBOT_CMAKE; no file
    has been generated at that point.

    --xrobot 生成的工程由 LibXR 的这个文件构建 XRobot 模块，缺少它的检出（早于它的 LibXR）在构建时
    报找不到模块头文件。提示用 --commit 检出较新的 LibXR；default_libxr_commit 非空且与当前检出
    不同时写出它。
    A project generated with --xrobot builds its XRobot Modules through this file of LibXR; a
    checkout without it, a LibXR older than the file, fails at build time with missing Module
    headers. The message suggests checking out a newer LibXR with --commit and names
    default_libxr_commit when it is not empty and differs from the checkout.
    """
    libxr_path = os.path.join(project_dir, "Middlewares", "Third_Party", "LibXR")
    if os.path.isfile(os.path.join(libxr_path, *LIBXR_XROBOT_CMAKE.split("/"))):
        return
    head = get_git_head(libxr_path) if is_git_worktree_root(libxr_path) else ""
    checkout_en = checkout_zh = "Middlewares/Third_Party/LibXR"
    if head:
        checkout_en += f" ({head[:12]})"
        checkout_zh += f"（{head[:12]}）"
    commit = default_libxr_commit if default_libxr_commit and default_libxr_commit != head else ""
    _stop(
        f"{checkout_en} has no {LIBXR_XROBOT_CMAKE}, which a --xrobot project needs to build its "
        "XRobot Modules; check out a LibXR commit that has it with `libxr stm32 setup --xrobot "
        f"--commit {commit or '<commit>'}`",
        f"{checkout_zh} 中没有 {LIBXR_XROBOT_CMAKE}，--xrobot 工程需要它构建 XRobot 模块；请用 "
        f"`libxr stm32 setup --xrobot --commit {commit or '<提交>'}` 检出含有该文件的 LibXR 提交",
    )


def _report_next_steps(project_dir: str, xrobot: bool = False) -> None:
    """说明 setup 之后还需手动完成的事：CubeMX 生成的源文件还没有调用 app_main() 时说明在哪里调用，
    XRobot 工程还没有 Modules/modules.yaml 时依次给出 XRobot 的设置命令，工程有 CMake preset 时
    给出构建命令。
    Describe what is left to do by hand after setup: where to call app_main() when the
    CubeMX sources do not call it yet, the XRobot setup commands in order when an XRobot project
    has no Modules/modules.yaml yet, and the build commands when the project has CMake presets.

    ThreadX 工程在 App_ThreadX_Init() 中创建一个调用它的线程，FreeRTOS 工程在定义
    StartDefaultTask 的源文件中调用，其他工程在 Core/Src/main.c 的 main() 中调用。xrobot 为真
    表示入口源文件按 --xrobot 生成。构建命令使用第一个同时有 configure 和 build preset 的名字。
    A ThreadX project creates a thread calling it in App_ThreadX_Init(), a FreeRTOS project
    calls it in the source file defining StartDefaultTask, any other project in main() of
    Core/Src/main.c. xrobot true means the entry source was generated with
    --xrobot. The build commands use the first name that has both a configure and a build
    preset.
    """
    sources = os.path.join(project_dir, "Core", "Src")
    texts = {}
    if os.path.isdir(sources):
        for name in sorted(os.listdir(sources)):
            if name.endswith(".c"):
                with open(os.path.join(sources, name), encoding="utf-8", errors="replace") as f:
                    texts[name] = f.read()
    if not any(re.search(r"\bapp_main\s*\(\s*\)\s*;", text) for text in texts.values()):
        threadx = next((name for name, text in texts.items() if "App_ThreadX_Init(" in text), None)
        task = next((name for name, text in texts.items() if "StartDefaultTask(" in text), None)
        keep_en = "inside USER CODE sections, which CubeMX keeps when it regenerates the code."
        keep_zh = "写在 USER CODE 区域中，CubeMX 重新生成代码时保留。"
        if threadx:
            # App_ThreadX_Init() 在调度器启动前执行，app_main() 要在线程中运行。
            # App_ThreadX_Init() runs before the scheduler starts; app_main() runs in a thread.
            english = (
                f"Next: in App_ThreadX_Init() (Core/Src/{threadx}), create a thread whose entry "
                f'function includes "app_main.h" and calls app_main(), {keep_en} '
                "App_ThreadX_Init() runs before the scheduler starts, so it cannot call "
                "app_main() directly."
            )
            chinese = (
                f"下一步：在 App_ThreadX_Init()（Core/Src/{threadx}）中创建一个线程，线程入口函数 "
                f'#include "app_main.h" 并调用 app_main()，{keep_zh}App_ThreadX_Init() 在调度器'
                "启动前执行，不能在其中直接调用 app_main()。"
            )
        else:
            if task:
                where_en = f"in the default task StartDefaultTask (Core/Src/{task})"
                where_zh = f"在默认任务 StartDefaultTask（Core/Src/{task}）中"
            else:
                where_en, where_zh = (
                    "in main() of Core/Src/main.c",
                    "在 Core/Src/main.c 的 main() 中",
                )
            english = f'Next: #include "app_main.h" and call app_main() {where_en}, {keep_en}'
            chinese = f'下一步：{where_zh} #include "app_main.h" 并调用 app_main()，{keep_zh}'
        logging.info(tr(english, chinese))
    report_xrobot_steps(project_dir, xrobot)
    preset = _build_preset(os.path.join(project_dir, "CMakePresets.json"))
    if preset:
        command = f"cmake --preset {preset} && cmake --build --preset {preset}"
        logging.info(tr(f"Build: {command}", f"构建：{command}"))


def report_xrobot_steps(project_dir: str, xrobot: bool) -> None:
    """XRobot 工程（xrobot 为真）还没有 Modules/modules.yaml 时依次给出 XRobot 的设置命令
    （XROBOT_STEPS，其中 xrobot setup 生成 User/xrobot_main.hpp）；各平台的 setup 共用。
    Give the XRobot setup commands in order (XROBOT_STEPS, where xrobot setup generates
    User/xrobot_main.hpp) when an XRobot project (xrobot true) has no Modules/modules.yaml
    yet; the setup of every platform shares this.
    """
    if xrobot and not os.path.isfile(os.path.join(project_dir, "Modules", "modules.yaml")):
        logging.info(
            tr(
                "Next: Modules/modules.yaml does not exist yet; set up XRobot in this order:",
                "下一步：还没有 Modules/modules.yaml，按以下顺序完成 XRobot 的设置：",
            )
        )
        for command, english, chinese in XROBOT_STEPS:
            logging.info(f"  {command:<36}  " + tr(english, chinese))


def _build_preset(path: str) -> str:
    """CMakePresets.json 中第一个同时有 configure 和 build preset 的名字；没有或文件不可读时为空字符串。
    The first name in CMakePresets.json with both a configure and a build preset; an empty
    string when there is none or the file cannot be read.
    """
    try:
        with open(path, encoding="utf-8") as f:
            presets = json.load(f)
        configure = [p["name"] for p in presets.get("configurePresets", []) if not p.get("hidden")]
        build = {p["name"] for p in presets.get("buildPresets", [])}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return ""
    return next((name for name in configure if name in build), "")


if __name__ == "__main__":
    from libxr.legacy import run

    raise SystemExit(run("xr_cubemx_cfg"))
