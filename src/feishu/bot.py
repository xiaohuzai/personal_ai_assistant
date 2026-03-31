"""
飞书机器人 — Personal AI Assistant

消息流向:  飞书 WebSocket → bot.py → agent/assistant.py → Claude SDK

支持消息类型: text（普通文本）、post（富文本）、image（图片）、file（文件）
支持场景: 私聊（p2p）、群聊（group，@Bot 触发）

斜杠指令:
  /help     — 显示帮助
  /new      — 新建会话（清空历史上下文）
  /reset    — 同 /new（兼容旧指令）
  /context  — 查看当前会话上下文摘要
  /compact  — 压缩当前会话上下文
  /sessions — 查看历史会话并切换

配置（环境变量）:
  FEISHU_APP_ID        飞书应用 App ID
  FEISHU_APP_SECRET    飞书应用 App Secret
  ASSISTANT_CWD        工作目录（默认 ./workspace）
  AGENT_OWNER          用户标识（默认 user）
"""
import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import lark_oapi as lark
from cachetools import TTLCache
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackCard,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from .feishu_client import FeishuClient
from . import card as cards
from ..agent import assistant, session as session_store
from ..agent.assistant import AVAILABLE_MODELS, get_current_model, set_model

logger = logging.getLogger(__name__)

# ─────────────────────────── 全局单例 ────────────────────────────

_feishu: Optional[FeishuClient] = None

# 共享 asyncio 事件循环（后台 daemon 线程），所有消息处理协程在此并发执行
_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="bot-async-loop").start()

# ─────────────────── 消息去重（内存版，单实例）────────────────────
# TTLCache 替代 Redis SETNX，key = message_id，TTL = 1 小时
_dedup_cache: TTLCache = TTLCache(maxsize=10_000, ttl=3600)
_dedup_lock = threading.Lock()


def _mark_processed(msg_id: str) -> bool:
    """标记消息已处理。返回 True 表示已存在（重复消息，应丢弃）。"""
    with _dedup_lock:
        if msg_id in _dedup_cache:
            return True
        _dedup_cache[msg_id] = 1
        return False


# ─────────────────── PendingChoice / PendingSessionSwitch ────────
# 替代 Redis，用带过期时间的内存字典管理待处理状态
_pending_choices: TTLCache = TTLCache(maxsize=1_000, ttl=600)
_pending_sessions: TTLCache = TTLCache(maxsize=1_000, ttl=600)
_pending_models: TTLCache = TTLCache(maxsize=1_000, ttl=600)
_pending_lock = threading.Lock()


@dataclass
class _PendingChoice:
    open_id: str
    source_msg_id: Optional[str]
    chat_type: str
    card_msg_id: str
    question: str
    choices: list
    reply_text: str


@dataclass
class _PendingSessionSwitch:
    open_id: str
    source_msg_id: Optional[str]
    chat_type: str
    card_msg_id: str
    sessions: list[dict]


@dataclass
class _PendingModelSwitch:
    open_id: str
    source_msg_id: Optional[str]
    chat_type: str
    card_msg_id: str
    models: list[dict]


def _store_pending_choice(token: str, pending: _PendingChoice) -> None:
    with _pending_lock:
        _pending_choices[token] = pending


def _pop_pending_choice(token: str) -> Optional[_PendingChoice]:
    with _pending_lock:
        return _pending_choices.pop(token, None)


def _peek_pending_choice(token: str) -> Optional[_PendingChoice]:
    with _pending_lock:
        return _pending_choices.get(token)


def _store_pending_session_switch(token: str, pending: _PendingSessionSwitch) -> None:
    with _pending_lock:
        _pending_sessions[token] = pending


def _pop_pending_session_switch(token: str) -> Optional[_PendingSessionSwitch]:
    with _pending_lock:
        return _pending_sessions.pop(token, None)


def _peek_pending_session_switch(token: str) -> Optional[_PendingSessionSwitch]:
    with _pending_lock:
        return _pending_sessions.get(token)


def _store_pending_model_switch(token: str, pending: _PendingModelSwitch) -> None:
    with _pending_lock:
        _pending_models[token] = pending


def _pop_pending_model_switch(token: str) -> Optional[_PendingModelSwitch]:
    with _pending_lock:
        return _pending_models.pop(token, None)


def _peek_pending_model_switch(token: str) -> Optional[_PendingModelSwitch]:
    with _pending_lock:
        return _pending_models.get(token)


# ─────────────────── 提交协程到后台事件循环 ──────────────────────

def _submit(coro) -> None:
    """从同步 lark 回调向 async 事件循环提交协程，fire-and-forget。"""
    asyncio.run_coroutine_threadsafe(coro, _loop)


def _submit_wait(coro, timeout: float = 15.0):
    """从同步 lark 回调提交协程并阻塞等待结果。"""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


# ─────────────────── 用户信息缓存 ───────────────────────────────

_user_info_cache: TTLCache = TTLCache(maxsize=5_000, ttl=3600)


def _get_user_info_cached(open_id: str) -> Optional[dict]:
    cached = _user_info_cache.get(open_id)
    if cached is not None:
        return cached
    if not _feishu:
        return None
    user_info = _feishu.get_user_by_open_id(open_id)
    if user_info:
        _user_info_cache[open_id] = user_info
    return user_info


# ──────────────────────── 打字机效果 ─────────────────────────────

def _split_into_chunks(lines: list[str]) -> list[list[str]]:
    """将文本行分组：Markdown 表格作为不可分割的原子块，其余每行一块。"""
    chunks: list[list[str]] = []
    table_buf: list[str] = []
    for line in lines:
        if line.strip().startswith("|"):
            table_buf.append(line)
        else:
            if table_buf:
                chunks.append(table_buf)
                table_buf = []
            chunks.append([line])
    if table_buf:
        chunks.append(table_buf)
    return chunks


async def _typewriter_update(message_id: str, text: str) -> None:
    """以打字机效果逐步更新消息卡片（最多 10 次中间更新）。"""
    lines = text.split("\n")
    if len(lines) <= 3:
        await asyncio.to_thread(_feishu.update_card, message_id, cards.build_text_card(text))
        return

    chunks = _split_into_chunks(lines)
    if len(chunks) <= 1:
        await asyncio.to_thread(_feishu.update_card, message_id, cards.build_text_card(text))
        return

    MAX_STEPS = 10
    step = max(1, len(chunks) // MAX_STEPS)
    accumulated: list[str] = []

    for i, chunk in enumerate(chunks):
        accumulated.extend(chunk)
        is_final = i == len(chunks) - 1
        if (i + 1) % step == 0 or is_final:
            display = "\n".join(accumulated)
            if not is_final:
                display += "\n▌"
            await asyncio.to_thread(_feishu.update_card, message_id, cards.build_text_card(display))
            if not is_final:
                await asyncio.sleep(0.2)


# ──────────────────────── mentions 解析 ──────────────────────────

def _resolve_mentions(text: str, raw_mentions: list) -> str:
    """将 @_user_X 占位符替换为「名字（邮箱：xxx）」；查不到通讯录的（如 Bot）直接移除。"""
    for mention in raw_mentions:
        key = mention.get("key", "")
        name = mention.get("name", "")
        open_id = (mention.get("id") or {}).get("open_id", "")
        if not key:
            continue
        replacement = ""
        if open_id:
            user_info = _get_user_info_cached(open_id)
            if user_info and user_info.get("email"):
                replacement = f"{name}（邮箱：{user_info['email']}）"
            elif user_info:
                replacement = name
        text = text.replace(key, replacement)
    return text


def _extract_post_content(content: dict) -> tuple[str, list[str]]:
    """从富文本(post)消息中提取纯文本和图片 key 列表。"""
    lang_content = (
        content.get("zh_cn")
        or content.get("en_us")
        or (content if "content" in content else {})
    )
    if not lang_content:
        return "", []
    parts = []
    image_keys: list[str] = []
    title = lang_content.get("title", "")
    if title:
        parts.append(title)
    for paragraph in lang_content.get("content", []):
        line_parts = []
        for elem in paragraph:
            tag = elem.get("tag", "")
            if tag == "text":
                line_parts.append(elem.get("text", ""))
            elif tag == "at":
                name = elem.get("user_name", "")
                line_parts.append(f"@{name}" if name else "")
            elif tag == "a":
                link_text = elem.get("text", "")
                href = elem.get("href", "")
                line_parts.append(f"{link_text}({href})" if href else link_text)
            elif tag == "img":
                key = elem.get("image_key", "")
                if key:
                    image_keys.append(key)
        parts.append("".join(line_parts))
    return "\n".join(parts).strip(), image_keys


# ─────────────────── 发送卡片（私聊/群聊）────────────────────────

async def _send_card(
    open_id: str,
    card: dict,
    chat_type: str,
    source_msg_id: Optional[str],
) -> Optional[str]:
    """根据聊天类型选择发送方式：群聊 reply，私聊 send。"""
    if chat_type == "group" and source_msg_id:
        return await asyncio.to_thread(_feishu.reply_card_to_message, source_msg_id, card)
    return await asyncio.to_thread(_feishu.send_card_to_open_id, open_id, card)


# ─────────────────── 消息处理协程 ────────────────────────────────

async def _process_message_async(
    open_id: str,
    text: str,
    feishu_msg_id: str,
    chat_type: str = "p2p",
    source_msg_id: Optional[str] = None,
    image_keys: Optional[list[str]] = None,
    file_tuples: Optional[list[tuple[str, str]]] = None,  # [(file_key, file_name), ...]
    meta: Optional[dict] = None,
) -> None:
    """异步协程：（下载图片/文件）→ 发占位卡片 → 调用 Agent → 打字机更新卡片。"""
    try:
        # 0a. 下载图片
        images: list[dict] = []
        if image_keys and _feishu:
            for key in image_keys:
                result = await asyncio.to_thread(_feishu.download_image_b64, feishu_msg_id, key)
                if result:
                    images.append(result)

        # 0b. 下载文件（原始字节，由 assistant 负责保存到 workspace）
        files: list[dict] = []
        if file_tuples and _feishu:
            for file_key, file_name in file_tuples:
                file_bytes = await asyncio.to_thread(_feishu.download_file, feishu_msg_id, file_key)
                if file_bytes:
                    files.append({"file_name": file_name, "data": file_bytes})
                else:
                    logger.warning("飞书文件下载失败，跳过: file_key=%s", file_key)

        # 1. 发"思考中"占位卡片
        thinking_card = cards.build_text_card("⏳ 正在思考...", is_thinking=True)
        message_id = await _send_card(open_id, thinking_card, chat_type, source_msg_id)

        # 2. 调用 Agent，实时更新占位卡片（工具调用进度）
        last_tool_update = 0.0

        async def on_tool_use(tool_name: str) -> None:
            nonlocal last_tool_update
            if not message_id:
                return
            now = time.monotonic()
            if now - last_tool_update > 2.0:
                last_tool_update = now
                progress = cards.build_text_card(f"⚙️ 正在执行 {tool_name}...", is_thinking=True)
                await asyncio.to_thread(_feishu.update_card, message_id, progress)

        choice_request = None
        try:
            resp_data = await assistant.run_message(
                open_id=open_id,
                content=text,
                images=images if images else None,
                files=files if files else None,
                meta=meta,
                on_tool_use=on_tool_use,
            )
            reply = resp_data.get("reply", "（无回复）")
            choice_request = resp_data.get("choice_request")
            logger.info("Agent 响应: open_id=%s reply_len=%d choice_request=%s",
                        open_id, len(reply), choice_request is not None)
        except Exception as e:
            logger.error("Agent 调用失败: open_id=%s, error=%s", open_id, e)
            reply = f"❌ 服务暂时不可用，请稍后重试。\n\n`{e}`"

        # 3. 更新卡片（choice 或普通文字）
        if choice_request:
            token = str(uuid.uuid4())
            question = choice_request.get("question", "请选择：")
            choices = choice_request.get("choices", [])
            card = cards.build_choice_card(reply, question, choices, token)
            if message_id:
                await asyncio.to_thread(_feishu.update_card, message_id, card)
                new_card_id = message_id
            else:
                new_card_id = await _send_card(open_id, card, chat_type, source_msg_id)
            if new_card_id:
                _store_pending_choice(token, _PendingChoice(
                    open_id=open_id,
                    source_msg_id=source_msg_id,
                    chat_type=chat_type,
                    card_msg_id=new_card_id,
                    question=question,
                    choices=choices,
                    reply_text=reply,
                ))
        elif message_id:
            await _typewriter_update(message_id, reply)
        else:
            await _send_card(open_id, cards.build_text_card(reply), chat_type, source_msg_id)

    except Exception as e:
        logger.error("消息处理异常: open_id=%s, error=%s", open_id, e, exc_info=True)


async def _process_reset_async(
    open_id: str,
    chat_type: str = "p2p",
    source_msg_id: Optional[str] = None,
) -> None:
    """重置用户会话（清除 session_id 映射）。"""
    try:
        session_store.clear_session(open_id)
        card = cards.build_text_card("✅ 会话已重置，开始全新对话。")
        await _send_card(open_id, card, chat_type, source_msg_id)
    except Exception as e:
        logger.error("重置处理异常: open_id=%s, error=%s", open_id, e, exc_info=True)


async def _process_slash_async(
    open_id: str,
    command: str,
    chat_type: str = "p2p",
    source_msg_id: Optional[str] = None,
) -> None:
    """向 Agent 发送系统指令（/compact、/context 等），打字机更新卡片。"""
    try:
        thinking_card = cards.build_text_card("⏳ 正在执行...", is_thinking=True)
        message_id = await _send_card(open_id, thinking_card, chat_type, source_msg_id)
        try:
            data = await assistant.run_slash(open_id, command)
            reply = data.get("reply", "✅ 已完成")
        except Exception as e:
            reply = f"❌ 执行失败，请稍后重试。\n\n`{e}`"
        if message_id:
            await _typewriter_update(message_id, reply)
        else:
            await _send_card(open_id, cards.build_text_card(reply), chat_type, source_msg_id)
    except Exception as e:
        logger.error("slash 处理异常: open_id=%s command=%s error=%s", open_id, command, e, exc_info=True)


async def _process_sessions_async(
    open_id: str,
    chat_type: str = "p2p",
    source_msg_id: Optional[str] = None,
) -> None:
    """拉取历史 session 列表，发送交互式切换卡片。"""
    try:
        from ..agent.assistant import WORKSPACE
        sessions = await asyncio.to_thread(session_store.list_sessions, WORKSPACE)
        current_session_id = session_store.get_session(open_id)

        if not sessions:
            await _send_card(open_id, cards.build_text_card("📂 暂无历史会话。"),
                             chat_type, source_msg_id)
            return

        token = str(uuid.uuid4())
        card = cards.build_sessions_card(sessions, current_session_id, token)
        card_msg_id = await _send_card(open_id, card, chat_type, source_msg_id)
        if card_msg_id:
            _store_pending_session_switch(token, _PendingSessionSwitch(
                open_id=open_id,
                source_msg_id=source_msg_id,
                chat_type=chat_type,
                card_msg_id=card_msg_id,
                sessions=sessions,
            ))
    except Exception as e:
        logger.error("sessions 处理异常: open_id=%s error=%s", open_id, e, exc_info=True)


async def _process_models_async(
    open_id: str,
    chat_type: str = "p2p",
    source_msg_id: Optional[str] = None,
) -> None:
    """拉取可用模型列表，发送交互式切换卡片。"""
    try:
        current_model = await asyncio.to_thread(get_current_model)
        models = AVAILABLE_MODELS
        token = str(uuid.uuid4())
        card = cards.build_models_card(models, current_model, token)
        card_msg_id = await _send_card(open_id, card, chat_type, source_msg_id)
        if card_msg_id:
            _store_pending_model_switch(token, _PendingModelSwitch(
                open_id=open_id,
                source_msg_id=source_msg_id,
                chat_type=chat_type,
                card_msg_id=card_msg_id,
                models=models,
            ))
    except Exception as e:
        logger.error("models 处理异常: open_id=%s error=%s", open_id, e, exc_info=True)

def _on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """飞书卡片按钮点击回调，必须在超时前返回响应。"""
    try:
        return _submit_wait(_on_card_action_async(data), timeout=15.0)
    except Exception as e:
        logger.error("card action 处理超时或异常: %s", e, exc_info=True)
    return P2CardActionTriggerResponse()


async def _on_card_action_async(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    try:
        value = (data.event.action.value or {}) if data.event and data.event.action else {}
        token = value.get("token")
        action = value.get("action", "choice")

        operator = data.event.operator if data.event else None
        clicker_open_id = operator.open_id if operator else None

        if action == "switch_session":
            option = getattr(data.event.action, "option", None) if data.event and data.event.action else None
            return await _handle_switch_session_action_async(clicker_open_id, value, token, option)
        elif action == "switch_model":
            option = getattr(data.event.action, "option", None) if data.event and data.event.action else None
            return await _handle_switch_model_action_async(clicker_open_id, value, token, option)
        else:
            return await _handle_choice_action_async(clicker_open_id, value, token)

    except Exception as e:
        logger.error("card action 处理异常: %s", e, exc_info=True)
    return P2CardActionTriggerResponse()


async def _handle_choice_action_async(
    clicker_open_id: Optional[str],
    value: dict,
    token: Optional[str],
) -> P2CardActionTriggerResponse:
    choice = value.get("choice")
    if not token or not choice:
        return P2CardActionTriggerResponse()

    # 先 peek 校验 owner，再原子 pop，防止非所有者点击消耗 token
    pending = _peek_pending_choice(token)
    if not pending:
        return P2CardActionTriggerResponse()

    if clicker_open_id != pending.open_id:
        logger.warning("choice action 被非所有者点击: clicker=%s owner=%s", clicker_open_id, pending.open_id)
        return P2CardActionTriggerResponse()

    pending = _pop_pending_choice(token)
    if not pending:
        return P2CardActionTriggerResponse()

    _submit(_process_message_async(
        pending.open_id, choice, pending.card_msg_id,
        pending.chat_type, pending.source_msg_id,
    ))
    chosen_card = cards.build_chosen_card(
        pending.reply_text, pending.question, pending.choices, choice
    )
    resp = P2CardActionTriggerResponse()
    resp.card = CallBackCard()
    resp.card.type = "raw"
    resp.card.data = chosen_card
    return resp


async def _handle_switch_session_action_async(
    clicker_open_id: Optional[str],
    value: dict,
    token: Optional[str],
    option: Optional[str] = None,
) -> P2CardActionTriggerResponse:
    session_id = option or value.get("session_id")
    if not token or not session_id:
        return P2CardActionTriggerResponse()

    pending = _peek_pending_session_switch(token)
    if not pending:
        return P2CardActionTriggerResponse()

    if clicker_open_id != pending.open_id:
        logger.warning("switch_session 被非所有者点击: clicker=%s owner=%s", clicker_open_id, pending.open_id)
        return P2CardActionTriggerResponse()

    pending = _pop_pending_session_switch(token)
    if not pending:
        return P2CardActionTriggerResponse()

    session_store.set_session(pending.open_id, session_id)
    logger.info("switched session: open_id=%s session_id=%s", pending.open_id, session_id)

    switched_card = cards.build_session_switched_card(pending.sessions, session_id)
    resp = P2CardActionTriggerResponse()
    resp.card = CallBackCard()
    resp.card.type = "raw"
    resp.card.data = switched_card
    return resp


async def _handle_switch_model_action_async(
    clicker_open_id: Optional[str],
    value: dict,
    token: Optional[str],
    option: Optional[str] = None,
) -> P2CardActionTriggerResponse:
    model_id = option or value.get("model_id")
    if not token or not model_id:
        return P2CardActionTriggerResponse()

    pending = _peek_pending_model_switch(token)
    if not pending:
        return P2CardActionTriggerResponse()

    if clicker_open_id != pending.open_id:
        logger.warning("switch_model 被非所有者点击: clicker=%s owner=%s", clicker_open_id, pending.open_id)
        return P2CardActionTriggerResponse()

    pending = _pop_pending_model_switch(token)
    if not pending:
        return P2CardActionTriggerResponse()

    await asyncio.to_thread(set_model, model_id)
    logger.info("switched model: open_id=%s model=%s", pending.open_id, model_id)

    switched_card = cards.build_model_switched_card(model_id)
    resp = P2CardActionTriggerResponse()
    resp.card = CallBackCard()
    resp.card.type = "raw"
    resp.card.data = switched_card
    return resp


def _on_message_receive(data: P2ImMessageReceiveV1) -> None:
    """飞书 im.message.receive_v1 事件回调（必须在 3s 内返回）。"""
    message = data.event.message

    # 幂等去重
    if _mark_processed(message.message_id):
        logger.debug("跳过重复消息: %s", message.message_id)
        return

    msg_type = message.message_type
    if msg_type not in ("text", "post", "image", "file"):
        return

    try:
        content = json.loads(message.content)
        text = ""
        image_keys: list[str] = []
        file_tuples: list[tuple[str, str]] = []
        if msg_type == "text":
            text = content.get("text", "").strip()
        elif msg_type == "post":
            text, image_keys = _extract_post_content(content)
        elif msg_type == "image":
            key = content.get("image_key", "")
            if key:
                image_keys = [key]
        elif msg_type == "file":
            file_key = content.get("file_key", "")
            file_name = content.get("file_name", "unnamed_file")
            if file_key:
                file_tuples = [(file_key, file_name)]
    except (json.JSONDecodeError, AttributeError):
        logger.warning("消息内容解析失败: %s", message.content)
        return

    # mentions 占位符替换（仅 text 类型）
    if msg_type == "text":
        mentions = getattr(message, "mentions", None) or []
        # @所有人 消息忽略
        if any(getattr(getattr(m, "id", None), "user_id", "") == "all" for m in mentions):
            logger.debug("忽略 @所有人 消息: %s", message.message_id)
            return
        raw_mentions = [
            {
                "key": getattr(m, "key", ""),
                "name": getattr(m, "name", ""),
                "id": {"open_id": getattr(m.id, "open_id", "") if getattr(m, "id", None) else ""},
            }
            for m in mentions
        ]
        if raw_mentions:
            text = _resolve_mentions(text, raw_mentions)

    text = text.lstrip()

    if not text and not image_keys and not file_tuples:
        return

    sender = data.event.sender
    open_id = sender.sender_id.open_id if sender and sender.sender_id else None
    if not open_id:
        logger.warning("无法获取 sender open_id，跳过")
        return

    chat_type = getattr(message, "chat_type", "p2p")
    source_msg_id = message.message_id

    meta: dict = {
        "chat_type": chat_type,
        "chat_id": message.chat_id,
        "message_time": message.create_time,
    }
    if message.parent_id:
        meta["parent_id"] = message.parent_id
    if message.root_id:
        meta["root_id"] = message.root_id

    logger.info("收到消息: sender=%s, chat_type=%s, type=%s, text=%.80s, images=%d",
                open_id, chat_type, msg_type, text, len(image_keys))

    # 斜杠指令
    cmd = text.strip()
    if cmd in ("/reset", "/new"):
        _submit(_process_reset_async(open_id, chat_type, source_msg_id))
        return
    if cmd == "/help":
        _submit(_send_card(open_id, cards.build_help_card(), chat_type, source_msg_id))
        return
    if cmd == "/sessions":
        _submit(_process_sessions_async(open_id, chat_type, source_msg_id))
        return
    if cmd == "/models":
        _submit(_process_models_async(open_id, chat_type, source_msg_id))
        return
    if cmd in ("/compact", "/context"):
        _submit(_process_slash_async(open_id, cmd, chat_type, source_msg_id))
        return

    # 普通消息路由
    _submit(_process_message_async(
        open_id, text, source_msg_id, chat_type, source_msg_id,
        image_keys or None, file_tuples or None, meta,
    ))


# ─────────────────────────── 入口 ────────────────────────────────

def start(app_id: str, app_secret: str) -> None:
    """启动飞书 WebSocket 长连接（阻塞）。"""
    global _feishu
    _feishu = FeishuClient(app_id, app_secret)

    logger.info("Personal Agent Bot 启动 (app_id=%s)", app_id)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message_receive)
        .register_p2_im_message_message_read_v1(lambda _: None)
        .register_p2_card_action_trigger(_on_card_action)
        .build()
    )

    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    ws_client.start()  # 阻塞，内部自动重连
