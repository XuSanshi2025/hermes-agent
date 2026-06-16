# Hermes Agent 核心源码详解

> 学习路线阶段 3：Agent 核心层（run_agent.py + agent/ 子模块）

---

## 一、整体定位

Agent 核心是 Hermes 的"大脑"——从接收用户消息到产出最终回复的全部逻辑都在此层。

```
CLI / Gateway / TUI
       ↓  调用
  AIAgent.chat() / run_conversation()     ← run_agent.py（壳层）
       ↓  转发
  agent/agent_init.py         — 初始化（60+ 属性）
  agent/conversation_loop.py  — 对话循环（~4600 行，最核心）
  agent/chat_completion_helpers.py — API 调用构建与执行
  agent/tool_executor.py      — 工具调用（并发/顺序）
  agent/prompt_builder.py     — 系统提示词组装
  agent/agent_runtime_helpers.py — 运行时辅助（修复、恢复、提取）
```

**核心设计原则：**
- `run_agent.py` 是"壳文件"（~5300 行），AIAgent 类的 `__init__` 只是薄转发器
- 实际逻辑全部提取到 `agent/` 子模块，每个模块用 `_ra()` 惰性引用 `run_agent`
- `_ra()` 模式：`def _ra(): import run_agent; return run_agent` —— 保持 `mock.patch("run_agent.xxx")` 测试兼容

---

## 二、文件结构总览

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `run_agent.py` | ~5300 | AIAgent 类定义、`__init__` 薄转发、工具调用分发、`chat()`/`run_conversation()` 入口 |
| `agent/agent_init.py` | ~1740 | `init_agent()` — 实际初始化逻辑，60+ 参数 |
| `agent/conversation_loop.py` | ~4579 | `run_conversation()` — 对话循环主体（最大单文件） |
| `agent/chat_completion_helpers.py` | ~2473 | API 调用构建、中断式调用、fallback 激活 |
| `agent/tool_executor.py` | ~1410 | 工具执行（并发/顺序）、中间件、guardrail |
| `agent/prompt_builder.py` | ~1554 | 系统提示词常量和构建函数 |
| `agent/agent_runtime_helpers.py` | ~2504 | 消息修复、凭证恢复、reasoning 提取、轨迹转换 |

---

## 三、核心方法详解

### 3.1 AIAgent.__init__ → init_agent()

**位置：** `run_agent.py:343-484`（壳） → `agent/agent_init.py:154-600+`（实际实现）

AIAgent 构造接收 ~60 个参数，`__init__` 只做一件事——调用 `init_agent(self, ...)`：

```python
class AIAgent:
    def __init__(self, base_url=None, api_key=None, provider=None, api_mode=None,
                 model="", max_iterations=90, ...):
        from agent.agent_init import init_agent
        init_agent(self, base_url=base_url, api_key=api_key, ...)
```

**`init_agent()` 设置的 60+ 属性分 6 大类：**

| 类别 | 关键属性 | 说明 |
|------|---------|------|
| **凭证/路由** | `base_url`, `api_key`, `provider`, `api_mode` | API 连接基础 |
| **API 模式检测** | `api_mode` | 自动检测 5 种模式（见下文） |
| **缓存策略** | `_use_prompt_caching`, `_use_native_cache_layout` | Anthropic prompt caching |
| **迭代预算** | `iteration_budget` (IterationBudget) | 共享预算（父→子 Agent 继承） |
| **中断机制** | `_interrupt_requested`, `_interrupt_message`, `_tool_worker_threads` | 双队列中断模型 |
| **/steer 机制** | `_pending_steer`, `_pending_steer_lock` | 运行时中间转向注入 |

**5 种 API 模式自动检测逻辑：**

```
chat_completions    — 默认，OpenAI 兼容 API
codex_responses     — GPT-5.x / Codex 自动升级（agent_init.py:370-393）
anthropic_messages  — Anthropic Claude 原生
bedrock_converse    — AWS Bedrock Converse API
codex_app_server    — Codex App Server 模式
```

GPT-5.x 自动升级逻辑：当 model 包含 `gpt-5` 且 api_mode 为 `chat_completions` 时，自动切换到 `codex_responses`。

**OpenRouter 预热：** `_openrouter_prewarm_done` 使用进程级 `multiprocessing.Event` 防止线程泄漏——只在第一次请求时触发预热。

---

### 3.2 run_conversation() — 对话循环核心

**位置：** `agent/conversation_loop.py:371-4574`（~4200 行）

这是整个 Hermes 最核心、最复杂的函数。从 AIAgent 类提取出来后成为独立函数。

#### 3.2.1 函数签名

```python
def run_conversation(agent, user_message, system_message=None,
                     conversation_history=None, task_id=None) -> dict:
    """返回: {final_response, messages, api_calls, completed, ...}"""
```

#### 3.2.2 整体流程

```
┌─ 前奏 ──────────────────────────────────────────────┐
│  build_turn_context() → 统一 per-turn setup         │
│  _restore_or_build_system_prompt() → 系统提示词恢复  │
└──────────────────────────────────────────────────────┘
         ↓
┌─ 主循环 ────────────────────────────────────────────┐
│  while (api_call_count < max_iterations              │
│         and budget.remaining > 0)                    │
│        or grace_call:                                │
│                                                      │
│    ① 中断检查 → if _interrupt_requested: break      │
│    ② /steer 预排空 → 注入到最后 tool result          │
│    ③ 消息准备 → ephemeral/reasoning/cache/orphan清理 │
│    ④ 重试循环 → API 调用 + 响应验证 + fallback       │
│    ⑤ 工具执行 → 并发/顺序执行 tool calls             │
│    ⑥ 后处理 → token 统计/cost 估算/会话持久化        │
└──────────────────────────────────────────────────────┘
         ↓
┌─ 收尾 ──────────────────────────────────────────────┐
│  插件 hooks → transform_llm_output / post_llm_call  │
│  结果字典组装 → {final_response, messages, ...}      │
│  后台任务 → memory review / skill review             │
└──────────────────────────────────────────────────────┘
```

#### 3.2.3 主循环入口条件

```python
while (api_call_count < self.max_iterations
       and self.iteration_budget.remaining > 0) \
      or self._budget_grace_call:
```

- `max_iterations`：默认 90，单 turn 最大 API 调用次数
- `iteration_budget`：`IterationBudget` 对象，父 Agent 创建、子 Agent 继承（共享配额）
- `_budget_grace_call`：布尔标记，允许最后一次调用（当模型即将给出最终回复时）

#### 3.2.4 消息准备流水线（③）

每轮 API 调用前的消息处理链：

1. **ephemeral 注入** — 临时系统提示（如 `/steer` 转向指令）
2. **reasoning 复制** — `reasoning` → `reasoning_content`/`reasoning_details`（按 API 模式）
3. **Anthropic cache_control** — 在 system/user 消息上注入缓存断点
4. **orphan 清理** — 移除没有对应 assistant tool_call 的孤立 tool 消息
5. **whitespace 归一化** — 合并连续空白
6. **surrogate 清理** — 去除 U+D800..U+DFFF 非法代理对

#### 3.2.5 重试循环（④）— 最复杂的子系统

```
┌─ 重试循环 ──────────────────────────────────────────┐
│  for retry_count in range(max_retries):              │
│                                                      │
│    Nous rate guard → 检查全局速率限制                 │
│    API 调用 → interruptible_api_call()               │
│    响应验证 → 按 api_mode 分别验证                    │
│                                                      │
│    成功 → break                                      │
│    失败 → 错误分类(classify_api_error)               │
│          → 专项恢复（见下文）                         │
│          → jittered backoff(5s base, 120s cap)       │
│          → credential pool 轮换                      │
│          → fallback provider 链                      │
└──────────────────────────────────────────────────────┘
```

**错误分类与专项恢复（conversation_loop.py:1736-3200）：**

`classify_api_error()` 将错误分类为 `FailoverReason` 枚举，每种有独立恢复策略：

| FailoverReason | 恢复策略 |
|----------------|---------|
| `rate_limit` (429) | credential pool 轮换 → fallback provider → Nous rate guard |
| `billing` (402) | credential pool → fallback → 终止（不无限重试烧钱） |
| `context_overflow` (400) | 自动压缩（最多 3 次）→ 缩减 context_length |
| `payload_too_large` (413) | 自动压缩 → 终止 |
| `long_context_tier` (429) | 缩减到 200K context → 压缩重试 |
| `thinking_signature` (400) | 剥离所有 reasoning_details → 重试 |
| `invalid_encrypted_content` | 禁用 Codex reasoning replay → 重试 |
| `image_rejection` | 剥离所有图片 → 标记 session 不支持 vision → 重试 |
| `llama_cpp_grammar` | 剥离 tool schema 的 pattern/format → 重试 |
| `content_policy_blocked` | 尝试 fallback → 提供改写建议 |
| `oauth_long_context_beta` | 禁用 1M beta → 重建 client → 重试 |
| 各种 401 auth | 按 provider 分别刷新 OAuth/credential |

**jittered backoff 算法：**
```python
base_delay = 5  # 秒
cap_delay = 120  # 秒上限
delay = min(base_delay * (2 ** retry_count), cap_delay)
jitter = random.uniform(0, delay * 0.3)
actual_delay = delay + jitter
```

#### 3.2.6 流式 API 调用优先

对话循环始终优先使用流式路径（conversation_loop.py:977-1006）：
- 流式提供更好的健康检查（可检测 stale connection）
- 流式支持中途打断（用户中断时无需等待完整响应）
- 非流式只在流式不可用时降级使用

#### 3.2.7 Token 统计与 Cost 估算（⑥）

每次成功 API 调用后（conversation_loop.py:1566-1703）：

```python
canonical_usage = normalize_usage(response.usage, provider, api_mode)
# 更新 session 级累计
agent.session_prompt_tokens += prompt_tokens
agent.session_completion_tokens += completion_tokens
# ...
# 持久化到 SessionDB（供 /insights 使用）
session_db.update_token_counts(session_id, ...)
# Cost 估算
cost_result = estimate_usage_cost(model, canonical_usage, provider, base_url)
agent.session_estimated_cost_usd += cost_result.amount_usd
```

**cache hit 统计**：不依赖 `_use_prompt_caching` 标记——OpenAI/Kimi/DeepSeek/Qwen 等服务端自动缓存也会计入。

---

### 3.3 interruptible_api_call() — 中断式 API 调用

**位置：** `agent/chat_completion_helpers.py:125-523`

将 API 调用放入后台线程，主循环可检测中断而不阻塞于 HTTP 往返。

```
主线程                        工作线程
  │                              │
  ├─ Thread(target=_call) ─────→ ├─ 创建独立 OpenAI client
  │                              ├─ client.chat.completions.create()
  │  ← poll (0.3s interval) →   │
  ├─ 检查 interrupt              │
  ├─ 检查 stale timeout          │
  ├─ 检查 Codex TTFB watchdog    │
  ├─ 检查 Codex idle watchdog    │
  │                              ├─ 完成 → result["response"]
  │  ← join ──────────────────── │
  └─ 返回 response / 抛出异常    └─ _close_request_client_once()
```

**三层看门狗：**

| 看门狗 | 触发条件 | 默认超时 |
|--------|---------|---------|
| **TTFB** | Codex stream 零字节 | 120s（大请求禁用） |
| **Stream idle** | 首字节后无 SSE 事件 | 12-180s（按 token 量动态） |
| **Stale call** | 非流式无响应 | 按 provider/context 计算 |

**线程安全的 client 关闭（#29507 修复）：**
- 工作线程完成 → 自己关闭 client（完整 close）
- 外部线程中断 → 只 shutdown socket（不 close），让工作线程自行 close
- 防止 FD 回收竞争：TLS socket FD 被内核重新分配给 SQLite 后，SSL BIO 写入损坏数据库头

**5 种 API 模式的分发（_call 内部）：**

```python
if api_mode == "codex_responses":
    agent._run_codex_stream(api_kwargs, client=request_client)
elif api_mode == "anthropic_messages":
    agent._anthropic_messages_create(api_kwargs)
elif api_mode == "bedrock_converse":
    bedrock_runtime_client.converse(**api_kwargs)
else:  # chat_completions
    request_client.chat.completions.create(**api_kwargs)
```

---

### 3.4 build_api_kwargs() — API 请求参数构建

**位置：** `agent/chat_completion_helpers.py:527-786`

根据 `api_mode` 构建不同的请求参数：

| api_mode | 构建路径 | 特殊处理 |
|----------|---------|---------|
| `anthropic_messages` | `_get_transport().build_kwargs()` | cache_control、OAuth 1M beta、reasoning_config |
| `bedrock_converse` | `_get_transport().build_kwargs()` | region、guardrail_config |
| `codex_responses` | `_get_transport().build_kwargs()` | xAI schema 清理、encrypted reasoning replay |
| `chat_completions` | 直接组装 dict | provider 偏好、温度策略、provider profile |

**chat_completions 模式的 provider 检测：**
```python
_is_qwen = agent._is_qwen_portal()
_is_or = agent._is_openrouter_url()
_is_gh = base_url_host_matches(base_url, "models.github.ai")
_is_nous = "nousresearch" in base_url_lower
_is_nvidia = "integrate.api.nvidia.com" in base_url_lower
_is_kimi = ...
```

每个 provider 有不同的 `extra_body`、`temperature`、`max_tokens` 策略。

---

### 3.5 工具执行系统

**位置：** `agent/tool_executor.py`（~1410 行）

#### 3.5.1 并发执行路径

`execute_tool_calls_concurrent()`（243-769 行）：

```
┌─ 预检 ──────────────────────────────────────────────┐
│  ① 中断检查 → if _interrupt_requested: 全部取消      │
│  ② 参数解析 → JSON.loads(tool_call.arguments)        │
│  ③ Tool Search 解包 → _unwrap_tool_search_result()   │
│  ④ 中间件应用 → _apply_tool_request_middleware()     │
│  ⑤ 插件 block 检查 → 插件可拦截工具调用              │
│  ⑥ Guardrail 检查 → tool_guardrails 验证             │
│  ⑦ Checkpoint preflight → 检查点预检                 │
└──────────────────────────────────────────────────────┘
         ↓
┌─ 并发执行 ──────────────────────────────────────────┐
│  ThreadPoolExecutor(max_workers=8)                   │
│  for each tool_call:                                 │
│    executor.submit(_run_tool, ...)                   │
│                                                      │
│  _run_tool() worker:                                 │
│    ① 注册 tid → _tool_worker_threads                 │
│    ② 传播上下文（task_id 等）                         │
│    ③ agent._invoke_tool() → 实际执行                  │
│    ④ 收集结果 → {name, result, tool_call_id}         │
└──────────────────────────────────────────────────────┘
```

#### 3.5.2 顺序执行路径

`execute_tool_calls_sequential()`（770+ 行）：
- 逐个执行，每个 tool call 前检查中断
- 适用于不能并发的工具（如交互式 clarify）

#### 3.5.3 并发/顺序选择

`_should_parallelize_tool_batch()` 决定使用哪条路径：
- 单个 tool call → 顺序
- 多个独立 tool calls → 并发
- 包含 clarify/secret 等交互工具 → 顺序

---

### 3.6 系统提示词构建

**位置：** `agent/prompt_builder.py`（~1554 行）

所有函数**无状态**，接收参数返回字符串。

#### 3.6.1 核心常量

| 常量 | 行号 | 作用 |
|------|------|------|
| `DEFAULT_AGENT_IDENTITY` | 122-130 | Agent 基础身份（"You are Hermes..."） |
| `MEMORY_GUIDANCE` | 143-163 | 持久记忆使用指导 |
| `SKILLS_GUIDANCE` | 172-179 | 技能管理指导 |
| `KANBAN_GUIDANCE` | 181-255 | 看板任务执行协议（6 步生命周期） |
| `TOOL_USE_ENFORCEMENT_GUIDANCE` | 257-270 | 强制工具使用（防止纯文本回复） |
| `TASK_COMPLETION_GUIDANCE` | 292-305 | 任务完成指导（通用） |
| `OPENAI_MODEL_EXECUTION_GUIDANCE` | 315-373 | GPT/Codex/Grok 执行纪律 |
| `GOOGLE_MODEL_OPERATIONAL_GUIDANCE` | 377-395 | Gemini/Gemma 操作指令 |
| `COMPUTER_USE_GUIDANCE` | 400-440 | macOS 桌面控制指导 |
| `STEER_MARKER_OPEN/CLOSE` | 452-453 | `/steer` 中间转向标记 |
| `PLATFORM_HINTS` | 481+ | 平台特定格式化提示 |

#### 3.6.2 提示词组装流程

```
_restore_or_build_system_prompt()          ← conversation_loop.py:225
  │
  ├─ 检查 DB 缓存 → 有则恢复（保证 prefix cache 命中）
  │
  └─ 构建新提示词:
     ├─ DEFAULT_AGENT_IDENTITY
     ├─ build_environment_hints()    ← CWD、OS、终端类型
     ├─ MEMORY_GUIDANCE             ← 如果 memory 启用
     ├─ SKILLS_GUIDANCE             ← 如果 skills 启用
     ├─ KANBAN_GUIDANCE             ← 如果 kanban 启用
     ├─ TOOL_USE_ENFORCEMENT        ← 始终注入
     ├─ 模型家族引导:
     │   ├─ GPT/Codex → OPENAI_MODEL_EXECUTION_GUIDANCE
     │   ├─ Gemini    → GOOGLE_MODEL_OPERATIONAL_GUIDANCE
     │   └─ 其他      → TASK_COMPLETION_GUIDANCE
     ├─ COMPUTER_USE_GUIDANCE       ← 如果 computer_use 启用
     ├─ PLATFORM_HINTS              ← 按 platform 选择
     └─ 上下文文件注入:
         ├─ AGENTS.md               ← 项目级指令
         └─ SOUL.md                 ← 人格定制
```

#### 3.6.3 DB 持久化与 Prefix Cache 优化

`_restore_or_build_system_prompt()` 的关键设计：
- 构建完系统提示词后**存入 SessionDB**
- 后续 turn 从 DB 恢复（而非重建），保证**完全相同的字符串**
- 这让 Anthropic 的 prompt caching 和 OpenAI 的 server-side prefix caching 都能命中
- 系统提示词变化 → cache 失效 → 成本翻倍

---

### 3.7 agent_runtime_helpers.py — 运行时辅助

**位置：** `agent/agent_runtime_helpers.py`（~2504 行）

提供 AIAgent 运行时的各种修复/恢复/提取工具。

#### 3.7.1 核心函数一览

| 函数 | 行号 | 作用 |
|------|------|------|
| `convert_to_trajectory_format()` | 66 | 内部消息格式 → 训练轨迹格式（XML tag 包裹） |
| `sanitize_tool_call_arguments()` | 237 | 修复损坏的 tool_call JSON 参数 |
| `repair_message_sequence()` | 347 | 修复消息序列（role 交替不变量） |
| `strip_think_blocks()` | 449 | 从存储内容中移除 reasoning 块 |
| `recover_with_credential_pool()` | 545 | 凭证池轮换（429/401 恢复） |
| `try_recover_primary_transport()` | 725 | 重建 OpenAI client（stale connection 恢复） |
| `drop_thinking_only_and_merge_users()` | 809 | Anthropic 消息清理 |
| `restore_primary_runtime()` | 895 | 撤销 fallback 激活（恢复主 provider） |
| `extract_reasoning()` | 991 | 从 API 响应提取 reasoning 字段 |
| `dump_api_request_debug()` | 1073 | 写请求体到 debug 文件（post-mortem） |
| `anthropic_prompt_cache_policy()` | 1154 | 计算 Anthropic cache_control 断点 |
| `create_openai_client()` | 1260 | 构建 per-agent OpenAI SDK client |

#### 3.7.2 repair_message_sequence() 详解

Providers 要求严格的 role 交替：`system → user/tool ↔ assistant`。违规会导致空响应。

修复策略：
1. **孤立 tool 消息**（无对应 assistant tool_call）→ 丢弃
2. **连续 user 消息** → 用换行符合并（不丢失输入）
3. **不处理** orphan `assistant(tool_calls)+tool` 对 — 这是合法的"用户跳入重定向"模式

#### 3.7.3 recover_with_credential_pool() 详解

当主凭证被 rate limit (429) 时：
```
① 检查 _credential_pool 是否存在
② 遍历 pool 中的凭证条目
③ 跳过状态为 EXHAUSTED 的条目
④ 选择可用条目 → 替换 agent.api_key
⑤ 重建 OpenAI client
⑥ 返回 (True, has_retried_429)
```

---

## 四、重要辅助系统

### 4.1 上下文压缩集成

`context_compressor` 是对话循环的核心依赖：
- **主动压缩**：token 达到阈值时自动触发
- **被动压缩**：413/400 context overflow 时强制触发
- **最大压缩次数**：`max_compression_attempts`（默认 3），防止无限循环
- **压缩后**：创建新 session → 清空 conversation_history → 重试

### 4.2 Fallback Provider 链

`try_activate_fallback()`（chat_completion_helpers.py:1005）：

```
主 provider 失败
  ↓
检查 _fallback_chain[_fallback_index]
  ↓
切换到 fallback:
  - 替换 base_url / api_key / provider / model / api_mode
  - 重建 OpenAI client
  - 重置 retry_count / compression_attempts
  ↓
所有 fallback 耗尽 → 终止
```

### 4.3 中断机制

双队列模型（与 CLI 层配合）：

| 队列 | 时机 | 处理 |
|------|------|------|
| `_interrupt_requested` | Agent 运行时 | 循环顶部检查 → break |
| `_tool_worker_threads` | 工具执行时 | 中断扇出到所有 worker tid |

中断在以下点被检查：
1. 主循环顶部（每次迭代）
2. API 调用 poll 循环（每 0.3s）
3. 工具执行前（预检）
4. 错误恢复决策前

### 4.4 /steer 中间转向

允许用户在 Agent 运行时注入指令（不中断当前执行）：

```
用户输入 /steer "请优先使用 Python"
  ↓
_pending_steer = "请优先使用 Python"
  ↓
下一轮 API 调用前:
  _pending_steer 排空 → 注入到最后 tool result 中
  用 STEER_MARKER_OPEN/CLOSE 包裹
  ↓
模型看到转向指令 → 调整行为
```

---

## 五、设计模式总结

### 5.1 壳层 + 提取模块（_ra 模式）

```
run_agent.py (壳)          agent/*.py (实际逻辑)
┌─────────────────┐       ┌──────────────────────┐
│ class AIAgent:   │──────→│ def init_agent()      │
│   def __init__():│       │ def run_conversation() │
│     init_agent() │       │ def build_api_kwargs() │
│                  │       │ def execute_tool_...()  │
│   def run_conv():│──────→│                        │
│     forward()    │       │ def _ra():             │
│                  │       │   import run_agent      │
└─────────────────┘       └──────────────────────┘
```

好处：代码可分文件维护，测试 patch 仍生效。

### 5.2 错误分类 + 专项恢复

`classify_api_error()` → `FailoverReason` 枚举 → 每种 reason 有独立恢复策略。
避免"一刀切"重试，对 rate_limit/context_overflow/auth 等分别处理。

### 5.3 惰性 Client 创建

OpenAI client 不在 `__init__` 创建，首次 API 调用时才构建。
fallback 切换时重建，credential 轮换时重建。

### 5.4 状态缓冲 + 延迟输出

错误重试期间的所有状态信息用 `_buffer_vprint()` / `_buffer_status()` 缓冲：
- 恢复成功 → 丢弃缓冲（用户不看到噪音）
- 全部失败 → `_flush_status_buffer()` 输出完整重试历史

### 5.5 进程级共享状态

- `_openrouter_prewarm_done`：`multiprocessing.Event`，进程级单次预热
- `nous_rate_guard`：共享文件记录 rate limit 状态，跨 session 生效
- `credential_pool`：多凭证共享池，支持 kanban/cron 并发场景

---

## 六、数据流图

```
用户消息 "帮我写一个函数"
    │
    ↓
cli.py: chat(message)
    │
    ↓
run_agent.py: AIAgent.run_conversation(user_message)
    │
    ↓
conversation_loop.py: run_conversation(agent, user_message)
    │
    ├─① build_turn_context() → 准备 turn 上下文
    │
    ├─② _restore_or_build_system_prompt()
    │     └─ prompt_builder.py → 组装系统提示词
    │
    ├─③ 主循环 while(api_call_count < max_iterations):
    │     │
    │     ├─ 消息准备（ephemeral/reasoning/cache/cleanup）
    │     │
    │     ├─ chat_completion_helpers.py: build_api_kwargs()
    │     │     └─ 按 api_mode 构建请求参数
    │     │
    │     ├─ chat_completion_helpers.py: interruptible_api_call()
    │     │     ├─ 后台线程 → API 调用
    │     │     ├─ 看门狗监控（TTFB/idle/stale）
    │     │     └─ 中断检测 → InterruptedError
    │     │
    │     ├─ 响应验证:
    │     │     ├─ 有 tool_calls → tool_executor.py 执行
    │     │     │     ├─ 并发: ThreadPoolExecutor(8 workers)
    │     │     │     └─ 顺序: 逐个执行
    │     │     ├─ 无 tool_calls → 最终回复 → return
    │     │     └─ 空响应 → empty-retry 循环
    │     │
    │     └─ 错误恢复:
    │           ├─ classify_api_error() → FailoverReason
    │           ├─ credential pool 轮换
    │           ├─ 上下文压缩
    │           ├─ fallback provider 切换
    │           └─ jittered backoff
    │
    ├─④ 后处理:
    │     ├─ 插件 hooks (transform_llm_output, post_llm_call)
    │     ├─ 结果字典组装
    │     └─ 后台 memory/skill review
    │
    └─⑤ return {final_response, messages, api_calls, completed}
         │
         ↓
    cli.py: 渲染最终回复到终端
```

---

## 七、文件引用速查表

| 想了解... | 看哪个文件 | 关键行号 |
|-----------|-----------|---------|
| AIAgent 类定义 | `run_agent.py` | 343-484 |
| Agent 初始化（60+ 属性） | `agent/agent_init.py` | 154-600 |
| 对话循环主体 | `agent/conversation_loop.py` | 371-4574 |
| 系统提示词组装 | `agent/prompt_builder.py` | 122-500 |
| API 请求参数构建 | `agent/chat_completion_helpers.py` | 527-786 |
| 中断式 API 调用 | `agent/chat_completion_helpers.py` | 125-523 |
| Fallback 激活 | `agent/chat_completion_helpers.py` | 1005-1264 |
| 并发工具执行 | `agent/tool_executor.py` | 243-769 |
| 顺序工具执行 | `agent/tool_executor.py` | 770+ |
| 消息序列修复 | `agent/agent_runtime_helpers.py` | 347-448 |
| 凭证池轮换 | `agent/agent_runtime_helpers.py` | 545-724 |
| Reasoning 提取 | `agent/agent_runtime_helpers.py` | 991-1072 |
| Anthropic 缓存策略 | `agent/agent_runtime_helpers.py` | 1154-1259 |
| 轨迹格式转换 | `agent/agent_runtime_helpers.py` | 66-233 |
| 错误分类器 | `agent/error_classifier.py` | （独立模块） |
| 上下文压缩器 | `agent/context_compressor.py` | （独立模块） |

---

## 八、关键数字

- **对话循环总行数**：~4200 行（`run_conversation` 单函数）
- **API 模式**：5 种（chat_completions / codex_responses / anthropic_messages / bedrock_converse / codex_app_server）
- **错误恢复策略**：15+ 种专项恢复（每种 FailoverReason 独立处理）
- **最大压缩次数**：默认 3 次
- **重试退避**：5s base, 120s cap, jitter ±30%
- **并发工具 worker**：最多 8 个 ThreadPoolExecutor worker
- **看门狗层数**：3 层（TTFB / stream idle / stale call）
- **系统提示词常量**：10+ 个独立 guidance 块
- **init_agent 属性数**：60+ 个实例属性
