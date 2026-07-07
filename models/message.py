"""Data models for messages and conversations."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any
from enum import Enum


class MessageType(Enum):
    """Message types."""
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    SYSTEM = "system"


class MessageStatus(Enum):
    """Message processing status."""
    PENDING = "pending"
    PROCESSING = "processing"
    REPLIED = "replied"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class User:
    """User information."""
    user_id: str
    nickname: str
    avatar: Optional[str] = None
    phone: Optional[str] = None


@dataclass
class Message:
    """IM Message model."""
    message_id: str
    conversation_id: str
    store_id: str
    user: User
    content: str
    message_type: MessageType = MessageType.TEXT
    timestamp: datetime = field(default_factory=datetime.now)
    status: MessageStatus = MessageStatus.PENDING
    reply_content: Optional[str] = None
    reply_timestamp: Optional[datetime] = None
    api_response_time_ms: Optional[int] = None
    error_message: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def to_api_payload(self) -> Dict[str, Any]:
        """Convert to API request payload."""
        return {
            "store_id": self.store_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user.user_id,
            "user_name": self.user.nickname,
            "message_id": self.message_id,
            "message": self.content,
            "timestamp": int(self.timestamp.timestamp()),
            "message_type": self.message_type.value,
        }

    @classmethod
    def from_kele_data(cls, data: Dict[str, Any], store_id: str) -> "Message":
        """Create Message from Douyin Kele data."""
        # Parse the raw data from Douyin Kele
        user_data = data.get("user", {})
        # Use user_id if available, otherwise fallback to user_unique_id or generate from nickname
        user_id = str(user_data.get("user_id", ""))
        if not user_id:
            user_id = str(user_data.get("user_unique_id", ""))
        if not user_id:
            # Fallback to nickname-based ID
            user_id = f"user_{user_data.get('nickname', 'unknown')}"

        user = User(
            user_id=user_id,
            nickname=user_data.get("nickname", ""),
            avatar=user_data.get("avatar"),
        )

        # Determine message type
        msg_type_str = data.get("type", "text")
        msg_type = MessageType.TEXT
        try:
            msg_type = MessageType(msg_type_str)
        except ValueError:
            pass

        # Parse timestamp
        timestamp_ms = data.get("timestamp", 0)
        timestamp = datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else datetime.now()

        return cls(
            message_id=str(data.get("message_id", "")),
            conversation_id=str(data.get("conversation_id", "")),
            store_id=store_id,
            user=user,
            content=data.get("content", ""),
            message_type=msg_type,
            timestamp=timestamp,
            raw_data=data,
        )


@dataclass
class Conversation:
    """Conversation session."""
    conversation_id: str
    store_id: str
    user: User
    messages: List[Message] = field(default_factory=list)
    last_message_at: Optional[datetime] = None
    unread_count: int = 0
    is_auto_reply_enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_message(self, message: Message) -> None:
        """Add a message to the conversation."""
        self.messages.append(message)
        self.last_message_at = message.timestamp
        if message.status == MessageStatus.PENDING:
            self.unread_count += 1

    def get_history(self, limit: int = 10) -> List[Dict[str, str]]:
        """Get conversation history for API context."""
        history = []
        for msg in self.messages[-limit:]:
            history.append({"role": "user", "content": msg.content})
            if msg.reply_content:
                history.append({"role": "assistant", "content": msg.reply_content})
        return history


@dataclass
class ReplyResponse:
    """API response for message reply."""
    code: int
    message: str
    reply: Optional[str] = None
    reply_type: str = "text"
    delay: float = 0
    end_conversation: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_response(cls, data: Dict[str, Any]) -> "ReplyResponse":
        """Create ReplyResponse from API response data.

        Supports formats:
        1. Standard format: {"code": 0, "message": "...", "data": {"reply": "..."}}
        2. API specific format: {"msg": "回复内容", "msg_type": 0, ...}
        3. Simple format: {"reply": "...", ...} or {"message": "...", ...}
        """
        # Check for API specific format (has acc_type or msg_type fields)
        if "acc_type" in data or "msg_type" in data:
            # This is the API specific format, reply is in 'msg' field
            reply_content = data.get("msg", "")
            # Only treat as valid reply if msg is not empty and not an error message
            if reply_content and reply_content.strip():
                # Check if it's an error message
                if any(err in str(reply_content) for err in ["繁忙", "错误", "失败", "未接收到数据"]):
                    return cls(
                        code=-1,
                        message=str(reply_content),
                        reply=None,
                    )
                return cls(
                    code=0,
                    message="success",
                    reply=str(reply_content),
                    reply_type="text",
                    metadata=data,
                )
            return cls(
                code=0,
                message="success but no reply",
                reply=None,
            )

        # Check for standard format
        if "code" in data:
            response_data = data.get("data", {})
            return cls(
                code=data.get("code", -1),
                message=data.get("message", ""),
                reply=response_data.get("reply"),
                reply_type=response_data.get("type", "text"),
                delay=response_data.get("delay", 0),
                end_conversation=response_data.get("end_conversation", False),
                metadata=response_data.get("metadata", {}),
            )

        # Handle simple format
        reply_content = data.get("reply") or data.get("msg") or data.get("message") or data.get("content")

        if reply_content:
            # Check if it's an error message
            if any(err in str(reply_content) for err in ["繁忙", "错误", "失败", "未接收到数据"]):
                return cls(
                    code=-1,
                    message=str(reply_content),
                    reply=None,
                )
            return cls(
                code=0,
                message="success",
                reply=str(reply_content),
                reply_type="text",
            )

        # Unknown format - no reply
        return cls(
            code=0,
            message="success but no reply",
            reply=None,
        )

    def is_success(self) -> bool:
        """Check if the API call was successful."""
        return self.code == 0 and self.reply is not None

    def should_reply(self) -> bool:
        """Check if should send a reply."""
        return self.is_success() and self.reply and self.reply.strip()