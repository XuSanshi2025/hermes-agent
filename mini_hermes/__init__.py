"""mini-hermes: Hermes Agent 核心原理的最小化实现。

用约 340 行代码重现 Hermes 的三大核心机制:
1. 可插拔工具注册框架 (registry.register / registry.dispatch)
2. LLM 对话循环 (调用 -> 检测工具调用 -> 执行 -> 回填 -> 循环)
3. 工具调用闭环

对应关系:
  registry.py  <- tools/registry.py
  tools.py     <- tools/file_tools.py + terminal_tool.py
  agent.py     <- run_agent.py + agent/conversation_loop.py
  main.py      <- cli.py
"""
