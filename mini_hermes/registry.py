"""工具注册中心 — 对应 Hermes 的 tools/registry.py。

真实版额外含:toolset 分组、check_fn 可用性检查、TTL 缓存、
线程锁、async handler 桥接、自动发现。这里只保留最小骨架。
"""

import json
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class ToolEntry:
    """单个工具的元数据 — 对应 Hermes 的 ToolEntry(精简版)。"""
    name: str
    schema: dict                       # OpenAI function schema
    handler: Callable[[dict], str]     # 接收 args dict,返回 JSON 字符串


class ToolRegistry:
    """收集工具 schema + handler,按名调度执行。

    对应 Hermes 的 ToolRegistry,去掉了:
      - toolset 分组与 includes 组合
      - check_fn 可用性检查 + TTL 缓存
      - 线程锁(MCP 动态刷新场景才需要)
      - async handler 桥接
      - 自动发现(discover_builtin_tools)
    """

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}

    def register(self, name: str, schema: dict, handler: Callable[[dict], str]):
        """注册一个工具 — 对应 Hermes 的 registry.register()。"""
        self._tools[name] = ToolEntry(name=name, schema=schema, handler=handler)

    def dispatch(self, name: str, args: dict) -> str:
        """按名执行工具,异常转为 JSON error — 对应 registry.dispatch()。"""
        entry = self._tools.get(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            return entry.handler(args)
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    def get_schemas(self) -> List[dict]:
        """收集所有工具 schema,包装成 OpenAI tools 格式。

        对应 Hermes 的 model_tools.get_tool_definitions() 的核心逻辑:
        把 registry 里每个 ToolEntry 的 schema 包成
        {"type": "function", "function": {...}} 传给 LLM。
        """
        return [
            {"type": "function", "function": entry.schema}
            for entry in self._tools.values()
        ]


# 全局单例 — 对应 Hermes 的 tools/registry.py 末尾 `registry = ToolRegistry()`
registry = ToolRegistry()
