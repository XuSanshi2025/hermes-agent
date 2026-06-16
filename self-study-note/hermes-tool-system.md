# Hermes 工具系统源码详解

> 代码阅读范围：`tools/registry.py`、`toolsets.py`、`model_tools.py`、`agent/tool_executor.py`、`agent/agent_runtime_helpers.py`、`run_agent.py`、`tools/terminal_tool.py`、`tools/file_tools.py`、`tools/memory_tool.py`

## 1. 总体心智模型

工具系统是 Hermes 的"能力层"。它回答一个核心问题：**模型想做什么，系统能做什么，最终怎么执行**。

整个工具系统可以理解成一条 **四层依赖链**：

```text
tools/registry.py    ← 第 1 层：注册中心（零依赖，最底层）
       ↑
tools/*.py           ← 第 2 层：各工具实现（自注册到 registry）
       ↑
toolsets.py          ← 第 3 层：工具集定义（按场景分组）
       ↑
model_tools.py       ← 第 4 层：编排层（对外公共 API）
       ↑
run_agent.py / agent/tool_executor.py / agent/agent_runtime_helpers.py
                     ← 第 5 层：Agent 循环消费（调用 + 拦截）
```

**设计原则**：每一层只依赖下面的层，不反向依赖。`registry.py` 不导入任何工具文件，`model_tools.py` 是唯一触发工具发现的入口。这避免了循环导入。

---

## 2. 第一层：`tools/registry.py` — 注册中心

### 2.1 核心数据结构

`registry.py` 只有 590 行，是整个工具系统的地基。它定义了两个核心类：

#### `ToolEntry` — 单个工具的元数据

```python
class ToolEntry:
    __slots__ = (
        "name",           # 工具名，如 "terminal"、"read_file"
        "toolset",        # 所属工具集，如 "terminal"、"file"
        "schema",         # OpenAI function calling 格式的 JSON Schema
        "handler",        # 实际执行函数（Callable）
        "check_fn",       # 可用性检查函数（返回 True/False）
        "requires_env",   # 需要的环境变量列表
        "is_async",       # 是否是 async handler
        "description",    # 描述文本
        "emoji",          # UI 显示用 emoji
        "max_result_size_chars",   # 结果最大字符数
        "dynamic_schema_overrides", # 运行时动态 schema 覆盖函数
    )
```

**关键点**：`schema` 字段就是最终发送给模型 API 的 `function` 定义，格式为：
```json
{
    "name": "terminal",
    "description": "Execute a shell command...",
    "parameters": {
        "type": "object",
        "properties": { "command": {"type": "string", ...} },
        "required": ["command"]
    }
}
```

#### `ToolRegistry` — 单例注册中心

```python
class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}      # name -> ToolEntry
        self._toolset_checks: Dict[str, Callable] = {} # toolset -> check_fn
        self._toolset_aliases: Dict[str, str] = {}     # alias -> canonical
        self._lock = threading.RLock()              # 线程安全
        self._generation: int = 0                   # 变更计数器
```

文件末尾创建了全局单例：
```python
registry = ToolRegistry()
```

### 2.2 注册方法：`register()`

每个工具文件在模块级别调用 `registry.register()` 来声明自己：

```python
def register(
    self,
    name: str,          # 工具名
    toolset: str,       # 所属工具集
    schema: dict,       # JSON Schema
    handler: Callable,  # 执行函数
    check_fn: Callable = None,    # 可用性检查
    requires_env: list = None,    # 所需环境变量
    is_async: bool = False,       # 是否异步
    emoji: str = "",              # UI emoji
    max_result_size_chars=None,   # 结果大小限制
    dynamic_schema_overrides=None, # 动态 schema
    override: bool = False,       # 是否允许覆盖已有工具
):
```

**防覆盖机制**：如果两个不同 toolset 注册了同名工具，默认拒绝并打错误日志。只有以下情况允许覆盖：
1. 两个都是 MCP 工具（`mcp-` 前缀）
2. 显式传入 `override=True`（插件主动替换内置工具）

**`_generation` 计数器**：每次 `register()` / `deregister()` 都会 +1。下游的 `get_tool_definitions()` 用它做缓存失效键——generation 变了说明工具列表发生了增删。

### 2.3 工具发现：`discover_builtin_tools()`

这是整个工具系统的**自动发现机制**，也是最精巧的部分之一：

```python
def discover_builtin_tools(tools_dir=None) -> List[str]:
    tools_path = Path(tools_dir) if tools_dir else Path(__file__).resolve().parent
    module_names = [
        f"tools.{path.stem}"
        for path in sorted(tools_path.glob("*.py"))
        if path.name not in {"__init__.py", "registry.py", "mcp_tool.py"}
        and _module_registers_tools(path)  # ← AST 预检
    ]
    for mod_name in module_names:
        importlib.import_module(mod_name)  # ← 导入触发 register()
```

**工作流程**：
1. 扫描 `tools/` 目录下所有 `.py` 文件
2. 用 **AST 解析**（不执行代码！）检查文件是否包含顶层 `registry.register(...)` 调用
3. 过滤掉 `__init__.py`、`registry.py`、`mcp_tool.py`
4. 逐个 `importlib.import_module()` —— Python 的模块导入机制会执行模块顶层代码，包括 `registry.register()` 调用

**为什么要用 AST 预检？** 因为 `tools/` 目录下有很多辅助文件（`fuzzy_match.py`、`binary_extensions.py` 等），它们不包含注册调用。直接全部 import 会浪费时间并可能触发不必要的副作用。AST 检查是纯静态分析，非常快。

### 2.4 Schema 获取：`get_definitions()`

```python
def get_definitions(self, tool_names: Set[str], quiet=False) -> List[dict]:
    result = []
    entries_by_name = {e.name: e for e in self._snapshot_entries()}
    for name in sorted(tool_names):
        entry = entries_by_name.get(name)
        if not entry:
            continue
        # 1. 运行 check_fn（带 30 秒 TTL 缓存）
        if entry.check_fn:
            if not check_results[entry.check_fn]:
                continue  # 工具不可用，跳过
        # 2. 合并动态 schema 覆盖
        schema_with_name = {**entry.schema, "name": entry.name}
        if entry.dynamic_schema_overrides:
            schema_with_name.update(entry.dynamic_schema_overrides())
        # 3. 包装成 OpenAI 格式
        result.append({"type": "function", "function": schema_with_name})
    return result
```

**输出格式**就是 OpenAI API 要求的 `tools` 参数格式：
```json
[
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "...",
            "parameters": { ... }
        }
    },
    ...
]
```

**`check_fn` TTL 缓存**：check_fn 通常要探测外部状态（Docker daemon 是否运行、Playwright 是否安装等），每次调用都探测太浪费。registry 用 30 秒 TTL 缓存结果，既保证了环境变更能及时生效，又避免了频繁探测。

### 2.5 调度执行：`dispatch()`

```python
def dispatch(self, name: str, args: dict, **kwargs) -> str:
    entry = self.get_entry(name)
    if not entry:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        if entry.is_async:
            from model_tools import _run_async
            return _run_async(entry.handler(args, **kwargs))
        return entry.handler(args, **kwargs)
    except Exception as e:
        raw = f"Tool execution failed: {type(e).__name__}: {e}"
        sanitized = _sanitize_tool_error(raw)
        return json.dumps({"error": sanitized})
```

**关键设计**：
- 所有异常都被捕获并转成 JSON 错误字符串——**工具永远不会抛异常到调用方**
- 异步 handler 自动通过 `_run_async()` 桥接到同步
- 错误消息经过 `_sanitize_tool_error()` 清洗，去掉可能被模型误解的 XML 标签、代码围栏等

### 2.6 辅助函数

```python
def tool_error(message, **extra) -> str:
    """快捷返回 JSON 错误字符串"""
    # tool_error("file not found") → '{"error": "file not found"}'

def tool_result(data=None, **kwargs) -> str:
    """快捷返回 JSON 结果字符串"""
    # tool_result(success=True, count=42) → '{"success": true, "count": 42}'
```

---

## 3. 第二层：`tools/*.py` — 工具自注册

每个工具文件都遵循相同的模式。以 `tools/terminal_tool.py` 为例：

```python
# tools/terminal_tool.py (尾部)

# 1. 定义 Schema（OpenAI function calling 格式）
TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": "Execute a shell command in the user's terminal...",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute"
            },
            ...
        },
        "required": ["command"]
    }
}

# 2. 实现 handler
def _handle_terminal(args, **kwargs):
    command = args.get("command", "")
    task_id = kwargs.get("task_id", "default")
    # ... 实际执行逻辑 ...
    return json.dumps({"success": True, "output": "..."})

# 3. 实现 check_fn
def check_terminal_requirements() -> bool:
    # 检查终端后端是否可用（local/docker/ssh/modal 等）
    ...

# 4. 在模块顶层调用 register()
registry.register(
    name="terminal",
    toolset="terminal",
    schema=TERMINAL_SCHEMA,
    handler=_handle_terminal,
    check_fn=check_terminal_requirements,
    emoji="💻",
    max_result_size_chars=100_000,
)
```

**再看 `tools/file_tools.py` —— 一个文件注册多个工具：**

```python
registry.register(name="read_file", toolset="file",
    schema=READ_FILE_SCHEMA, handler=_handle_read_file,
    check_fn=_check_file_reqs, emoji="📖", max_result_size_chars=100_000)

registry.register(name="write_file", toolset="file",
    schema=WRITE_FILE_SCHEMA, handler=_handle_write_file,
    check_fn=_check_file_reqs, emoji="✍️", max_result_size_chars=100_000)

registry.register(name="patch", toolset="file",
    schema=PATCH_SCHEMA, handler=_handle_patch,
    check_fn=_check_file_reqs, emoji="🔧", max_result_size_chars=100_000)

registry.register(name="search_files", toolset="file",
    schema=SEARCH_FILES_SCHEMA, handler=_handle_search_files,
    check_fn=_check_file_reqs, emoji="🔎", max_result_size_chars=100_000)
```

**再看 `tools/memory_tool.py` —— handler 用 lambda 包装：**

```python
registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="🧠",
)
```

> **注意**：memory 工具的 handler 接收了 `kw.get("store")` —— 这个 `store` 参数不是来自模型，而是来自 Agent 循环中的 `agent._memory_store`。这说明 memory 工具是**被 Agent 拦截后注入状态的**，后面会详细讲。

---

## 4. 第三层：`toolsets.py` — 工具集定义

### 4.1 核心工具列表

```python
_HERMES_CORE_TOOLS = [
    "web_search", "web_extract",       # Web
    "terminal", "process",              # Terminal
    "read_file", "write_file", "patch", "search_files",  # File
    "vision_analyze", "image_generate", # Vision + Image
    "skills_list", "skill_view", "skill_manage",  # Skills
    "browser_navigate", "browser_snapshot", ...,  # Browser
    "text_to_speech",                   # TTS
    "todo", "memory",                   # Planning & Memory
    "session_search",                   # 会话搜索
    "clarify",                          # 澄清问题
    "execute_code", "delegate_task",    # 代码执行 + 委派
    "cronjob",                          # 定时任务
    "send_message",                     # 跨平台消息
    "ha_list_entities", ...,            # Home Assistant
    "kanban_show", ...,                 # Kanban 多 Agent
    "computer_use",                     # macOS 桌面控制
]
```

这个列表是**所有平台的默认基础**。CLI、Telegram、Discord、Slack 等都从这里继承。

### 4.2 工具集定义

```python
TOOLSETS = {
    # 基础工具集 —— 按类别分组
    "web":      {"tools": ["web_search", "web_extract"], "includes": []},
    "terminal": {"tools": ["terminal", "process"], "includes": []},
    "file":     {"tools": ["read_file", "write_file", "patch", "search_files"], "includes": []},

    # 组合工具集 —— 通过 includes 引用其他工具集
    "debugging": {
        "tools": ["terminal", "process"],
        "includes": ["web", "file"]  # ← 递归包含 web 和 file 的所有工具
    },
    "safe": {
        "tools": [],
        "includes": ["web", "vision", "image_gen"]  # 无终端的安全工具集
    },

    # 平台工具集
    "hermes-cli":     {"tools": _HERMES_CORE_TOOLS, "includes": []},
    "hermes-telegram":{"tools": _HERMES_CORE_TOOLS, "includes": []},
    "hermes-discord": {"tools": _HERMES_CORE_TOOLS + ["discord", "discord_admin"], "includes": []},
    "hermes-feishu":  {"tools": _HERMES_CORE_TOOLS + ["feishu_doc_read", ...], "includes": []},

    # 网关总集 —— 包含所有平台
    "hermes-gateway": {
        "tools": [],
        "includes": ["hermes-telegram", "hermes-discord", "hermes-slack", ...]
    },
}
```

### 4.3 递归解析

```python
def resolve_toolset(name: str, visited: Set[str] = None) -> List[str]:
    """递归解析工具集，返回所有工具名"""
    if name in {"all", "*"}:  # 特殊别名：所有工具
        all_tools = set()
        for ts_name in get_toolset_names():
            all_tools.update(resolve_toolset(ts_name, visited.copy()))
        return sorted(all_tools)

    if name in visited:  # 防止循环依赖
        return []
    visited.add(name)

    toolset = get_toolset(name)
    if not toolset:
        return []

    tools = set(toolset.get("tools", []))
    for included_name in toolset.get("includes", []):
        tools.update(resolve_toolset(included_name, visited))  # ← 递归
    return sorted(tools)
```

**示例**：解析 `"debugging"` → 自身的 `["terminal", "process"]` + `resolve("web")` 的 `["web_search", "web_extract"]` + `resolve("file")` 的 `["read_file", "write_file", "patch", "search_files"]` = 共 8 个工具。

### 4.4 插件工具集的动态合并

`get_toolset()` 不仅查 `TOOLSETS` 字典，还会查 registry 里插件注册的工具集：

```python
def get_toolset(name: str):
    toolset = TOOLSETS.get(name)
    if toolset:
        # 合并：静态定义的工具 + registry 中属于该 toolset 的工具
        merged_tools = set(toolset.get("tools", [])) | set(registry.get_tool_names_for_toolset(name))
        return {**toolset, "tools": sorted(merged_tools)}
    # 如果不在 TOOLSETS 里，检查是否是插件注册的 toolset
    ...
```

---

## 5. 第四层：`model_tools.py` — 编排层

这是工具系统的**对外公共 API**。它 1217 行，做三件事：
1. **触发工具发现**（import 所有工具模块）
2. **提供 `get_tool_definitions()`**（给模型准备 schema）
3. **提供 `handle_function_call()`**（执行模型的工具调用请求）

### 5.1 模块加载时的初始化

```python
# model_tools.py 模块级别

# 1. 触发内置工具发现（import 所有 tools/*.py → 触发 registry.register()）
discover_builtin_tools()

# 2. 触发插件工具发现
from hermes_cli.plugins import discover_plugins
discover_plugins()

# 3. 构建向后兼容的常量
TOOL_TO_TOOLSET_MAP = registry.get_tool_to_toolset_map()
TOOLSET_REQUIREMENTS = registry.get_toolset_requirements()
```

**导入顺序链**（模块级副作用）：

```text
import model_tools
  → discover_builtin_tools()
    → import tools.web_tools      → registry.register("web_search", ...)
    → import tools.terminal_tool  → registry.register("terminal", ...)
    → import tools.file_tools     → registry.register("read_file", ...)
    → ... (约 30+ 工具模块)
  → discover_plugins()
    → 加载 plugins/*/plugin.yaml 声明的插件
    → 插件也可能调用 registry.register()
```

### 5.2 `get_tool_definitions()` — 给模型准备工具列表

这是模型 API 请求中 `tools` 参数的来源：

```python
def get_tool_definitions(
    enabled_toolsets=None,    # 只包含这些工具集
    disabled_toolsets=None,   # 排除这些工具集
    quiet_mode=False,         # 是否打印状态
    skip_tool_search_assembly=False,
) -> List[Dict[str, Any]]:
```

**完整流程图**：

```text
get_tool_definitions(enabled_toolsets=["hermes-cli"])
  │
  ├─ 1. 解析工具集名称 → 具体工具名列表
  │     resolve_toolset("hermes-cli") → _HERMES_CORE_TOOLS → ~35 个工具名
  │
  ├─ 2. 减去 disabled_toolsets 的工具
  │     resolve_toolset("disabled_toolset") → difference_update
  │
  ├─ 3. 查 registry.get_definitions(tool_names)
  │     → 运行每个工具的 check_fn（带 30s TTL 缓存）
  │     → 过滤掉不可用的工具
  │     → 应用 dynamic_schema_overrides
  │     → 包装成 OpenAI {"type": "function", "function": {...}} 格式
  │
  ├─ 4. 特殊 schema 后处理
  │     → execute_code: 重建 schema，只列出实际可用的沙箱工具
  │     → discord: 根据 bot 权限动态调整 schema
  │     → browser_navigate: 如果 web_search 不可用，去掉交叉引用
  │
  ├─ 5. Schema 兼容性清洗
  │     → sanitize_tool_schemas() — 修复 llama.cpp 等后端不接受的格式
  │
  └─ 6. Tool Search 渐进式披露
        → 如果 MCP/插件工具太多，超过上下文窗口 10%
        → 把非核心工具隐藏在 tool_search/tool_describe/tool_call 三个桥接工具后面
```

**缓存策略**：

```python
# 缓存键 = (启用集, 禁用集, registry generation, config.yaml 的 mtime+size, ...)
cache_key = (
    frozenset(enabled_toolsets),
    frozenset(disabled_toolsets),
    registry._generation,       # ← registry 变更时自动失效
    cfg_fp,                     # ← config.yaml 修改时自动失效
    bool(os.environ.get("HERMES_KANBAN_TASK")),
    bool(skip_tool_search_assembly),
)
```

### 5.3 `handle_function_call()` — 执行工具调用

这是**模型 tool_call 请求的最终分发点**。签名很长，因为它承载了大量上下文：

```python
def handle_function_call(
    function_name: str,           # 工具名
    function_args: dict,          # 参数
    task_id: str = None,          # 终端/浏览器会话隔离 ID
    tool_call_id: str = None,     # OpenAI API 的 tool_call_id
    session_id: str = None,       # 会话 ID
    turn_id: str = None,          # 当前轮次 ID
    api_request_id: str = None,   # 当前 API 请求 ID
    user_task: str = None,        # 用户原始任务描述
    enabled_tools: list = None,   # 当前会话启用的工具列表
    skip_pre_tool_call_hook: bool = False,
    skip_tool_request_middleware: bool = False,
    tool_request_middleware_trace: list = None,
    enabled_toolsets: list = None,
    disabled_toolsets: list = None,
) -> str:
```

**完整执行流程**：

```text
handle_function_call("terminal", {"command": "ls -la"}, task_id="abc123")
  │
  ├─ 1. 参数类型强制转换 coerce_tool_args()
  │     "42" → 42 (如果 schema 期望 integer)
  │     "true" → True (如果 schema 期望 boolean)
  │     裸标量 → [标量] (如果 schema 期望 array)
  │
  ├─ 2. Tool Search 桥接工具分发
  │     如果 function_name 是 tool_search/tool_describe/tool_call:
  │     → tool_search: 搜索可用工具目录
  │     → tool_describe: 获取工具详细 schema
  │     → tool_call: 解包底层工具名+参数，递归调用 handle_function_call()
  │
  ├─ 3. 工具请求中间件 apply_tool_request_middleware()
  │     插件可以修改/增强工具参数
  │
  ├─ 4. Agent 级工具拦截检查
  │     如果 function_name 在 {"todo", "memory", "session_search", "delegate_task"}:
  │     → 返回错误（这些工具由 Agent 循环直接处理，不走 registry）
  │
  ├─ 5. pre_tool_call 插件 hook
  │     插件可以阻止工具执行（block_message）
  │     → 被阻止时返回 {"error": block_message}
  │
  ├─ 6. ACP 编辑审批 maybe_require_edit_approval()
  │     在文件修改操作前要求用户批准（VS Code/Zed 集成场景）
  │
  ├─ 7. 实际执行
  │     registry.dispatch(name, args, task_id=task_id, ...)
  │     → entry.handler(args, **kwargs)
  │     → 返回 JSON 字符串
  │
  ├─ 8. post_tool_call 插件 hook
  │     通知观察者插件（指标收集、日志等）
  │
  └─ 9. transform_tool_result 插件 hook
        插件可以修改/替换工具返回结果
```

### 5.4 异步桥接：`_run_async()`

工具 handler 有同步和异步两种。但 Agent 循环是同步的。`_run_async()` 负责在同步上下文中执行异步 coroutine：

```python
def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # 场景 A：已经在异步上下文中（gateway）
        # → 启动一个新线程 + 新 event loop 来运行 coroutine
        # → 300 秒超时
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_run_in_worker)
        return future.result(timeout=300)

    if threading.current_thread() is not threading.main_thread():
        # 场景 B：在 worker 线程中（并行工具执行）
        # → 使用线程本地的持久 event loop
        worker_loop = _get_worker_loop()
        return worker_loop.run_until_complete(coro)

    # 场景 C：在主线程且无 event loop（CLI 模式）
    # → 使用全局持久 event loop
    tool_loop = _get_tool_loop()
    return tool_loop.run_until_complete(coro)
```

**为什么不用 `asyncio.run()`？** 因为 `asyncio.run()` 每次创建并关闭 event loop，会导致缓存的 httpx/AsyncOpenAI 客户端在 GC 时报 "Event loop is closed"。持久 loop 解决了这个问题。

---

## 6. 第五层：Agent 循环中的工具调用

### 6.1 两层拦截：Agent 级工具 vs. Registry 工具

工具被分成两类：

| 类别 | 工具名 | 执行位置 | 原因 |
|------|--------|---------|------|
| **Agent 级工具** | `todo`, `memory`, `session_search`, `clarify`, `delegate_task` | `agent/agent_runtime_helpers.py::invoke_tool()` | 需要 Agent 状态（TodoStore、MemoryStore、SessionDB、回调等） |
| **Registry 工具** | 其余所有（terminal, read_file, web_search, ...） | `model_tools.handle_function_call()` → `registry.dispatch()` | 不需要 Agent 状态 |

**`invoke_tool()` 中的分发逻辑**（`agent/agent_runtime_helpers.py`）：

```python
def invoke_tool(agent, function_name, function_args, ...):
    if function_name == "todo":
        # 使用 agent._todo_store
        return todo_tool(..., store=agent._todo_store)
    elif function_name == "session_search":
        # 使用 agent._get_session_db_for_recall()
        return session_search(..., db=session_db, current_session_id=agent.session_id)
    elif function_name == "memory":
        # 使用 agent._memory_store
        result = memory_tool(..., store=agent._memory_store)
        # 通知外部 memory provider
        if agent._memory_manager:
            agent._memory_manager.on_memory_write(...)
        return result
    elif agent._memory_manager and agent._memory_manager.has_tool(function_name):
        # 外部 memory provider 注册的工具
        return agent._memory_manager.handle_tool_call(function_name, args)
    elif function_name == "clarify":
        # 使用 agent.clarify_callback
        return clarify_tool(..., callback=agent.clarify_callback)
    elif function_name == "delegate_task":
        # 使用 agent._dispatch_delegate_task
        return agent._dispatch_delegate_task(args)
    else:
        # 其他所有工具 → 走 handle_function_call
        return handle_function_call(function_name, args, ...)
```

### 6.2 执行入口：`tool_executor.py`

Agent 循环中的工具执行有两条路径：

```python
# 路径 A：并发执行（多个工具同时调用）
def execute_tool_calls_concurrent(agent, assistant_message, messages, ...):
    # 最多 8 个 worker threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(num_tools, 8)) as executor:
        for call in tool_calls:
            future = executor.submit(_worker_body, ...)
        results = [f.result() for f in futures]

# 路径 B：顺序执行（交互式工具或只有一个调用）
def execute_tool_calls_sequential(agent, assistant_message, messages, ...):
    for tool_call in assistant_message.tool_calls:
        result = agent._invoke_tool(function_name, function_args, ...)
```

两条路径最终都调用 `agent._invoke_tool()` → `invoke_tool()` → 分发到 Agent 级工具或 `handle_function_call()`。

### 6.3 `handle_function_call` 在 tool_executor 中的调用方式

在 `tool_executor.py` 的顺序执行路径中（约 line 1181-1235）：

```python
function_result = handle_function_call(
    function_name, function_args, effective_task_id,
    tool_call_id=tool_call.id,
    session_id=agent.session_id or "",
    turn_id=getattr(agent, "_current_turn_id", "") or "",
    api_request_id=getattr(agent, "_current_api_request_id", "") or "",
    enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
    skip_pre_tool_call_hook=True,     # ← Agent 层已经检查过了
    skip_tool_request_middleware=True, # ← Agent 层已经运行过了
    enabled_toolsets=getattr(agent, "enabled_toolsets", None),
    disabled_toolsets=getattr(agent, "disabled_toolsets", None),
    tool_request_middleware_trace=list(middleware_trace),
)
```

**注意 `skip_pre_tool_call_hook=True`**：因为 `invoke_tool()` 在调用 `handle_function_call()` 之前已经触发了 pre_tool_call hook，避免重复触发。

---

## 7. 完整数据流转图

以一次用户对话为例，追踪"模型请求执行 `terminal` 工具"的完整路径：

```text
用户输入 "帮我查看当前目录的文件列表"
  │
  ▼
cli.py: HermesCLI.chat()
  → agent.run_conversation(user_message="帮我查看当前目录的文件列表")
  │
  ▼
agent/conversation_loop.py: run_conversation()
  → 构建 messages，包含 system prompt + 历史 + 用户消息
  → client.chat.completions.create(model=..., messages=..., tools=tool_schemas)
  │
  ▼ 模型返回 tool_calls: [{name: "terminal", args: {command: "ls -la"}}]
  │
  ▼
agent/tool_executor.py: execute_tool_calls_sequential()
  → agent._invoke_tool("terminal", {command: "ls -la"}, task_id)
  │
  ▼
agent/agent_runtime_helpers.py: invoke_tool()
  → "terminal" 不在 Agent 级工具列表中
  → else 分支：调用 handle_function_call()
  │
  ▼
model_tools.py: handle_function_call("terminal", {command: "ls -la"}, ...)
  → coerce_tool_args() — 参数类型检查（"ls -la" 已经是 string，OK）
  → Tool Search 桥接检查 — "terminal" 不是桥接工具，跳过
  → 中间件处理
  → "terminal" 不在 _AGENT_LOOP_TOOLS 中，继续
  → pre_tool_call hook 检查（已跳过，invoke_tool 已做过）
  → ACP 编辑审批（terminal 不涉及文件修改，跳过）
  │
  ▼
tools/registry.py: registry.dispatch("terminal", {command: "ls -la"}, task_id=...)
  → entry = registry._tools["terminal"]
  → entry.handler({command: "ls -la"}, task_id=...)
  │
  ▼
tools/terminal_tool.py: _handle_terminal({command: "ls -la"}, task_id=...)
  → 获取/创建终端环境
  → 执行 shell 命令
  → 返回 JSON: '{"success": true, "output": "total 42\ndrwxr-xr-x ..."}'
  │
  ▼ (结果沿调用链回传)
  │
registry.dispatch → 返回结果字符串
handle_function_call → post_tool_call hook → transform_tool_result hook → 返回
invoke_tool → _finish_agent_tool() → post_tool_call hook → 返回
tool_executor → 构造 tool message: {"role": "tool", "content": result, "tool_call_id": ...}
  → messages.append(tool_message)
  │
  ▼
conversation_loop → 继续循环，带着 tool result 再调用模型 API
  → 模型看到 ls -la 的结果，生成最终文本回复
```

---

## 8. 三个关键设计模式总结

### 8.1 自注册模式（Self-Registration）

```text
                  tools/registry.py (无依赖)
                         ↑ import
                  tools/terminal_tool.py
                         ↑ import (触发 register())
                  model_tools.py::discover_builtin_tools()
```

每个工具文件自己负责注册自己。新增工具不需要修改任何中心文件（除了 `toolsets.py` 的工具集定义）。这是经典的"控制反转"模式。

### 8.2 工具集组合模式（Toolset Composition）

```text
"hermes-gateway"
  includes → "hermes-telegram" + "hermes-discord" + "hermes-slack" + ...
    each → _HERMES_CORE_TOOLS + 平台特有工具
```

通过 `includes` 实现工具集的递归组合，避免重复定义。修改 `_HERMES_CORE_TOOLS` 一处即可影响所有平台。

### 8.3 两级拦截模式（Two-Level Interception）

```text
模型请求 tool_call
  → 第 1 级：invoke_tool()
    → Agent 级工具？(todo/memory/session_search/clarify/delegate_task)
      → 是：直接处理，注入 Agent 状态
      → 否：转发到第 2 级
  → 第 2 级：handle_function_call()
    → Tool Search 桥接？(tool_search/tool_describe/tool_call)
      → 是：处理目录查询/解包
      → 否：registry.dispatch() → handler 执行
```

Agent 级工具需要访问 Agent 的内部状态（TodoStore、MemoryStore、SessionDB、clarify_callback 等），所以必须在 Agent 层拦截，不能交给 registry 直接 dispatch。

---

## 9. 关键文件速查表

| 文件 | 行数 | 核心职责 | 关键符号 |
|------|------|---------|---------|
| `tools/registry.py` | 590 | 工具注册、发现、调度、查询 | `registry` (单例), `ToolEntry`, `discover_builtin_tools()`, `tool_error()`, `tool_result()` |
| `toolsets.py` | 883 | 工具集定义和递归解析 | `_HERMES_CORE_TOOLS`, `TOOLSETS`, `resolve_toolset()`, `validate_toolset()` |
| `model_tools.py` | 1217 | 编排层公共 API | `get_tool_definitions()`, `handle_function_call()`, `coerce_tool_args()`, `_run_async()` |
| `agent/tool_executor.py` | 1410 | 并发/顺序工具执行 | `execute_tool_calls_concurrent()`, `execute_tool_calls_sequential()` |
| `agent/agent_runtime_helpers.py` | 2504 | Agent 级工具拦截 + 杂项 | `invoke_tool()`, `AGENT_RUNTIME_POST_HOOK_TOOL_NAMES` |
| `run_agent.py` | 5329 | AIAgent 类（thin forwarder） | `AIAgent._invoke_tool()`, `from model_tools import handle_function_call` |
