"""
会话状态管理

在内存中维护 open_id → session_id 的映射，并持久化到 {WORKSPACE}/.sessions.json，
以便重启后自动恢复上次的会话。
"""
import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# open_id → 当前活跃 session_id（内存缓存）
_active_sessions: dict[str, str] = {}
_sessions_loaded: bool = False
_sessions_lock = threading.Lock()


def _sessions_path() -> str:
    workspace = os.environ.get("ASSISTANT_CWD", os.path.join(os.getcwd(), "workspace"))
    return os.path.join(workspace, ".sessions.json")


def _load_sessions() -> None:
    """从磁盘加载持久化的 session 映射（仅首次调用时执行）。"""
    global _sessions_loaded
    path = _sessions_path()
    if not os.path.exists(path):
        _sessions_loaded = True
        return
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            _active_sessions.update({k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)})
            logger.info("Loaded %d sessions from %s", len(_active_sessions), path)
    except Exception as e:
        logger.warning("Failed to load sessions from %s: %s", path, e)
    _sessions_loaded = True


def _save_sessions() -> None:
    """将当前 session 映射持久化到磁盘。"""
    path = _sessions_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(_active_sessions, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to save sessions to %s: %s", path, e)


def get_session(open_id: str) -> Optional[str]:
    with _sessions_lock:
        if not _sessions_loaded:
            _load_sessions()
        return _active_sessions.get(open_id)


def set_session(open_id: str, session_id: str) -> None:
    with _sessions_lock:
        if not _sessions_loaded:
            _load_sessions()
        _active_sessions[open_id] = session_id
        _save_sessions()
    logger.debug("session updated: open_id=%s session_id=%s", open_id, session_id)


def clear_session(open_id: str) -> None:
    with _sessions_lock:
        if not _sessions_loaded:
            _load_sessions()
        _active_sessions.pop(open_id, None)
        _save_sessions()
    logger.info("session cleared: open_id=%s", open_id)


def _sessions_dir(cwd: str) -> str:
    """返回 Claude Code CLI 实际存储 session JSONL 的目录。
    SDK 使用系统 HOME，路径为 ~/.claude/projects/{cwd_slug}/
    CLI 的 slug 规则：将路径中所有非字母数字字符替换为 '-'
    """
    import re
    project_slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", project_slug)


def session_exists(cwd: str, session_id: str) -> bool:
    """检查 session JSONL 文件是否存在于磁盘。"""
    path = os.path.join(_sessions_dir(cwd), f"{session_id}.jsonl")
    return os.path.isfile(path)


def _extract_session_preview(fpath: str) -> str:
    """从 session JSONL 文件中提取最后一条 assistant 文字消息（最多 120 字）。"""
    try:
        with open(fpath, "rb") as f:
            lines = f.read().decode("utf-8", errors="ignore").strip().split("\n")
        for line in reversed(lines):
            try:
                obj = json.loads(line)
                if obj.get("type") != "assistant":
                    continue
                for block in obj.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block["text"].strip()
                        if text:
                            return text[:120] + ("..." if len(text) > 120 else "")
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return "（无内容）"


def list_sessions(cwd: str) -> list[dict]:
    """
    扫描 JSONL 文件，返回所有历史 session 列表，按最后修改时间倒序。
    每项格式：{"session_id": str, "updated_at": int, "preview": str}
    """
    sessions_dir = _sessions_dir(cwd)
    results: list[dict] = []
    if not os.path.isdir(sessions_dir):
        return results
    for fname in os.listdir(sessions_dir):
        if not fname.endswith(".jsonl"):
            continue
        session_id = fname[:-6]
        fpath = os.path.join(sessions_dir, fname)
        try:
            mtime = int(os.path.getmtime(fpath))
        except OSError:
            mtime = 0
        preview = _extract_session_preview(fpath)
        results.append({"session_id": session_id, "updated_at": mtime, "preview": preview})
    results.sort(key=lambda x: x["updated_at"], reverse=True)
    return results
