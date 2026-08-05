"""渠道抽象层。

适配器只负责渠道 I/O；用户隔离、会话、角色卡、模型调用和可靠投递仍由
服务层与 Outbox 统一处理。这样接入新渠道不会复制业务规则。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class ChannelMessage:
    """渠道无关的入站消息。instance_id 标识一个具体账号/应用实例。"""

    channel: str
    instance_id: str
    sender_id: str
    external_message_id: str
    message_type: str = "text"
    content: str | None = None
    media: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class OutgoingMessage:
    """渠道无关的出站消息。"""

    channel: str
    instance_id: str
    to_sender_id: str
    text: str
    media: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class ChannelAdapter(ABC):
    """渠道实例适配器协议。

    新渠道实现 start/stop/send；收到消息后转换为 ChannelMessage 并交给
    services.ingest_channel_message。适配器不得绕过服务层直接读取用户私密数据。
    """

    channel_key: str

    @abstractmethod
    async def start_instance(self, instance_id: str) -> None:
        """启动指定渠道实例。"""

    @abstractmethod
    async def stop_instance(self, instance_id: str) -> None:
        """停止指定渠道实例。"""

    @abstractmethod
    async def send(self, message: OutgoingMessage) -> str:
        """发送消息并返回渠道侧消息 ID。"""


class ChannelRegistry:
    """进程内适配器注册表；重复注册被拒绝，避免实例被静默覆盖。"""

    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        if not adapter.channel_key:
            raise ValueError("channel_key 不能为空")
        if adapter.channel_key in self._adapters:
            raise ValueError(f"渠道已注册：{adapter.channel_key}")
        self._adapters[adapter.channel_key] = adapter

    def get(self, channel_key: str) -> ChannelAdapter:
        try:
            return self._adapters[channel_key]
        except KeyError as exc:
            raise ValueError(f"未注册的渠道：{channel_key}") from exc

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


registry = ChannelRegistry()