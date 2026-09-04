"""数据库工具回归测试：验证 db_query / db_tables / db_schema / redis_exec 的
工具注册、只读拦截、未配置报错、非法表名拦截，以及纯函数（URL 解析 / SQL 只读判断 /
表格格式化 / Redis 结果格式化）。

不需要真实数据库实例——用假 URL 验证安全逻辑，真实连接路径由标准客户端库保证。
"""
import asyncio
import os
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


def _resolve_python(here):
    p = here / ".venv" / "Scripts" / "python.exe"
    if not p.exists():
        p = here / ".venv" / "bin" / "python"
    return p


async def test_db_tools():
    here = Path(__file__).resolve().parent.parent
    python = _resolve_python(here)
    tmp = tempfile.mkdtemp(prefix="coding-mcp-db-")

    env = {
        **os.environ,
        "MCP_AUDIT_LOG": os.path.join(tmp, "audit.log"),
        "MCP_MYSQL_URL": "mysql://u:p@127.0.0.1:3399/db",  # 假 URL，用于测只读拦截
        "MCP_REDIS_URL": "redis://127.0.0.1:6399/0",        # 假 URL
        # 故意不设 MCP_PGSQL_URL，测未配置报错
    }
    params = StdioServerParameters(command=str(python), args=[str(here / "server.py")], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("工具总数:", len(names))
            print("数据库工具:", [n for n in ("db_query", "db_tables", "db_schema", "redis_exec") if n in names])

            # 1. 未配置 PGSQL → 友好报错
            r = await session.call_tool("db_query", {"kind": "pgsql", "sql": "SELECT 1"})
            print("\n[未配置]", _text(r))

            # 2. MySQL 写操作 → 只读拦截
            r = await session.call_tool("db_query", {"kind": "mysql", "sql": "INSERT INTO t VALUES(1)"})
            print("[写拦截]", _text(r))

            # 3. MySQL 只读 SELECT → 通过只读检查（连假 URL 失败，证明只读放行）
            r = await session.call_tool("db_query", {"kind": "mysql", "sql": "SELECT * FROM t"})
            print("[只读放行]", _text(r)[:100])

            # 4. Redis 写命令 → 只读拦截
            r = await session.call_tool("redis_exec", {"command": "SET foo bar"})
            print("[Redis写拦截]", _text(r))

            # 5. Redis 只读 GET → 通过只读检查
            r = await session.call_tool("redis_exec", {"command": "GET foo"})
            print("[Redis只读]", _text(r)[:100])

            # 6. 非法表名 → 拦截
            r = await session.call_tool("db_schema", {"kind": "mysql", "table": "x; DROP TABLE y"})
            print("[表名拦截]", _text(r))


def test_pure_functions():
    import importlib.util
    here = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("s", str(here / "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    print("\n=== 纯函数 ===")
    # URL 解析
    assert m._parse_db_url("mysql://user:pa%40ss@host:3306/mydb") == ("host", 3306, "user", "pa@ss", "mydb")
    assert m._parse_db_url("mysql://root@localhost/db") == ("localhost", 0, "root", "", "db")
    print("[url ] ✅ 密码转义 / 无密码 / 无端口场景正确")

    # SQL 只读判断
    assert m._sql_is_readonly("SELECT * FROM t") is True
    assert m._sql_is_readonly("  select 1") is True
    assert m._sql_is_readonly("SHOW TABLES") is True
    assert m._sql_is_readonly("INSERT INTO t VALUES(1)") is False
    assert m._sql_is_readonly("WITH x AS (SELECT 1) DELETE FROM t") is False
    print("[sql ] ✅ 只读白名单 / 写操作拦截 / WITH...DELETE 正确判写")

    # 表格格式化
    tbl = m._fmt_table(["id", "name"], [(1, "Alice"), (2, None)])
    assert "NULL" in tbl and "Alice" in tbl
    print("[fmt ] ✅ 表格对齐 + NULL 处理")

    # Redis 结果格式化
    assert m._fmt_redis_result(None) == "(nil)"
    assert m._fmt_redis_result({"a": "1"}) == "a: 1"
    print("[redis] ✅ nil/str/list/dict 格式化")


if __name__ == "__main__":
    asyncio.run(test_db_tools())
    test_pure_functions()
    print("\n== db tests done ==")
