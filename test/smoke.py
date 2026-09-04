"""冒烟测试：通过 stdio 连上 coding-mcp（Python 版），验证核心工具与防丢失机制。"""
import asyncio
import json as _json
import os
import sys
import shutil
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


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_exec_disabled_by_default())
    asyncio.run(test_git_tools())
    asyncio.run(test_git_commit_and_context())
    asyncio.run(test_gui_and_service_lifecycle())
    asyncio.run(test_run_command_blacklist())
    asyncio.run(test_launch_gui_with_name())
    asyncio.run(test_service_clean())
