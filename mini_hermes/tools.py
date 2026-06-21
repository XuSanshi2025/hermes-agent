"""四个内置工具 — 对应 Hermes 的 tools/file_tools.py + tools/terminal_tool.py。

还原 Hermes 的工具文件模式:定义 schema + handler,末尾调用 registry.register()。
真实版每个工具都有数百行(模糊匹配、语法检查、环境后端、安全审批等),这里只保留核心逻辑。

扩展新工具只需:新建文件,定义 schema + handler,调用 registry.register() —— 和 Hermes 完全一致。
"""

import json
import os
import subprocess

from mini_hermes.registry import registry

# ── terminal ──────────────────────────────────────────────
# 对应 tools/terminal_tool.py(简化:去掉环境后端/PTY/后台进程/审批)
TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": "Execute a shell command and return stdout+stderr.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute"},
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait (default 60)",
                "default": 60,
            },
        },
        "required": ["command"],
    },
}


def _handle_terminal(args: dict) -> str:
    cmd = args["command"]
    timeout = args.get("timeout", 60)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout
    )
    return json.dumps({
        "returncode": result.returncode,
        "stdout": result.stdout[:20000],
        "stderr": result.stderr[:20000],
    })


# ── read_file ─────────────────────────────────────────────
# 对应 tools/file_tools.py 的 READ_FILE_SCHEMA(简化:去掉分页/相似名建议)
READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "Read a text file with line numbers. Output format: 'LINE_NUM|CONTENT'.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read"},
        },
        "required": ["path"],
    },
}


def _handle_read_file(args: dict) -> str:
    path = args["path"]
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    # 带行号输出,格式与 Hermes 一致:LINE_NUM|CONTENT
    numbered = "\n".join(f"{i + 1}|{line.rstrip()}" for i, line in enumerate(lines))
    return json.dumps({"content": numbered, "lines": len(lines)})


# ── write_file ────────────────────────────────────────────
# 对应 tools/file_tools.py 的 WRITE_FILE_SCHEMA(简化:去掉语法检查/跨配置保护)
WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": "Write content to a file, creating parent dirs. Overwrites existing content.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write"},
            "content": {"type": "string", "description": "Complete file content"},
        },
        "required": ["path", "content"],
    },
}


def _handle_write_file(args: dict) -> str:
    path = args["path"]
    # 自动创建父目录 — 与 Hermes 一致
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(args["content"])
    return json.dumps({"success": True, "bytes_written": len(args["content"])})


# ── patch ─────────────────────────────────────────────────
# 对应 tools/file_tools.py 的 PATCH_SCHEMA(简化:纯精确匹配,去掉模糊匹配 9 策略)
PATCH_SCHEMA = {
    "name": "patch",
    "description": "Find-and-replace a unique string in a file. The old_string must appear exactly once.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit"},
            "old_string": {"type": "string", "description": "Exact text to find"},
            "new_string": {"type": "string", "description": "Text to replace it with"},
        },
        "required": ["path", "old_string", "new_string"],
    },
}


def _handle_patch(args: dict) -> str:
    path = args["path"]
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    count = content.count(args["old_string"])
    if count == 0:
        return json.dumps({"error": "old_string not found"})
    if count > 1:
        return json.dumps({"error": f"old_string matches {count} times; must be unique"})
    content = content.replace(args["old_string"], args["new_string"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return json.dumps({"success": True, "matches": count})


# ── 注册(对应 Hermes 工具文件末尾的 registry.register() 调用)──
# 真实 Hermes 用 auto-discovery 扫描 tools/*.py 里的 register 调用;
# 这里手动注册 4 个,效果等价 —— 导入本模块即完成注册。
registry.register("terminal", TERMINAL_SCHEMA, _handle_terminal)
registry.register("read_file", READ_FILE_SCHEMA, _handle_read_file)
registry.register("write_file", WRITE_FILE_SCHEMA, _handle_write_file)
registry.register("patch", PATCH_SCHEMA, _handle_patch)
