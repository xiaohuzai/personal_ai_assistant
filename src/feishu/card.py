"""
飞书消息卡片构建工具

所有卡片使用 schema 2.0，支持完整 GFM（表格、代码块等）。
"""
import json
import time
from datetime import datetime
from typing import Optional


def build_thinking_card(text: str, stop_token: str) -> dict:
    """构建思考中/执行中卡片，含进度文本和 ⏹ 停止按钮（danger 样式）。"""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": text},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "⏹ 停止"},
                    "type": "danger",
                    "value": {"action": "stop", "token": stop_token},
                },
            ],
        },
    }


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


_MAX_THINKING = 300


def _build_event_panels(events: list[dict]) -> list[dict]:
    """将 thinking/tool_use/tool_result 事件列表渲染为折叠面板列表。"""
    def _panel(title: str, content: str) -> dict:
        return {
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "icon": {"tag": "standard_icon", "token": "right-small_outlined"},
                "icon_position": "follow_text",
                "icon_expanded_angle": -180,
            },
            "elements": [{"tag": "markdown", "content": content}],
        }

    elements: list[dict] = []
    i = 0
    while i < len(events):
        ev = events[i]
        if ev["type"] == "thinking":
            text = ev.get("thinking", "")
            if len(text) > _MAX_THINKING:
                text = text[:_MAX_THINKING] + "\n…（已截断）"
            elements.append(_panel("🤔 思考过程", f"```\n{text}\n```"))
            i += 1
        elif ev["type"] == "tool_use":
            name = ev.get("name", "未知工具")
            input_str = json.dumps(ev.get("input", {}), ensure_ascii=False, indent=2)
            content = f"**输入**\n```json\n{input_str}\n```"
            next_ev = events[i + 1] if i + 1 < len(events) else None
            if next_ev and next_ev["type"] == "tool_result":
                is_err = next_ev.get("is_error", False)
                content += f"\n\n**结果：** {'❌ 失败' if is_err else '✅ 成功'}"
                i += 2
            else:
                i += 1
            elements.append(_panel(f"⚙️ {name}", content))
        elif ev["type"] == "tool_result":
            i += 1  # 孤立 result，跳过
        else:
            i += 1
    return elements


def build_progress_card(
    events_done: list[dict],
    current_label: str,
    stop_token: str,
    current_text: str = "",
    current_thinking: str = "",
) -> dict:
    """构建流式进行中的进度卡片。

    已完成的 events 渲染为折叠面板；current_thinking 展示最新思考内容（不折叠）；
    current_text 显示已累积正文；current_label 显示当前状态行；底部保留停止按钮。
    """
    events_for_panels = events_done
    # 如果最后一个事件是正在进行的 thinking，且有 current_thinking，则不折叠最后 thinking
    if events_for_panels and events_for_panels[-1].get("type") == "thinking" and current_thinking:
        events_for_panels = events_for_panels[:-1]
    elements = _build_event_panels(events_for_panels)

    if current_thinking:
        if elements:
            elements.append({"tag": "hr"})
        display = current_thinking[:300] + "..." if len(current_thinking) > 300 else current_thinking
        elements.append({"tag": "markdown", "content": f"> 💭 {display}"})

    if current_text:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": current_text})

    elements.append({"tag": "markdown", "content": current_label})
    elements.append({
        "tag": "button",
        "text": {"tag": "plain_text", "content": "⏹ 停止"},
        "type": "danger",
        "value": {"action": "stop", "token": stop_token},
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }


def build_rich_reply_card(reply: str, events: list[dict]) -> dict:
    """构建带折叠面板的回复卡片。折叠面板默认收起，正文始终展示。"""
    elements = _build_event_panels(events)
    if elements:
        elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": reply})
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "😘 搞定啦~"},
        },
        "body": {"elements": elements},
    }


def build_choice_card(text: str, question: str, choices: list, token: str,
                      events: Optional[list[dict]] = None) -> dict:
    """构建带按钮的交互卡片，用于用户确认/选择场景。
    events 非空时在顶部渲染折叠面板（思考过程/工具调用）。
    """
    elements = _build_event_panels(events) if events else []
    if elements:
        elements.append({"tag": "hr"})
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
        "| `/save <名称>` | 给当前会话保存别名 |\n"
        "| `/s <名称> <消息>` | 在指定别名会话内发送消息（不切换当前会话） |\n"
        "| `/models` | 查看可用模型并切换 |\n"
        "| `/rich` | 切换富文本展示模式（折叠面板开/关） |\n"
        "| `/thread [on/off]` | 切换群聊话题回复模式（不传参数则取反） |\n"
        "| `/turns [N]` | 查看或设置 max_turns（默认 20，范围 1–200） |\n"
        "| `/effort [level]` | 查看或设置 effort（low/medium/high/xhigh/max） |\n"
        "| `/shell <命令>` | 直接执行 shell 命令并返回输出 |\n\n"
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
        name_tag = f"[{s['name']}] " if s.get("name") else ""
        label = f"{'✅ ' if is_current else ''}{name_tag}{sid[:8]} · {time_str} — {preview}"
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
        name_tag = f"**[{session['name']}]** " if session.get("name") else ""
        detail = f"{name_tag}`{switched_to_id[:8]}` · {time_str}\n> {preview}"
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
