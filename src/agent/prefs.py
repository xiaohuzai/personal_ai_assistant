"""
用户偏好持久化

每个用户（open_id）的个人设置：
  rich_mode        — 是否展示思考/工具折叠面板（默认 False）
  reply_in_thread  — 群聊是否以「话题」回复（默认 False）
  max_turns        — Agent 最大工具调用轮数（默认 20）
  effort           — 思考深度 low/medium/high/xhigh/max（默认 high）

存储：内存 dict + {WORKSPACE}/.user_prefs.json 持久化（启动时加载）
"""
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

_PREFS_FILE: str = ""
_prefs: dict[str, dict] = {}
_lock = threading.Lock()

_DEFAULTS = {
    "rich_mode": False,
    "reply_in_thread": False,
    "max_turns": 20,
    "effort": "high",
}


def init(workspace: str) -> None:
    """由 assistant.initialize() 调用一次，设置持久化路径并加载已有数据。"""
    global _PREFS_FILE
    _PREFS_FILE = os.path.join(workspace, ".user_prefs.json")
    _load()


def _load() -> None:
    if not _PREFS_FILE or not os.path.exists(_PREFS_FILE):
        return
    try:
        with open(_PREFS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _lock:
                _prefs.update(data)
            logger.info("用户偏好已加载: %d 个用户", len(_prefs))
    except Exception as e:
        logger.warning("加载用户偏好失败: %s", e)


def _save() -> None:
    if not _PREFS_FILE:
        return
    try:
        os.makedirs(os.path.dirname(_PREFS_FILE), exist_ok=True)
        with open(_PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(_prefs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("保存用户偏好失败: %s", e)


def get(open_id: str, key: str):
    """获取用户偏好，返回默认值若未设置。"""
    with _lock:
        return _prefs.get(open_id, {}).get(key, _DEFAULTS.get(key))


def set(open_id: str, key: str, value) -> None:
    """设置用户偏好并持久化。"""
    with _lock:
        if open_id not in _prefs:
            _prefs[open_id] = {}
        _prefs[open_id][key] = value
    _save()


def get_rich_mode(open_id: str) -> bool:
    return bool(get(open_id, "rich_mode"))


def set_rich_mode(open_id: str, value: bool) -> None:
    set(open_id, "rich_mode", value)


def get_reply_in_thread(open_id: str) -> bool:
    return bool(get(open_id, "reply_in_thread"))


def set_reply_in_thread(open_id: str, value: bool) -> None:
    set(open_id, "reply_in_thread", value)


def get_max_turns(open_id: str) -> int:
    return int(get(open_id, "max_turns"))


def set_max_turns(open_id: str, value: int) -> None:
    set(open_id, "max_turns", int(value))


def get_effort(open_id: str) -> str:
    return str(get(open_id, "effort"))


def set_effort(open_id: str, value: str) -> None:
    set(open_id, "effort", value)
