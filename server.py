#!/usr/bin/env python3
"""coding-mcp —— 通用编码 MCP 服务器（stdio，Python 版）

设计目标（对应三个核心要求）：
  1. 不占用文件 —— 所有读写"打开即关"，从不持有长句柄；
     写入采用"临时文件 + fsync + 原子重命名"，绝不锁定目标文件。
  2. 自动保存   —— 每次写入/编辑立即落盘（fsync + 原子替换），无"未保存"状态。
  3. 避免丢失   —— 任何修改前自动备份上一版到 .coding-mcp-backup/，
     加上原子写入，杜绝"改一半/写坏/进程中断丢代码"。

环境变量（可选）：
  MCP_DISABLE_BACKUP=1          关闭自动备份
  MCP_BACKUP_DIR=/abs/path      自定义备份目录（默认：目标文件旁 .coding-mcp-backup/）
  MCP_ALLOWED_ROOTS=/a;/b       限制可访问根目录（Windows 用 ; 分隔）
  MCP_DISABLE_AUDIT=1           关闭操作审计日志
  MCP_AUDIT_LOG=/abs/file.log   自定义审计日志路径（默认 ~/.coding-mcp/audit.log）
  MCP_ENABLE_EXEC=1             开启命令执行工具（默认关闭，需显式开启）
  MCP_EXEC_TIMEOUT=30           命令执行超时上限（秒，默认 30）
"""

import os
import sys
import json
import shutil
import fnmatch
import tempfile
import datetime
import subprocess
import difflib

from mcp.server.mcpserver import MCPServer

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DISABLE_BACKUP = os.environ.get("MCP_DISABLE_BACKUP", "") in ("1", "true", "True")

BACKUP_DIR_ENV = os.environ.get("MCP_BACKUP_DIR", "").strip()

AUDIT_ENABLED = os.environ.get("MCP_DISABLE_AUDIT", "") not in ("1", "true", "True")
_AUDIT_LOG_ENV = os.environ.get("MCP_AUDIT_LOG", "").strip()
if _AUDIT_LOG_ENV:
    AUDIT_LOG = os.path.abspath(_AUDIT_LOG_ENV)
elif AUDIT_ENABLED:
    AUDIT_LOG = os.path.join(str(os.path.expanduser("~")), ".coding-mcp", "audit.log")
else:
    AUDIT_LOG = None

ALLOWED_ROOTS = [
    os.path.abspath(p.strip())
    for p in os.environ.get("MCP_ALLOWED_ROOTS", "").split(os.pathsep)
    if p.strip()
]

SKIP_DIRS = {"node_modules", ".git", ".svn", ".hg", ".coding-mcp-backup", "__pycache__"}

# 命令执行开关：默认关闭，需设置 MCP_ENABLE_EXEC=1 才启用
EXEC_ENABLED = os.environ.get("MCP_ENABLE_EXEC", "") in ("1", "true", "True")

# 命令执行超时上限（秒），单次请求的 timeout 不得超过此值
try:
    EXEC_TIMEOUT_MAX = float(os.environ.get("MCP_EXEC_TIMEOUT", "30"))
except ValueError:
    EXEC_TIMEOUT_MAX = 30.0

# git 可执行文件路径（可选覆盖，默认从 PATH 查找）
GIT_BIN = os.environ.get("MCP_GIT_BIN", "git").strip() or "git"

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def run_git(args, cwd=None, timeout=30.0):
    """安全地调用 git：禁 shell、参数列表传递，返回 (returncode, stdout, stderr)。"""
    cmd = [GIT_BIN, *args]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, timeout=timeout, shell=False
        )
    except FileNotFoundError:
        raise RuntimeError(f"未找到 git 可执行文件：{GIT_BIN}，请确认已安装 git 或设置 MCP_GIT_BIN")
    return proc.returncode, proc.stdout, proc.stderr


def _git_output(args, cwd=None, timeout=30.0, limit=8000):
    """执行 git 并返回格式化文本（stdout + 截断 + stderr 标注）。"""
    code, stdout, stderr = run_git(args, cwd=cwd, timeout=timeout)

    def _dec(b):
        try:
            return b.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return str(b)

    def _trim(s, limit=8000):
        if len(s) <= limit:
            return s
        return s[:limit] + f"\n…（输出过长，已截断，共 {len(s)} 字符）"

    out = _trim(_dec(stdout))
    err = _trim(_dec(stderr))
    lines = []
    if code != 0:
        lines.append(f"[git 退出码 {code}]")
    if out:
        lines.append(out.rstrip("\n"))
    if err:
        lines.append(f"[stderr] {err.rstrip(chr(10))}")
    if not out and not err:
        lines.append("（无输出）")
    return "\n".join(lines)


def to_abs(p):
    if not isinstance(p, str) or not p.strip():
        raise ValueError("路径不能为空")
    return os.path.abspath(p.strip())


def ensure_allowed(abs_path):
    if not ALLOWED_ROOTS:
        return
    if not any(abs_path == r or abs_path.startswith(r + os.sep) for r in ALLOWED_ROOTS):
        raise ValueError(f"路径越界：{abs_path} 不在允许的根目录内（MCP_ALLOWED_ROOTS）")


def ensure_dir(d):
    os.makedirs(d, exist_ok=True)


def read_file_safe(abs_path):
    """一次性读入并立即关闭句柄（不占用文件）；返回 (文本, 是否带 UTF-8 BOM)。"""
    with open(abs_path, "rb") as f:
        data = f.read()
    has_bom = data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")  # utf-8-sig 自动剥离首部 BOM
    return text, has_bom


def make_backup(abs_path):
    """修改前备份上一版，返回备份路径（未备份返回 None）。"""
    if DISABLE_BACKUP or not os.path.exists(abs_path):
        return None
    backup_dir = (
        os.path.abspath(BACKUP_DIR_ENV)
        if BACKUP_DIR_ENV
        else os.path.join(os.path.dirname(abs_path), ".coding-mcp-backup")
    )
    ensure_dir(backup_dir)
    stamp = datetime.datetime.now().isoformat().replace(":", "-").replace(".", "-")
    backup_path = os.path.join(backup_dir, f"{os.path.basename(abs_path)}.{stamp}.bak")
    shutil.copy2(abs_path, backup_path)
    return backup_path


def atomic_write(abs_path, data):
    """原子写入：写临时文件 → fsync → 原子重命名。不锁定目标文件，不产生半写入。"""
    d = os.path.dirname(abs_path)
    ensure_dir(d)
    fd, tmp = tempfile.mkstemp(
        dir=d, prefix="." + os.path.basename(abs_path) + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, abs_path)  # 同目录原子替换
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_file_with_backup(abs_path, text):
    """写入文件：保留原有 BOM，修改前备份，原子落盘。返回备份路径。"""
    ensure_allowed(abs_path)
    has_bom = False
    if os.path.exists(abs_path):
        with open(abs_path, "rb") as f:
            has_bom = f.read().startswith(b"\xef\xbb\xbf")
    backup_path = make_backup(abs_path)
    content = str(text)
    if content.startswith("\ufeff"):
        content = content[1:]
    if has_bom:
        content = "\ufeff" + content
    atomic_write(abs_path, content.encode("utf-8"))
    return backup_path


def log_op(entry):
    """追加一条审计日志（JSONL）。审计失败不影响主流程。"""
    if not AUDIT_LOG:
        return
    try:
        ensure_dir(os.path.dirname(AUDIT_LOG))
        record = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), **entry}
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _backup_note(backup_path):
    if backup_path:
        return f"备份：{backup_path}"
    if DISABLE_BACKUP:
        return "备份：已关闭（MCP_DISABLE_BACKUP）"
    return "备份：新文件，无需备份"


# ---------------------------------------------------------------------------
# MCP 服务器
# ---------------------------------------------------------------------------
mcp = MCPServer("coding-mcp", version=VERSION)


@mcp.tool()
def read_file(path: str) -> str:
    """读取文本文件内容（UTF-8）。采用一次性读入、读后立即释放句柄，绝不占用文件。
    path：要读取的文件绝对路径。"""
    try:
        abs_path = to_abs(path)
        ensure_allowed(abs_path)
        if not os.path.exists(abs_path):
            return f"错误：文件不存在：{abs_path}"
        if os.path.isdir(abs_path):
            return f"错误：这是目录，请改用 list_directory：{abs_path}"
        text, _ = read_file_safe(abs_path)
        return text
    except Exception as e:  # noqa: BLE001
        return f"错误：{e}"


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """将完整内容写入文件。自动保存（立即原子落盘）；修改前自动备份上一版；不占用文件。
    path：目标文件绝对路径。content：要写入的完整内容。"""
    try:
        abs_path = to_abs(path)
        backup_path = write_file_with_backup(abs_path, content)
        log_op({"tool": "write_file", "path": abs_path, "ok": True, "backup": backup_path})
        return f"已写入（自动保存完成）：{abs_path}\n{_backup_note(backup_path)}"
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "write_file", "path": path, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """在文件中查找并替换字符串。修改前自动备份、原子写入、自动保存。
    path：目标文件绝对路径。old_string：要被替换的原文本（需精确匹配）。
    new_string：替换后的新文本。replace_all：是否替换全部（默认 False，要求唯一匹配）。"""
    try:
        abs_path = to_abs(path)
        ensure_allowed(abs_path)
        if not os.path.exists(abs_path):
            log_op({"tool": "edit_file", "path": abs_path, "ok": False, "error": "文件不存在"})
            return f"错误：文件不存在：{abs_path}"
        if old_string == new_string:
            log_op({"tool": "edit_file", "path": abs_path, "ok": False, "error": "old_string 与 new_string 相同"})
            return "错误：old_string 与 new_string 相同，无需修改"

        text, has_bom = read_file_safe(abs_path)
        first = text.find(old_string)
        if first == -1:
            log_op({"tool": "edit_file", "path": abs_path, "ok": False, "error": "未找到 old_string"})
            return "错误：未找到 old_string，文件未被修改"

        if replace_all:
            count = text.count(old_string)
            result = text.replace(old_string, new_string)
        else:
            second = text.find(old_string, first + len(old_string))
            if second != -1:
                log_op({"tool": "edit_file", "path": abs_path, "ok": False, "error": "old_string 匹配多处"})
                return "错误：old_string 匹配到多处，请提供更长的上下文使其唯一，或设置 replace_all=True"
            count = 1
            result = text[:first] + new_string + text[first + len(old_string):]

        backup_path = make_backup(abs_path)
        clean = result[1:] if result.startswith("\ufeff") else result
        out = ("\ufeff" + clean) if has_bom else clean
        atomic_write(abs_path, out.encode("utf-8"))
        log_op({"tool": "edit_file", "path": abs_path, "ok": True, "count": count, "backup": backup_path})
        return f"已编辑（替换 {count} 处，自动保存完成）：{abs_path}\n{_backup_note(backup_path)}"
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "edit_file", "path": path, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def list_directory(path: str) -> str:
    """列出目录下的条目（区分文件/目录/链接）。path：目录绝对路径。"""
    try:
        abs_path = to_abs(path)
        ensure_allowed(abs_path)
        if not os.path.exists(abs_path):
            return f"错误：目录不存在：{abs_path}"
        if not os.path.isdir(abs_path):
            return f"错误：不是目录：{abs_path}"
        entries = sorted(os.listdir(abs_path))
        if not entries:
            return f"（空目录）{abs_path}"
        lines = []
        for name in entries:
            full = os.path.join(abs_path, name)
            if os.path.islink(full):
                tag = "[链接]"
            elif os.path.isdir(full):
                tag = "[目录]"
            else:
                tag = "[文件]"
            lines.append(f"{tag} {name}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"错误：{e}"


@mcp.tool()
def search_files(directory: str, pattern: str, glob: str = "") -> str:
    """在目录中递归搜索包含指定文本的行，返回「相对路径:行号: 内容」。跳过 node_modules/.git 与二进制文件。
    directory：搜索根目录绝对路径。pattern：要搜索的文本（字面量匹配）。glob：可选文件名过滤（如 *.js）。"""
    try:
        root = to_abs(directory)
        ensure_allowed(root)
        if not os.path.exists(root):
            return f"错误：目录不存在：{root}"
        needle = str(pattern)
        matches = []

        def walk(d):
            try:
                names = os.listdir(d)
            except OSError:
                return
            for name in names:
                full = os.path.join(d, name)
                if os.path.isdir(full):
                    if name in SKIP_DIRS:
                        continue
                    walk(full)
                elif os.path.isfile(full):
                    if glob and not fnmatch.fnmatch(name, glob):
                        continue
                    try:
                        with open(full, "rb") as f:
                            data = f.read()
                    except OSError:
                        continue
                    if b"\x00" in data[:8192]:
                        continue
                    text = data.decode("utf-8", errors="replace")
                    for i, line in enumerate(text.splitlines(), 1):
                        if needle in line:
                            matches.append(f"{os.path.relpath(full, root)}:{i}: {line.rstrip()}")

        walk(root)
        if not matches:
            return "未找到匹配"
        shown = matches[:200]
        suffix = f"\n…（共 {len(matches)} 条，仅显示前 200 条）" if len(matches) > 200 else ""
        return "\n".join(shown) + suffix
    except Exception as e:  # noqa: BLE001
        return f"错误：{e}"


@mcp.tool()
def diff_files(path_a: str, path_b: str, context: int = 3) -> str:
    """对比两个文件的差异，输出 unified diff 格式。path_a：第一个文件绝对路径（旧版）。
    path_b：第二个文件绝对路径（新版）。context：上下文行数（可选，默认 3）。"""
    try:
        a = to_abs(path_a)
        b = to_abs(path_b)
        ensure_allowed(a)
        ensure_allowed(b)
        if not os.path.exists(a):
            return f"错误：文件不存在：{a}"
        if not os.path.exists(b):
            return f"错误：文件不存在：{b}"

        # 都按文本读取（BOM 剥离 + 按行拆分），保留行尾
        ta, _ = read_file_safe(a)
        tb, _ = read_file_safe(b)
        lines_a = ta.splitlines(keepends=True)
        lines_b = tb.splitlines(keepends=True)

        n = max(1, int(context)) if context else 3
        diff = difflib.unified_diff(
            lines_a, lines_b, fromfile=path_a, tofile=path_b, n=n
        )
        text = "".join(diff)

        log_op(
            {
                "tool": "diff_files",
                "path_a": a,
                "path_b": b,
                "ok": True,
                "changed": text != "",
            }
        )

        if text == "":
            return "两个文件内容相同，无差异。"
        # 输出过长截断
        limit = 8000
        if len(text) > limit:
            text = text[:limit] + f"\n…（diff 过长，已截断，共 {len(text)} 字符）"
        return text
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "diff_files", "path_a": path_a, "path_b": path_b, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def git(args: list[str], cwd: str = "") -> str:
    """执行任意 git 子命令（安全封装：禁 shell、参数列表传递，避免命令注入）。
    args：git 参数列表，如 ["status", "--short"] 或 ["log", "--oneline", "-5"]。
    cwd：仓库目录（可选，默认服务器进程当前目录）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else None
        if workdir is not None and not os.path.isdir(workdir):
            return f"错误：目录不存在：{workdir}"
        argv = [str(x) for x in args]
        log_op({"tool": "git", "args": argv, "cwd": workdir, "ok": None})
        out = _git_output(argv, cwd=workdir)
        log_op({"tool": "git", "args": argv, "cwd": workdir, "ok": True})
        return out
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "git", "args": args, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def git_status(cwd: str = "") -> str:
    """查看 git 工作区状态（等价于 git status --short --branch）。cwd：仓库目录（可选）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else None
        log_op({"tool": "git_status", "cwd": workdir, "ok": None})
        out = _git_output(["status", "--short", "--branch"], cwd=workdir)
        log_op({"tool": "git_status", "cwd": workdir, "ok": True})
        return out
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "git_status", "cwd": cwd, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def git_log(count: int = 20, cwd: str = "") -> str:
    """查看 git 提交历史（oneline 格式）。count：显示条数（可选，默认 20）。cwd：仓库目录（可选）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else None
        n = max(1, int(count)) if count else 20
        log_op({"tool": "git_log", "count": n, "cwd": workdir, "ok": None})
        out = _git_output(["log", "--oneline", f"-{n}"], cwd=workdir)
        log_op({"tool": "git_log", "count": n, "cwd": workdir, "ok": True})
        return out
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "git_log", "cwd": cwd, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def run_command(command: str, cwd: str = "", timeout: float = 0) -> str:
    """执行一条命令行并返回退出码、标准输出、标准错误。默认关闭，需设置 MCP_ENABLE_EXEC=1 才可用。
    command：要执行的命令（字符串，交给系统 shell 解析）。cwd：工作目录（可选，默认继承服务器进程目录）。
    timeout：超时秒数（可选，默认 30，不得超过服务器上限 MCP_EXEC_TIMEOUT）。"""
    if not EXEC_ENABLED:
        return "错误：命令执行未开启。请在启动服务器时设置 MCP_ENABLE_EXEC=1。"
    try:
        cmd = str(command)
        if not cmd.strip():
            return "错误：命令不能为空"

        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else None
        if workdir is not None and not os.path.isdir(workdir):
            return f"错误：工作目录不存在：{workdir}"

        # 超时：取请求值，未指定用上限，且不得超过上限
        t = float(timeout) if timeout else EXEC_TIMEOUT_MAX
        t = min(max(t, 0.1), EXEC_TIMEOUT_MAX)

        log_op({"tool": "run_command", "command": cmd, "cwd": workdir, "ok": None})

        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=workdir,
            capture_output=True,
            timeout=t,
        )

        # 解码输出（容忍非 UTF-8），并截断过长输出防止撑爆上下文
        def _decode(b):
            try:
                return b.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return str(b)

        def _trim(s, limit=8000):
            if len(s) <= limit:
                return s
            return s[:limit] + f"\n…（输出过长，已截断，共 {len(s)} 字符）"

        stdout = _trim(_decode(proc.stdout))
        stderr = _trim(_decode(proc.stderr))

        log_op(
            {
                "tool": "run_command",
                "command": cmd,
                "cwd": workdir,
                "ok": True,
                "exit_code": proc.returncode,
            }
        )

        lines = [f"退出码：{proc.returncode}"]
        if stdout:
            lines.append(f"--- 标准输出 ---\n{stdout}")
        if stderr:
            lines.append(f"--- 标准错误 ---\n{stderr}")
        if not stdout and not stderr:
            lines.append("（无输出）")
        return "\n".join(lines)
    except subprocess.TimeoutExpired:
        log_op({"tool": "run_command", "command": command, "ok": False, "error": "超时"})
        return f"错误：命令执行超时（>{t} 秒），已终止"
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "run_command", "command": command, "ok": False, "error": str(e)})
        return f"错误：{e}"


if __name__ == "__main__":
    sys.stderr.write(
        f"[coding-mcp] 已启动 v{VERSION}"
        f"（备份：{'关' if DISABLE_BACKUP else '开'}，审计：{AUDIT_LOG or '关'}，"
        f"执行：{'开' if EXEC_ENABLED else '关'}）\n"
    )
    mcp.run(transport="stdio")
