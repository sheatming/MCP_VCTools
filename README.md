# coding-mcp —— 通用编码 MCP 服务器（Python 版）

一个本地运行的通用编码 MCP（Model Context Protocol）服务器，向 AI 助手提供安全的文件读写与搜索能力。纯 Python 实现，无需 Node.js。

## 为什么安全（三个核心要求）

| 要求         | 实现方式                                                                   |
| ---------- | ---------------------------------------------------------------------- |
| **不占用文件**  | 所有读取/写入均"打开即关"，从不持有长句柄；写入采用「临时文件 + fsync + 原子重命名」，绝不锁定目标文件，也不产生半写入的坏文件 |
| **自动保存**   | 每次写入/编辑立即 `fsync` 落盘并原子替换，不存在"未保存"的中间状态                                |
| **避免代码丢失** | 任何修改前，自动把上一版备份到 `.coding-mcp-backup/`（带时间戳）；配合原子写入，进程中断、断电也不会丢代码       |

## 提供的工具

| 工具               | 作用                                 |
| ---------------- | ---------------------------------- |
| `read_file`      | 读取文本文件（UTF-8，自动识别 BOM）             |
| `write_file`     | 写入完整内容（自动保存 + 自动备份）                |
| `edit_file`      | 精确字符串查找替换（可替换全部，默认要求唯一匹配）          |
| `list_directory` | 列出目录条目                             |
| `search_files`   | 递归搜索文件内容（跳过 node_modules/.git/二进制） |
| `diff_files`     | 对比两个文件差异（unified diff 格式）          |
| `git`            | 执行任意 git 子命令（安全封装，禁 shell）          |
| `git_status`     | 查看工作区状态（等价 git status --short --branch）|
| `git_log`        | 查看提交历史（oneline 格式）                |
| `run_command`    | 执行命令行（需显式开启，带超时与退出码）            |

## 环境要求

- Python 3.10+（本项目在 Python 3.13 上验证，无需 Node.js）

## 本地运行

```bash
cd coding-mcp

# 1. 创建虚拟环境并安装依赖（首次）
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # Windows
# .venv/bin/python -m pip install -r requirements.txt        # macOS/Linux

# 2. 启动（等价于 .venv/Scripts/python server.py）
.venv/Scripts/python server.py
```

## 环境变量（可选）

| 变量                            | 说明                                                 |
| ----------------------------- | -------------------------------------------------- |
| `MCP_DISABLE_BACKUP=1`        | 关闭自动备份                                             |
| `MCP_BACKUP_DIR=/abs/path`    | 自定义备份目录（默认在目标文件旁 `.coding-mcp-backup/`）            |
| `MCP_ALLOWED_ROOTS=/a;/b`     | 限制可访问的根目录（Windows 用 `;` 分隔，未设置则不限制）                |
| `MCP_DISABLE_AUDIT=1`         | 关闭操作审计日志                                           |
| `MCP_AUDIT_LOG=/abs/file.log` | 自定义审计日志路径（默认 `~/.coding-mcp/audit.log`，JSONL 一行一条） |
| `MCP_ENABLE_EXEC=1`           | 开启 `run_command` 命令执行（**默认关闭**，需显式开启）              |
| `MCP_EXEC_TIMEOUT=30`         | 命令执行超时上限（秒，默认 30）                              |
| `MCP_GIT_BIN=/path/to/git`    | 指定 git 可执行文件路径（默认从 PATH 查找 `git`）            |

## 命令执行（run_command）

默认**关闭**，出于安全考虑需显式开启：在 MCP 配置里给服务器加上环境变量 `MCP_ENABLE_EXEC=1`。

- 返回退出码、标准输出、标准错误三段，便于区分。
- 带超时控制（默认 30 秒，可用 `MCP_EXEC_TIMEOUT` 调上限；单次调用也可传 `timeout` 参数，但不会超过上限）。
- 输出超长自动截断（8000 字符），避免撑爆上下文。
- 每次执行都会写入审计日志（命令、退出码、成功与否）。
- 命令交给系统 shell 解析：Windows 上是 `cmd.exe`，Linux/macOS 上是 `sh`，请按对应平台写命令语法。

> ⚠️ 开启后，AI 助手将具备在你机器上执行任意命令的能力，请仅在可信环境使用。

## 接入 WorkBuddy

已写入 `~/.workbuddy/mcp.json`：

```json
{
  "mcpServers": {
    "coding-mcp": {
      "type": "stdio",
      "command": "C:\\Users\\gaofei\\WorkBuddy\\2026-09-03-20-33-59\\coding-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\gaofei\\WorkBuddy\\2026-09-03-20-33-59\\coding-mcp\\server.py"]
    }
  }
}
```



> 注意：MCP 新增后不会自动生效，需在 WorkBuddy 右上角「连接器管理」中找到 `coding-mcp` 并点击「信任 / 启用」。

## 接入其它 MCP 客户端

- **Claude Desktop / Cursor / 其它 stdio 客户端**：把上面 `mcpServers.coding-mcp` 段合并到对应客户端的 MCP 配置即可（`command` 指向你的 Python 解释器）。
