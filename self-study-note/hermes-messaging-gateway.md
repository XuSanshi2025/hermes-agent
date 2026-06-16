# Hermes Agent 消息网关（Messaging Gateway）

> 阶段 6：消息网关架构与多平台适配机制

---

## 一、架构总览

消息网关是 Hermes Agent 的多平台消息入口，将来自 Telegram、Discord、Slack、飞书、微信、WhatsApp 等 20+ 平台的消息统一路由到 AIAgent，并将回复投递回原平台。

```
用户消息 → 平台适配器(Adapter) → GatewayRunner._handle_message()
         → 授权检查 → Session 解析 → Slash 命令分发 / Agent 调用
         → 回复 → StreamConsumer 流式编辑 → Adapter.send()
```

### 核心文件布局

| 文件 | 行数 | 职责 |
|------|------|------|
| `gateway/run.py` | ~19K | GatewayRunner 主控制器：消息处理、Agent 调度、Slash 命令、生命周期 |
| `gateway/session.py` | ~1.4K | SessionSource / SessionEntry / SessionStore：会话持久化与上下文 |
| `gateway/config.py` | ~2K | Platform 枚举、GatewayConfig、PlatformConfig、SessionResetPolicy |
| `gateway/platforms/base.py` | ~4.8K | BasePlatformAdapter 抽象基类、MessageEvent、SendResult |
| `gateway/platforms/*.py` | 各不同 | 各平台具体实现（telegram/discord/slack/feishu/weixin/...） |
| `gateway/delivery.py` | ~430 | DeliveryRouter：cron 输出和跨平台投递路由 |
| `gateway/stream_consumer.py` | ~1.4K | GatewayStreamConsumer：同步回调 → 异步流式编辑 |
| `gateway/hooks.py` | ~230 | HookRegistry：事件钩子系统 |

---

## 二、Platform 枚举与配置体系

### 2.1 Platform 枚举（config.py）

```python
class Platform(Enum):
    LOCAL = "local"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    SLACK = "slack"
    SIGNAL = "signal"
    MATRIX = "matrix"
    FEISHU = "feishu"
    WECOM = "wecom"
    WEIXIN = "weixin"
    BLUEBUBBLES = "bluebubbles"
    QQBOT = "qqbot"
    YUANBAO = "yuanbao"
    DINGTALK = "dingtalk"
    API_SERVER = "api_server"
    WEBHOOK = "webhook"
    # ... 等 20+ 平台
```

**动态扩展机制**：`Platform._missing_()` 支持插件平台的动态枚举——只需在 `plugins/platforms/` 下放置 `plugin.yaml`，即可通过 `Platform("irc")` 获取稳定的伪枚举成员。

### 2.2 GatewayConfig 层级

```
GatewayConfig
├── platforms: Dict[Platform, PlatformConfig]  # 各平台配置
│   └── PlatformConfig
│       ├── enabled: bool
│       ├── token / api_key
│       ├── home_channel: HomeChannel           # 默认投递目标
│       ├── reply_to_mode: "off"|"first"|"all"
│       ├── gateway_restart_notification: bool
│       └── extra: Dict                          # 平台特有设置
├── default_reset_policy: SessionResetPolicy    # 会话重置策略
├── reset_by_platform: Dict[Platform, policy]   # 按平台定制
├── streaming: StreamingConfig                   # 流式传输配置
├── sessions_dir: Path                           # 会话存储路径
├── group_sessions_per_user: bool               # 群聊按用户隔离
└── unauthorized_dm_behavior: "pair"|"ignore"   # 未授权 DM 行为
```

### 2.3 SessionResetPolicy

控制会话何时自动重置：

| 模式 | 触发条件 |
|------|---------|
| `"daily"` | 每天固定时间（默认凌晨 4 点） |
| `"idle"` | 空闲超过 N 分钟（默认 1440 分钟 = 24 小时） |
| `"both"` | 以上两者先到先触发 |
| `"none"` | 不自动重置，仅靠压缩管理上下文 |

### 2.4 StreamingConfig

控制实时 token 流式传输到消息平台：

```python
@dataclass
class StreamingConfig:
    enabled: bool = False
    transport: str = "auto"    # "auto"|"draft"|"edit"|"off"
    edit_interval: float = 0.8  # 编辑间隔（秒）
    buffer_threshold: int = 24  # 缓冲区阈值
    cursor: str = " ▉"          # 流式光标
    fresh_final_after_seconds: float = 60.0  # 长响应后发送新消息代替编辑
```

**传输模式**：
- `"auto"`：优先使用平台原生草稿流（Telegram sendMessageDraft Bot API 9.5+），不支持时回退到编辑模式
- `"draft"`：明确请求原生草稿，不支持时回退
- `"edit"`：经典渐进式 editMessageText
- `"off"`：禁用流式传输

---

## 三、GatewayRunner 核心控制器

### 3.1 初始化（__init__）

GatewayRunner 是网关的心脏，初始化时构建：

| 组件 | 用途 |
|------|------|
| `self.adapters` | 平台适配器字典 `{Platform: BasePlatformAdapter}` |
| `self.session_store` | SessionStore 会话持久化 |
| `self.delivery_router` | DeliveryRouter 投递路由 |
| `self.hooks` | HookRegistry 事件钩子 |
| `self.pairing_store` | PairingStore DM 配对码授权 |
| `self._agent_cache` | OrderedDict Agent 实例 LRU 缓存（上限 128） |
| `self._running_agents` | 运行中 Agent 追踪（用于中断支持） |
| `self._session_model_overrides` | `/model` 命令的会话级模型覆盖 |
| `self._session_reasoning_overrides` | `/reasoning` 命令的推理努力覆盖 |
| `self._queued_events` | `/queue` 命令的 FIFO 队列 |

**Agent 缓存设计**：
- 使用 `OrderedDict` 实现 LRU：命中时 `move_to_end()`，驱逐时 `popitem(last=False)`
- 硬上限 `_AGENT_CACHE_MAX_SIZE = 128`
- 空闲 TTL 1 小时，由 `_session_expiry_watcher()` 执行
- 缓存 Agent 实例保持 prompt cache 命中——每次新建 Agent 会重建系统提示，导致 10x 成本

### 3.2 启动流程（start()）

```
start()
├── 写 runtime status: "starting"
├── 检查 systemd TimeoutStopSec 对齐
├── 日志记录 max_iterations / redaction 状态 / profile
├── 检查供应链安全公告
├── 警告无 allowlist 配置
├── _create_adapter() 逐个创建适配器
│   ├── 超时保护: _connect_adapter_with_timeout()
│   ├── 失败记录: _failed_platforms 用于后台重连
│   └── 锁获取: _acquire_platform_lock()
├── 启动 session expiry watcher
├── 启动 agent cache eviction
├── 安装 loop 异常处理器
├── 启动后台重连循环
├── hooks.emit("gateway:startup")
└── 等待 shutdown_event
```

### 3.3 消息处理流水线（_handle_message）

```
_handle_message(event: MessageEvent)
│
├── 1. pre_gateway_dispatch 插件钩子
│   ├── action="skip"    → 丢弃
│   ├── action="rewrite" → 改写消息文本
│   └── action="allow"   → 正常放行
│
├── 2. 用户授权检查 _is_user_authorized()
│   ├── 平台 env allowlist（TELEGRAM_ALLOWED_USERS 等）
│   ├── 全局 GATEWAY_ALLOWED_USERS
│   ├── 适配器自有 access policy（enforces_own_access_policy）
│   └── 未授权 DM → 配对码流程（PairingStore）
│
├── 3. 拦截层（按优先级）
│   ├── /update prompt 响应拦截
│   ├── clarify 文本响应拦截
│   ├── slash-confirm 响应拦截（/approve, /always, /cancel）
│   └── 运行中 Agent 的特殊处理
│       ├── /status → 直接返回
│       ├── /stop → 硬杀 + 中断
│       ├── /new, /reset → 会话重置
│       ├── 文本消息 → busy_input_mode 分发
│       │   ├── "interrupt" → 中断当前 Agent，处理新消息
│       │   └── "queue" → 排入 _pending_messages
│       └── 照片突发 → 不中断，让适配器批处理
│
├── 4. 过期 Agent 驱逐（stale eviction）
│   └── 检查 idle 时间 > HERMES_AGENT_TIMEOUT → 释放状态
│
└── 5. _handle_message_with_agent()
    ├── Session 获取/创建（get_or_create_session）
    ├── Telegram topic 恢复与绑定
    ├── 构建 SessionContext → 系统提示注入
    ├── 自动重置通知（was_auto_reset 消费）
    ├── 自动 Skill 加载（topic/channel 绑定）
    ├── 加载对话历史（load_transcript）
    ├── 会话卫生：超大 transcript 预压缩（85% 阈值）
    ├── 构建 AIAgent（_build_agent / _agent_cache）
    ├── 运行 Agent（_run_agent / _run_agent_via_proxy）
    ├── 保存 transcript
    ├── 投递回复（StreamConsumer + adapter.send）
    └── hooks.emit("agent:end")
```

### 3.4 Slash 命令处理

Gateway 中的 Slash 命令走独立路径，不经过 AIAgent：

| 命令 | 处理位置 | 说明 |
|------|---------|------|
| `/new`, `/reset` | `_handle_message` 内 | 重置会话 |
| `/stop` | `_handle_message` 内 | 中断 Agent + 挂起会话 |
| `/status` | `_handle_status_command` | 显示 Agent 状态 |
| `/restart` | `_handle_restart_command` | 网关重启 |
| `/model` | `_handle_model_command` | 切换模型（会话级覆盖） |
| `/reasoning` | 会话级覆盖 | 调整推理努力 |
| `/voice` | 持久化到 JSON | TTS 模式切换 |
| `/sethome` | 设置 HomeChannel | 默认投递目标 |
| `/queue` | 排入 FIFO 队列 | 显式排队执行 |
| `/help` | 生成帮助文本 | 平台特定格式化 |

**命令注册中心**：所有命令在 `hermes_cli/commands.py` 的 `COMMAND_REGISTRY` 中统一定义为 `CommandDef`，Gateway、CLI、Telegram 菜单、Slack 子命令路由全部从此派生。

---

## 四、会话管理（Session）

### 4.1 SessionSource

描述消息来源的完整上下文：

```python
@dataclass
class SessionSource:
    platform: Platform          # 平台枚举
    chat_id: str                # 聊天 ID
    chat_type: str              # "dm" | "group" | "channel" | "thread"
    user_id: Optional[str]      # 用户 ID
    user_name: Optional[str]    # 用户名
    thread_id: Optional[str]    # 论坛话题 / 线程 ID
    guild_id: Optional[str]     # Discord guild / Slack workspace
    parent_chat_id: Optional[str]
    message_id: Optional[str]   # 触发消息 ID（用于回复/固定/反应）
    user_id_alt: Optional[str]  # 跨应用稳定 ID（Signal UUID, 飞书 union_id）
    is_bot: bool = False        # 是否为机器人消息
```

### 4.2 Session Key 构建

Session Key 是会话的唯一标识，构建规则决定隔离粒度：

```
session_key = "{platform}:{chat_id}"                          # DM 基础
session_key = "{platform}:{chat_id}:{user_id}"               # 群聊 per-user 隔离
session_key = "{platform}:{chat_id}:{thread_id}"             # 线程共享
session_key = "{platform}:{chat_id}:{thread_id}:{user_id}"   # 线程 per-user
```

由 `group_sessions_per_user`（默认 True）和 `thread_sessions_per_user`（默认 False）控制。

### 4.3 SessionEntry

持久化的会话元数据：

```python
@dataclass
class SessionEntry:
    session_key: str
    session_id: str           # UUID，实际 transcript 文件名
    created_at: datetime
    updated_at: datetime
    origin: SessionSource     # 来源（用于 cron 投递路由）
    input_tokens / output_tokens / cache_read_tokens  # Token 追踪
    estimated_cost_usd: float
    last_prompt_tokens: int   # 压缩预检用
    was_auto_reset: bool      # 自动重置标记（消费一次）
    is_fresh_reset: bool      # 手动 /new 标记（消费一次）
    suspended: bool           # /stop 挂起
    resume_pending: bool      # 网关重启后的恢复标记
```

### 4.4 系统提示注入

`build_session_context_prompt()` 为 Agent 构建动态上下文提示：

- **来源信息**：平台名、聊天类型、用户名
- **PII 脱敏**：`redact_pii=True` 时，对安全平台（WhatsApp/Signal/Telegram/BlueBubbles）的 user_id/chat_id 进行 SHA-256 哈希
- **平台行为说明**：Discord 是否有工具、Slack 的 API 限制、iMessage 的短消息建议
- **已连接平台列表**：Agent 知道哪些平台在线
- **HomeChannel 投递选项**：cron 任务的目标列表
- **多用户会话提示**：共享会话时不固定用户名，避免 prompt cache 失效

---

## 五、平台适配器（BasePlatformAdapter）

### 5.1 抽象接口

```python
class BasePlatformAdapter(ABC):
    # 必须实现
    async def connect() -> bool           # 连接并启动监听
    async def disconnect()                # 断开连接
    async def send(chat_id, content, reply_to, metadata) -> SendResult

    # 可选实现（有默认空操作）
    async def edit_message(chat_id, message_id, content) -> SendResult
    async def send_typing(chat_id)        # 输入指示器
    async def send_image(chat_id, path/url, caption) -> SendResult
    async def send_document(chat_id, path, caption) -> SendResult
    async def send_voice(chat_id, path) -> SendResult
    async def send_video(chat_id, path, caption) -> SendResult
    async def send_animation(chat_id, path, caption) -> SendResult
    async def create_handoff_thread(parent_chat_id, name) -> Optional[str]
```

### 5.2 关键属性与机制

| 属性/机制 | 说明 |
|----------|------|
| `supports_code_blocks` | 是否渲染 Markdown 代码块（默认 False） |
| `enforces_own_access_policy` | 适配器是否在入口层自行鉴权（WeCom/Weixin/Yuanbao/QQBot） |
| `supports_draft_streaming()` | 是否支持原生草稿流（仅 Telegram DM，Bot API 9.5+） |
| `REQUIRES_EDIT_FINALIZE` | 是否需要显式 finalize 调用（如钉钉 AI 卡片） |
| `_active_sessions` | 会话级中断 Event 字典 |
| `_session_tasks` | 会话 → Task 映射，确保 /stop 取消正确的任务 |
| `_busy_text_mode` | "interrupt" 或 "queue"，控制忙碌时的消息处理 |
| `_auto_tts_*` | 自动 TTS 的三层配置（全局默认 / 按 chat 启用 / 按 chat 禁用） |
| `_typing_paused` | 暂停输入指示器的 chat 集合（如审批等待时） |

### 5.3 消息事件模型

```python
@dataclass
class MessageEvent:
    source: SessionSource        # 消息来源
    text: str                    # 消息文本
    message_type: MessageType    # TEXT / IMAGE / VOICE / DOCUMENT / COMMAND
    message_id: Optional[str]    # 平台消息 ID
    image_paths: List[str]       # 缓存后的图片路径
    document_paths: List[str]    # 缓存后的文档路径
    audio_path: Optional[str]    # 缓存后的音频路径
    reply_to_message_id: Optional[str]

    def is_command() -> bool     # text 以 / 开头
    def get_command() -> str     # 提取命令名（不含 /）
    def get_command_args() -> str # 命令参数
```

### 5.4 UTF-16 长度计算

Telegram 的消息长度限制（4096）按 UTF-16 code unit 计算，非 Unicode 码位：

```python
def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2

def _prefix_within_utf16_limit(s: str, limit: int) -> str:
    # 二分查找最长安全前缀，不切断代理对
```

BMP 外的字符（emoji、CJK 扩展 B）消耗 2 个 UTF-16 单元。

### 5.5 代理（Proxy）支持

`resolve_proxy_url()` 的优先级链：
1. 平台专用 env（如 `DISCORD_PROXY`）
2. `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`（含小写变体）
3. macOS 系统代理（`scutil --proxy`）

支持 `NO_PROXY` 排除、SOCKS5 远程 DNS（`rdns=True`，解决 DNS 污染）。

---

## 六、飞书适配器详解（FeishuAdapter）

作为复杂平台实现的典型代表，飞书适配器展示了完整的适配模式：

### 6.1 身份模型

飞书使用三级用户 ID：
- **open_id** (ou_xxx)：应用级，同一人在不同应用有不同 open_id
- **user_id** (u_xxx)：租户级，公司内稳定
- **union_id** (on_xxx)：开发者级，跨应用稳定（优先用于 session key）

### 6.2 连接模式

- **WebSocket 长连接**：`FeishuWSClient` 官方 SDK，自动重连
- **Webhook 回调**：aiohttp 服务器，带速率限制、异常追踪、验证令牌

### 6.3 消息处理

```
消息到达 → 去重检查（24h TTL, LRU 2048）
         → 准入策略（self_echo / bots_disabled / bot_not_mentioned / group_policy）
         → 消息类型解析（text / post / image / file / audio / interactive）
         → Post 富文本解析为 Markdown（样式、@提及、代码块、图片引用）
         → 构建 MessageEvent → handle_message() 分发
```

### 6.4 群聊策略引擎

```python
@dataclass
class FeishuGroupRule:
    policy: str      # "open" | "allowlist" | "blacklist" | "admin_only" | "disabled"
    allowlist: set
    blacklist: set
    require_mention: Optional[bool]  # None = 继承全局设置
```

支持按群独立配置，通过 `group_rules` 字典映射 chat_id → 规则。

### 6.5 反应状态管理

- 处理开始：添加 "Typing" 反应
- 处理成功：移除 "Typing"
- 处理失败：替换为 "CrossMark"

使用 LRU 缓存（1024 条）追踪 message_id → reaction_id 映射。

### 6.6 审批按钮

卡片按钮点击事件路由为合成 COMMAND 事件：

```python
_APPROVAL_CHOICE_MAP = {
    "approve_once": "once",
    "approve_session": "session",
    "approve_always": "always",
    "deny": "deny",
}
```

---

## 七、流式传输（StreamConsumer）

### 7.1 架构

```
AIAgent (sync worker thread)
  └── stream_delta_callback(text)
        └── GatewayStreamConsumer.on_delta(text)  # thread-safe, queue.Queue
              └── asyncio task: run()
                    └── 缓冲 → 速率限制 → adapter.send() / adapter.edit_message()
```

### 7.2 核心机制

| 机制 | 说明 |
|------|------|
| **缓冲阈值** | 积累 24 字符后才触发首次发送（减少空编辑） |
| **编辑间隔** | 0.8 秒，匹配 Telegram ~1 edit/s 的防洪限制 |
| **自适应退避** | 连续 flood control 失败 3 次后永久禁用编辑 |
| **Think 块过滤** | 剥离 `<think>`, `<reasoning>` 等推理标签内容 |
| **工具边界** | `_NEW_SEGMENT` 标记：工具调用后开始新消息段 |
| **Fresh Final** | 长响应（>60s）完成后发送新消息代替编辑，显示正确时间戳 |
| **草稿流** | Telegram DM 使用 `sendMessageDraft` 实现动画预览 |

### 7.3 传输选择逻辑

```python
# run() 中的传输解析：
if transport == "auto":
    if adapter.supports_draft_streaming(chat_type):
        use_draft = True  # Telegram DM + Bot API 9.5+
    else:
        use_edit = True   # 回退到编辑模式
elif transport == "draft":
    use_draft = True       # 明确请求，失败时降级
elif transport == "edit":
    use_edit = True        # 经典模式
```

---

## 八、事件钩子系统（HookRegistry）

### 8.1 事件类型

| 事件 | 触发时机 |
|------|---------|
| `gateway:startup` | 网关进程启动 |
| `session:start` | 新会话创建 |
| `session:end` | 用户执行 /new 或 /reset |
| `session:reset` | 会话重置完成 |
| `agent:start` | Agent 开始处理消息 |
| `agent:step` | 工具调用循环每一步 |
| `agent:end` | Agent 完成处理 |
| `command:*` | 任意 Slash 命令（通配符匹配） |

### 8.2 钩子结构

每个钩子是 `~/.hermes/hooks/<name>/` 下的目录：

```
hooks/my_hook/
├── HOOK.yaml      # name, description, events: ["agent:start", ...]
└── handler.py     # async def handle(event_type, context)
```

通配符匹配：注册 `command:*` 的钩子会接收所有 `command:xxx` 事件。

---

## 九、投递路由（DeliveryRouter）

### 9.1 目标解析

```python
DeliveryTarget.parse("origin")              # → 回到来源
DeliveryTarget.parse("local")               # → 本地文件
DeliveryTarget.parse("telegram")            # → Telegram HomeChannel
DeliveryTarget.parse("telegram:123456")     # → 指定 chat
DeliveryTarget.parse("telegram:123:456")    # → 指定 chat + thread
```

### 9.2 投递链

```
deliver(content, targets)
├── 对每个 target:
│   ├── LOCAL → 保存到 ~/.hermes/cron/output/
│   └── 平台 → adapter.send() 或 standalone_sender_fn()
├── 消息截断：>4000 字符分段发送
├── 静默叙述过滤：丢弃 "*(silent)*" 等幻觉
└── 失败日志 + 继续（不阻塞其他 target）
```

---

## 十、安全与隐私

### 10.1 用户授权

三层授权机制：
1. **env allowlist**：`TELEGRAM_ALLOWED_USERS` 等平台特定变量
2. **全局 allowlist**：`GATEWAY_ALLOWED_USERS`
3. **适配器自有策略**：`enforces_own_access_policy=True` 的平台在入口层自行鉴权

未授权 DM 处理：
- `"pair"`（默认）：生成配对码，用户让所有者执行 `hermes pairing approve`
- `"ignore"`：静默忽略

### 10.2 密钥脱敏

`_redact_gateway_user_facing_secrets()` 在回复发送前扫描：
- `sk-*`（OpenAI 密钥）
- `gh[pousr]_*`（GitHub token）
- `xox[baprs]-*`（Slack token）
- `hf_*`（HuggingFace token）
- `Bearer *`

匹配到的密钥替换为 `[REDACTED]`。

### 10.3 Provider 错误安全

`_sanitize_gateway_final_response()` 对 Telegram 回复做特殊处理：
- 认证失败 → 简短安全提示（不暴露原始 HTTP 错误）
- 策略拒绝 → 告知请求被拒（不暴露 policy 细节）
- 速率限制 → 友好等待提示

### 10.4 PII 脱敏

`build_session_context_prompt(redact_pii=True)` 对安全平台执行：
- user_id → `user_<sha256[:12]>`
- chat_id → `platform:<sha256[:12]>`
- 电话号码剥离

Discord 被排除在外（mention 语法需要真实 ID）。

---

## 十一、添加新平台

### 11.1 插件路径（推荐）

在 `~/.hermes/plugins/<name>/` 下创建：

```yaml
# plugin.yaml
name: my_platform
description: My custom platform adapter
register:
  platform:
    name: my_platform
    display_name: My Platform
```

```python
# adapter.py
from gateway.platforms.base import BasePlatformAdapter

class MyAdapter(BasePlatformAdapter):
    async def connect(self) -> bool: ...
    async def disconnect(self) -> None: ...
    async def send(self, chat_id, content, ...) -> SendResult: ...
```

**零核心代码修改**——插件系统自动处理适配器创建、配置解析、授权、cron 投递、send_message 路由等。

### 11.2 内置路径（16 步清单）

核心贡献者需要修改的集成点：

1. **Core Adapter** — `gateway/platforms/<name>.py`
2. **Platform Enum** — `gateway/config.py` 添加枚举值
3. **Adapter Factory** — `gateway/run.py` `_create_adapter()`
4. **Authorization Maps** — `gateway/run.py` `_is_user_authorized()` 的两个字典
5. **Session Source** — `gateway/session.py` 如需额外字段
6. **System Prompt** — `agent/prompt_builder.py` PLATFORM_HINTS
7. **Toolset** — `toolsets.py` 添加平台工具集
8. **Cron Delivery** — `cron/scheduler.py` platform_map
9. **Send Message Tool** — `tools/send_message_tool.py` 路由
10. **Cronjob Schema** — `tools/cronjob_tools.py` deliver 参数描述
11. **Channel Directory** — `gateway/channel_directory.py` 会话发现
12. **Status Display** — `hermes_cli/status.py` 平台状态
13. **Setup Wizard** — `hermes_cli/gateway.py` 设置向导
14. **Phone/ID Redaction** — `agent/redact.py` 标识符脱敏
15. **Documentation** — README, AGENTS.md, website docs
16. **Tests** — `tests/gateway/test_<name>.py`

---

## 十二、关键设计模式

### 12.1 瞬态网络错误吞没

`_gateway_loop_exception_handler()` 安装在 asyncio 事件循环上，吞没 `TimedOut`、`NetworkError`、`ConnectError` 等 13 种瞬态错误，防止它们杀死整个网关进程。非瞬态错误转发到默认处理器。

### 12.2 SSL 证书自动检测

`_ensure_ssl_certs()` 在任何 HTTP 库导入前执行：
1. 检查 `SSL_CERT_FILE` 是否指向真实文件
2. Python 编译时默认路径
3. `certifi` 包
4. 常见发行版路径（Debian/RHEL/SUSE/Alpine/macOS Homebrew）

### 12.3 会话卫生（Hygiene）

在 Agent 启动前检测超大 transcript：
- **阈值**：85% 模型上下文长度（高于 Agent 自身的 50%）
- **硬消息限制**：400 条（可配置）
- **目的**：防止 API 级别的上下文溢出——Agent 自己的压缩器在工具循环内处理常规上下文管理

### 12.4 运行中 Agent 的陈旧驱逐

```python
# 驱逐条件：
should_evict = (
    agent is not PENDING_SENTINEL
    and (
        idle_time >= HERMES_AGENT_TIMEOUT  # 空闲超时
        or wall_age > max(timeout * 10, 7200)  # 极端墙钟时间
    )
)
```

检查 Agent 的 `get_activity_summary()` 获取真实空闲时间，防止泄漏的锁阻塞新消息。

### 12.5 Telegram 特殊处理

- **Topic 恢复**：跨话题的回复自动路由到用户最后活跃的话题
- **Topic 绑定**：Telegram forum topic → session_id 的持久映射
- **压缩尖端追踪**：绑定指向压缩前的父 session 时，自动跳转到压缩后的子 session
- **命令提及格式化**：`/command` 提及在 Telegram 中规范化为合法命令名
- **噪音状态过滤**：压缩/重试/rate-limit 等状态消息不发送到 Telegram 聊天

---

## 十三、数据流总结

```
                    ┌─────────────────────────────────────────┐
                    │            GatewayRunner                │
                    │                                         │
  Telegram ──┐     │  _handle_message()                      │
  Discord  ──┤     │    ├── auth check                       │
  Slack    ──┤     │    ├── session resolution               │
  Feishu   ──┼──→  │    ├── slash command dispatch           │
  WeChat   ──┤     │    └── _handle_message_with_agent()     │
  WhatsApp ──┤     │         ├── build context prompt        │
  Signal   ──┤     │         ├── load transcript             │
  Matrix   ──┤     │         ├── hygiene compression         │
  QQ       ──┤     │         ├── _build_agent (cached)       │
  Yuanbao  ──┘     │         ├── _run_agent (thread pool)    │
                    │         │    └── StreamConsumer.on_delta│
                    │         └── save transcript + hooks     │
                    │                                         │
                    │  DeliveryRouter                          │
                    │    ├── origin → adapter.send()          │
                    │    ├── platform → home channel          │
                    │    └── local → file                     │
                    └─────────────────────────────────────────┘
```

---

## 十四、self-study-note 系列索引

| 阶段 | 文档 | 核心内容 |
|------|------|---------|
| 1 | hermes-runtime-mechanism.md | 启动链、配置体系、常量管理 |
| 2 | hermes-tool-system.md | 工具注册、发现、执行、Toolset 管理 |
| 3 | hermes-agent-core.md | AIAgent 初始化、对话循环、API 调用 |
| 4 | hermes-cli-system.md | CLI 架构、Slash 命令、皮肤引擎 |
| 5 | hermes-key-subsystems.md | 压缩、凭证池、错误分类、模型路由 |
| **6** | **hermes-messaging-gateway.md** | **消息网关、多平台适配、流式传输** |
| 7 | （待完成） | 插件系统 + 高级主题 |
