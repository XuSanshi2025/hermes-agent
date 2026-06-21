"""CLI 入口 — 对应 cli.py 的交互核心。

真实 cli.py 有 ~11k 行(Rich 面板、prompt_toolkit 自动补全、斜杠命令、
皮肤引擎、会话管理等)。这里只保留 input 循环 + 打印响应的最小交互。

用法:
  python -m mini_hermes.main [model_name]

环境变量:
  OPENAI_BASE_URL  — OpenAI 兼容 API 地址(默认 http://localhost:11434/v1)
  OPENAI_API_KEY   — API 密钥(默认 ollama)
  MODEL            — 默认模型名(默认 qwen2.5:7b)
"""

import os
import sys

from mini_hermes.agent import MiniAgent

# 导入 tools 触发 registry.register()
# 对应 Hermes 的 model_tools.py 在启动时自动发现并导入 tools/*.py
import mini_hermes.tools  # noqa: F401


def main():
    base_url = os.getenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    api_key = os.getenv("OPENAI_API_KEY", "ollama")
    model = sys.argv[1] if len(sys.argv) > 1 else os.getenv("MODEL", "qwen2.5:7b")

    agent = MiniAgent(base_url=base_url, api_key=api_key, model=model)
    history: list = []

    print(f"mini-hermes ready | model={model} | tools={len(agent.tools)}")
    print("Type 'exit' to quit.\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input or user_input.lower() in ("exit", "quit"):
            break

        response = agent.run_conversation(user_input, history=history)
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": response})
        print(f"\nassistant> {response}\n")


if __name__ == "__main__":
    main()
