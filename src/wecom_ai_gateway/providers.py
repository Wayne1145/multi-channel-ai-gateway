from dataclasses import dataclass

import httpx

from .config import settings


@dataclass
class CompletionResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class OpenAICompatibleProvider:
    """OpenAI 兼容供应商。base_url/api_key 可覆盖（用户 BYOK 时注入解密后的值）。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._base_url = (base_url or settings.openai_compatible_base_url).rstrip("/")
        self._api_key = api_key if api_key is not None else settings.openai_compatible_api_key

    async def complete(
        self, messages: list[dict], model: str, temperature: float, max_tokens: int
    ) -> CompletionResult:
        if not self._api_key:
            raise RuntimeError("未配置模型 API Key")
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            r = await client.post(
                self._base_url + "/chat/completions",
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


def provider_for(name: str, base_url: str | None = None, api_key: str | None = None):
    if name == "openai-compatible":
        return OpenAICompatibleProvider(base_url=base_url, api_key=api_key)
    raise ValueError(f"不支持的供应商：{name}")
