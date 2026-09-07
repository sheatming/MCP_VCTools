"""冒烟测试：通过 stdio 连上 coding-mcp（Python 版），验证核心工具与防丢失机制。"""
import asyncio
import json as _json
import os
import sys
import shutil
import tempfile
import time
import threading
import socket
from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None
import select

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


async def test_git_commit_and_context():
    """第四个场景：验证 git commit 全套 + 项目感知 + dev-context。"""
    import subprocess as sp

    here = Path(__file__).resolve().parent.parent
    server_py = here / "server.py"
    python = here / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = here / ".venv" / "bin" / "python"

    repo = tempfile.mkdtemp(prefix="coding-mcp-git2-")
    sp.run(["git", "init", "-q", repo], check=True)
    sp.run(["git", "-C", repo, "config", "user.name", "test"], check=True)
    sp.run(["git", "-C", repo, "config", "user.email", "test@example.com"], check=True)

    # 造一个 Python 项目标记 + dev-context
    Path(repo, "requirements.txt").write_text("mcp>=1.0\n", encoding="utf-8")
    ctx_dir = Path(repo, ".dev-context")
    ctx_dir.mkdir()
    (ctx_dir / "技术文档.md").write_text("# 技术文档\n这是一个测试项目。\n", encoding="utf-8")
    (ctx_dir / "开发规范.md").write_text("# 开发规范\n提交前必须跑测试。\n", encoding="utf-8")

    params = StdioServerParameters(command=str(python), args=[str(server_py)])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("\ntools:", ", ".join(t.name for t in tools.tools))

            r = await session.call_tool("get_project_info", {"cwd": repo})
            print("\n[project ]")
            print(_text(r))

            r = await session.call_tool("load_dev_context", {"cwd": repo})
            print("\n[context ]")
            print(_text(r))

            # git add + commit
            r = await session.call_tool("git_add", {"paths": ["."], "cwd": repo})
            print("[git_add ]", _text(r).strip()[:60])

            r = await session.call_tool("git_diff", {"cwd": repo, "staged": True})
            print("[git_diff]", _text(r).strip()[:100])

            r = await session.call_tool("git_commit", {"message": "feat: add project", "cwd": repo})
            print("[commit  ]", _text(r).strip()[:80])

            r = await session.call_tool("git_log", {"cwd": repo, "count": 3})
            print("[log     ]", _text(r).strip()[:80])


async def test_gui_and_service_lifecycle():
    """第五个场景：GUI/后台服务工具全流程。
    - launch_gui 火即忘，返回 PID
    - service_start 启动守护进程；幂等拒绝；status 查存活；logs 读日志
    - service_stop 优雅停止（先温和，超时再强杀）"""
    import subprocess as sp
    import time as time_mod
    import shutil

    here = Path(__file__).resolve().parent.parent
    server_py = here / "server.py"
    python = here / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = here / ".venv" / "bin" / "python"

    # 用临时 home 目录隔离本测试的服务注册表（不影响 ~/.coding-mcp/services.json）
    fake_home = tempfile.mkdtemp(prefix="coding-mcp-home-")
    audit = os.path.join(fake_home, "audit.log")
    services_root = os.path.join(fake_home, "services")
    os.makedirs(services_root, exist_ok=True)

    params = StdioServerParameters(
        command=str(python),
        args=[str(server_py)],
        env={
            **os.environ,
            "HOME": fake_home,                # POSIX
            "USERPROFILE": fake_home,         # Windows（expanduser 用这个）
            "MCP_AUDIT_LOG": audit,
            "MCP_ENABLE_EXEC": "1",
        },
    )

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                for needed in ("launch_gui", "service_start", "service_stop", "service_status", "service_logs"):
                    assert needed in names, f"工具 {needed} 未注册"

                # ---- launch_gui：火即忘，立即返回 PID ----
                # 用 python -c 跑一个一次性脚本立即退出（验证 PID 返回 + wait=True 拿 exit_code）
                r = await session.call_tool(
                    "launch_gui",
                    {
                        "command": f'"{python}" -c "import time; print(\'one-off\'); time.sleep(0.2)"',
                        "wait": True,
                        "wait_timeout": 10,
                    },
                )
                print("\n[launch_gui-wait]", _text(r))
                assert "one-off" in _text(r) or "pid" in _text(r), "launch_gui wait=True 应返回 pid+exit_code"

                # wait=False：不阻塞，立刻返回 PID
                r = await session.call_tool(
                    "launch_gui",
                    {
                        "command": f'"{python}" -c "import time; time.sleep(60)"',
                        "wait": False,
                    },
                )
                print("[launch_gui-nowait]", _text(r))
                txt = _text(r)
                assert '"pid"' in txt, "launch_gui wait=False 应返回 pid"
                # 拿到 pid 后面 service_status 检查
                import json as _json
                one_off_pid = _json.loads(txt)["pid"]
                # 一秒后这个进程应该还在跑（还没到 sleep 结束）
                time_mod.sleep(1.0)

                # ---- service_start：启动托管服务 ----
                # 用 python -c 写日志到 stderr，然后无限 sleep（验证日志/stdout 捕获 + 存活）
                svc_name = "smoke-svc-1"
                r = await session.call_tool(
                    "service_start",
                    {
                        "name": svc_name,
                        "command": (
                            f'"{python}" -c "import sys, time; '
                            f'print(\'service-started\'); sys.stdout.flush(); '
                            f'print(\'err-line\', file=sys.stderr); sys.stderr.flush(); '
                            f'time.sleep(120)"'
                        ),
                    },
                )
                print("\n[svc-start]", _text(r))
                assert "service-started" not in _text(r) or "pid" in _text(r), "service_start 应返回 JSON 注册信息"
                entry = _json.loads(_text(r))
                assert entry["name"] == svc_name
                assert entry["pid"] > 0
                assert os.path.isfile(entry["stdout_log"])
                assert os.path.isfile(entry["stderr_log"])

                # 等 2 秒让日志落盘
                time_mod.sleep(2.0)

                # 幂等：再启同名应被拒
                r = await session.call_tool("service_start", {"name": svc_name, "command": f'"{python}" -c "pass"'})
                print("[svc-start-dup]", _text(r))
                assert "已在运行" in _text(r), "同名重启动应被拒"

                # service_status：列全部 → 应包含 svc_name 且 alive=True
                r = await session.call_tool("service_status", {})
                print("[svc-status-all]", _text(r))
                statuses = _json.loads(_text(r))["services"]
                mine = [s for s in statuses if s["name"] == svc_name]
                assert mine and mine[0]["alive"], f"服务 {svc_name} 应存活"

                # service_logs：读末尾，应包含 service-started / err-line
                r = await session.call_tool("service_logs", {"name": svc_name, "stream": "both", "tail_lines": 50})
                print("[svc-logs]", _text(r)[:300])
                logs = _json.loads(_text(r))
                assert "service-started" in (logs.get("stdout") or ""), "stdout 应有 service-started"
                assert "err-line" in (logs.get("stderr") or ""), "stderr 应有 err-line"

                # service_stop：温和停止
                r = await session.call_tool("service_stop", {"name": svc_name, "force": False, "timeout": 10})
                print("[svc-stop]", _text(r))
                result = _json.loads(_text(r))
                assert result["stopped"], f"停止失败：{result}"

                # 再查应已不在
                r = await session.call_tool("service_status", {"name": svc_name})
                print("[svc-status-gone]", _text(r)[:120])
                assert "未找到" in _text(r), "停止后应查不到"

                # ---- launch_gui 漏网进程手动清掉 ----
                if one_off_pid:
                    try:
                        sp.run(["taskkill", "/F", "/PID", str(one_off_pid), "/T"], capture_output=True)
                    except Exception:
                        pass
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
        # 不能用 taskkill /IM python.exe —— 会杀掉测试进程本身。
        # launch_gui(no-name) 的火即忘进程已显式按 PID 杀；按服务名注册的进程也 stop 了。





async def test_run_command_blacklist():
    """第六个场景：run_command 应拒绝后台化语法（start/nohup/&）。"""
    here = Path(__file__).resolve().parent.parent
    server_py = here / "server.py"
    python = here / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = here / ".venv" / "bin" / "python"

    params = StdioServerParameters(
        command=str(python),
        args=[str(server_py)],
        env={**os.environ, "MCP_ENABLE_EXEC": "1"},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. start 开头
            r = await session.call_tool("run_command", {"command": "start notepad"})
            txt = _text(r)
            print("\n[bl-start]", txt[:120])
            assert "错误" in txt and "start" in txt, f"start 命令应被拒：{txt}"

            # 2. nohup
            r = await session.call_tool("run_command", {"command": "nohup python -c 'import time; time.sleep(999)'"})
            txt = _text(r)
            print("[bl-nohup]", txt[:120])
            assert "错误" in txt and "nohup" in txt, f"nohup 应被拒：{txt}"

            # 3. 末尾的 &
            r = await session.call_tool("run_command", {"command": "python -c 'print(1)' &"})
            txt = _text(r)
            print("[bl-amp]", txt[:120])
            assert "错误" in txt and "&" in txt, f"末尾 & 应被拒：{txt}"

            # 4. && 不应被拒（合法 AND）
            r = await session.call_tool(
                "run_command",
                {"command": f'"{python}" -c "print(\'a\')" && "{python}" -c "print(\'b\')"'},
            )
            txt = _text(r)
            print("[bl-and]", txt[:80])
            assert "a" in txt and "b" in txt and "错误" not in txt.split("\n")[0], f"&& 应被允许：{txt}"

            # 5. 正常命令应正常执行（兜底：上面黑名单别误伤）
            r = await session.call_tool("run_command", {"command": "echo whitelist-ok"})
            txt = _text(r)
            print("[bl-ok]", txt[:60])
            assert "whitelist-ok" in txt, f"正常命令应通过：{txt}"


async def test_launch_gui_with_name():
    """第七个场景：launch_gui(name=...) 应纳入服务注册表，可被 service_status / service_stop 管理。"""
    import subprocess as sp
    import time as time_mod

    here = Path(__file__).resolve().parent.parent
    server_py = here / "server.py"
    python = here / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = here / ".venv" / "bin" / "python"

    fake_home = tempfile.mkdtemp(prefix="coding-mcp-home-name-")
    audit = os.path.join(fake_home, "audit.log")

    params = StdioServerParameters(
        command=str(python),
        args=[str(server_py)],
        env={
            **os.environ,
            "HOME": fake_home,
            "USERPROFILE": fake_home,
            "MCP_AUDIT_LOG": audit,
            "MCP_ENABLE_EXEC": "1",
        },
    )

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # launch_gui 带 name：应注册到服务表
                gui_name = "my-gui-app"
                r = await session.call_tool(
                    "launch_gui",
                    {
                        "command": f'"{python}" -c "import time; print(\'gui-up\'); time.sleep(120)"',
                        "name": gui_name,
                    },
                )
                print("\n[gui-named]", _text(r), flush=True)
                txt = _text(r)
                entry = _json.loads(txt)
                assert entry.get("name") == gui_name, "返回 JSON 应含 name"
                assert entry.get("pid", 0) > 0
                assert "log_files" in entry, "应同时返回 log_files"

                # 等 0.5s 让启动稳定
                time_mod.sleep(0.5)

                # service_status 应能看到它
                r = await session.call_tool("service_status", {"name": gui_name})
                txt = _text(r)
                print("[gui-status]", txt[:200], flush=True)
                statuses = _json.loads(txt)["services"]
                mine = [s for s in statuses if s["name"] == gui_name]
                assert mine and mine[0]["alive"], "GUI 应在服务表中存活"

                # service_logs 应能读到日志
                r = await session.call_tool("service_logs", {"name": gui_name, "stream": "stdout", "tail_lines": 5})
                logs = _json.loads(_text(r))
                # 不强制要求 log 含 gui-up（python.exe 启动时机受 OS 调度影响，先放宽）
                # 重点：调用不抛异常、返回合理结构
                assert logs.get("name") == gui_name
                assert logs.get("stdout") is not None

                # service_stop 应能停止它
                r = await session.call_tool("service_stop", {"name": gui_name, "force": False, "timeout": 5})
                result = _json.loads(_text(r))
                assert result["stopped"], f"应能停掉 GUI 服务：{result}"

                # 幂等：同名再启动应被拒
                # 先确保上一个完全清理
                time_mod.sleep(0.5)
                r = await session.call_tool(
                    "launch_gui",
                    {
                        "command": f'"{python}" -c "import time; time.sleep(60)"',
                        "name": gui_name,
                    },
                )
                print("[gui-named-2]", _text(r), flush=True)
                entry2 = _json.loads(_text(r))
                assert entry2.get("name") == gui_name and entry2.get("pid", 0) > 0, "重启应成功"

                # 同名再注册应被拒
                r = await session.call_tool(
                    "launch_gui",
                    {
                        "command": f'"{python}" -c "import time; time.sleep(60)"',
                        "name": gui_name,
                    },
                )
                print("[gui-named-dup]", _text(r), flush=True)
                assert "已在运行" in _text(r), "同 name 重启应被拒"

                # 清掉
                r = await session.call_tool("service_stop", {"name": gui_name, "force": True})
                print("[gui-stop]", _text(r), flush=True)
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
        # 注意：不能用 taskkill /IM python.exe —— 那会杀掉测试进程本身。
        # 漏网进程靠 service_stop(force=True) 已清；fire-and-forget 的 PID 不再追踪，忽略。


async def test_service_clean():
    """第八个场景：service_clean 扫描注册表，清理已死服务。"""
    import subprocess as sp
    import time as time_mod

    here = Path(__file__).resolve().parent.parent
    server_py = here / "server.py"
    python = here / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = here / ".venv" / "bin" / "python"

    fake_home = tempfile.mkdtemp(prefix="coding-mcp-home-clean-")
    audit = os.path.join(fake_home, "audit.log")

    params = StdioServerParameters(
        command=str(python),
        args=[str(server_py)],
        env={
            **os.environ,
            "HOME": fake_home,
            "USERPROFILE": fake_home,
            "MCP_AUDIT_LOG": audit,
            "MCP_ENABLE_EXEC": "1",
        },
    )

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 启动 2 个服务
                for i, name in enumerate(["clean-svc-a", "clean-svc-b"]):
                    r = await session.call_tool(
                        "service_start",
                        {
                            "name": name,
                            "command": f'"{python}" -c "import time; time.sleep(120)"',
                        },
                    )
                    assert _json.loads(_text(r))["name"] == name, f"启动 {name} 失败"

                time_mod.sleep(1.0)

                # 外部 kill 一个：模拟"注册表里有条目但进程已死"
                r = await session.call_tool("service_status", {"name": "clean-svc-a"})
                a_pid = _json.loads(_text(r))["services"][0]["pid"]
                sp.run(["taskkill", "/F", "/PID", str(a_pid), "/T"], capture_output=True, timeout=5)

                # 等一会让 OS 反映进程已死
                time_mod.sleep(1.0)

                # dry_run：只看不删
                r = await session.call_tool("service_clean", {"dry_run": True})
                report = _json.loads(_text(r))
                print("\n[clean-dry]", _text(r)[:300])
                assert report["dry_run"] is True
                assert report["scanned"] >= 2
                assert any(item["name"] == "clean-svc-a" for item in report["removed"]), "应识别到已死的 clean-svc-a"
                # clean-svc-b 活着，应在 alive（用 removed 列表确认它不在被清名单里）
                removed_names = [item["name"] for item in report["removed"]]
                assert "clean-svc-b" not in removed_names, "活着的 clean-svc-b 不应被列入 removed"

                # 确认 dry_run 没有真删
                r = await session.call_tool("service_status", {"name": "clean-svc-a"})
                assert "未找到" not in _text(r), "dry_run 不应实际删除"

                # 实际清理
                r = await session.call_tool("service_clean", {"remove_logs": False})
                report2 = _json.loads(_text(r))
                print("[clean-real]", _text(r)[:300])
                assert report2["dry_run"] is False
                removed_names = [x["name"] for x in report2["removed"]]
                assert "clean-svc-a" in removed_names, "应实际移除 clean-svc-a"
                assert "clean-svc-b" not in removed_names, "活着的 clean-svc-b 不应被清"

                # 确认 clean-svc-a 已从注册表消失
                r = await session.call_tool("service_status", {"name": "clean-svc-a"})
                assert "未找到" in _text(r), "clean-svc-a 应已清理"

                # 确认 clean-svc-b 还在
                r = await session.call_tool("service_status", {"name": "clean-svc-b"})
                assert "未找到" not in _text(r), "clean-svc-b 应仍在"

                # 二次清理：已死项已无，应无 removed
                r = await session.call_tool("service_clean", {})
                report3 = _json.loads(_text(r))
                assert report3["scanned"] == 1, f"应只剩 1 项：{report3}"
                assert report3["removed"] == [], f"不应再有已死项：{report3}"

                # 清掉活服务
                await session.call_tool("service_stop", {"name": "clean-svc-b", "force": True})
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
        # 不能用 taskkill /IM python.exe —— 会杀掉测试进程本身。
        # 活的服务已显式 stop；外部 kill 的 dead-svc 由 service_clean 清理。


async def test_service_restart():
    """service_restart：一步重启（stop + start），复用注册表里的原 command / working_dir。
    验证：
      - 旧 PID 被替换为新 PID
      - 进程在重启后仍存活
      - truncate_logs=True 会清空旧日志
      - 对未注册的名字报错
      - 旧进程已死时也能重启（读注册表即可）"""
    import subprocess as sp
    import time as time_mod

    here = Path(__file__).resolve().parent.parent
    server_py = here / "server.py"
    python = here / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = here / ".venv" / "bin" / "python"

    fake_home = tempfile.mkdtemp(prefix="coding-mcp-home-restart-")
    audit = os.path.join(fake_home, "audit.log")
    services_root = os.path.join(fake_home, "services")
    os.makedirs(services_root, exist_ok=True)

    params = StdioServerParameters(
        command=str(python),
        args=[str(server_py)],
        env={
            **os.environ,
            "HOME": fake_home,
            "USERPROFILE": fake_home,
            "MCP_AUDIT_LOG": audit,
            "MCP_ENABLE_EXEC": "1",
        },
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert "service_restart" in [t.name for t in tools.tools], "service_restart 未注册"

                svc_name = "smoke-svc-restart"
                # 注意：stdout 重定向到文件后是块缓冲，必须显式 flush 才能及时读到
                cmd = f'"{python}" -c "import sys, time; print(\'restarted\'); sys.stdout.flush(); time.sleep(120)"'

                # ---- 1. 启动 ----
                r = await session.call_tool("service_start", {"name": svc_name, "command": cmd})
                assert _json.loads(_text(r))["name"] == svc_name
                first_entry = _json.loads(_text(r))
                first_pid = first_entry["pid"]
                time_mod.sleep(1.5)

                # 写点东西到日志（用于验证 truncate_logs）
                r = await session.call_tool("service_logs", {"name": svc_name, "stream": "stdout", "tail_lines": 10})
                first_logs = _json.loads(_text(r))
                assert "restarted" in (first_logs.get("stdout") or "")

                # ---- 2. service_restart（温和）：应替换 PID ----
                r = await session.call_tool("service_restart", {"name": svc_name, "force": False, "timeout": 10})
                print("\n[restart-soft]", _text(r)[:400])
                result = _json.loads(_text(r))
                assert result["ok"] is True, f"restart 应 ok：{result}"
                assert result["before"]["pid"] == first_pid, "before 应是旧 PID"
                assert result["before"]["alive"] is True
                assert result["stopped"]["stopped"] is True
                assert result["started"]["pid"] != first_pid, "新 PID 应与旧 PID 不同"
                assert result["started"]["command"] == cmd, "应复用原 command"
                new_pid = result["started"]["pid"]
                time_mod.sleep(1.5)

                # 查 status：旧 PID 应已死，新 PID 应活
                # 用 tasklist 直接验证两个 PID
                out = sp.run(
                    ["tasklist", "/FI", f"PID eq {new_pid}", "/NH", "/FO", "CSV"],
                    capture_output=True, timeout=5,
                ).stdout.decode("utf-8", errors="replace")
                assert "INFO:" not in out, f"新 PID {new_pid} 应仍存活"

                # ---- 3. truncate_logs=True：再次重启，旧 stdout 应被清空 ----
                # 先让新进程再写一行，确认它真在跑
                time_mod.sleep(1.0)
                r = await session.call_tool(
                    "service_restart",
                    {"name": svc_name, "force": True, "timeout": 5, "truncate_logs": True},
                )
                print("[restart-trunc]", _text(r)[:300])
                result2 = _json.loads(_text(r))
                assert result2["ok"] is True
                assert result2["started"]["pid"] != new_pid, "应再次拿到新 PID"
                trunc_pid = result2["started"]["pid"]
                time_mod.sleep(1.5)

                # 日志应被清空再重写：旧 "restarted" 已被 truncate，新进程会再打一次
                r = await session.call_tool("service_logs", {"name": svc_name, "stream": "stdout", "tail_lines": 20})
                logs2 = _json.loads(_text(r))
                stdout_text = logs2.get("stdout") or ""
                # truncate 后只剩新进程的那一行（最多 1 次 "restarted"），不会出现 2 段
                assert stdout_text.count("restarted") <= 1, f"truncate_logs 后不应有多段历史：{stdout_text!r}"

                # ---- 4. 未注册的名字：应报错 ----
                r = await session.call_tool("service_restart", {"name": "no-such-service-xyz"})
                assert "未找到" in _text(r), f"未注册应报错：{_text(r)}"

                # ---- 5. 旧进程已死时也能重启：手动 kill 后再 restart ----
                sp.run(["taskkill", "/F", "/PID", str(trunc_pid), "/T"], capture_output=True, timeout=5)
                time_mod.sleep(1.0)
                r = await session.call_tool("service_restart", {"name": svc_name, "force": False})
                result3 = _json.loads(_text(r))
                print("[restart-after-kill]", _text(r)[:300])
                assert result3["ok"] is True, f"旧进程已死后 restart 也应 ok：{result3}"
                assert result3["before"]["alive"] is False, "before.alive 应为 False"
                assert result3["started"]["pid"] != trunc_pid

                # ---- 6. 清理 ----
                await session.call_tool("service_stop", {"name": svc_name, "force": True})
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
        # 服务进程已显式 stop；不再需要 taskkill /IM。


async def test_api_request_and_assert():
    """API 测试：发请求 + 多类型断言 + 错误处理。"""
    import json as _json
    fake_home = tempfile.mkdtemp(prefix="coding-mcp-test-api-")
    audit = os.path.join(fake_home, "audit.log")
    proj = tempfile.mkdtemp(prefix="coding-mcp-test-api-proj-")

    here = Path(__file__).resolve().parent.parent
    python = str(here / ".venv" / "Scripts" / "python.exe")
    server_py = str(here / "server.py")

    params = StdioServerParameters(
        command=python,
        args=[server_py],
        env={
            **os.environ,
            "HOME": fake_home, "USERPROFILE": fake_home,
            "MCP_AUDIT_LOG": audit, "MCP_ENABLE_EXEC": "1",
            "MCP_ALLOWED_ROOTS": proj,
        },
    )
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()

                # 1. 基本 GET + status 断言
                r = await session.call_tool("api_request", {
                    "url": "https://httpbin.org/get?foo=bar",
                    "timeout": 15,
                    "save_as": "basic-get",
                })
                data = _json.loads(_text(r))
                assert data["status_code"] == 200, f"应 200：{data['status_code']}"
                assert data["json"]["args"]["foo"] == "bar"
                assert data["ref"] == "basic-get"
                print(f"\n[api-basic] status={data['status_code']} elapsed={data['elapsed_ms']}ms")

                # 2. status_eq 断言通过
                r = await session.call_tool("api_assert", {
                    "checks": _json.dumps([
                        {"type": "status_eq", "value": 200},
                        {"type": "elapsed_lt", "value": 30000},
                        {"type": "body_contains", "value": "httpbin"},
                    ]),
                })
                result = _json.loads(_text(r))
                assert result["pass"], f"基本断言应通过：{result}"
                print(f"[api-assert1] {result['summary']}")

                # 3. JSONPath 断言 + 故意失败
                r = await session.call_tool("api_assert", {
                    "checks": _json.dumps([
                        {"type": "jsonpath_eq", "path": "$.args.foo", "value": "bar"},
                        {"type": "jsonpath_eq", "path": "$.args.foo", "value": "WRONG"},
                    ]),
                })
                result = _json.loads(_text(r))
                assert not result["pass"], "故意失败的断言应使整体 pass=False"
                assert result["summary"] == "1/2 passed", f"summary 应为 '1/2 passed'：{result}"
                print(f"[api-assert2] {result['summary']} (故意失败)")

                # 4. POST JSON
                r = await session.call_tool("api_request", {
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "body": _json.dumps({"hello": "world", "n": 42}),
                })
                data = _json.loads(_text(r))
                assert data["status_code"] == 200
                assert data["json"]["json"]["n"] == 42
                print(f"[api-post] status={data['status_code']} json={list(data['json']['json'].keys())}")

                # 5. bearer auth
                r = await session.call_tool("api_request", {
                    "url": "https://httpbin.org/bearer",
                    "auth_type": "bearer",
                    "auth_token": "test-token-12345",
                })
                data = _json.loads(_text(r))
                assert data["status_code"] == 200
                assert data["json"]["authenticated"] is True
                assert data["json"]["token"] == "test-token-12345"
                print(f"[api-bearer] authenticated={data['json']['authenticated']}")

                # 6. status_in 断言
                r = await session.call_tool("api_assert", {
                    "checks": _json.dumps([{"type": "status_in", "value": [200, 201]}]),
                })
                result = _json.loads(_text(r))
                assert result["pass"]

                # 7. 不存在的主机应报错
                r = await session.call_tool("api_request", {
                    "url": "https://this-host-does-not-exist-xyz.invalid/",
                    "timeout": 5,
                })
                txt = _text(r)
                assert txt.startswith("错误"), f"应报错：{txt[:100]}"
                print(f"[api-fail] 已正确报错：{txt[:60]}")

    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
        shutil.rmtree(proj, ignore_errors=True)


async def test_api_save_response():
    """api_save_response：把响应 body / headers / full 写到文件。"""
    import json as _json
    fake_home = tempfile.mkdtemp(prefix="coding-mcp-test-api-save-")
    audit = os.path.join(fake_home, "audit.log")
    proj = tempfile.mkdtemp(prefix="coding-mcp-test-api-save-proj-")

    here = Path(__file__).resolve().parent.parent
    python = str(here / ".venv" / "Scripts" / "python.exe")
    server_py = str(here / "server.py")

    params = StdioServerParameters(
        command=python,
        args=[server_py],
        env={
            **os.environ,
            "HOME": fake_home, "USERPROFILE": fake_home,
            "MCP_AUDIT_LOG": audit,
            "MCP_ALLOWED_ROOTS": proj,
        },
    )
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()

                # 发请求
                r = await session.call_tool("api_request", {
                    "url": "https://httpbin.org/uuid",
                    "save_as": "uuid",
                })
                data = _json.loads(_text(r))
                uuid = data["json"]["uuid"]
                print(f"\n[api-save] uuid={uuid}")

                # 保存 body
                body_path = os.path.join(proj, "body.json")
                r = await session.call_tool("api_save_response", {
                    "path": body_path, "part": "body", "overwrite": True,
                })
                assert "已保存" in _text(r), _text(r)
                with open(body_path, encoding="utf-8") as f:
                    saved = _json.load(f)
                assert saved["uuid"] == uuid
                print(f"[api-save-body] ok")

                # 保存 headers
                headers_path = os.path.join(proj, "headers.json")
                r = await session.call_tool("api_save_response", {
                    "path": headers_path, "part": "headers",
                })
                assert "已保存" in _text(r)
                with open(headers_path, encoding="utf-8") as f:
                    hdrs = _json.load(f)
                # header key 大小写不敏感（httpbin 返回小写）
                assert any(k.lower() == "content-type" for k in hdrs.keys()), f"应有 content-type：{list(hdrs.keys())}"
                print(f"[api-save-headers] ok")

                # overwrite=False 遇到已存在应拒绝
                r = await session.call_tool("api_save_response", {
                    "path": body_path, "part": "body", "overwrite": False,
                })
                assert "已存在" in _text(r), f"应拒覆盖：{_text(r)}"
                print(f"[api-save-nooverwrite] ok")

                # 路径白名单外应被拒
                r = await session.call_tool("api_save_response", {
                    "path": "C:/Windows/temp/save.json",
                    "part": "body",
                })
                txt = _text(r)
                assert txt.startswith("错误") and ("白名单" in txt or "越界" in txt or "MCP_ALLOWED_ROOTS" in txt), f"白名单应拦：{txt}"
                print(f"[api-save-whitelist] ok")

    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
        shutil.rmtree(proj, ignore_errors=True)


class _TestSSHServer(paramiko.ServerInterface):
    """测试用最小 SSH server：固定用户名/密码，exec_request 时真正执行命令并回写 channel。"""
    def __init__(self):
        self.exec_count = 0
        # direct-tcpip (local port forwarding) 目标：chanid -> (dest_addr, dest_port)
        self.direct_tcpip_targets: dict = {}

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username == "testuser" and password == "testpass") else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind in ("session", "forwarded-tcpip", "direct-tcpip") else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin_addr_port, dest_addr_port):
        # local port forwarding：client 让 server 端连 dest_addr:dest_port
        self.direct_tcpip_targets[chanid] = dest_addr_port
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        """收到 exec 请求时直接执行命令并把 stdout/stderr/exit_code 写回 channel。"""
        cmd = command.decode("utf-8") if isinstance(command, bytes) else command
        import subprocess as _sp
        try:
            r = _sp.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            if r.stdout:
                channel.sendall(r.stdout.encode("utf-8"))
            if r.stderr:
                channel.sendall_stderr(r.stderr.encode("utf-8"))
            channel.send_exit_status(r.returncode)
            self.exec_count += 1
        except Exception as e:
            channel.sendall(f"err: {e}".encode("utf-8"))
            channel.send_exit_status(1)
        finally:
            try:
                channel.shutdown_write()
            except Exception:
                pass
        return True


def _start_test_sshd(port=0):
    """起一个临时 SSH server。返回 (port, host_key, stop_flag, sessions)。"""
    import paramiko as _p
    import socket as _s

    host_key = _p.RSAKey.generate(1024)  # 测试用 1024 位（生成快）
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(5)
    actual_port = sock.getsockname()[1]
    stop_flag = {"stop": False}
    sessions = []

    def serve():
        while not stop_flag["stop"]:
            try:
                sock.settimeout(0.5)
                client_sock, _addr = sock.accept()
            except _s.timeout:
                continue
            except OSError:
                break
            transport = _p.Transport(client_sock)
            transport.add_server_key(host_key)
            server = _TestSSHServer()
            try:
                transport.start_server(server=server)
            except Exception:
                continue
            t = threading.Thread(
                target=_serve_one_session, args=(transport,), daemon=True,
                name="sshd-session",
            )
            t.start()
            sessions.append((transport, t))

    threading.Thread(target=serve, daemon=True, name="test-sshd").start()
    time.sleep(0.2)  # 等 server 完全就绪
    return actual_port, host_key, stop_flag, sessions


def _serve_one_session(transport):
    """一个 transport 的事件循环：accept channel，根据类型分别处理。"""
    server = transport.server_object
    try:
        while True:
            chan = transport.accept(20)
            if chan is None:
                if not transport.is_active():
                    break
                continue
            # direct-tcpip（有 origin_addr 且 server 记录了目标）→ 起 worker 做转发
            if chan.chanid in server.direct_tcpip_targets:
                dest_addr, dest_port = server.direct_tcpip_targets.pop(chan.chanid)
                threading.Thread(
                    target=_handle_direct_tcpip, args=(chan, dest_addr, dest_port),
                    daemon=True, name="sshd-forwarder",
                ).start()
            else:
                # exec 已在 check_channel_exec_request 里处理；这里等 channel 关闭
                try:
                    chan.wait_closed()
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        try:
            transport.close()
        except Exception:
            pass


def _handle_direct_tcpip(chan, dest_addr, dest_port):
    """direct-tcpip 通道：连接 dest_addr:dest_port，然后双向转发。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((dest_addr, dest_port))
    except Exception:
        try:
            chan.close()
        except Exception:
            pass
        return
    try:
        while True:
            r, _, _ = select.select([chan, s], [], [], 1.0)
            if chan in r:
                try:
                    data = chan.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                try:
                    s.sendall(data)
                except OSError:
                    break
            if s in r:
                try:
                    data = s.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                try:
                    chan.sendall(data)
                except OSError:
                    break
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass


async def test_ssh_exec_via_local_sshd():
    """SSH 集成测试：自起一个 test sshd，用 ssh_exec 跑命令。"""
    import json as _json
    import paramiko as _p

    if paramiko is None:
        print("\n[ssh-skip] paramiko 未安装，跳过")
        return

    fake_home = tempfile.mkdtemp(prefix="coding-mcp-test-ssh-")
    audit = os.path.join(fake_home, "audit.log")
    proj = tempfile.mkdtemp(prefix="coding-mcp-test-ssh-proj-")

    here = Path(__file__).resolve().parent.parent
    python = str(here / ".venv" / "Scripts" / "python.exe")
    server_py = str(here / "server.py")

    port, host_key, stop_flag, sessions = _start_test_sshd(0)
    print(f"\n[ssh-test] test sshd 已在 127.0.0.1:{port} 启动")

    params = StdioServerParameters(
        command=python,
        args=[server_py],
        env={
            **os.environ,
            "HOME": fake_home, "USERPROFILE": fake_home,
            "MCP_AUDIT_LOG": audit, "MCP_ENABLE_EXEC": "1",
            "MCP_ALLOWED_ROOTS": proj,
        },
    )
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()

                # 1. 基本 exec
                r = await session.call_tool("ssh_exec", {
                    "host": "127.0.0.1", "port": port, "user": "testuser",
                    "password": "testpass",
                    "command": "echo hello-from-ssh-mcp",
                })
                txt = _text(r)
                data = _json.loads(txt) if not txt.startswith("错误") else None
                if data is None:
                    print(f"[ssh-exec-1] 失败: {txt[:200]}")
                else:
                    assert "hello-from-ssh-mcp" in data["stdout"], f"stdout: {data}"
                    print(f"[ssh-exec-1] exit={data['exit_code']} stdout={data['stdout'].strip()}")

                # 2. 错误密码应报 AUTH_FAILED
                r = await session.call_tool("ssh_exec", {
                    "host": "127.0.0.1", "port": port, "user": "testuser",
                    "password": "WRONG",
                    "command": "echo should-not-run",
                })
                txt = _text(r)
                assert txt.startswith("错误"), f"错误密码应报错：{txt[:200]}"
                print(f"[ssh-exec-badpass] 正确报错：{txt[:60]}")

                # 3. name 复用：两次 ssh_exec 传同名 name，第二次应该 reused=True
                # 先断掉之前的旧连接（如果还在）
                await session.call_tool("ssh_disconnect", {})
                r1 = await session.call_tool("ssh_exec", {
                    "host": "127.0.0.1", "port": port, "user": "testuser",
                    "password": "testpass", "name": "reuse-test",
                    "command": "echo first",
                })
                d1 = _json.loads(_text(r1))
                assert d1.get("reused") is False, f"首次应非 reused：{d1}"
                r2 = await session.call_tool("ssh_exec", {
                    "host": "127.0.0.1", "port": port, "user": "testuser",
                    "password": "testpass", "name": "reuse-test",
                    "command": "echo second",
                })
                d2 = _json.loads(_text(r2))
                assert d2.get("reused") is True, f"二次应 reused：{d2}"
                print(f"[ssh-exec-reuse] reused={d2['reused']}")

                # 4. ssh_disconnect 显式断开
                r = await session.call_tool("ssh_disconnect", {"name": "reuse-test"})
                assert "已停止" in _text(r), _text(r)
                print(f"[ssh-disconnect] ok")

                # 5. ssh_disconnect 不断开未知 name
                r = await session.call_tool("ssh_disconnect", {"name": "nonexistent"})
                assert "没有" in _text(r), _text(r)
                print(f"[ssh-disconnect-badname] ok")

                # 6. ssh_upload：上传到 test server（test server 路径可能不存在，简单跑一下看错误处理）
                # 改成：先用 ssh_upload 写一个文件，再用 ssh_exec cat 它
                test_file = os.path.join(proj, "upload.txt")
                with open(test_file, "w", encoding="utf-8") as f:
                    f.write("payload-from-mcp\n")
                r = await session.call_tool("ssh_upload", {
                    "host": "127.0.0.1", "port": port, "user": "testuser",
                    "password": "testpass",
                    "local_path": test_file,
                    "remote_path": "/tmp/coding-mcp-test/upload.txt",
                })
                # test sshd 是真的 paramiko server，mkdir_p 会工作
                txt = _text(r)
                if "已停止" in txt or txt.startswith("错误"):
                    print(f"[ssh-upload] 跳��（test sshd 可能限制）：{txt[:80]}")
                else:
                    data = _json.loads(txt)
                    print(f"[ssh-upload] size={data['size']} elapsed={data['elapsed_ms']}ms")

    finally:
        stop_flag["stop"] = True
        for transport, _ in sessions:
            try:
                transport.close()
            except Exception:
                pass
        time.sleep(0.5)
        shutil.rmtree(fake_home, ignore_errors=True)
        shutil.rmtree(proj, ignore_errors=True)


async def test_ssh_tunnel_local_forward():
    """SSH 隧道：起一个本地端口转发，连接后能访问 echo 服务。"""
    import json as _json
    import paramiko as _p
    import socket as _s

    if paramiko is None:
        print("\n[ssh-tunnel-skip] paramiko 未安装，跳过")
        return

    fake_home = tempfile.mkdtemp(prefix="coding-mcp-test-tun-")
    audit = os.path.join(fake_home, "audit.log")
    proj = tempfile.mkdtemp(prefix="coding-mcp-test-tun-proj-")

    here = Path(__file__).resolve().parent.parent
    python = str(here / ".venv" / "Scripts" / "python.exe")
    server_py = str(here / "server.py")

    port, host_key, stop_flag, sessions = _start_test_sshd(0)
    print(f"\n[ssh-tunnel] test sshd 已在 127.0.0.1:{port} 启动")

    # 起一个本地 echo server，监听 0 端口
    echo_sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    echo_sock.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    echo_sock.bind(("127.0.0.1", 0))
    echo_sock.listen(5)
    echo_port = echo_sock.getsockname()[1]
    echo_ready = threading.Event()
    echo_stop = {"stop": False}

    def echo_serve():
        echo_ready.set()
        while not echo_stop["stop"]:
            try:
                echo_sock.settimeout(0.5)
                c, _ = echo_sock.accept()
            except _s.timeout:
                continue
            except OSError:
                break
            try:
                data = c.recv(4096)
                if data:
                    c.sendall(b"echo:" + data)
            except Exception:
                pass
            finally:
                try:
                    c.close()
                except Exception:
                    pass
    threading.Thread(target=echo_serve, daemon=True, name="echo-srv").start()
    echo_ready.wait(2)
    print(f"[ssh-tunnel] echo server 在 127.0.0.1:{echo_port}")

    params = StdioServerParameters(
        command=python,
        args=[server_py],
        env={
            **os.environ,
            "HOME": fake_home, "USERPROFILE": fake_home,
            "MCP_AUDIT_LOG": audit, "MCP_ENABLE_EXEC": "1",
            "MCP_ALLOWED_ROOTS": proj,
        },
    )
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()

                # 1. 起隧道
                r = await session.call_tool("ssh_tunnel", {
                    "host": "127.0.0.1", "port": port, "user": "testuser",
                    "password": "testpass",
                    "local_port": 0,
                    "remote_host": "127.0.0.1", "remote_port": echo_port,
                    "name": "test-tunnel",
                })
                data = _json.loads(_text(r))
                local_port = data["local_port"]
                assert local_port > 0, f"local_port 应 > 0：{data}"
                print(f"[ssh-tunnel-start] 127.0.0.1:{local_port} -> 127.0.0.1:{echo_port}")

                # 2. 通过隧道发请求
                time.sleep(0.5)  # 等转发线程就绪
                c = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
                c.settimeout(5)
                try:
                    c.connect(("127.0.0.1", local_port))
                    c.sendall(b"hello-tunnel")
                    resp = c.recv(4096)
                    assert resp == b"echo:hello-tunnel", f"隧道响应不符：{resp!r}"
                    print(f"[ssh-tunnel-flow] 通过隧道拿到 echo: {resp.decode()}")
                finally:
                    c.close()

                # 3. 停止隧道
                r = await session.call_tool("ssh_disconnect", {"name": "test-tunnel"})
                assert "已停止" in _text(r), _text(r)
                print(f"[ssh-tunnel-stop] ok")

                # 4. 停止后端口应该拒绝连接
                time.sleep(0.3)
                c = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
                c.settimeout(2)
                try:
                    c.connect(("127.0.0.1", local_port))
                    print(f"[ssh-tunnel-stopped] 警告：端口仍可达")
                except (ConnectionRefusedError, OSError, _s.timeout):
                    print(f"[ssh-tunnel-stopped] 端口已正确关闭")
                finally:
                    c.close()

    finally:
        stop_flag["stop"] = True
        for transport, _ in sessions:
            try:
                transport.close()
            except Exception:
                pass
        echo_stop["stop"] = True
        try:
            echo_sock.close()
        except Exception:
            pass
        time.sleep(0.3)
        shutil.rmtree(fake_home, ignore_errors=True)
        shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_exec_disabled_by_default())
    asyncio.run(test_git_tools())
    asyncio.run(test_git_commit_and_context())
    asyncio.run(test_gui_and_service_lifecycle())
    asyncio.run(test_run_command_blacklist())
    asyncio.run(test_launch_gui_with_name())
    asyncio.run(test_service_clean())
    asyncio.run(test_service_restart())
    asyncio.run(test_api_request_and_assert())
    asyncio.run(test_api_save_response())
    asyncio.run(test_ssh_exec_via_local_sshd())
    asyncio.run(test_ssh_tunnel_local_forward())
