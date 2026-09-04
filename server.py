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

服务注册表（GUI/后台服务托管）：
  ~/.coding-mcp/services.json  注册表文件（JSON 列表）
  ~/.coding-mcp/services/<name>/  每个服务的 pid/log 子目录
  service_clean 扫描注册表，清理已死进程的孤儿条目

数据库（可选，URL 格式，未设置则对应工具不可用）：
  MCP_MYSQL_URL=mysql://user:pass@host:3306/dbname
  MCP_PGSQL_URL=postgresql://user:pass@host:5432/dbname
  MCP_REDIS_URL=redis://:pass@host:6379/0
  MCP_DB_ALLOW_WRITE=1          允许数据库写操作（默认只读，需显式开启）
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
import re

from mcp.server.mcpserver import MCPServer

VERSION = "1.2.0"

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

# 数据库连接（可选，URL 格式，未设置则对应工具不可用）
MYSQL_URL = os.environ.get("MCP_MYSQL_URL", "").strip()
PGSQL_URL = os.environ.get("MCP_PGSQL_URL", "").strip()
REDIS_URL = os.environ.get("MCP_REDIS_URL", "").strip()

# 数据库写操作开关：默认只读，需设置 MCP_DB_ALLOW_WRITE=1 才允许写
DB_ALLOW_WRITE = os.environ.get("MCP_DB_ALLOW_WRITE", "") in ("1", "true", "True")

# 服务注册表：存放 ~/.coding-mcp/services.json + 每个服务的 pid/log 子目录
# 默认沙箱在用户家目录下，不受 MCP_ALLOWED_ROOTS 约束（属于服务器内部状态，非用户项目）
SERVICES_ROOT = os.path.join(str(os.path.expanduser("~")), ".coding-mcp", "services")
SERVICES_REGISTRY = os.path.join(str(os.path.expanduser("~")), ".coding-mcp", "services.json")

# 服务名白名单：仅允许字母/数字/./_/-，防止 shell/路径注入
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

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


def run_cmd(cmd, cwd=None, timeout=60.0):
    """通用命令执行：禁 shell、参数列表，返回 (returncode, stdout_text, stderr_text)。"""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, shell=False)
    except FileNotFoundError:
        raise RuntimeError(f"未找到可执行文件：{cmd[0] if cmd else ''}")
    def _dec(b):
        try:
            return b.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return str(b)
    return proc.returncode, _dec(proc.stdout), _dec(proc.stderr)


def _fmt_cmd_result(code, stdout, stderr, limit=8000):
    """格式化命令执行结果为文本。"""
    def _trim(s, limit=8000):
        if len(s) <= limit:
            return s
        return s[:limit] + f"\n…（输出过长，已截断，共 {len(s)} 字符）"
    out = _trim(stdout)
    err = _trim(stderr)
    lines = [f"退出码：{code}"]
    if out:
        lines.append(f"--- 标准输出 ---\n{out.rstrip(chr(10))}")
    if err:
        lines.append(f"--- 标准错误 ---\n{err.rstrip(chr(10))}")
    if not out and not err:
        lines.append("（无输出）")
    return "\n".join(lines)


# 项目探测：常见包管理器/测试框架/配置文件的标记文件
_PM_MARKERS = [
    ("pnpm", "pnpm-lock.yaml"),
    ("yarn", "yarn.lock"),
    ("npm", "package-lock.json"),
    ("npm", "package.json"),
    ("poetry", "pyproject.toml"),
    ("uv", "uv.lock"),
    ("pip", "requirements.txt"),
    ("cargo", "Cargo.toml"),
    ("go", "go.mod"),
]

_TEST_MARKERS = [
    ("pytest", "pytest.ini"),
    ("pytest", "tox.ini"),
    ("jest", "jest.config.js"),
    ("vitest", "vitest.config.ts"),
    ("go test", "go.mod"),
    ("cargo test", "Cargo.toml"),
]


def detect_project(root):
    """探测项目根目录的语言/包管理器/测试框架。返回 dict。"""
    info = {"root": root, "package_manager": None, "language": None, "test": None}
    names = set(os.listdir(root)) if os.path.isdir(root) else set()

    # 包管理器（优先级按 _PM_MARKERS 顺序）
    for pm, marker in _PM_MARKERS:
        if marker in names:
            info["package_manager"] = pm
            break

    # 语言启发式
    langs = []
    if any(n in names for n in ("package.json", "tsconfig.json", "jsconfig.json", "vite.config.ts")):
        langs.append("TypeScript/JavaScript")
    if any(n in names for n in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile")):
        langs.append("Python")
    if "go.mod" in names:
        langs.append("Go")
    if "Cargo.toml" in names:
        langs.append("Rust")
    if "pom.xml" in names or "build.gradle" in names:
        langs.append("Java/JVM")
    if not langs and info["package_manager"]:
        langs.append(info["package_manager"])
    info["language"] = " / ".join(langs) or "未知"

    # 测试框架
    for tf, marker in _TEST_MARKERS:
        if marker in names:
            info["test"] = tf
            break
    return info


def dev_context_dir(root):
    """返回项目 dev-context 目录路径（不存在返回 None）。"""
    candidates = [
        os.path.join(root, ".dev-context"),
        os.path.join(root, ".coding-mcp", "context"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def read_dev_context(root):
    """加载开发上下文：返回 {文件名: 内容}。"""
    d = dev_context_dir(root)
    if not d:
        return {}
    result = {}
    for name in sorted(os.listdir(d)):
        full = os.path.join(d, name)
        if os.path.isfile(full):
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    result[name] = f.read()
            except OSError:
                continue
    return result


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
# 数据库辅助函数
# ---------------------------------------------------------------------------
import re
import shlex
from urllib.parse import urlparse, unquote

# SQL 只读关键字白名单：首关键字命中则视为只读，其余视为写操作
_SQL_READONLY_KW = {"SELECT", "SHOW", "DESC", "DESCRIBE", "EXPLAIN", "PRAGMA"}

# Redis 只读命令白名单：默认只读模式下仅允许这些命令
_REDIS_READONLY_CMDS = {
    "GET", "MGET", "KEYS", "SCAN", "EXISTS", "TYPE", "TTL", "PTTL", "STRLEN",
    "HGET", "HMGET", "HGETALL", "HKEYS", "HVALS", "HLEN", "HEXISTS", "HSCAN",
    "LRANGE", "LLEN", "LINDEX", "LPOS",
    "SMEMBERS", "SCARD", "SISMEMBER", "SSCAN", "SRANDMEMBER",
    "ZRANGE", "ZRANGEBYSCORE", "ZREVRANGE", "ZREVRANGEBYSCORE", "ZSCORE",
    "ZCARD", "ZCOUNT", "ZRANK", "ZREVRANK", "ZSCAN", "ZLEXCOUNT",
    "DBSIZE", "INFO", "PING", "ECHO", "RANDOMKEY",
    "BITCOUNT", "BITPOS", "GETRANGE", "PFCOUNT", "XLEN", "XRANGE", "XREVRANGE",
    "GEOPOS", "GEODIST", "GEOHASH", "GEOSEARCH",
}

_DB_CONFIGS = {
    "mysql": {"url": MYSQL_URL, "scheme": "mysql", "default_port": 3306},
    "pgsql": {"url": PGSQL_URL, "scheme": "postgresql", "default_port": 5432},
}


def _parse_db_url(url):
    """解析数据库 URL，返回 (host, port, user, password, dbname)。"""
    p = urlparse(url)
    host = p.hostname or "127.0.0.1"
    port = p.port or 0
    user = unquote(p.username) if p.username else ""
    password = unquote(p.password) if p.password else ""
    db = (p.path or "/").lstrip("/")
    return host, port, user, password, db


def _sql_is_readonly(sql):
    m = re.match(r"^\s*([A-Za-z]+)", sql)
    if not m:
        return False
    return m.group(1).upper() in _SQL_READONLY_KW


def _fmt_table(cols, rows, limit=8000):
    """把列名 + 行数据格式化为对齐文本表格。"""
    cols = [str(c) for c in cols]
    rows = [[str(c) if c is not None else "NULL" for c in r] for r in rows]
    widths = [len(c) for c in cols]
    for r in rows:
        for i, v in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(v))
    lines = [" | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))]
    lines.append("-+-".join("-" * w for w in widths))
    for r in rows:
        lines.append(" | ".join(v.ljust(widths[i]) for i, v in enumerate(r) if i < len(widths)))
    out = "\n".join(lines)
    if len(out) > limit:
        out = out[:limit] + f"\n...（输出截断，共 {len(rows)} 行）"
    return out


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
def git_diff(cwd: str = "", staged: bool = False) -> str:
    """查看工作区差异。cwd：仓库目录（可选）。staged：True 看暂存区差异，False 看未暂存差异（默认）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else None
        args = ["diff", "--cached"] if staged else ["diff"]
        log_op({"tool": "git_diff", "cwd": workdir, "staged": staged, "ok": None})
        out = _git_output(args, cwd=workdir)
        log_op({"tool": "git_diff", "cwd": workdir, "staged": staged, "ok": True})
        return out
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "git_diff", "cwd": cwd, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def git_add(paths: list[str], cwd: str = "") -> str:
    """暂存文件（git add）。paths：要暂存的文件/目录路径列表（相对仓库，如 ["."] 暂存全部）。
    cwd：仓库目录（可选）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else None
        p = [str(x) for x in paths] if paths else ["."]
        log_op({"tool": "git_add", "paths": p, "cwd": workdir, "ok": None})
        out = _git_output(["add", *p], cwd=workdir)
        log_op({"tool": "git_add", "paths": p, "cwd": workdir, "ok": True})
        return out
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "git_add", "cwd": cwd, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def git_commit(message: str, cwd: str = "") -> str:
    """提交暂存的改动（git commit -m）。message：提交说明。cwd：仓库目录（可选）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else None
        if not message or not str(message).strip():
            return "错误：提交说明不能为空"
        log_op({"tool": "git_commit", "message": message, "cwd": workdir, "ok": None})
        out = _git_output(["commit", "-m", str(message)], cwd=workdir)
        log_op({"tool": "git_commit", "message": message, "cwd": workdir, "ok": True})
        return out
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "git_commit", "cwd": cwd, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def install_deps(cwd: str = "") -> str:
    """自动探测包管理器并安装依赖（pnpm/yarn/npm/poetry/uv/pip/cargo/go）。cwd：项目目录（可选）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else os.getcwd()
        if not os.path.isdir(workdir):
            return f"错误：目录不存在：{workdir}"
        info = detect_project(workdir)
        pm = info["package_manager"]
        if not pm:
            return f"错误：未识别到包管理器（目录 {workdir}）"
        cmds = {
            "pnpm": ["pnpm", "install"],
            "yarn": ["yarn", "install"],
            "npm": ["npm", "install"],
            "poetry": ["poetry", "install"],
            "uv": ["uv", "sync"],
            "pip": ["pip", "install", "-r", "requirements.txt"],
            "cargo": ["cargo", "build"],
            "go": ["go", "mod", "download"],
        }
        cmd = cmds.get(pm)
        if not cmd:
            return f"错误：不支持的包管理器 {pm}"
        log_op({"tool": "install_deps", "pm": pm, "cwd": workdir, "ok": None})
        code, out, err = run_cmd(cmd, cwd=workdir, timeout=180.0)
        log_op({"tool": "install_deps", "pm": pm, "cwd": workdir, "ok": code == 0})
        return f"[包管理器 {pm}] 命令：{' '.join(cmd)}\n" + _fmt_cmd_result(code, out, err)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "install_deps", "cwd": cwd, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def run_tests(cwd: str = "") -> str:
    """自动探测测试框架并运行测试（pytest/jest/vitest/go test/cargo test）。cwd：项目目录（可选）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else os.getcwd()
        if not os.path.isdir(workdir):
            return f"错误：目录不存在：{workdir}"
        info = detect_project(workdir)
        tf = info["test"]
        if not tf:
            return f"错误：未识别到测试框架（目录 {workdir}）"
        cmds = {
            "pytest": ["python", "-m", "pytest", "-q"],
            "jest": ["npx", "jest"],
            "vitest": ["npx", "vitest", "run"],
            "go test": ["go", "test", "./..."],
            "cargo test": ["cargo", "test"],
        }
        cmd = cmds.get(tf)
        if not cmd:
            return f"错误：不支持的测试框架 {tf}"
        log_op({"tool": "run_tests", "test": tf, "cwd": workdir, "ok": None})
        code, out, err = run_cmd(cmd, cwd=workdir, timeout=180.0)
        log_op({"tool": "run_tests", "test": tf, "cwd": workdir, "ok": code == 0})
        return f"[测试 {tf}] 命令：{' '.join(cmd)}\n" + _fmt_cmd_result(code, out, err)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "run_tests", "cwd": cwd, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def run_lint(cwd: str = "") -> str:
    """自动探测并运行代码检查/格式化（ruff/black/eslint/prettier/go vet）。cwd：项目目录（可选）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else os.getcwd()
        if not os.path.isdir(workdir):
            return f"错误：目录不存在：{workdir}"
        names = set(os.listdir(workdir))
        candidates = []
        if "ruff.toml" in names or "ruff" in names or any(n.startswith(".ruff") for n in names):
            candidates.append(["ruff", "check", "."])
        if "pyproject.toml" in names:
            candidates.append(["ruff", "check", "."])
        if ".eslintrc" in names or "eslint.config.js" in names or "eslint.config.mjs" in names:
            candidates.append(["npx", "eslint", "."])
        if ".prettierrc" in names or "prettier.config.js" in names:
            candidates.append(["npx", "prettier", "--check", "."])
        if "go.mod" in names:
            candidates.append(["go", "vet", "./..."])
        if not candidates:
            # 兜底：Python 用 py_compile，JS 无则提示
            if "package.json" in names:
                candidates.append(["npx", "prettier", "--check", "."])
            else:
                return "错误：未识别到 lint/格式化工具（ruff/eslint/prettier/go vet）"
        cmd = candidates[0]
        log_op({"tool": "run_lint", "cmd": cmd, "cwd": workdir, "ok": None})
        code, out, err = run_cmd(cmd, cwd=workdir, timeout=120.0)
        log_op({"tool": "run_lint", "cmd": cmd, "cwd": workdir, "ok": code == 0})
        return f"[lint] 命令：{' '.join(cmd)}\n" + _fmt_cmd_result(code, out, err)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "run_lint", "cwd": cwd, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def get_project_info(cwd: str = "") -> str:
    """识别项目概况：语言、包管理器、测试框架、目录结构、依赖清单。
    cwd：项目目录（可选，默认当前目录）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else os.getcwd()
        if not os.path.isdir(workdir):
            return f"错误：目录不存在：{workdir}"
        info = detect_project(workdir)
        lines = [
            f"项目根目录：{workdir}",
            f"识别语言：{info['language']}",
            f"包管理器：{info['package_manager'] or '未识别'}",
            f"测试框架：{info['test'] or '未识别'}",
        ]

        # 目录结构（两层，跳过常见忽略目录）
        def _tree(d, depth=0, max_depth=2):
            if depth > max_depth:
                return
            try:
                entries = sorted(os.listdir(d))
            except OSError:
                return
            for name in entries:
                if name in SKIP_DIRS or name.startswith("."):
                    continue
                full = os.path.join(d, name)
                if os.path.isdir(full):
                    lines.append("  " * depth + f"{name}/")
                    _tree(full, depth + 1, max_depth)
                else:
                    lines.append("  " * depth + name)

        lines.append("\n目录结构：")
        _tree(workdir)
        log_op({"tool": "get_project_info", "cwd": workdir, "ok": True})
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "get_project_info", "cwd": cwd, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def load_dev_context(cwd: str = "") -> str:
    """加载项目预制开发上下文（技术文档/开发规范/构建规范/进度等）。开发前应优先调用。
    上下文放在项目根的 .dev-context/ 目录下（.md 或 .txt），返回全部文件内容。
    cwd：项目目录（可选，默认当前目录）。"""
    try:
        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else os.getcwd()
        ctx = read_dev_context(workdir)
        if not ctx:
            return (
                "未找到预制开发上下文。请在项目根目录创建 .dev-context/ 目录，"
                "放入技术文档、开发规范、构建规范、开发进度等 .md 文件，"
                "开发时我会自动读取这些文件。"
            )
        parts = []
        for name, content in ctx.items():
            parts.append(f"===== {name} =====\n{content}")
        log_op({"tool": "load_dev_context", "cwd": workdir, "ok": True, "files": list(ctx.keys())})
        return "\n\n".join(parts)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "load_dev_context", "cwd": cwd, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def run_command(command: str, cwd: str = "", timeout: float = 0) -> str:
    """执行一条命令行并返回退出码、标准输出、标准错误。默认关闭，需设置 MCP_ENABLE_EXEC=1 才可用。
    command：要执行的命令（字符串，交给系统 shell 解析）。cwd：工作目录（可选，默认继承服务器进程目录）。
    timeout：超时秒数（可选，默认 30，不得超过服务器上限 MCP_EXEC_TIMEOUT）。
    ⚠️ 本工具只适合"有明确结束点"的命令（编译、测试、git 等）。
    拒绝后台化语法（start / nohup / setsid / disown / 末尾 & 等），请改用 launch_gui 或 service_start。"""
    if not EXEC_ENABLED:
        return "错误：命令执行未开启。请在启动服务器时设置 MCP_ENABLE_EXEC=1。"
    try:
        cmd = str(command)
        if not cmd.strip():
            return "错误：命令不能为空"

        workdir = os.path.abspath(str(cwd)) if str(cwd).strip() else None
        if workdir is not None and not os.path.isdir(workdir):
            return f"错误：工作目录不存在：{workdir}"

        # 后台化语法黑名单：从源头避免误用
        bg_reason = _command_wants_background(cmd)
        if bg_reason:
            log_op({"tool": "run_command", "command": cmd, "ok": False, "error": "后台化语法"})
            return f"错误：{bg_reason}"

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


def _fmt_redis_result(result, limit=8000):
    """格式化 Redis 返回值（str/list/dict/None/int）。"""
    if result is None:
        return "(nil)"
    if isinstance(result, (list, tuple)):
        if not result:
            return "(empty list)"
        out = "\n".join(str(x) for x in result)
    elif isinstance(result, dict):
        if not result:
            return "(empty hash)"
        out = "\n".join(f"{k}: {v}" for k, v in result.items())
    else:
        out = str(result)
    if len(out) > limit:
        out = out[:limit] + f"\n…（输出过长，已截断，共 {len(out)} 字符）"
    return out


@mcp.tool()
def db_query(kind: str, sql: str, limit: int = 100) -> str:
    """在 MySQL / PostgreSQL 上执行 SQL 并返回结果。
    kind：数据库类型，"mysql" 或 "pgsql"（连接信息由 MCP_MYSQL_URL / MCP_PGSQL_URL 环境变量提供）。
    sql：要执行的 SQL 语句。默认只读，INSERT/UPDATE/DELETE/DDL 需设置 MCP_DB_ALLOW_WRITE=1。
    limit：SELECT 返回行数上限（可选，默认 100）。"""
    try:
        kind = (kind or "").strip().lower()
        cfg = _DB_CONFIGS.get(kind)
        if not cfg:
            return f"错误：不支持的数据库类型 {kind}（仅支持 mysql / pgsql）"
        if not cfg["url"]:
            return f"错误：未配置 {kind} 连接（请设置 MCP_{kind.upper()}_URL）"
        sql = (sql or "").strip()
        if not sql:
            return "错误：SQL 不能为空"

        if not _sql_is_readonly(sql) and not DB_ALLOW_WRITE:
            log_op({"tool": "db_query", "kind": kind, "sql": sql, "ok": False, "error": "写操作被拒（只读模式）"})
            return "错误：该 SQL 是写操作，已被只读模式拦截。如需执行请设置 MCP_DB_ALLOW_WRITE=1。"

        host, port, user, password, db = _parse_db_url(cfg["url"])
        port = port or cfg["default_port"]

        log_op({"tool": "db_query", "kind": kind, "sql": sql, "ok": None})

        if kind == "mysql":
            import pymysql
            conn = pymysql.connect(
                host=host, port=port, user=user, password=password,
                database=db or None, charset="utf8mb4", connect_timeout=5,
            )
        else:
            import psycopg2
            conn = psycopg2.connect(
                host=host, port=port, user=user, password=password,
                dbname=db or "postgres", connect_timeout=5,
            )
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description:  # 有结果集（SELECT/SHOW/EXPLAIN 等）
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchmany(max(1, int(limit)))
                    log_op({"tool": "db_query", "kind": kind, "ok": True, "rows": len(rows)})
                    return f"{_fmt_table(cols, rows)}\n（返回 {len(rows)} 行，limit={limit}）"
                else:  # 写操作 / 无结果集
                    conn.commit()
                    n = cur.rowcount
                    log_op({"tool": "db_query", "kind": kind, "ok": True, "affected": n})
                    return f"执行成功，影响 {n} 行"
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "db_query", "kind": kind, "sql": sql, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def db_tables(kind: str) -> str:
    """列出数据库中的所有表。kind：数据库类型，"mysql" 或 "pgsql"。"""
    try:
        kind = (kind or "").strip().lower()
        cfg = _DB_CONFIGS.get(kind)
        if not cfg:
            return f"错误：不支持的数据库类型 {kind}（仅支持 mysql / pgsql）"
        if not cfg["url"]:
            return f"错误：未配置 {kind} 连接（请设置 MCP_{kind.upper()}_URL）"
        host, port, user, password, db = _parse_db_url(cfg["url"])
        port = port or cfg["default_port"]
        log_op({"tool": "db_tables", "kind": kind, "ok": None})
        if kind == "mysql":
            import pymysql
            conn = pymysql.connect(
                host=host, port=port, user=user, password=password,
                database=db or None, charset="utf8mb4", connect_timeout=5,
            )
            sql = "SHOW TABLES"
        else:
            import psycopg2
            conn = psycopg2.connect(
                host=host, port=port, user=user, password=password,
                dbname=db or "postgres", connect_timeout=5,
            )
            sql = "SELECT tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') ORDER BY tablename"
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                tables = [r[0] for r in cur.fetchall()]
            log_op({"tool": "db_tables", "kind": kind, "ok": True, "count": len(tables)})
            return "\n".join(tables) if tables else "（无表）"
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "db_tables", "kind": kind, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def db_schema(kind: str, table: str) -> str:
    """查看表结构（字段名 / 类型 / 是否可空 / 默认值等）。kind：数据库类型，"mysql" 或 "pgsql"。table：表名。"""
    try:
        kind = (kind or "").strip().lower()
        cfg = _DB_CONFIGS.get(kind)
        if not cfg:
            return f"错误：不支持的数据库类型 {kind}（仅支持 mysql / pgsql）"
        if not cfg["url"]:
            return f"错误：未配置 {kind} 连接（请设置 MCP_{kind.upper()}_URL）"
        table = (table or "").strip()
        if not re.match(r"^[A-Za-z0-9_]+$", table):
            return f"错误：非法表名 {table}（仅允许字母/数字/下划线）"
        host, port, user, password, db = _parse_db_url(cfg["url"])
        port = port or cfg["default_port"]
        log_op({"tool": "db_schema", "kind": kind, "table": table, "ok": None})
        if kind == "mysql":
            import pymysql
            conn = pymysql.connect(
                host=host, port=port, user=user, password=password,
                database=db or None, charset="utf8mb4", connect_timeout=5,
            )
            cols = ["Field", "Type", "Null", "Key", "Default", "Extra"]
            with conn.cursor() as cur:
                cur.execute(f"DESCRIBE `{table}`")
                rows = cur.fetchall()
            conn.close()
            log_op({"tool": "db_schema", "kind": kind, "table": table, "ok": True})
            return _fmt_table(cols, rows)
        else:
            import psycopg2
            conn = psycopg2.connect(
                host=host, port=port, user=user, password=password,
                dbname=db or "postgres", connect_timeout=5,
            )
            cols = ["column_name", "data_type", "is_nullable", "column_default"]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position",
                    (table,),
                )
                rows = cur.fetchall()
            conn.close()
            log_op({"tool": "db_schema", "kind": kind, "table": table, "ok": True})
            return _fmt_table(cols, rows)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "db_schema", "kind": kind, "table": table, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def redis_exec(command: str) -> str:
    """执行 Redis 命令并返回结果。
    command：完整命令字符串，如 "GET foo"、"KEYS *"、"HGETALL myhash"。
    连接信息由 MCP_REDIS_URL 环境变量提供。默认只读，写命令（SET/DEL 等）需设置 MCP_DB_ALLOW_WRITE=1。"""
    try:
        if not REDIS_URL:
            return "错误：未配置 Redis 连接（请设置 MCP_REDIS_URL）"
        command = (command or "").strip()
        if not command:
            return "错误：命令不能为空"
        parts = shlex.split(command, posix=False)
        if not parts:
            return "错误：命令不能为空"
        cmd = parts[0].upper()
        if cmd not in _REDIS_READONLY_CMDS and not DB_ALLOW_WRITE:
            log_op({"tool": "redis_exec", "command": command, "ok": False, "error": "写命令被拒（只读模式）"})
            return f"错误：命令 {cmd} 是写操作，已被只读模式拦截。如需执行请设置 MCP_DB_ALLOW_WRITE=1。"

        import redis as redis_lib
        r = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=5)
        log_op({"tool": "redis_exec", "command": command, "ok": None})
        result = r.execute_command(*parts)
        log_op({"tool": "redis_exec", "command": command, "ok": True})
        return _fmt_redis_result(result)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "redis_exec", "command": command, "ok": False, "error": str(e)})
        return f"错误：{e}"


# ---------------------------------------------------------------------------
# GUI / 后台服务：解决 run_command 不适合长驻/无结束点进程的问题
# ---------------------------------------------------------------------------
def _validate_service_name(name):
    """校验服务名，返回规范化结果；不合法抛 ValueError。"""
    n = (name or "").strip()
    if not _NAME_PATTERN.match(n):
        raise ValueError(
            f"服务名不合法：{name!r}（仅允许字母/数字/./_/-，最长 64 字符）"
        )
    return n


def _service_dir(name):
    return os.path.join(SERVICES_ROOT, name)


def _pid_file(name):
    return os.path.join(_service_dir(name), "pid")


def _log_path(name, stream):
    """stream: 'stdout' 或 'stderr'。"""
    return os.path.join(_service_dir(name), "logs", f"{stream}.log")


def _read_registry():
    if not os.path.exists(SERVICES_REGISTRY):
        return []
    try:
        with open(SERVICES_REGISTRY, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_registry(services):
    """原子写入注册表（临时文件 + fsync + 替换），与文件工具同一安全策略。"""
    ensure_dir(os.path.dirname(SERVICES_REGISTRY))
    tmp = SERVICES_REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(services, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SERVICES_REGISTRY)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _pid_alive(pid):
    """跨平台检测进程是否存活。Windows 上 PID 被复用可能误判（短窗口），服务场景够用。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            # text=False：避开 subprocess 内部 reader 线程的 UTF-8 解码（tasklist 输出含非 UTF-8 字节时会崩）
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, timeout=5,
            ).stdout.decode("utf-8", errors="replace")
            # tasklist 在找不到时输出 "INFO: No tasks are running..."；找到则包含 pid 字段
            if "INFO:" in out:
                return False
            return f'"{pid}"' in out or f",{pid}," in out or f'"{pid}"' in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但当前用户无权访问信号
    except OSError:
        return False


def _kill_tree(pid, force=False, timeout=10.0):
    """终止进程树（先温和，超时再强制）。返回是否成功结束。
    Windows 注意：taskkill 无 /F 只发 WM_CLOSE，命令行/控制台进程大多忽略；
    所以首次尝试给一个较短超时，触底即转 /F。"""
    import time

    if not _pid_alive(pid):
        return True

    # Windows 上温和路径几乎等于无效——只给 2s；Unix 上给完整 timeout 给进程清理机会
    first_wait = 2.0 if (sys.platform == "win32" and not force) else timeout

    if sys.platform == "win32":
        flags = ["/PID", str(pid), "/T"]
        if force:
            flags.append("/F")
        try:
            subprocess.run(["taskkill", *flags], capture_output=True, timeout=first_wait)
        except (subprocess.TimeoutExpired, OSError):
            pass
    else:
        import signal
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False

    deadline = time.time() + first_wait
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)

    if not force:
        return _kill_tree(pid, force=True, timeout=timeout)
    return not _pid_alive(pid)


def _parse_command(command):
    """跨平台解析命令字符串为参数列表。
    Windows 路径含反斜杠，直接 shlex(posix=True) 会把 \\ 当转义符；
    先 double-up 反斜杠，shlex 当字面量处理后还原成原路径。
    Windows 含空格路径用引号包裹即可。"""
    if sys.platform == "win32":
        normalized = command.replace("\\", "\\\\")
        return shlex.split(normalized, posix=True)
    return shlex.split(command, posix=True)


# 命令首词黑名单：这些命令会启动后台进程/守护/终端复用器，不适合 run_command（带结束点的）
_BACKGROUND_FIRST_WORDS = {
    "start",      # Windows cmd 的 start：启动独立窗口
    "nohup",      # Unix：脱离终端
    "setsid",     # Unix：新建会话
    "disown",     # Unix：把进程脱离作业
    "screen",     # 终端复用器
    "tmux",       # 终端复用器
    "daemonize",  # 类 Unix 守护进程工具
}


def _command_wants_background(cmd):
    """检测命令是否尝试以后台/守护方式运行。返回错误说明（不匹配返回 None）。"""
    s = (cmd or "").lstrip()
    if not s:
        return None
    # 首词匹配：只取第一段空白前的 token，去掉路径前缀
    m = re.match(r"^([^\s|&;]+)", s)
    if m:
        first = os.path.basename(m.group(1)).lower()
        # Windows 上 start 可能跟 /b /min 等参数，但首词仍是 start
        if first in _BACKGROUND_FIRST_WORDS:
            return (
                f"命令首词 {first!r} 会启动后台进程/守护进程/终端复用器，"
                f"不适合 run_command（有超时，会被中途杀掉）。"
                f"请改用 launch_gui（GUI/一次性）或 service_start（后台服务）。"
            )
    # 末尾的裸 & （排除 &&）：命令以 & 结束会被放入后台
    if re.search(r"(?<!--)&(?!!)\s*$", s):
        return (
            "命令以 & 结尾，会把进程放入后台。run_command 适合有结束点的命令，"
            "请改用 launch_gui 或 service_start。"
        )
    return None


def _spawn_detached(args, cwd=None, stdout_file=None, stderr_file=None):
    """以"与父进程解耦"方式启动子进程，返回 Popen 对象。
    Windows：DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    Unix：start_new_session=True（脱离控制终端）
    日志文件：直接传二进制 file 对象。subprocess 通过 fd 直传给子进程，不创建内部 pipe/reader 线程，
    完全避开 TextIOWrapper 的 UTF-8 解码（子进程若输出含非法 UTF-8 字节不会崩）。"""
    popen_kwargs = {
        "args": args,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        # text=False 阻止 subprocess 把 stdout/stderr 包成 TextIOWrapper，避免 _communicate 里的
        # reader 线程在子进程输出非 UTF-8 字节时抛 UnicodeDecodeError（Windows 上 Python 启动期会写
        # 一些非 UTF-8 字节到控制台，CreateProcess 默认继承的句柄会捕获到）。
        "text": False,
    }
    if cwd:
        popen_kwargs["cwd"] = cwd
    if stdout_file:
        # 二进制追加 + 无缓冲：subprocess 用 fileno() 直传给子进程，无 pipe、无 reader 线程
        popen_kwargs["stdout"] = open(stdout_file, "ab", buffering=0)
    else:
        popen_kwargs["stdout"] = subprocess.DEVNULL
    if stderr_file:
        popen_kwargs["stderr"] = open(stderr_file, "ab", buffering=0)
    else:
        popen_kwargs["stderr"] = subprocess.DEVNULL

    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    return subprocess.Popen(**popen_kwargs)


@mcp.tool()
def launch_gui(
    command: str,
    working_dir: str = "",
    wait: bool = False,
    wait_timeout: float = 0,
    name: str = "",
) -> str:
    """启动一个 GUI 应用或一次性可执行程序。火即忘（不阻塞当前 MCP 调用）。
    command：要执行的命令字符串。按 shlex 拆分后传参——避免 shell 注入。
      Windows 上若要启动 .exe，直接传 exe 路径；不要套用 cmd 的 start（start 走 shell 会被 run_command 拦截并提示用本工具）。
    working_dir：工作目录（可选）。受 MCP_ALLOWED_ROOTS 约束。
    wait：是否等待进程退出。默认 False（启动后立即返回 PID）。一次性安装包等需要等结束的传 True。
    wait_timeout：wait=True 时的最大等待秒数（默认 30，不得超过 MCP_EXEC_TIMEOUT）。
    name：可选。若传入则同时把进程纳入服务注册表（可被 service_status / service_logs / service_stop 管理），
      日志写入 ~/.coding-mcp/services/<name>/logs/。同一名字只能跑一个实例，重复会被拒绝。
      不传则纯火即忘——拿不回 PID 也没法清理。
    ⚠️ 需开启 MCP_ENABLE_EXEC=1。
    返回 JSON：{"pid":..., "command":..., "working_dir":..., "started_at":...,
               "name"?:..., "log_files"?:{stdout,stderr}, "exit_code"?:..., "killed"?:...}"""
    if not EXEC_ENABLED:
        return "错误：命令执行未开启。请设置 MCP_ENABLE_EXEC=1。"
    try:
        if not command or not command.strip():
            return "错误：命令不能为空"
        parts = _parse_command(command)
        if not parts:
            return "错误：命令解析为空"

        cwd = os.path.abspath(working_dir) if working_dir.strip() else None
        if cwd is not None:
            if not os.path.isdir(cwd):
                return f"错误：工作目录不存在：{cwd}"
            try:
                ensure_allowed(cwd)
            except ValueError as e:
                return f"错误：{e}"

        # 是否纳入服务注册
        registered = bool(name and name.strip())
        if registered:
            try:
                name = _validate_service_name(name)
            except ValueError as e:
                return f"错误：{e}"

        # 日志路径：仅 registered 模式才落日志
        logs_root = os.path.join(_service_dir(name), "logs") if registered else None
        stdout_log = os.path.join(logs_root, "stdout.log") if logs_root else None
        stderr_log = os.path.join(logs_root, "stderr.log") if logs_root else None
        if logs_root:
            ensure_dir(logs_root)

        # 幂等检查（仅 registered 模式）：同名已在跑则拒
        if registered:
            pid_path = _pid_file(name)
            if os.path.exists(pid_path):
                try:
                    with open(pid_path, "r", encoding="utf-8") as f:
                        old_pid = int(f.read().strip() or "0")
                    if _pid_alive(old_pid):
                        log_op({"tool": "launch_gui", "name": name, "ok": False, "error": "已在运行"})
                        return f"错误：服务 {name!r} 已在运行（pid={old_pid}）。如需重启请先 service_stop。"
                    try:
                        os.remove(pid_path)
                    except OSError:
                        pass
                except (ValueError, OSError):
                    pass

        log_op(
            {
                "tool": "launch_gui",
                "command": command,
                "cwd": cwd,
                "wait": wait,
                "name": name if registered else None,
                "ok": None,
            }
        )

        proc = _spawn_detached(parts, cwd=cwd, stdout_file=stdout_log, stderr_file=stderr_log)

        result = {
            "pid": proc.pid,
            "command": command,
            "working_dir": cwd,
            "started_at": _now_iso(),
        }

        # registered 模式：写 pid_file + 注册表 + 短轮询存活
        if registered:
            ensure_dir(_service_dir(name))
            pid_path = _pid_file(name)
            try:
                with open(pid_path, "w", encoding="utf-8") as f:
                    f.write(str(proc.pid))
            except OSError as e:
                return f"错误：写入 pid_file 失败：{e}"

            services = [s for s in _read_registry() if s.get("name") != name]
            entry = {
                "name": name,
                "pid": proc.pid,
                "command": command,
                "working_dir": cwd,
                "log_dir": logs_root,
                "stdout_log": stdout_log,
                "stderr_log": stderr_log,
                "started_at": result["started_at"],
            }
            services.append(entry)
            _write_registry(services)
            result["name"] = name
            result["log_files"] = {"stdout": stdout_log, "stderr": stderr_log}

            import time
            time.sleep(1.0)
            if not _pid_alive(proc.pid):
                # 启动后立刻退出：清理注册
                services = [s for s in _read_registry() if s.get("name") != name]
                _write_registry(services)
                try:
                    os.remove(pid_path)
                except OSError:
                    pass
                log_op({"tool": "launch_gui", "name": name, "ok": False, "error": "启动后立即退出"})
                return (
                    f"错误：进程 {name!r} 启动后立即退出（pid={proc.pid}）。"
                    f"请检查命令或查看日志：{stderr_log}"
                )

        if not wait:
            log_op({"tool": "launch_gui", "pid": proc.pid, "name": name if registered else None, "ok": True})
            return json.dumps(result, ensure_ascii=False, indent=2)

        # wait=True：等到结束或超时
        # 用 poll() 轮询而不是 proc.wait()：wait() 内部走 _communicate，会启动 reader 线程去读
        # stdout/stderr 的 TextIOWrapper，遇到非 UTF-8 字节会抛 UnicodeDecodeError。
        # 我们已经把 stdout/stderr 重定向到文件，不需要再读一次；只关心退出码即可。
        t = float(wait_timeout) if wait_timeout else EXEC_TIMEOUT_MAX
        t = min(max(t, 0.1), EXEC_TIMEOUT_MAX)
        import time
        deadline = time.time() + t
        exit_code = None
        while time.time() < deadline:
            exit_code = proc.poll()
            if exit_code is not None:
                break
            time.sleep(0.1)
        if exit_code is not None:
            result["exit_code"] = exit_code
            log_op({"tool": "launch_gui", "pid": proc.pid, "exit_code": exit_code, "ok": True})
        else:
            _kill_tree(proc.pid, force=True, timeout=5.0)
            result["killed"] = True
            result["error"] = f"超时（>{t} 秒），已强制终止"
            log_op({"tool": "launch_gui", "pid": proc.pid, "ok": False, "error": "超时"})

        # wait 模式下若已注册：进程已结束，从注册表移除（wait 用法通常是"装个东西等它跑完"）
        if registered and result.get("exit_code") is not None:
            services = [s for s in _read_registry() if s.get("name") != name]
            if len(services) != len(_read_registry()):
                _write_registry(services)
            try:
                pid_path = _pid_file(name)
                if os.path.exists(pid_path):
                    os.remove(pid_path)
            except OSError:
                pass

        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "launch_gui", "command": command, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def service_start(
    name: str,
    command: str,
    working_dir: str = "",
    log_dir: str = "",
) -> str:
    """启动一个后台守护进程并托管其生命周期。
    name：服务标识（字母/数字/./_/-，最长 64 字符）。同一名字只能跑一个实例；重名且仍在运行会被拒绝。
    command：要执行的命令字符串。按 shlex 拆分后传参。
    working_dir：工作目录（可选，受 MCP_ALLOWED_ROOTS 约束）。
    log_dir：日志目录（可选，默认 ~/.coding-mcp/services/<name>/logs/，受 MCP_ALLOWED_ROOTS 约束）。
    ⚠️ 需开启 MCP_ENABLE_EXEC=1。stdout/stderr 持续追加到日志文件，不会丢也不会撑爆上下文。
    返回 JSON：{"name":..., "pid":..., "command":..., "working_dir":..., "log_files":{stdout,stderr}, "started_at":...}"""
    if not EXEC_ENABLED:
        return "错误：命令执行未开启。请设置 MCP_ENABLE_EXEC=1。"
    try:
        try:
            name = _validate_service_name(name)
        except ValueError as e:
            return f"错误：{e}"
        if not command or not command.strip():
            return "错误：命令不能为空"
        parts = _parse_command(command)
        if not parts:
            return "错误：命令解析为空"

        cwd = os.path.abspath(working_dir) if working_dir.strip() else None
        if cwd is not None:
            if not os.path.isdir(cwd):
                return f"错误：工作目录不存在：{cwd}"
            try:
                ensure_allowed(cwd)
            except ValueError as e:
                return f"错误：{e}"

        if log_dir.strip():
            logs_root = os.path.abspath(log_dir)
            try:
                ensure_allowed(logs_root)
            except ValueError as e:
                return f"错误：{e}"
        else:
            logs_root = os.path.join(_service_dir(name), "logs")

        # 幂等：同 name 已在运行则拒绝
        pid_path = _pid_file(name)
        if os.path.exists(pid_path):
            try:
                with open(pid_path, "r", encoding="utf-8") as f:
                    old_pid = int(f.read().strip() or "0")
                if _pid_alive(old_pid):
                    log_op({"tool": "service_start", "name": name, "ok": False, "error": "已在运行"})
                    return f"错误：服务 {name!r} 已在运行（pid={old_pid}）。如需重启请先 service_stop。"
                # pid 已死但 pid_file 残留——清理后继续启动
                try:
                    os.remove(pid_path)
                except OSError:
                    pass
            except (ValueError, OSError):
                pass  # pid_file 损坏，忽略

        # 注册表去重（防御性：pid_file 丢了但注册表里还在）
        services = _read_registry()
        services = [s for s in services if s.get("name") != name]

        # 准备日志目录与文件（必须先建好再 spawn，否则启动初期日志会丢）
        ensure_dir(logs_root)
        stdout_path = os.path.join(logs_root, "stdout.log")
        stderr_path = os.path.join(logs_root, "stderr.log")

        log_op({"tool": "service_start", "name": name, "command": command, "cwd": cwd, "ok": None})
        proc = _spawn_detached(parts, cwd=cwd, stdout_file=stdout_path, stderr_file=stderr_path)

        # 写 pid_file + 更新注册表（原子）
        ensure_dir(_service_dir(name))
        try:
            with open(pid_path, "w", encoding="utf-8") as f:
                f.write(str(proc.pid))
        except OSError as e:
            return f"错误：写入 pid_file 失败：{e}"

        entry = {
            "name": name,
            "pid": proc.pid,
            "command": command,
            "working_dir": cwd,
            "log_dir": logs_root,
            "stdout_log": stdout_path,
            "stderr_log": stderr_path,
            "started_at": _now_iso(),
        }
        services.append(entry)
        _write_registry(services)

        # 短轮询：1 秒内死了就报错（避免启动后立刻退出的命令被误认为"运行中"）
        import time
        time.sleep(1.0)
        if not _pid_alive(proc.pid):
            # 从注册表移除
            services = [s for s in _read_registry() if s.get("name") != name]
            _write_registry(services)
            try:
                os.remove(pid_path)
            except OSError:
                pass
            log_op({"tool": "service_start", "name": name, "ok": False, "error": "启动后立即退出"})
            return f"错误：服务 {name!r} 启动后立即退出（pid={proc.pid}）。请检查命令或查看日志：{stderr_path}"

        log_op({"tool": "service_start", "name": name, "pid": proc.pid, "ok": True})
        return json.dumps(entry, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "service_start", "name": name, "command": command, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def service_stop(
    name: str,
    force: bool = False,
    timeout: float = 10.0,
) -> str:
    """停止一个由 service_start 启动的后台服务。
    name：服务标识。
    force：是否强制终止（默认 False：先温和发信号，10 秒不退再强杀）。
    timeout：等待退出的秒数（默认 10）。
    返回 JSON：{"name":..., "pid":..., "stopped":bool, "took_ms":..., "forced":bool}"""
    if not EXEC_ENABLED:
        return "错误：命令执行未开启。请设置 MCP_ENABLE_EXEC=1。"
    try:
        try:
            name = _validate_service_name(name)
        except ValueError as e:
            return f"错误：{e}"

        services = _read_registry()
        entry = next((s for s in services if s.get("name") == name), None)
        if entry is None:
            return f"错误：未找到服务 {name!r}（未通过 service_start 注册）"
        pid = int(entry.get("pid", 0))
        pid_path = _pid_file(name)

        import time
        start = time.time()
        # 即使 force=True 也先温和一次，给应用清理机会
        stopped = _kill_tree(pid, force=force, timeout=timeout)
        took_ms = int((time.time() - start) * 1000)

        if stopped:
            try:
                if os.path.exists(pid_path):
                    os.remove(pid_path)
            except OSError:
                pass
            services = [s for s in _read_registry() if s.get("name") != name]
            _write_registry(services)
            log_op({"tool": "service_stop", "name": name, "pid": pid, "ok": True})
        else:
            log_op({"tool": "service_stop", "name": name, "pid": pid, "ok": False, "error": "终止失败"})

        return json.dumps(
            {"name": name, "pid": pid, "stopped": stopped, "took_ms": took_ms, "forced": force},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "service_stop", "name": name, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def service_status(name: str = "") -> str:
    """列出所有托管服务或查询单个服务的存活状态。
    name：可选。传单个名字查询该服务；不传则列出全部。
    返回 JSON：{"services":[{name, pid, alive, started_at, command, working_dir, log_dir}, ...]}"""
    try:
        if name.strip():
            try:
                name = _validate_service_name(name)
            except ValueError as e:
                return f"错误：{e}"
            services = [s for s in _read_registry() if s.get("name") == name]
            if not services:
                return f"错误：未找到服务 {name!r}"
        else:
            services = _read_registry()

        out = []
        for s in services:
            pid = int(s.get("pid", 0))
            out.append(
                {
                    "name": s.get("name"),
                    "pid": pid,
                    "alive": _pid_alive(pid),
                    "started_at": s.get("started_at"),
                    "command": s.get("command"),
                    "working_dir": s.get("working_dir"),
                    "log_dir": s.get("log_dir"),
                }
            )
        log_op({"tool": "service_status", "name": name or "(all)", "count": len(out), "ok": True})
        return json.dumps({"services": out}, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "service_status", "name": name, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def service_logs(
    name: str,
    stream: str = "both",
    tail_lines: int = 100,
) -> str:
    """读取后台服务的日志末尾片段。
    name：服务标识。
    stream："stdout"、"stderr" 或 "both"（默认 both）。
    tail_lines：返回最后 N 行（默认 100，最大 1000）。
    ⚠️ 不支持 follow（持续推送）。MCP 是请求/响应协议，实时 tail 请用 shell 自己 less/tail。
    返回：{"name":..., "stream":..., "lines":N, "stdout":..., "stderr":..., "tail_lines":N}"""
    try:
        try:
            name = _validate_service_name(name)
        except ValueError as e:
            return f"错误：{e}"

        services = _read_registry()
        entry = next((s for s in services if s.get("name") == name), None)
        if entry is None:
            return f"错误：未找到服务 {name!r}"

        tail_lines = max(1, min(int(tail_lines or 100), 1000))
        out = {
            "name": name,
            "stream": stream,
            "tail_lines": tail_lines,
            "stdout": None,
            "stderr": None,
        }

        def _tail(path):
            if not path or not os.path.exists(path):
                return "(日志文件不存在)"
            try:
                # 用 deque 高效取尾
                from collections import deque
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    return "\n".join(deque(f, maxlen=tail_lines))
            except Exception as e:  # noqa: BLE001
                return f"(读取失败：{e})"

        if stream in ("stdout", "both"):
            out["stdout"] = _tail(entry.get("stdout_log"))
        if stream in ("stderr", "both"):
            out["stderr"] = _tail(entry.get("stderr_log"))

        log_op({"tool": "service_logs", "name": name, "stream": stream, "ok": True})
        return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "service_logs", "name": name, "ok": False, "error": str(e)})
        return f"错误：{e}"


@mcp.tool()
def service_clean(remove_logs: bool = False, dry_run: bool = False) -> str:
    """扫描服务注册表，清理已死进程对应的条目（孤儿清理）。
    remove_logs：是否同时删除该服务的日志目录（默认 False，保留日志用于事后排错）。
    dry_run：仅扫描不删除（默认 False）。
    典型场景：服务器重启后注册表里残留着上轮进程的条目；或服务进程被外部 kill 后 pid_file 与注册表不一致。
    返回 JSON：{"scanned":N, "removed":[{name, pid, had_pid_file, logs_removed}], "alive":M, "dry_run":bool}"""
    try:
        services = _read_registry()
        alive_names = []
        removed_list = []
        for s in services:
            name = s.get("name")
            pid = int(s.get("pid", 0))
            pid_path = _pid_file(name)
            had_pid_file = os.path.exists(pid_path)
            if _pid_alive(pid):
                alive_names.append(name)
                continue
            # 已死
            logs_removed = False
            item = {"name": name, "pid": pid, "had_pid_file": had_pid_file, "logs_removed": False}
            if not dry_run:
                # 默认仅清理 pid_file；只有明确要求才删日志目录（事后排错有用）
                try:
                    if os.path.exists(pid_path):
                        os.remove(pid_path)
                except OSError:
                    pass
                if remove_logs:
                    svc_dir = _service_dir(name)
                    try:
                        shutil.rmtree(svc_dir)
                        logs_removed = True
                    except OSError:
                        pass
                item["logs_removed"] = logs_removed
            removed_list.append(item)

        if not dry_run:
            # 重写注册表（移除已死条目）
            new_registry = [s for s in services if s.get("name") in alive_names]
            if len(new_registry) != len(services):
                _write_registry(new_registry)

        log_op(
            {
                "tool": "service_clean",
                "scanned": len(services),
                "removed": len(removed_list),
                "alive": len(alive_names),
                "dry_run": dry_run,
                "ok": True,
            }
        )
        return json.dumps(
            {
                "scanned": len(services),
                "removed": removed_list,
                "alive": len(alive_names),
                "dry_run": dry_run,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:  # noqa: BLE001
        log_op({"tool": "service_clean", "ok": False, "error": str(e)})
        return f"错误：{e}"


if __name__ == "__main__":
    db_status = "/".join(
        k for k in ("mysql", "pgsql", "redis")
        if (MYSQL_URL if k == "mysql" else PGSQL_URL if k == "pgsql" else REDIS_URL)
    ) or "无"
    sys.stderr.write(
        f"[coding-mcp] 已启动 v{VERSION}"
        f"（备份：{'关' if DISABLE_BACKUP else '开'}，审计：{AUDIT_LOG or '关'}，"
        f"执行：{'开' if EXEC_ENABLED else '关'}，数据库：{db_status}，"
        f"写库：{'开' if DB_ALLOW_WRITE else '关'}，"
        f"服务目录：{SERVICES_ROOT}）\n"
    )
    mcp.run(transport="stdio")
