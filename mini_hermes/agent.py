"""MiniAgent + 对话循环 — 对应 run_agent.py 的 AIAgent + agent/conversation_loop.py。

真实版 run_conversation 有 4500 行,包含:
  TTFB/idle/stale 三层看门狗、流式 delta、压缩触发、重试降级、
  中断检查、预算追踪、steer 注入、空响应 nudge、
  truncated tool-call 修复、post-turn 后台审查...

这里只保留「调用 -> 工具闭环 -> 返回」的主干循环。
"""

import json
import logging
from typing import Any, Dict, List, Optional

from openai import OpenAI

from mini_hermes.registry import registry

logger = logging.getLogger(__name__)


class MiniAgent:
    """最小 Agent — 对应 run_agent.py 的 AIAgent。

    真实 AIAgent.__init__ 有 ~60 个参数(凭据池/回调/预算/线程ID/...);
    这里只保留让循环跑起来的最小配置。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str = "You are a helpful coding assistant.",
        max_iterations: int = 30,
    ):
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        # 对应 model_tools.get_tool_definitions() —— 从 registry 收集所有工具 schema
        self.tools = registry.get_schemas()

    def run_conversation(
        self,
        user_message: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """核心对话循环 — 对应 agent/conversation_loop.py 的 run_conversation()。

        循环流程(对照 AGENTS.md 里的简化伪码):
          1. 调用 LLM(真实版:含流式 + 看门狗 + 重试)
          2. 无 tool_calls -> 返回最终响应
          3. 有 tool_calls -> 追加 assistant 消息(含 tool_calls)
          4. 逐个执行工具并回填 tool 消息(对应 _execute_tool_calls)
          5. 循环回到第 1 步,带上工具结果继续调用 LLM
        """
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        for iteration in range(self.max_iterations):
            # 1. 调用 LLM
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools,
            )
            msg = response.choices[0].message

            # 2. 无工具调用 -> 返回最终响应
            if not msg.tool_calls:
                return msg.content or "(empty response)"

            # 3. 有工具调用 -> 追加 assistant 消息(含 tool_calls)
            #    这一步必须做:OpenAI API 要求 tool 结果消息的 tool_call_id
            #    对应一条含 tool_calls 的 assistant 消息。
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })

            # 4. 逐个执行工具并回填结果(对应 _execute_tool_calls)
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                print(f"  [tool] {name}({args})")
                # 对应 registry.dispatch() —— 按名执行,返回 JSON 字符串
                result = registry.dispatch(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            # 5. 循环回到第 1 步,带上工具结果继续调用 LLM

        return "(reached max iterations without final response)"
