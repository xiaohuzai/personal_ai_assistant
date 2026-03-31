"""
飞书消息卡片构建工具

所有卡片使用 schema 2.0，支持完整 GFM（表格、代码块等）。
"""
import time
from datetime import datetime
from typing import Optional


def build_text_card(
    text: str,
    title: Optional[str] = None,
    is_thinking: bool = False,
) -> dict:
    """构建 schema 2.0 Markdown 消息卡片。"""
    card: dict = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "direction": "vertical",
            "elements": [{"tag": "markdown", "content": text}],
        },
    }
    if title is not None:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": "grey" if is_thinking else "turquoise",
        }
    return card


def build_choice_card(text: str, question: str, choices: list, token: str) -> dict:
    """构建带按钮的交互卡片，用于用户确认/选择场景。"""
    elements = []
    if text:
        elements.append({"tag": "markdown", "content": text})
        elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": f"**{question}**"})
    for i, choice in enumerate(choices):
        elements.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": choice},
            "type": "primary" if i == 0 else "default",
            "value": {"choice": choice, "token": token},
        })
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }


def build_chosen_card(text: str, question: str, choices: list, chosen: str) -> dict:
    """按钮点击后：选中项高亮保留，其余按钮置灰，全部不可再点。"""
    elements = []
    if text:
        elements.append({"tag": "markdown", "content": text})
        elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": f"**{question}**"})
    for choice in choices:
        elements.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": choice},
            "type": "primary" if choice == chosen else "default",
            "disabled": True,
        })
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }


def build_help_card() -> dict:
    """构建帮助卡片，列出所有可用斜杠指令。"""
    content = (
        "| 指令 | 功能 |\n"
        "|------|------|\n"
        "| `/help` | 显示此帮助 |\n"
        "| `/new` | 新建会话（清空历史上下文） |\n"
        "| `/context` | 查看当前会话的上下文摘要 |\n"
        "| `/compact` | 压缩当前会话上下文（节省 token） |\n"
        "| `/sessions` | 查看历史会话并切换 |\n"
        "| `/models` | 查看可用模型并切换 |\n\n"
        "💡 直接发送消息即可与 Agent 对话。"
    )
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Personal Agent 指令手册"},
            "template": "blue",
        },
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }


def _format_session_time(timestamp: int) -> str:
    """将 unix 时间戳格式化为友好的相对/绝对时间字符串。"""
    diff = time.time() - timestamp
    if diff < 3600:
        return f"{max(1, int(diff / 60))} 分钟前"
    if diff < 86400:
        return f"{int(diff / 3600)} 小时前"
    if diff < 86400 * 7:
        return f"{int(diff / 86400)} 天前"
    return datetime.fromtimestamp(timestamp).strftime("%m月%d日")


def _truncate(text: str, max_len: int = 32) -> str:
    return text[:max_len] + "..." if len(text) > max_len else text


def build_sessions_card(
    sessions: list[dict],
    current_session_id: Optional[str],
    token: str,
) -> dict:
    """构建 session 切换卡片：下拉选择框 + 确认弹窗。"""
    options = []
    for s in sessions:
        sid = s["session_id"]
        preview = _truncate(s.get("preview", "（无内容）"), 80)
        time_str = _format_session_time(s.get("updated_at", 0))
        is_current = sid == current_session_id
        label = f"{'✅ ' if is_current else ''}{sid[:8]} · {time_str} — {preview}"
        options.append({"text": {"tag": "plain_text", "content": label}, "value": sid})

    elements: list[dict] = [
        {
            "tag": "select_static",
            "placeholder": {"tag": "plain_text", "content": "选择要切换的会话..."},
            "initial_option": current_session_id or (sessions[0]["session_id"] if sessions else ""),
            "options": options,
            "value": {"action": "switch_session", "token": token},
            "confirm": {
                "title": {"tag": "plain_text", "content": "确认切换会话"},
                "text": {"tag": "plain_text", "content": "切换后下条消息将在所选会话中继续。"},
            },
        }
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"切换会话（共 {len(sessions)} 条）"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def build_session_switched_card(sessions: list[dict], switched_to_id: str) -> dict:
    """切换成功后的确认卡片。"""
    session = next((s for s in sessions if s["session_id"] == switched_to_id), None)
    if session:
        time_str = _format_session_time(session.get("updated_at", 0))
        preview = _truncate(session.get("preview", ""), 40)
        detail = f"`{switched_to_id[:8]}` · {time_str}\n> {preview}"
    else:
        detail = f"`{switched_to_id[:8]}`"
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "✅ 已切换会话"},
            "template": "green",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"已切换到：{detail}\n\n下条消息将在此会话中继续。"},
            ]
        },
    }


def build_models_card(
    models: list[dict],
    current_model: str,
    token: str,
) -> dict:
    """构建模型切换卡片：下拉选择框 + 确认弹窗。"""
    options = []
    for m in models:
        mid = m["id"]
        label = f"{'✅ ' if mid == current_model else ''}{m.get('name', mid)}"
        options.append({"text": {"tag": "plain_text", "content": label}, "value": mid})

    current_hint = f"当前：`{current_model}`" if current_model else "当前：默认模型"
    elements: list[dict] = [
        {"tag": "markdown", "content": current_hint},
        {
            "tag": "select_static",
            "placeholder": {"tag": "plain_text", "content": "选择模型..."},
            "initial_option": current_model or (models[0]["id"] if models else ""),
            "options": options,
            "value": {"action": "switch_model", "token": token},
            "confirm": {
                "title": {"tag": "plain_text", "content": "确认切换模型"},
                "text": {"tag": "plain_text", "content": "切换后下条消息将使用所选模型。"},
            },
        },
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"切换模型（共 {len(models)} 个可用）"},
            "template": "purple",
        },
        "body": {"elements": elements},
    }


def build_model_switched_card(model_id: str) -> dict:
    """模型切换成功后的确认卡片。"""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "✅ 已切换模型"},
            "template": "green",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"已切换到：`{model_id}`\n\n下条消息将使用此模型。"},
            ]
        },
    }
