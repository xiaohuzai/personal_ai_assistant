"""
飞书 IM 客户端（精简版）
仅保留 Personal Agent Bot 所需的消息相关 API。
"""
import base64
import json
import logging
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.contact.v3 import GetUserRequest
from lark_oapi.api.im.v1 import (
    CreateChatMembersRequest,
    CreateChatMembersRequestBody,
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageRequest,
    GetMessageRequest,
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.api.im.v1.model import Emoji

logger = logging.getLogger(__name__)


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str):
        self._client = (
            lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        )

    # ------------------------------------------------------------------ 用户

    def get_user_by_open_id(self, open_id: str) -> Optional[dict]:
        """通过 open_id 查询用户信息（含 email、name）。失败返回 None。"""
        request = (
            GetUserRequest.builder()
            .user_id(open_id)
            .user_id_type("open_id")
            .build()
        )
        response = self._client.contact.v3.user.get(request)
        if not response.success():
            logger.error(
                "查询用户信息失败: open_id=%s, code=%s, msg=%s",
                open_id, response.code, response.msg,
            )
            return None
        user = response.data.user
        return {
            "open_id": open_id,
            "name": getattr(user, "name", ""),
            "email": getattr(user, "email", "") or getattr(user, "enterprise_email", ""),
        }

    def add_user_to_chat(self, chat_id: str, open_id: str) -> bool:
        """将用户加入指定群（幂等，已在群里时也返回 True）。失败返回 False。"""
        body = (
            CreateChatMembersRequestBody.builder()
            .id_list([open_id])
            .build()
        )
        request = (
            CreateChatMembersRequest.builder()
            .chat_id(chat_id)
            .member_id_type("open_id")
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.chat_members.create(request)
        if not response.success():
            logger.warning(
                "加群失败: chat_id=%s open_id=%s code=%s msg=%s",
                chat_id, open_id, response.code, response.msg,
            )
            return False
        return True

    # ------------------------------------------------------------------ 图片 / 文件

    @staticmethod
    def _detect_media_type(data: bytes) -> str:
        """从字节流检测图片 MIME 类型，默认 image/jpeg。"""
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return "image/png"
        if data[:3] == b'\xff\xd8\xff':
            return "image/jpeg"
        if data[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return "image/webp"
        return "image/jpeg"

    def download_image_b64(self, message_id: str, image_key: str) -> Optional[dict]:
        """下载飞书消息图片，返回 {"media_type": "...", "data": "<base64>"}；失败返回 None。"""
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(image_key)
            .type("image")
            .build()
        )
        response = self._client.im.v1.message_resource.get(request)
        if not response.success():
            logger.error(
                "下载图片失败: image_key=%s, code=%s, msg=%s",
                image_key, response.code, response.msg,
            )
            return None
        raw = response.file.read()
        return {
            "media_type": self._detect_media_type(raw),
            "data": base64.b64encode(raw).decode(),
        }

    def download_file(self, message_id: str, file_key: str) -> Optional[bytes]:
        """下载飞书消息文件，返回原始字节；失败返回 None。"""
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type("file")
            .build()
        )
        response = self._client.im.v1.message_resource.get(request)
        if not response.success():
            logger.error(
                "下载文件失败: file_key=%s, code=%s, msg=%s",
                file_key, response.code, response.msg,
            )
            return None
        return response.file.read()

    def get_message(self, message_id: str) -> Optional[dict]:
        """获取指定消息内容。返回 {msg_type, content, message_id}；失败返回 None。"""
        request = GetMessageRequest.builder().message_id(message_id).build()
        # card_msg_content_type=user_card_content：获取 schema 2.0 卡片的原始 JSON
        # 不加此参数时，飞书对 schema 2.0 卡片返回降级的图片格式，内容为空
        request.add_query("card_msg_content_type", "user_card_content")
        response = self._client.im.v1.message.get(request)
        if not response.success():
            logger.error(
                "获取消息失败: message_id=%s, code=%s, msg=%s",
                message_id, response.code, response.msg,
            )
            return None
        items = getattr(response.data, "items", None)
        if not items:
            return None
        msg = items[0]
        body = getattr(msg, "body", None)
        return {
            "msg_type": getattr(msg, "msg_type", ""),
            "content": getattr(body, "content", "{}") if body else "{}",
            "message_id": getattr(msg, "message_id", message_id),
        }

    def resolve_quoted_content(self, message_id: str) -> dict:
        """解析被引用消息的内容。返回 {text, image_keys, file_tuples, quoted_msg_id}。"""
        empty: dict = {"text": "", "image_keys": [], "file_tuples": [], "quoted_msg_id": message_id}
        msg = self.get_message(message_id)
        if not msg:
            return empty
        msg_type = msg["msg_type"]
        quoted_msg_id = msg["message_id"]
        logger.info("resolve_quoted_content: message_id=%s msg_type=%s", message_id, msg_type)
        try:
            content = json.loads(msg["content"])
        except (json.JSONDecodeError, TypeError):
            return {**empty, "quoted_msg_id": quoted_msg_id}

        if msg_type == "text":
            return {"text": content.get("text", ""), "image_keys": [], "file_tuples": [], "quoted_msg_id": quoted_msg_id}
        elif msg_type == "post":
            text, image_keys = self._parse_post_content(content)
            return {"text": text, "image_keys": image_keys, "file_tuples": [], "quoted_msg_id": quoted_msg_id}
        elif msg_type == "image":
            key = content.get("image_key", "")
            return {"text": "", "image_keys": [key] if key else [], "file_tuples": [], "quoted_msg_id": quoted_msg_id}
        elif msg_type == "file":
            file_key = content.get("file_key", "")
            file_name = content.get("file_name", "unnamed_file")
            return {"text": "", "image_keys": [], "file_tuples": [(file_key, file_name)] if file_key else [], "quoted_msg_id": quoted_msg_id}
        elif msg_type == "interactive":
            text = self._parse_interactive_card_content(content)
            image_keys = self._extract_interactive_fallback_images(content)
            return {"text": text, "image_keys": image_keys, "file_tuples": [], "quoted_msg_id": quoted_msg_id}
        else:
            return {**empty, "quoted_msg_id": quoted_msg_id}

    @staticmethod
    def _parse_interactive_card_content(content: dict) -> str:
        """从 interactive 消息卡片中提取可读文本。支持 schema 2.0 和旧版卡片格式。"""
        if "card" in content and isinstance(content["card"], dict):
            content = content["card"]

        parts: list[str] = []

        header = content.get("header", {})
        title_obj = header.get("title", {}) if isinstance(header, dict) else {}
        if isinstance(title_obj, dict):
            if t := title_obj.get("content", "").strip():
                parts.append(f"[卡片标题: {t}]")

        # schema 2.0: body.elements
        body = content.get("body", {})
        if isinstance(body, dict):
            FeishuClient._extract_elements_text(body.get("elements", []), parts)

        # 旧格式: 顶层 elements
        if top := content.get("elements", []):
            FeishuClient._extract_elements_text(top, parts)

        return "\n".join(parts).strip()

    @staticmethod
    def _extract_elements_text(elements: list, parts: list[str]) -> None:
        """递归提取 elements 中的文本内容。折叠面板只取标题，跳过内部细节。"""
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            tag = elem.get("tag", "")
            if tag == "markdown":
                if c := elem.get("content", "").strip():
                    parts.append(c)
            elif tag in ("plain_text", "lark_md"):
                if c := elem.get("content", "").strip():
                    parts.append(c)
            elif tag == "div":
                text_obj = elem.get("text", {})
                if isinstance(text_obj, dict):
                    if c := text_obj.get("content", "").strip():
                        parts.append(c)
                for field in elem.get("fields", []):
                    t = field.get("text", {}) if isinstance(field, dict) else {}
                    if isinstance(t, dict):
                        if c := t.get("content", "").strip():
                            parts.append(c)
            elif tag == "collapsible_panel":
                ph = elem.get("header", {})
                pt = ph.get("title", {}) if isinstance(ph, dict) else {}
                if isinstance(pt, dict):
                    if c := pt.get("content", "").strip():
                        parts.append(f"[折叠: {c}]")

    @staticmethod
    def _extract_interactive_fallback_images(content: dict) -> list[str]:
        """提取飞书对 schema 2.0 卡片的降级格式中的图片 key（兜底方案）。
        降级格式：{"title": null, "elements": [[{"tag": "img", "image_key": "..."}]]}
        """
        actual = content.get("card", content) if "card" in content else content
        if "body" in actual or "schema" in actual:
            return []
        image_keys: list[str] = []
        for paragraph in actual.get("elements", []):
            if isinstance(paragraph, list):
                for elem in paragraph:
                    if isinstance(elem, dict) and elem.get("tag") == "img":
                        if key := elem.get("image_key", ""):
                            image_keys.append(key)
        return image_keys

    @staticmethod
    def _parse_post_content(content: dict) -> tuple[str, list[str]]:
        """从 post 类型消息中提取纯文本和图片 key 列表。"""
        lang_content = (
            content.get("zh_cn") or content.get("en_us")
            or (content if "content" in content else {})
        )
        parts: list[str] = []
        image_keys: list[str] = []
        if not lang_content:
            return "", []
        title = lang_content.get("title", "")
        if title:
            parts.append(title)
        for paragraph in lang_content.get("content", []):
            line_parts: list[str] = []
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

    # ------------------------------------------------------------------ 消息

    def send_message_to_open_id(self, open_id: str, text: str) -> bool:
        """向 open_id 发送纯文本消息（降级兜底用）。"""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if response.success():
            return True
        logger.error(
            "文本消息发送失败: open_id=%s, code=%s, msg=%s",
            open_id, response.code, response.msg,
        )
        return False

    def send_card_to_open_id(self, open_id: str, card: dict) -> Optional[str]:
        """向 open_id 发送消息卡片，返回 message_id；失败返回 None。"""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if response.success():
            message_id = response.data.message_id
            logger.info("卡片发送成功: open_id=%s, message_id=%s", open_id, message_id)
            return message_id
        logger.error(
            "卡片发送失败: open_id=%s, code=%s, msg=%s",
            open_id, response.code, response.msg,
        )
        return None

    def reply_card_to_message(self, origin_message_id: str, card: dict, in_thread: bool = False) -> Optional[str]:
        """回复指定消息（群聊场景），返回回复消息的 message_id；失败返回 None。"""
        body_builder = (
            ReplyMessageRequestBody.builder()
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
        )
        if in_thread:
            body_builder = body_builder.reply_in_thread(True)
        request = (
            ReplyMessageRequest.builder()
            .message_id(origin_message_id)
            .request_body(body_builder.build())
            .build()
        )
        response = self._client.im.v1.message.reply(request)
        if response.success():
            reply_msg_id = response.data.message_id
            logger.info("卡片回复成功: origin=%s, reply=%s", origin_message_id, reply_msg_id)
            return reply_msg_id
        logger.error(
            "卡片回复失败: message_id=%s, code=%s, msg=%s",
            origin_message_id, response.code, response.msg,
        )
        return None

    def add_reaction(self, message_id: str, emoji_type: str = "OneSecond") -> bool:
        """给指定消息添加表情回复。"""
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message_reaction.create(request)
        if response.success():
            return True
        logger.warning("添加表情回复失败: message_id=%s, code=%s, msg=%s",
                       message_id, response.code, response.msg)
        return False

    def upload_text_as_file(self, content: str, file_name: str) -> Optional[str]:
        """将文本内容上传为飞书文件，返回 file_key；失败返回 None。"""
        import io
        raw = content.encode("utf-8")
        request = (
            CreateFileRequest.builder()
            .request_body(
                CreateFileRequestBody.builder()
                .file_type("stream")
                .file_name(file_name)
                .file(io.BytesIO(raw))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.file.create(request)
        if response.success():
            return response.data.file_key
        logger.error("文件上传失败: code=%s, msg=%s", response.code, response.msg)
        return None

    def send_file_to_open_id(self, open_id: str, file_key: str) -> bool:
        """向 open_id 发送文件消息。"""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("file")
                .content(json.dumps({"file_key": file_key}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if response.success():
            return True
        logger.error("文件消息发送失败: open_id=%s, code=%s, msg=%s", open_id, response.code, response.msg)
        return False

    def reply_file_to_message(self, message_id: str, file_key: str, in_thread: bool = False) -> bool:
        """回复文件消息到指定消息。"""
        body_builder = (
            ReplyMessageRequestBody.builder()
            .msg_type("file")
            .content(json.dumps({"file_key": file_key}, ensure_ascii=False))
        )
        if in_thread:
            body_builder = body_builder.reply_in_thread(True)
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(body_builder.build())
            .build()
        )
        response = self._client.im.v1.message.reply(request)
        if response.success():
            return True
        logger.error("文件回复失败: message_id=%s, code=%s, msg=%s", message_id, response.code, response.msg)
        return False

    def recall_message(self, message_id: str) -> bool:
        """撤回指定消息（Bot 只能撤回自己发的消息，发送后 24 小时内有效）。"""
        request = DeleteMessageRequest.builder().message_id(message_id).build()
        response = self._client.im.v1.message.delete(request)
        if response.success():
            logger.info("消息撤回成功: message_id=%s", message_id)
            return True
        logger.error("消息撤回失败: message_id=%s, code=%s, msg=%s",
                     message_id, response.code, response.msg)
        return False

    def update_card(self, message_id: str, card: dict) -> bool:
        """更新已发送的消息卡片内容。"""
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.patch(request)
        if response.success():
            return True
        logger.error(
            "更新卡片失败: message_id=%s, code=%s, msg=%s",
            message_id, response.code, response.msg,
        )
        return False
