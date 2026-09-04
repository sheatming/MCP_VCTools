# coding-mcp —— 通用编码 MCP 服务器（Python 版）

一个本地运行的通用编码 MCP（Model Context Protocol）服务器，向 AI 助手提供安全的文件读写、版本控制、命令执行、数据库访问、**GUI 与后台服务托管**等能力。纯 Python 实现，无需 Node.js。

## 为什么安全（三个核心要求）

| 要求         | 实现方式                                                                   |
| ---------- | ---------------------------------------------------------------------- |
| **不占用文件**  | 所有读取/写入均"打开即关"，从不持有长句柄；写入采用「临时文件 + fsync + 原子重命名」，绝不锁定目标文件，也不产生半写入的坏文件 |
| **自动保存**   | 每次写入/编辑立即 `fsync` 落盘并原子替换，不存在"未保存"的中间状态                                |
| **避免代码丢失** | 任何修改前，自动把上一版备份到 `.coding-mcp-backup/`（带时间戳）；配合原子写入，进程中断、断电也不会丢代码       |

## 提供的工具（共 28 个）

### 文件与搜索

| 工具               | 作用                                 |
| ---------------- | ---------------------------------- |
| `read_file`      | 读取文本文件（UTF-8，自动识别 BOM）             |
| `write_file`     | 写入完整内容（自动保存 + 自动备份）                |
| `edit_file`      | 精确字符串查找替换（可替换全部，默认要求唯一匹配）          |
| `list_directory` | 列出目录条目                             |
| `search_files`   | 递归搜索文件内容（跳过 node_modules/.git/二进制） |
| `diff_files`     | 对比两个文件差异（unified diff 格式）          |

### git 版本控制

| 工具               | 作用                                 |
| ---------------- | ---------------------------------- |
| `git`            | 执行任意 git 子命令（安全封装，禁 shell）          |
| `git_status`     | 查看工作区状态（status --short --branch）    |
| `git_log`        | 查看提交历史（oneline）                     |
| `git_diff`       | 查看未暂存/暂存区差异                        |
| `git_add`        | 暂存文件（git add）                       |
| `git_commit`     | 提交暂存改动（git commit -m）                |

### 构建 / 测试 / 依赖

| 工具               | 作用                                 |
| ---------------- | ---------------------------------- |
| `install_deps`   | 自动探测包管理器并安装依赖（npm/yarn/pnpm/pip/poetry/uv/cargo/**go**） |
| `run_tests`      | 自动探测测试框架并运行（pytest/jest/vitest/**go test**/cargo） |
| `run_lint`       | 自动探测并运行 lint/格式化（ruff/eslint/prettier/**go vet**）  |
| `run_command`    | 执行命令行（需显式开启，带超时与退出码）            |

### 项目感知与开发上下文

| 工具               | 作用                                 |
| ---------------- | ---------------------------------- |
| `get_project_info`| 识别语言、包管理器、测试框架、目录结构             |
| `load_dev_context`| 加载项目预制开发上下文（技术文档/规范/进度等）       |

### 数据库（默认只读）

| 工具               | 作用                                 |
| ---------------- | ---------------------------------- |
| `db_query`       | 在 MySQL / PostgreSQL 上执行 SQL（默认只读）    |
| `db_tables`      | 列出数据库中的所有表                        |
| `db_schema`      | 查看表结构（字段名/类型/是否可空/默认值）           |
| `redis_exec`     | 执行 Redis 命令（默认只读）                  |

### GUI 与后台服务（解决 run_command 不适合长驻进程的问题）

| 工具               | 作用                                 |
| ---------------- | ---------------------------------- |
| `launch_gui`     | 启动 GUI 应用或一次性可执行（默认火即忘，可选 `name` 参数纳入服务托管） |
| `service_start`  | 启动一个后台守护进程并托管其生命周期（日志/PID/幂等） |
| `service_stop`   | 停止后台服务（先温和、再强杀）                |
| `service_status` | 查询单个或所有托管服务的存活状态              |
| `service_logs`   | 读取后台服务的 stdout/stderr 日志末尾       |
| `service_clean`  | 扫描注册表，清理已死进程对应的孤儿条目         |

GUI/服务的设计要点：
- 进程**与 MCP 服务器解耦**——Windows 用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB`，Unix 用 `start_new_session`。MCP 重启/退出不会带走子进程。
- **日志原样落盘**：用二进制文件 + 直传 fd 给子进程，避开 subprocess 内部 reader 线程在非 UTF-8 输出时的 `UnicodeDecodeError`。
- **PID 文件 + 注册表双轨**：`~/.coding-mcp/services.json` + 每个服务一个 `pid` 文件，幂等性靠二者交叉验证。
- **优雅停止**：先发信号给 2 秒（Windows 几乎立即转强杀），不退出再 `taskkill /F /T`。

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
| `MCP_MYSQL_URL=mysql://user:pass@host:3306/db` | MySQL 连接（未设置则 `db_*` 的 mysql 不可用）       |
| `MCP_PGSQL_URL=postgresql://user:pass@host:5432/db` | PostgreSQL 连接（未设置则 `db_*` 的 pgsql 不可用） |
| `MCP_REDIS_URL=redis://:pass@host:6379/0` | Redis 连接（未设置则 `redis_exec` 不可用）          |
| `MCP_DB_ALLOW_WRITE=1`        | 允许数据库写操作（**默认只读**，需显式开启）                       |

## 数据库支持

连接信息通过环境变量 `MCP_MYSQL_URL` / `MCP_PGSQL_URL` / `MCP_REDIS_URL` 提供（URL 格式，密码可用 URL 编码如 `%40` 表示 `@`）。

- `db_query`：执行 SQL。**默认只读**——`SELECT/SHOW/DESC/DESCRIBE/EXPLAIN` 放行，`INSERT/UPDATE/DELETE/DDL` 等写操作需设置 `MCP_DB_ALLOW_WRITE=1`。
- `db_tables`：列出所有表。
- `db_schema`：查看表结构（表名做白名单校验，防 SQL 注入）。
- `redis_exec`：执行 Redis 命令（如 `GET foo`、`KEYS *`、`HGETALL h`）。**默认只读**，写命令（`SET/DEL/...`）需设置 `MCP_DB_ALLOW_WRITE=1`。
- 所有数据库操作都写入审计日志；结果超长自动截断（8000 字符）。

> ⚠️ 数据库默认只读是最安全的形态。开启 `MCP_DB_ALLOW_WRITE=1` 后 AI 可执行任意写 SQL/Redis 写命令，请仅在可信环境使用。

## 命令执行（run_command）

默认**关闭**，出于安全考虑需显式开启：在 MCP 配置里给服务器加上环境变量 `MCP_ENABLE_EXEC=1`。

- 返回退出码、标准输出、标准错误三段，便于区分。
- 带超时控制（默认 30 秒，可用 `MCP_EXEC_TIMEOUT` 调上限；单次调用也可传 `timeout` 参数，但不会超过上限）。
- 输出超长自动截断（8000 字符），避免撑爆上下文。
- 每次执行都会写入审计日志（命令、退出码、成功与否）。
- 命令交给系统 shell 解析：Windows 上是 `cmd.exe`，Linux/macOS 上是 `sh`，请按对应平台写命令语法。

**只适合"有明确结束点"的命令**（编译、测试、git、pip install 等）。后端化/守护化命令会被**主动拒绝**：

| 触发写法 | 拒绝原因 | 推荐替代 |
| --- | --- | --- |
| `start xxx`（Windows cmd） | 启动独立窗口 | `launch_gui` |
| `nohup xxx` / `setsid xxx` / `disown`（Unix） | 进程脱离终端/作业 | `service_start` 或 `launch_gui` |
| `screen` / `tmux`（终端复用器） | 启动持久会话 | `service_start` |
| `python ... &`（命令以 `&` 结尾） | 把进程放入后台 | `service_start` |

`&&`（逻辑 AND）不会被误伤。

> ⚠️ 开启后，AI 助手将具备在你机器上执行任意命令的能力，请仅在可信环境使用。

## GUI 与后台服务托管

`run_command` 适合**有明确结束点**的命令（编译、测试、git 等）。但 GUI 应用和后台守护进程没有自然结束点——硬塞给 `run_command` 会导致：

- **GUI 应用**：窗口会随超时被杀，用户看不到
- **后台守护**：进程被 timeout 强杀，留下端口/文件残留
- **漏网进程**：火即忘后无法清理，PID 拿不回来

所以拆出三个语义清晰的工具：

### `launch_gui` — 启动 GUI 或一次性可执行

- 默认**火即忘**：启动后立即返回 `{"pid":..., "command":...}`
- 可选 `wait=True`：等到结束（一次性安装包等需要等结果）
- 可选 `name="xxx"`：纳入服务注册表，日志/PID 都会被管理
- 不传 `name` 时**纯火即忘**——你拿不回 PID 也清不掉，请慎用

```
# 火即忘：装一个应用就完事
launch_gui(command='setup.exe /S', wait=True)

# 纳入托管：能查、能杀、能看日志
launch_gui(command='my-app.exe', name='my-app')
service_status(name='my-app')   # 查存活
service_logs(name='my-app', stream='stdout')   # 看日志
service_stop(name='my-app')    # 优雅停止
```

### `service_start` / `service_stop` / `service_status` / `service_logs` — 后台守护

| 场景 | 用法 |
| --- | --- |
| 启动 dev server | `service_start(name='web', command='npm run dev')` |
| 查所有托管服务 | `service_status()` |
| 查单个服务 | `service_status(name='web')` |
| 看日志末尾 100 行 | `service_logs(name='web', stream='stdout', tail_lines=100)` |
| 优雅停止 | `service_stop(name='web')` |
| 强杀（不优雅） | `service_stop(name='web', force=True)` |

### `service_clean` — 清理孤儿

服务器重启或进程被外部 kill 后，注册表里可能残留"进程已死"的条目。`service_clean` 扫描注册表，移除已死条目：

```
service_clean()                  # 移除已死条目，保留日志目录
service_clean(remove_logs=True)  # 连日志目录一起删
service_clean(dry_run=True)      # 只看不删
```

返回结构：`{"scanned":N, "removed":[{name, pid, had_pid_file, logs_removed}], "alive":M}`。

### 服务注册表位置

```
~/.coding-mcp/
├── services.json                  # 注册表（JSON 列表）
└── services/
    └── <name>/
        ├── pid                    # 进程 ID
        └── logs/
            ├── stdout.log         # 标准输出（持续追加，二进制安全）
            └── stderr.log         # 标准错误
```

服务名只允许字母/数字/`.`/`_`/`-`，最长 64 字符。

### 进程安全保证

子进程与 MCP 服务器**完全解耦**：

| 平台 | 标志 |
| --- | --- |
| Windows | `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` |
| Unix | `start_new_session=True` |

MCP 重启、退出、被杀，**不会影响已托管的子进程**。子进程的 stdout/stderr 写入日志不经过 MCP server 中转，MCP 挂掉时日志也不会丢。

### `run_command` 边界

为了从源头避免误用，`run_command` 主动拒绝后端化命令（详见上文命令执行章节）。看到"首词 `start`/`nohup`/..."错误时，就知道该用 `launch_gui` 或 `service_start` 了。

## 预制开发流程（dev-context）

为避免每次开发都要反复口头交代、防止遗漏规范，支持在**任意项目根目录**放一个 `.dev-context/` 目录，里面放技术文档、开发规范、构建规范、开发进度等 Markdown 文件。

AI 开发时会用 `load_dev_context` 工具自动读取这些文件作为上下文，形成固定开发流程：

1. `load_dev_context` —— 读技术文档 / 规范 / 进度
2. `get_project_info` —— 识别语言、包管理器、目录结构
3. 改代码 → `install_deps` → `run_lint` → `run_tests`
4. `git_diff` 回看 → `git_add` + `git_commit` 提交

```
你的项目/
├── .dev-context/
│   ├── 技术文档.md
│   ├── 开发规范.md
│   ├── 构建规范.md
│   └── 开发进度.md
└── ...
```

完整的模板和说明见 `docs/dev-context-guide.md` 与 `docs/templates/` 目录。

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
