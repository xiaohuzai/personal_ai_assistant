"""
Claude Agent SDK 封装

提供 run_message() 和 run_slash() 两个核心接口，直接驱动 claude-agent-sdk
处理用户消息，无需 HTTP 中间层。
"""
import asyncio
import glob as _glob
import json
import logging
import os
import re
import shutil
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

# CHOICE_REQUEST 正则（与 test/mcp-hub/agent-server/main.py 一致）
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
- 执行 Bash 命令（Bash 工具已授权，可直接使用）
- 添加/删除 MCP server（编辑 {workspace}/.claude/settings.json）
- 安装 Skill（写入 {workspace}/.claude/commands/）
- 使用 `lark-cli` 访问飞书（已安装并完成账号授权，可直接在 Bash 中调用）

【文件回传规则】当你在 workspace 中生成了文件（代码、报告、数据等），用户可能希望直接在飞书收到文件。主动发文件给用户的方式：
  lark-cli im +messages-send --chat-id <feishu_context 中的 chat_id> --file <文件绝对路径>
发文件前无需确认，直接执行即可。

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


# ─────────────── Node.js 工具 PATH 自动注入 ──────────────────────

def _ensure_node_tools_in_path() -> None:
    """自动发现 node/npm 全局 bin 目录，注入到 os.environ["PATH"]。

    解决问题：Claude Agent SDK 启动的子进程不加载 .bashrc/.profile，
    因此 nvm 等工具设置的 PATH 不会被继承，导致 lark-cli 等工具不可用。

    搜索顺序：
    1. 已在 PATH 中 → 跳过
    2. 环境变量 LARK_CLI_BIN_DIR 显式指定
    3. nvm 管理的所有 node 版本 (~/.nvm/versions/node/*/bin)
    4. npm 全局前缀 (npm prefix -g)
    5. 常见固定路径 (~/.npm-global/bin, /usr/local/bin 等)
    """
    if shutil.which("lark-cli"):
        return  # 已在 PATH，无需处理

    candidates: list[str] = []

    # 1. 显式环境变量覆盖
    explicit = os.environ.get("LARK_CLI_BIN_DIR", "")
    if explicit:
        candidates.append(explicit)

    # 2. nvm 路径：~/.nvm/versions/node/*/bin（按版本号倒序，优先最新）
    nvm_pattern = os.path.expanduser("~/.nvm/versions/node/*/bin")
    nvm_dirs = sorted(_glob.glob(nvm_pattern), reverse=True)
    candidates.extend(nvm_dirs)

    # 3. 常见固定路径
    candidates.extend([
        os.path.expanduser("~/.npm-global/bin"),
        "/usr/local/bin",
        "/usr/local/lib/node_modules/.bin",
        "/snap/bin",
    ])

    for d in candidates:
        if os.path.isfile(os.path.join(d, "lark-cli")):
            current_path = os.environ.get("PATH", "")
            if d not in current_path.split(os.pathsep):
                os.environ["PATH"] = d + os.pathsep + current_path
                logger.info("已将 %s 注入 PATH（lark-cli 发现于此）", d)
            return

    logger.debug("未找到 lark-cli，lark 相关功能不可用")


# ─────────────────── lark-cli 自动配置 ───────────────────────────

_lark_cli_notice: Optional[str] = None   # 非 None 表示尚未完成授权
_lark_cli_notice_sent: bool = False      # 每个进程生命周期内只提示一次


async def _setup_lark_cli() -> Optional[str]:
    """检查并自动配置 lark-cli。
    1. 检查 auth status，已授权则返回 None
    2. config 不存在时用 FEISHU_APP_ID/APP_SECRET 自动 config init
    3. 返回提示用户执行 lark-cli auth login 的文字（未授权时）
    lark-cli 未安装或超时时静默忽略（返回 None）。
    """
    async def _run(*args, stdin_data: Optional[bytes] = None) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=stdin_data), timeout=10.0
                )
                return proc.returncode or 0, stdout_b.decode(errors="replace"), stderr_b.decode(errors="replace")
            except asyncio.TimeoutError:
                proc.kill()
                return -1, "", "timeout"
        except FileNotFoundError:
            return -1, "", "not_found"
        except Exception as exc:
            return -1, "", str(exc)

    # 1. 检查 auth status
    rc, stdout, stderr = await _run("lark-cli", "auth", "status")
    if rc == -1 and stderr in ("not_found", "timeout"):
        return None  # lark-cli 未安装或超时，静默忽略
    if rc == 0:
        try:
            if json.loads(stdout).get("tokenStatus") == "valid":
                return None  # 已授权，无需提示
        except (json.JSONDecodeError, AttributeError):
            pass

    # 2. 检查 config 文件是否存在，且 appId 与环境变量一致；不存在或不一致则自动初始化
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    config_file = os.path.join(os.environ.get("HOME", os.path.expanduser("~")), ".lark-cli", "config.json")

    need_init = True
    if os.path.exists(config_file):
        try:
            with open(config_file) as _f:
                _cfg = json.load(_f)
            existing_app_id = (_cfg.get("apps") or [{}])[0].get("appId", "")
            if existing_app_id == app_id:
                need_init = False  # appId 一致，跳过 init
            else:
                logger.warning("lark-cli config appId 不匹配: config=%s env=%s，重新初始化",
                               existing_app_id, app_id)
                os.remove(config_file)
        except Exception as exc:
            logger.warning("lark-cli config 读取失败，重新初始化: %s", exc)
            try:
                os.remove(config_file)
            except OSError:
                pass

    if need_init:
        if app_id and app_secret:
            rc_init, _, err_init = await _run(
                "lark-cli", "config", "init",
                "--app-id", app_id,
                "--app-secret-stdin",
                "--brand", "feishu",
                stdin_data=app_secret.encode(),
            )
            if rc_init == 0:
                logger.info("lark-cli config init 完成 (app_id=%s)", app_id)
            else:
                logger.warning("lark-cli config init 失败 (app_id=%s): %s", app_id, err_init)
        else:
            logger.warning("FEISHU_APP_ID/APP_SECRET 未设置，跳过 lark-cli config init")

    # 3. config 已就绪但 user token 尚未授权
    return "lark-cli 尚未完成飞书账号授权"


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
    global _lark_cli_notice_sent
    mcp_servers = _load_mcp_servers()
    system_prompt = SYSTEM_PROMPT.format(owner=AGENT_OWNER, workspace=WORKSPACE)
    if _lark_cli_notice and not _lark_cli_notice_sent:
        system_prompt += (
            "\n\n【启动提醒】lark-cli 尚未完成飞书账号授权。"
            "请在回复用户本次消息时顺带提示：运行 `lark-cli auth login` 可完成飞书账号授权，"
            "授权后即可使用飞书相关功能（日历、消息、妙记等）。仅提示一次，无需重复。"
        )
        _lark_cli_notice_sent = True
    return ClaudeAgentOptions(
        cwd=WORKSPACE,
        system_prompt=system_prompt,
        permission_mode="dontAsk",  # root 下 bypassPermissions 被 CLI 拒绝
        # dontAsk 模式必须显式传 allowed_tools 才能 headless 自动批准工具；
        # mcp__* 保证 MCP 工具不被阻断
        allowed_tools=["Bash(*)", "Read", "Write", "Edit", "Glob", "Grep",
                       "WebFetch", "WebSearch", "Task", "mcp__*"],
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

    # 每次消息前重新加载 .agent_env（支持运行时新增变量）
    _load_agent_env()

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
        # 有部分输出时保留内容，附上中断提示，避免丢失已有结果
        partial_text = "".join(reply_parts).strip()
        if partial_text:
            reply_parts = [partial_text + f"\n\n⚠️ 输出中断（{type(e).__name__}）"]
        else:
            raise

    if stderr_lines:
        logger.warning("query stderr:\n%s", "\n".join(stderr_lines))

    _limit_msg = "⏸️ 任务进行中但工具调用已达上限（20 轮），请回复「继续」让我接着做。"
    _reply_text = "".join(reply_parts).strip()
    if stop_reason == "tool_use":
        # max_turns 耗尽且最后一轮是工具调用，追加提示到已有输出末尾
        _reply_text = (_reply_text + "\n\n" + _limit_msg) if _reply_text else _limit_msg
    elif not _reply_text:
        if stop_reason == "max_turns":
            _reply_text = _limit_msg
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


_PROJECT_SETTINGS_PATH = os.path.join(WORKSPACE, ".claude", "settings.json")

# 可选模型列表（用于 /models 卡片）
# 优先读 .env 里的网关别名，保证发给网关的 model ID 与网关配置一致；
# 未配置时回退到 Anthropic 官方 ID。
def _build_available_models() -> list[dict]:
    entries = [
        (os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6"), "Claude Sonnet（默认）"),
        (os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL",   "claude-opus-4-6"),   "Claude Opus（最强）"),
        (os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL",  "claude-haiku-4-5-20251001"), "Claude Haiku（最快）"),
    ]
    seen: set[str] = set()
    result: list[dict] = []
    for model_id, name in entries:
        if model_id and model_id not in seen:
            result.append({"id": model_id, "name": f"{name} · {model_id}"})
            seen.add(model_id)
    return result


AVAILABLE_MODELS: list[dict] = _build_available_models()


def get_current_model() -> str:
    """读取 {WORKSPACE}/.claude/settings.json 中的 env.ANTHROPIC_MODEL。"""
    try:
        if os.path.exists(_PROJECT_SETTINGS_PATH):
            with open(_PROJECT_SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("env", {}).get("ANTHROPIC_MODEL", "")
    except Exception:
        pass
    return ""


def set_model(model_id: str) -> None:
    """将 ANTHROPIC_MODEL 写入 {WORKSPACE}/.claude/settings.json → env 节。"""
    os.makedirs(os.path.dirname(_PROJECT_SETTINGS_PATH), exist_ok=True)
    data: dict = {}
    if os.path.exists(_PROJECT_SETTINGS_PATH):
        try:
            with open(_PROJECT_SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    data.setdefault("env", {})["ANTHROPIC_MODEL"] = model_id
    with open(_PROJECT_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("ANTHROPIC_MODEL 已设置为 %s", model_id)


async def initialize() -> None:
    """模块初始化：加载持久化 env，注入 Node 工具路径，检查并自动配置 lark-cli。
    由 main.py 在启动时调用一次。
    """
    global _lark_cli_notice
    _load_agent_env()
    _ensure_node_tools_in_path()
    _lark_cli_notice = await _setup_lark_cli()
    if _lark_cli_notice:
        logger.warning("lark-cli 未完成授权，将在首次回复时提示用户执行 lark-cli auth login")
