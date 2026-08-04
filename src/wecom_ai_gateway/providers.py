from dataclasses import dataclass

import httpx

from .config import settings


@dataclass
class CompletionResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class OpenAICompatibleProvider:
    async def complete(
        self, messages: list[dict], model: str, temperature: float, max_tokens: int
    ) -> CompletionResult:
        if not settings.openai_compatible_api_key:
            raise RuntimeError("平台尚未配置模型 API Key")
        headers = {"Authorization": f"Bearer {settings.openai_compatible_api_key}"}
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            r = await client.post(
                settings.openai_compatible_base_url.rstrip("/") + "/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        usage = data.get("usage") or {}
        content = data["choices"][0]["message"].get("content") or ""
        return CompletionResult(
            content=content,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )


def provider_for(name: str):
    if name == "openai-compatible":
        return OpenAICompatibleProvider()
    raise ValueError(f"不支持的供应商：{name}")
