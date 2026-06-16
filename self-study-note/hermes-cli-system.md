# Hermes CLI 层源码详解（cli.py）

> 代码文件：`cli.py`（16181 行）
> 关联文件：`hermes_cli/main.py`、`hermes_cli/commands.py`、`hermes_cli/config.py`、`hermes_state.py`、`run_agent.py`

## 1. 总体定位

`cli.py` 是 Hermes Agent 的 **交互式终端前端**，负责：
- 构建 prompt_toolkit TUI（输入框 + 状态栏 + 滚动区）
- 管理会话生命周期（新建 / 恢复 / 分支 / 退出）
- 分发斜杠命令（`/help`、`/model`、`/new` 等 60+ 命令）
- 驱动 Agent 对话循环（创建 AIAgent → 发消息 → 显示响应 → 处理中断）
- 处理多模态交互（图片附件、语音模式、剪贴板粘贴）

它 **不包含** Agent 推理逻辑或工具执行逻辑——这些全在 `run_agent.py` 和 `agent/` 中。`cli.py` 只做"壳层"工作。

## 2. 文件结构总览

```text
cli.py
├── 模块级导入 + 工具函数（1-3070 行）
│   ├── 懒加载包装：CanonicalUsage, estimate_usage_cost
│   ├── 格式化辅助：format_duration_compact, format_token_count_compact
│   ├── Markdown 表格重对齐：realign_markdown_tables
│   ├── 内容提取：_assistant_content_as_text, _assistant_copy_text
│   ├── 配置加载：load_cli_config()
│   ├── 皮肤感知 ANSI：_SkinAwareAnsi 类（1878 行）
│   ├── ChatConsole 类（2802 行）—— Rich Console 的 prompt_toolkit 适配
│   └── ASCII 艺术 + Banner 构建
│
├── HermesCLI 类（3071-16181 行）
│   ├── __init__()          —— 400+ 行，初始化所有状态
│   ├── _init_agent()       —— Agent 懒创建
│   ├── _ensure_runtime_credentials()  —— 凭证解析
│   ├── chat()              —— 单轮对话核心
│   ├── run()               —— REPL 主循环
│   ├── process_command()   —— 斜杠命令分发（60+ 命令）
│   ├── 回调方法组           —— clarify / sudo / approval / secret
│   ├── 显示方法组           —— banner / status_bar / streaming
│   ├── 会话管理方法组       —— new / resume / undo / branch / save
│   ├── 语音模式方法组       —— voice record / TTS / STT
│   └── 内部嵌套函数         —— process_loop / spinner_loop / run_agent
```

## 3. 核心方法详解

### 3.1 `__init__()` — 全量状态初始化（3079-3480 行）

这是整个 HermesCLI 类的"地基"，约 400 行，初始化了 **60+ 个实例属性**，分为以下几组：

#### 3.1.1 配置读取

```python
self.config = CLI_CONFIG                    # 从 load_cli_config() 合并的全局配置
self.compact = compact or CLI_CONFIG["display"].get("compact", False)
self.tool_progress_mode = ...               # "off" | "new" | "all" | "verbose"
self.streaming_enabled = CLI_CONFIG["display"].get("streaming", False)
self.busy_input_mode = "interrupt"|"queue"|"steer"  # Agent 运行时用户输入行为
```

**优先级链**：CLI 参数 > 环境变量 > `config.yaml` > 硬编码默认值

#### 3.1.2 模型/凭证配置

```python
self.model = model or _config_model or ""   # 模型名
self.requested_provider = provider or config or env or "auto"
self.api_key = api_key or OPENROUTER_API_KEY or OPENAI_API_KEY
self.base_url = base_url or config or env
self.max_turns = max_turns or config or 90  # 工具调用迭代上限
```

注意：`provider` 的解析是 **延迟的**——`__init__` 只记录 `requested_provider`，实际解析在 `_ensure_runtime_credentials()` 中完成。

#### 3.1.3 会话状态

```python
self.agent = None                           # AIAgent 实例（懒创建）
self.conversation_history = []              # 对话历史列表
self.session_id = resume or f"{timestamp}_{uuid}"  # 会话 ID
self._session_db = SessionDB()             # SQLite 会话存储
self._pending_title = None                 # 延迟应用的会话标题
```

#### 3.1.4 交互状态（模态 UI）

cli.py 实现了一个 **状态机式的模态输入系统**，每种交互模式有独立的状态变量：

| 模式 | 状态变量 | 用途 |
|------|---------|------|
| Clarify（澄清选择） | `_clarify_state`, `_clarify_freetext`, `_clarify_deadline` | Agent 向用户提问 |
| Sudo（密码输入） | `_sudo_state`, `_sudo_deadline` | 终端提权密码 |
| Approval（命令审批） | `_approval_state`, `_approval_deadline`, `_approval_lock` | 危险命令确认 |
| Secret（密钥捕获） | `_secret_state`, `_secret_deadline` | Skill 密钥输入 |
| Slash Confirm（破坏性确认） | `_slash_confirm_state`, `_slash_confirm_deadline` | `/new`、`/clear` 等确认 |
| Model Picker（模型选择） | `_model_picker_state` | `/model` 下拉选择 |
| Voice（语音模式） | `_voice_mode`, `_voice_recording`, `_voice_tts`, `_voice_continuous` | 语音输入/输出 |

---

### 3.2 `_ensure_runtime_credentials()` — 凭证解析（4869-5015 行）

**调用时机**：每次 `chat()` 开始、`_init_agent()` 之前。

**核心职责**：通过 `resolve_runtime_provider()` 解析当前 provider 的真实凭证。

```text
请求 → resolve_runtime_provider(requested, explicit_key, explicit_url)
       ├── 成功 → 更新 self.api_key / self.base_url / self.provider / self.api_mode
       └── 失败 → 遍历 fallback 链 → 逐个尝试 → 全失败则报错
```

**关键逻辑**：

1. **Fallback 链**：主 provider 认证失败时，遍历 `_fallback_model` 列表尝试备选
2. **本地端点占位符**：自定义 `base_url`（如 ollama、vLLM）不需要 API key，用 `"no-key-required"` 占位
3. **模型归一化**：`_normalize_model_for_provider()` 确保模型名适配 provider（如 Codex 只能跑 Codex 模型）
4. **变更检测 → Agent 重建**：`credentials_changed or routing_changed or model_changed` → `self.agent = None`，触发下次 `chat()` 重建

---

### 3.3 `_init_agent()` — Agent 懒创建（5096-5310 行）

**调用时机**：`chat()` 中，当 `self.agent is None` 时触发。

**完整流程**：

```text
_init_agent()
├── _prepare_deferred_agent_startup()   # 延迟启动项（MCP 工具发现等）
├── _install_tool_callbacks()           # 注册 sudo/approval/secret 回调
├── _ensure_tirith_security()           # 安全检查器初始化
├── _ensure_runtime_credentials()       # 凭证解析（再次确认）
├── wait_for_mcp_discovery()            # 等待 MCP 服务器发现完成
├── SessionDB 初始化                    # SQLite 会话存储
├── 恢复会话历史（如果 resume）          # 从 SQLite 加载历史消息
└── AIAgent(...) 创建                   # 传入 40+ 参数
    ├── 模型/凭证：model, api_key, base_url, provider, api_mode
    ├── 工具配置：enabled_toolsets, disabled_toolsets
    ├── 回调函数：clarify_callback, reasoning_callback, thinking_callback,
    │             tool_progress_callback, stream_delta_callback, notice_callback
    ├── 会话上下文：session_id, platform="cli", session_db
    └── 高级配置：reasoning_config, service_tier, fallback_model, checkpoints
```

**恢复会话的特殊处理**：
- 验证 session_id 存在 → `resolve_resume_session_id()` 处理压缩链
- 加载历史消息 → `get_messages_as_conversation()` → 过滤掉 `session_meta` 角色
- 恢复 CWD → `_restore_session_cwd()`

---

### 3.4 `chat()` — 单轮对话核心（12213-12849 行）

这是 cli.py 中 **最复杂的方法**（630+ 行），处理一轮完整的用户输入→Agent 响应。

**完整流程**：

```text
chat(message, images=None)
├── 1. 凭证刷新：_ensure_runtime_credentials()
├── 2. Agent 路由解析：_resolve_turn_agent_config()
│       └── 签名变化 → self.agent = None（触发重建）
├── 3. Agent 懒创建：_init_agent()
├── 4. 图片路由：decide_image_input_mode() → "native" | "text"
│       ├── native → build_native_content_parts()（OpenAI 多模态格式）
│       └── text → _preprocess_images_with_vision()（视觉预分析转文本）
├── 5. @ 上下文引用展开：preprocess_context_references()
├── 6. Surrogate 字符清洗：_sanitize_surrogates()
├── 7. 消息加入 conversation_history
│
├── 8. 启动 Agent 线程：run_agent() [嵌套函数]
│       ├── 设置线程级回调（sudo/approval/secret）
│       ├── 绑定 approval session key
│       ├── 拼接 voice prefix + pending notes
│       └── self.agent.run_conversation(user_message, conversation_history,
│               stream_callback, task_id, persist_user_message)
│
├── 9. 中断监控循环（主线程）
│       └── while agent_thread.is_alive():
│           ├── 检查 _interrupt_queue（用户在 Agent 运行时输入的内容）
│           ├── 发现中断 → agent.interrupt(interrupt_msg)
│           └── 否则 → _invalidate()（刷新 UI）
│
├── 10. Agent 完成后处理
│       ├── 等待线程退出（中断路径：50×0.2s 轮询；正常路径：30s join）
│       ├── 清理异步客户端：cleanup_stale_async_clients()
│       ├── 刷新流式输出：_flush_stream()
│       ├── 同步 session_id（自动压缩可能创建了子会话）
│       ├── 自动生成会话标题：maybe_auto_title()
│       ├── 显示 reasoning 框（如果启用且未流式显示过）
│       ├── 显示最终响应 Panel（Rich 面板，皮肤感知颜色）
│       ├── 处理中断消息 → 重新入队到 _pending_input
│       └── 处理 leftover /steer → 重新入队
│
└── 11. 返回 response
```

**关键设计**：

- **双线程模型**：Agent 在 daemon 线程运行，主线程做中断监控
- **双队列设计**：`_pending_input`（空闲时输入）vs `_interrupt_queue`（Agent 运行时输入），避免竞争
- **中断恢复**：中断消息不会丢失——Agent 完成后，中断消息被重新入队为下一轮输入
- **流式 TTS**：ElevenLabs TTS 支持边生成边播放，通过 `text_queue` + `stop_event` 协调

---

### 3.5 `run()` — REPL 主循环（13172-~15500 行）

这是整个 CLI 的 **入口方法**，构建 prompt_toolkit Application 并启动交互循环。

**完整流程**：

```text
run()
├── 1. 环境准备
│       ├── _detect_light_mode()       # 检测终端亮/暗模式
│       ├── 推屏到底部                 # 空行填满终端高度
│       └── show_banner()              # 显示 ASCII 艺术 Banner
│
├── 2. 启动任务
│       ├── _show_security_advisories()    # 安全公告
│       ├── _preload_resumed_session()     # 恢复会话（如果有）
│       ├── prewarm_picker_cache_async()   # 预热 /model 选择器缓存
│       ├── maybe_run_curator()            # 后台 Skill 维护
│       └── 显示 Tip / 技能列表 / 安全警告
│
├── 3. 安装回调 + 安全检查
│       ├── _install_tool_callbacks()
│       └── _ensure_tirith_security()
│
├── 4. 构建 KeyBindings（键盘绑定）
│       ├── Enter → handle_enter()         # 提交输入（路由到正确的队列）
│       ├── Alt+Enter → handle_alt_enter() # 多行输入
│       ├── Ctrl+C → handle_ctrl_c()       # 中断/退出
│       ├── Ctrl+L → handle_ctrl_l()       # 重绘屏幕
│       ├── Ctrl+D → handle_ctrl_d()       # EOF 退出
│       ├── Ctrl+Z → handle_ctrl_z()       # 挂起（SIGTSTP）
│       ├── Tab → handle_tab()             # 自动补全
│       ├── 上/下 → history_up/down()      # 历史记录
│       ├── Escape → handle_escape_modal() # 关闭模态
│       ├── 数字键 → clarify/approval 快捷选择
│       └── 语音录制键（可配置）
│
├── 5. 构建 prompt_toolkit Layout
│       ├── 滚动区（历史输出）
│       ├── Spinner 区域（Agent 思考动画）
│       ├── 状态栏（模型/会话/上下文用量）
│       └── 输入框（TextArea + 补全菜单）
│
├── 6. 启动后台线程
│       ├── spinner_loop()  → spinner_thread  # UI 刷新
│       └── process_loop()  → process_thread  # 输入处理 + Agent 调度
│
├── 7. 信号处理
│       ├── SIGTERM → _signal_handler()     # 优雅关闭
│       ├── SIGHUP → _signal_handler()      # SSH 断开
│       └── SIGINT（Windows）→ _sigint_absorb()  # 吸收虚假 Ctrl+C
│
├── 8. 启动 prompt_toolkit
│       └── app.run()  ← 阻塞在这里，直到用户退出
│
└── 9. 退出清理
        ├── _print_exit_summary()   # 打印会话摘要
        └── _delete_session_on_exit # 可选：删除会话数据
```

---

### 3.6 `process_command()` — 斜杠命令分发（8790-~9300 行）

**调用时机**：`process_loop` 检测到 `/` 开头的输入时调用。

**分发机制**：
1. 通过 `resolve_command()` 解析别名 → 获取规范名（canonical name）
2. 基于 canonical name 做 `if/elif` 分发
3. 未匹配的 → 检查 quick_commands → 检查 skill commands → 报错

**支持的命令分类**：

| 类别 | 命令 | 说明 |
|------|------|------|
| **会话** | `/new`, `/clear`, `/resume`, `/sessions`, `/undo`, `/branch`, `/save`, `/history`, `/title`, `/handoff` | 会话生命周期管理 |
| **模型** | `/model`, `/fast`, `/reasoning`, `/personality`, `/codex-runtime` | 模型切换和配置 |
| **工具** | `/tools`, `/toolsets`, `/reload-mcp`, `/reload-skills` | 工具系统管理 |
| **配置** | `/config`, `/profile`, `/verbose`, `/footer`, `/statusbar`, `/busy`, `/skin`, `/reload` | 运行时配置 |
| **高级** | `/goal`, `/subgoal`, `/queue`, `/steer`, `/background`, `/kanban`, `/cron`, `/agents` | 高级功能 |
| **信息** | `/help`, `/status`, `/usage`, `/insights`, `/debug`, `/version`, `/plugins` | 信息查询 |
| **语音** | `/voice` | 语音模式切换 |
| **安全** | `/yolo`, `/snapshot`, `/rollback` | 安全检查点 |
| **退出** | `/quit`, `/exit` | 退出（可选 `--delete`） |

**破坏性命令确认**：`/new`、`/clear`、`/undo` 等通过 `_confirm_destructive_slash()` 弹出确认 UI，防止误操作。

---

### 3.7 `process_loop()` — 后台输入处理（15164-15313 行）

嵌套在 `run()` 中的后台线程函数，是 REPL 的"引擎"。

**循环逻辑**：

```text
while not self._should_exit:
├── 从 _pending_input 取输入（0.1s 超时）
├── 超时 → 空闲任务
│   ├── _check_config_mcp_changes()  # 监控 config.yaml 变化
│   └── drain_notifications()        # 后台进程通知
│
├── 输入预处理
│   ├── 解包图片附件 (tuple → text + images)
│   ├── 清理 bracketed paste 泄漏
│   ├── 检测文件拖放：_detect_file_drop()
│   └── 检查 /resume 数字选择
│
├── 斜杠命令检测：_looks_like_slash_command()
│   └── process_command() → 可能设置 _should_exit
│
└── 普通消息处理
    ├── _print_user_message_preview()
    ├── self._agent_running = True
    ├── self.chat(user_input, images)  ← 核心调用
    ├── self._agent_running = False
    ├── _maybe_continue_goal_after_turn()  # Goal 自动续跑
    ├── 语音模式自动重启录音
    └── 排空后台进程通知
```

## 4. 重要辅助方法

### 4.1 回调方法组（Agent ↔ CLI 交互桥梁）

这些回调在 `_init_agent()` 中注入到 AIAgent，让 Agent 能够与用户交互：

| 回调 | 方法 | 用途 |
|------|------|------|
| `_clarify_callback` | 11779 行 | Agent 调用 clarify 工具时，在 CLI 弹出选择 UI |
| `_sudo_password_callback` | 11846 行 | 终端需要 sudo 密码时弹出密码输入 |
| `_approval_callback` | 11892 行 | 危险命令执行前弹出审批选择 |
| `_computer_use_approval_callback` | 11954 行 | 计算机使用工具的操作审批 |
| `_secret_capture_callback` | 12165 行 | Skill 设置时安全捕获密钥输入 |
| `_on_tool_progress` | 11216 行 | 工具执行进度显示（scrollback 渲染） |
| `_on_thinking` | 4229 行 | Agent "思考中" 状态通知 |
| `_on_notice` | 4237 行 | 信用额度/使用量等通知 |

**模式统一**：所有模态回调都遵循相同模式——设置 `_xxx_state` 字典 → prompt_toolkit UI 切换为选择/输入模式 → 用户响应放入 `response_queue` → Agent 线程从队列取结果。

### 4.2 流式显示方法组

| 方法 | 用途 |
|------|------|
| `_stream_delta()` | 接收流式 token，行缓冲渲染到 scrollback |
| `_flush_stream()` | 关闭流式输出框，刷新剩余缓冲 |
| `_on_reasoning()` | 流式 reasoning 输出（thinking 内容） |
| `_render_spinner_text()` | Agent 思考时的动画文字 |

### 4.3 会话管理方法组

| 方法 | 用途 |
|------|------|
| `new_session()` | 创建新会话（重置 agent、history、session_id） |
| `_handle_resume_command()` | 列出最近会话 / 恢复指定会话 |
| `_preload_resumed_session()` | 从 SQLite 预加载历史 |
| `_display_resumed_history()` | 渲染恢复的历史消息 |
| `undo_last()` | 撤销最后 N 轮对话 |
| `_handle_branch_command()` | 从某轮对话创建分支 |
| `save_conversation()` | 导出对话为 Markdown/JSON |
| `retry_last()` | 重试最后一条消息 |

### 4.4 状态栏方法组

| 方法 | 用途 |
|------|------|
| `_get_status_bar_fragments()` | 构建状态栏各段（模型、会话、上下文用量等） |
| `_build_context_bar()` | 上下文使用量可视化（进度条） |
| `_format_prompt_elapsed()` | 每轮耗时显示 |
| `_build_status_bar_text()` | 完整状态栏文字组装 |

## 5. 简单说明的方法（非核心）

以下方法实现相对独立，简要说明：

| 方法 | 行号 | 简述 |
|------|------|------|
| `show_banner()` | 5337 | 显示 ASCII 艺术 Banner + 工具列表 + 模型信息 |
| `show_help()` | — | 渲染 `/help` 输出，按类别分组 |
| `show_config()` | — | 打印当前配置 |
| `show_toolsets()` | — | 列出可用工具集 |
| `show_history()` | — | 显示对话历史摘要 |
| `_handle_model_switch()` | — | `/model` 切换，弹出模型选择器 UI |
| `_handle_skills_command()` | — | `/skills` 管理（安装/卸载/浏览） |
| `_handle_voice_command()` | 11617 | `/voice` 切换语音模式 |
| `_voice_start_recording()` | 11329 | 开始录音（sounddevice） |
| `_voice_stop_and_transcribe()` | 11447 | 停止录音 + STT 转写 |
| `_voice_speak_response()` | 11565 | TTS 朗读响应 |
| `_handle_goal_command()` | 9759 | `/goal` 设置长期目标（自动续跑循环） |
| `_maybe_continue_goal_after_turn()` | 9900 | Goal 判断：是否需要自动继续 |
| `_manual_compress()` | 10387 | `/compress` 手动触发上下文压缩 |
| `_show_usage()` | 10595 | `/usage` 显示 token 用量和费用 |
| `_handle_update_command()` | 10543 | `/update` 自动更新 + 重启 |
| `_confirm_destructive_slash()` | 10876 | 破坏性操作确认 UI |
| `_reload_mcp()` | 11029 | 热重载 MCP 工具 |
| `_reload_skills()` | 11114 | 热重载 Skill 命令 |
| `_print_exit_summary()` | 12884 | 退出时打印会话摘要（ID、消息数、耗时、费用） |
| `_invalidate()` | 3481 | 节流 UI 重绘（0.25s 最小间隔） |
| `_force_full_redraw()` | 3490 | Ctrl+L 全屏重绘（恢复终端漂移） |
| `_recover_after_resize()` | 3536 | SIGWINCH 终端大小变化恢复 |
| `load_cli_config()` | 354 | 合并 CLI 默认配置 + 用户 YAML |

## 6. 关键设计模式

### 6.1 懒创建 + 签名变更检测

Agent 不在 `__init__` 中创建，而在第一次 `chat()` 时懒创建。同时通过 **路由签名**（model + provider + base_url + api_mode + command + args 的元组）检测配置变化，签名不匹配时自动重建 Agent：

```python
# chat() 中
turn_route = self._resolve_turn_agent_config(message)
if turn_route["signature"] != self._active_agent_route_signature:
    self.agent = None           # 触发 _init_agent() 重建
```

### 6.2 双队列中断模型

```text
用户空闲时输入 → _pending_input → process_loop 处理
Agent 运行时输入 → _interrupt_queue → chat() 中断监控处理
```

两个队列完全独立，避免了竞争条件。中断消息在 Agent 完成后被 **重新入队** 到 `_pending_input`，确保不丢失。

### 6.3 模态状态机

CLI 的输入处理是一个 **有限状态机**，通过 `_xxx_state` 变量切换模式：

```text
空闲 → 用户输入 → 斜杠命令 / Agent 对话
                → Agent 运行中 → 中断 / Clarify / Approval / Sudo / Secret
                                → Agent 完成 → 空闲
```

`handle_enter()` 是状态机的核心路由——根据当前状态将 Enter 键输入分发到正确的队列。

### 6.4 ChatConsole 适配层

`ChatConsole` 是 Rich Console 的 prompt_toolkit 适配器。Rich 渲染的 ANSI 输出被捕获到 StringIO 缓冲区，然后通过 `_cprint()` 路由到 prompt_toolkit 的 StdoutProxy，避免 ANSI 序列在 patch_stdout 下被损坏。

### 6.5 皮肤感知 UI

整个 CLI 的颜色方案通过 `_SkinAwareAnsi` 类延迟绑定到当前皮肤：

```python
_ACCENT = _SkinAwareAnsi("banner_accent", "#FFD700", bold=True)
# 每次 __str__() 调用时从 get_active_skin() 读取真实颜色
```

切换皮肤（`/skin`）后，所有 UI 元素自动更新颜色。

## 7. 数据流图：用户输入到 Agent 响应

```text
用户按键
  │
  ▼
prompt_toolkit KeyBindings
  │
  ├─ Enter → handle_enter()
  │           │
  │           ├─ _sudo_state → sudo response_queue
  │           ├─ _approval_state → approval response_queue
  │           ├─ _clarify_state → clarify response_queue
  │           ├─ _agent_running → _interrupt_queue（中断）
  │           └─ 空闲 → _pending_input（普通输入）
  │
  ▼
process_loop（后台线程）
  │
  ├─ 斜杠命令 → process_command() → 分发到具体 handler
  │
  └─ 普通消息 → chat(message)
                  │
                  ├─ _ensure_runtime_credentials()
                  ├─ _init_agent() → AIAgent(...)
                  │
                  ├─ [Agent 线程] run_agent()
                  │    └─ self.agent.run_conversation()
                  │         └─ 工具循环 → 返回 final_response
                  │
                  ├─ [主线程] 中断监控
                  │    └─ _interrupt_queue → agent.interrupt()
                  │
                  └─ 后处理
                       ├─ 同步 session_id
                       ├─ 自动标题生成
                       ├─ 显示响应 Panel
                       └─ 中断消息重入队
```

## 8. 关键文件速查表

| 文件 | 职责 | 关键符号 |
|------|------|---------|
| `cli.py` | 交互式 CLI 前端 | `HermesCLI`, `ChatConsole`, `load_cli_config()` |
| `hermes_cli/main.py` | CLI 入口，参数解析 → 创建 HermesCLI | `main()`, `HermesChatCommand` |
| `hermes_cli/commands.py` | 斜杠命令注册表 | `COMMAND_REGISTRY`, `resolve_command()`, `CommandDef` |
| `hermes_cli/config.py` | 配置合并（DEFAULT_CONFIG + 用户 YAML） | `load_config()`, `DEFAULT_CONFIG` |
| `hermes_cli/skin_engine.py` | 皮肤引擎 | `get_active_skin()`, `SkinConfig` |
| `hermes_cli/runtime_provider.py` | Provider 凭证解析 | `resolve_runtime_provider()` |
| `hermes_state.py` | SQLite 会话存储 | `SessionDB` |
| `run_agent.py` | AIAgent 核心 | `AIAgent.run_conversation()` |
| `model_tools.py` | 工具系统编排 | `get_tool_definitions()`, `handle_function_call()` |
