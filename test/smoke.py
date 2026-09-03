"""冒烟测试：通过 stdio 连上 coding-mcp（Python 版），验证核心工具与防丢失机制。"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _text(result):
    parts = []
    for c in getattr(result, "content", []):
        if getattr(c, "type", "") == "text":
            parts.append(c.text)
    return "\n".join(parts)


async def main():
    here = Path(__file__).resolve().parent.parent
    server_py = here / "server.py"
    python = here / ".venv" / "Scripts" / "python.exe"
    if not python.exists():  # Linux/macOS 回退
        python = here / ".venv" / "bin" / "python"

    tmp = tempfile.mkdtemp(prefix="coding-mcp-test-")
    file = os.path.join(tmp, "demo.txt")
    audit = os.path.join(tmp, "audit.log")

    params = StdioServerParameters(
        command=str(python),
        args=[str(server_py)],
        env={**os.environ, "MCP_AUDIT_LOG": audit, "MCP_ENABLE_EXEC": "1"},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))

            r = await session.call_tool("write_file", {"path": file, "content": "hello\nworld\nhello\n"})
            print("\n[write]", _text(r))

            r = await session.call_tool("read_file", {"path": file})
            print("[read ]", repr(_text(r)))

            r = await session.call_tool("edit_file", {"path": file, "old_string": "world", "new_string": "WORLD"})
            print("[edit ]", _text(r))

            r = await session.call_tool("edit_file", {"path": file, "old_string": "hello", "new_string": "HELLO", "replace_all": True})
            print("[editA]", _text(r))

            r = await session.call_tool("read_file", {"path": file})
            print("[final]", repr(_text(r)))

            r = await session.call_tool("edit_file", {"path": file, "old_string": "HELLO", "new_string": "X"})
            print("[unique]", _text(r))

            # ---- 命令执行（已开启 MCP_ENABLE_EXEC=1）----
            r = await session.call_tool("run_command", {"command": "echo hello-from-shell"})
            print("\n[exec ]", _text(r))

            # Windows cmd 与 Unix sh 语法不同，用 python -c 做跨平台退出码验证
            r = await session.call_tool(
                "run_command",
                {"command": f'"{python}" -c "import sys; print(\'to-stdout\'); print(\'to-stderr\', file=sys.stderr); sys.exit(3)"'},
            )
            print("[exec2]", _text(r))

            r = await session.call_tool("run_command", {"command": "this_command_does_not_exist_xyz"})
            print("[exec3]", _text(r))

            # ---- 差异对比 ----
            file_a = os.path.join(tmp, "a.txt")
            file_b = os.path.join(tmp, "b.txt")
            await session.call_tool("write_file", {"path": file_a, "content": "line1\nline2\nline3\n"})
            await session.call_tool("write_file", {"path": file_b, "content": "line1\nline2-changed\nline3\nline4\n"})
            r = await session.call_tool("diff_files", {"path_a": file_a, "path_b": file_b})
            print("\n[diff ]")
            print(_text(r))

            # 相同文件 → 无差异
            r = await session.call_tool("diff_files", {"path_a": file_a, "path_b": file_a})
            print("[same ]", _text(r))

            # ---- git 工具 ----
            r = await session.call_tool("git", {"args": ["--version"]})
            print("\n[git  ]", _text(r).strip()[:60])

            backup_dir = os.path.join(tmp, ".coding-mcp-backup")
            print("\nbackup dir exists:", os.path.isdir(backup_dir))
            if os.path.isdir(backup_dir):
                backups = sorted(os.listdir(backup_dir))
                print("backups:", ", ".join(backups))
                print("first backup content:", repr(Path(backup_dir, backups[0]).read_text(encoding="utf-8")))

            print("\naudit log exists:", os.path.exists(audit))
            if os.path.exists(audit):
                lines = Path(audit).read_text(encoding="utf-8").strip().splitlines()
                print("audit entries:", len(lines))
                for l in lines:
                    print("  ", l)

    print("\n== done ==")


async def test_exec_disabled_by_default():
    """第二个场景：未设置 MCP_ENABLE_EXEC 时，run_command 应拒绝执行。"""
    here = Path(__file__).resolve().parent.parent
    server_py = here / "server.py"
    python = here / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = here / ".venv" / "bin" / "python"

    params = StdioServerParameters(command=str(python), args=[str(server_py)])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await session.call_tool("run_command", {"command": "echo should-not-run"})
            print("\n[exec-disabled]", _text(r))


async def test_git_tools():
    """第三个场景：在临时目录初始化 git 仓库，验证 git/status/log 工具。"""
    import subprocess as sp

    here = Path(__file__).resolve().parent.parent
    server_py = here / "server.py"
    python = here / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = here / ".venv" / "bin" / "python"

    repo = tempfile.mkdtemp(prefix="coding-mcp-git-")
    sp.run(["git", "init", "-q", repo], check=True)
    sp.run(["git", "-C", repo, "config", "user.name", "test"], check=True)
    sp.run(["git", "-C", repo, "config", "user.email", "test@example.com"], check=True)
    Path(repo, "f.txt").write_text("hello\n", encoding="utf-8")
    sp.run(["git", "-C", repo, "add", "f.txt"], check=True)
    sp.run(["git", "-C", repo, "commit", "-q", "-m", "init"], check=True)

    params = StdioServerParameters(command=str(python), args=[str(server_py)])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await session.call_tool("git_status", {"cwd": repo})
            print("\n[git_status]", _text(r).strip()[:80])

            r = await session.call_tool("git_log", {"cwd": repo, "count": 5})
            print("[git_log ]", _text(r).strip()[:80])

            r = await session.call_tool("git", {"args": ["rev-parse", "HEAD"], "cwd": repo})
            print("[git rev ]", _text(r).strip()[:50])


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_exec_disabled_by_default())
    asyncio.run(test_git_tools())
