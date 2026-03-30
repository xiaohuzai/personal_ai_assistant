"""
Claude Agent SDK 封装

提供 run_message() 和 run_slash() 两个核心接口，直接驱动 claude-agent-sdk
处理用户消息，无需 HTTP 中间层。
"""
import json
import logging
import os
import re
import time
from typing import Awaitable, Callable, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from . import session as session_store

logger = logging.getLogger(__name__)

# CHOICE_REQUEST 正则（与 test/app/main.py 一致）
_CHOICE_RE = re.compile(r'CHOICE_REQUEST:(\{[^{}]*(?:\[[^\]]*\][^{}]*)?\})', re.DOTALL)

WORKSPACE: str = os.environ.get("ASSISTANT_CWD", os.path.join(os.getcwd(), "workspace"))
AGENT_OWNER: str = os.environ.get("AGENT_OWNER", "user")
UPLOADS_DIR: str = os.path.join(WORKSPACE, "uploads")

# MCP transport 类型归一化（SDK 仅接受 Literal["http"]）
_MCP_TYPE_ALIASES: dict[str, str] = {"streamableHttp": "http"}
_MCP_SUPPORTED_TYPES = {"http", "sse", "stdio", "sdk"}

SYSTEM_PROMPT = """
你是 {owner} 的个人 AI Agent，运行在飞书私聊对话中。

你有 {workspace} 目录的完整读写权限，可以：
- 管理文件和代码
- 执行 Bash 命令
- 添加/删除 MCP server（编辑 {workspace}/.claude/settings.json）
- 安装 Skill（写入 {workspace}/.claude/commands/）

【与用户交互规则】任何需要向用户提问、让用户选择、或请求用户确认的场景，必须在回复末尾输出 CHOICE_REQUEST 标记。禁止使用任何工具来询问用户，直接在文字回复末尾输出：
CHOICE_REQUEST:{{"question":"<问题>","choices":["<选项1>","<选项2>",...]}}

输出后立即停止，等待用户点击按钮，不要继续执行。

典型场景：
- 执行危险命令前确认：CHOICE_REQUEST:{{"question":"是否继续执行？","choices":["✅ 确认执行","❌ 取消"]}}
- 方案选择：CHOICE_REQUEST:{{"question":"请选择实现方式","choices":["方案 A","方案 B","方案 C"]}}
- 任意 Yes/No：CHOICE_REQUEST:{{"question":"是否继续？","choices":["是","否"]}}

需要执行危险操作（sudo 命令、安装软件包、网络下载、删除文件、修改配置文件）时，必须先输出 CHOICE_REQUEST 确认后再执行。

【环境变量持久化规则】设置任何需要长期有效的环境变量时，必须同时写入 {workspace}/.agent_env 文件（KEY=VALUE 格式）。该文件在重启后会自动加载，仅执行 export 的变量重启后会丢失。

【回复规则】无论执行任何操作，结束后必须用中文文字告诉用户操作结果。不允许静默完成，必须有文字回复。
""".strip()


def _load_mcp_servers() -> dict:
    """从 ~/.claude.json 读取 WORKSPACE 对应的 mcpServers 配置。"""
    claude_json_path = os.path.join(os.path.expanduser("~"), ".claude.json")
    try:
        with open(claude_json_path) as f:
            data = json.load(f)
        raw: dict = data.get("projects", {}).get(WORKSPACE, {}).get("mcpServers", {})
    except Exception:
        return {}
    result: dict = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        t = cfg.get("type", "")
        normalized = _MCP_TYPE_ALIASES.get(t, t)
        if normalized not in _MCP_SUPPORTED_TYPES:
            logger.warning("MCP server %r has unsupported type %r, skipping", name, t)
            continue
        result[name] = {**cfg, "type": normalized}
    return result


def _load_agent_env() -> None:
    """加载 {WORKSPACE}/.agent_env 文件中的环境变量（不覆盖已有变量）。"""
    env_path = os.path.join(WORKSPACE, ".agent_env")
    if not os.path.exists(env_path):
        return
    count = 0
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip()
                    count += 1
    except Exception as e:
        logger.warning("Failed to load .agent_env: %s", e)
        return
    if count:
        logger.info("Loaded %d env vars from .agent_env", count)


def _parse_choice_request(text: str) -> tuple[str, Optional[dict]]:
    """从回复文本中解析并移除 CHOICE_REQUEST 标记，返回 (clean_text, choice_request_or_None)。"""
    m = _CHOICE_RE.search(text)
    if not m:
        return text, None
    try:
        choice_request = json.loads(m.group(1))
        clean_text = text[:m.start()].rstrip()
        return clean_text, choice_request
    except json.JSONDecodeError:
        return text, None


async def _build_prompt_stream(content: str, session_id: Optional[str], images: list[dict]):
    """构建多模态消息流（文本 + 图片），供 query(AsyncIterable) 模式使用。"""
    content_blocks = [{"type": "text", "text": content}] if content else []
    for img in images:
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["data"],
            },
        })
    yield {
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }


def _make_options(session_id: Optional[str], max_turns: int = 20) -> ClaudeAgentOptions:
    """构建 ClaudeAgentOptions，resume 已有 session 或新建。"""
    mcp_servers = _load_mcp_servers()
    system_prompt = SYSTEM_PROMPT.format(owner=AGENT_OWNER, workspace=WORKSPACE)
    return ClaudeAgentOptions(
        cwd=WORKSPACE,
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        extra_args={"allow-dangerously-skip-permissions": None},
        mcp_servers=mcp_servers,
        setting_sources=["project"],
        resume=session_id,
        max_turns=max_turns,
    )


async def run_message(
    open_id: str,
    content: str,
    images: Optional[list[dict]] = None,
    files: Optional[list[dict]] = None,
    meta: Optional[dict] = None,
    on_tool_use: Optional[Callable[[str], Awaitable[None]]] = None,
) -> dict:
    """
    处理用户消息，返回 {"reply": str, "session_id": str, "choice_request": dict | None}。

    - open_id: 飞书用户 open_id，用于 session 路由
    - content: 消息文本
    - images: 图片列表，每项 {"media_type": "...", "data": "<base64>"}
    - files: 文件列表，每项 {"file_name": "...", "data": bytes}（已从飞书下载）
    - meta: 飞书消息元信息，注入为 <feishu_context>
    - on_tool_use: 工具调用时的异步回调，参数为工具名称
    """
    session_id = session_store.get_session(open_id)
    if session_id and not session_store.session_exists(WORKSPACE, session_id):
        logger.warning("session %s not found on disk, starting new", session_id)
        session_id = None

    # 文件保存到 workspace/uploads/，注入路径到消息
    if files:
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        saved_lines: list[str] = []
        for f in files:
            file_name = f.get("file_name", "unnamed_file")
            file_bytes = f.get("data")
            if not file_bytes:
                continue
            safe_name = re.sub(r"[^\w\-_\.]", "_", file_name)
            save_path = os.path.join(UPLOADS_DIR, f"{int(time.time())}_{safe_name}")
            try:
                with open(save_path, "wb") as fp:
                    fp.write(file_bytes)
                saved_lines.append(f"- `{save_path}`（原文件名：{file_name}，{len(file_bytes)} 字节）")
                logger.info("文件已保存: %s (%d bytes)", save_path, len(file_bytes))
            except Exception as e:
                logger.error("文件保存失败: %s, error=%s", file_name, e)
        if saved_lines:
            files_ctx = "用户上传了以下文件，已保存到 workspace，可直接使用工具读取：\n" + "\n".join(saved_lines)
            content = f"{files_ctx}\n\n{content}" if content else files_ctx

    # 注入飞书消息元信息
    if meta:
        meta_lines = "\n".join(f"{k}: {v}" for k, v in meta.items() if v is not None)
        content = f"<feishu_context>\n{meta_lines}\n</feishu_context>\n{content}"

    options = _make_options(session_id)
    stderr_lines: list[str] = []
    options.stderr = lambda line: stderr_lines.append(line)

    prompt = _build_prompt_stream(content, session_id, images or []) if images else content

    reply_parts: list[str] = []
    new_session_id: str = session_id or ""
    stop_reason: Optional[str] = None

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        reply_parts.append(block.text)
                    elif on_tool_use and hasattr(block, "name"):
                        tool_name = getattr(block, "name", "")
                        if tool_name:
                            await on_tool_use(tool_name)
            elif isinstance(message, ResultMessage):
                if message.session_id:
                    new_session_id = message.session_id
                stop_reason = getattr(message, "stop_reason", None)
    except Exception as e:
        if stderr_lines:
            logger.error("query stderr:\n%s", "\n".join(stderr_lines))
        logger.error("query failed: open_id=%s session=%s error=%s", open_id, session_id, e, exc_info=True)
        raise

    if stderr_lines:
        logger.warning("query stderr:\n%s", "\n".join(stderr_lines))

    _reply_text = "".join(reply_parts).strip()
    if not _reply_text:
        if stop_reason == "max_turns":
            _reply_text = "⏸️ 任务进行中但工具调用已达上限（20 轮），请回复「继续」让我接着做。"
        else:
            _reply_text = "✅ 已完成（Claude 未输出文字回复）"

    reply, choice_request = _parse_choice_request(_reply_text)

    if new_session_id:
        session_store.set_session(open_id, new_session_id)

    logger.info("done: open_id=%s session=%s reply_len=%d choice_request=%s",
                open_id, new_session_id, len(reply), choice_request is not None)
    return {"reply": reply, "session_id": new_session_id, "choice_request": choice_request}


async def run_slash(open_id: str, command: str) -> dict:
    """
    向当前 session 发送原生 Claude 系统指令（/compact、/context 等）。
    返回 {"reply": str, "session_id": str}。
    """
    session_id = session_store.get_session(open_id)
    if session_id and not session_store.session_exists(WORKSPACE, session_id):
        session_id = None

    options = _make_options(session_id, max_turns=1)
    slash_stderr: list[str] = []
    options.stderr = lambda line: slash_stderr.append(line)

    reply_parts: list[str] = []
    new_session_id: str = session_id or ""

    try:
        async for message in query(prompt=command, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        reply_parts.append(block.text)
            elif isinstance(message, ResultMessage) and message.session_id:
                new_session_id = message.session_id
    except Exception as e:
        if slash_stderr:
            logger.error("slash stderr:\n%s", "\n".join(slash_stderr))
        logger.error("slash failed: open_id=%s command=%s error=%s", open_id, command, e, exc_info=True)
        raise

    if slash_stderr:
        logger.warning("slash stderr:\n%s", "\n".join(slash_stderr))

    reply = "".join(reply_parts).strip() or "✅ 已完成"
    if new_session_id:
        session_store.set_session(open_id, new_session_id)

    logger.info("slash done: open_id=%s command=%s session=%s", open_id, command, new_session_id)
    return {"reply": reply, "session_id": new_session_id}
