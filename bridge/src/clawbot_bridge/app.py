"""网关与可选 ClawBot 运行时之间的 HTTP 契约。"""

from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


class BridgeRuntime(Protocol):
    async def restore_all(self) -> list[str]: ...

    async def start(self, instance_id: str) -> dict: ...

    async def stop(self, instance_id: str) -> None: ...

    async def send(
        self,
        instance_id: str,
        conversation_id: str,
        text: str,
        metadata: dict,
    ) -> str: ...


class OutboundMessage(BaseModel):
    conversation_id: str = Field(alias="conversationId")
    text: str = ""
    media: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


def create_app(*, runtime: BridgeRuntime, bridge_token: str) -> FastAPI:
    if not bridge_token:
        raise ValueError("bridge_token 不能为空")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime.restore_all()
        yield

    app = FastAPI(
        title="Multi-Channel ClawBot Bridge",
        version="0.1.0",
        lifespan=lifespan,
    )

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {bridge_token}":
            raise HTTPException(status_code=401, detail="invalid bridge token")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/instances/{instance_id}/start", dependencies=[Depends(authorize)])
    async def start_instance(instance_id: str) -> dict:
        return await runtime.start(instance_id)

    @app.post("/instances/{instance_id}/stop", dependencies=[Depends(authorize)])
    async def stop_instance(instance_id: str) -> dict:
        await runtime.stop(instance_id)
        return {"ok": True}

    @app.post("/instances/{instance_id}/messages", dependencies=[Depends(authorize)])
    async def send_message(instance_id: str, message: OutboundMessage) -> dict:
        if message.media:
            raise HTTPException(status_code=501, detail="media is not supported yet")
        message_id = await runtime.send(
            instance_id,
            message.conversation_id,
            message.text,
            message.metadata,
        )
        return {"messageId": message_id}

    return app
