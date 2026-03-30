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
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageRequest,
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

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

    def reply_card_to_message(self, origin_message_id: str, card: dict) -> Optional[str]:
        """回复指定消息（群聊场景），返回回复消息的 message_id；失败返回 None。"""
        request = (
            ReplyMessageRequest.builder()
            .message_id(origin_message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
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

    def recall_message(self, message_id: str) -> bool:
        """撤回指定消息（发送后 24 小时内有效）。"""
        request = DeleteMessageRequest.builder().message_id(message_id).build()
        response = self._client.im.v1.message.delete(request)
        if response.success():
            logger.info("消息撤回成功: message_id=%s", message_id)
            return True
        logger.error(
            "消息撤回失败: message_id=%s, code=%s, msg=%s",
            message_id, response.code, response.msg,
        )
        return False
