from dataclasses import dataclass

import httpx

from .config import settings


@dataclass
class CompletionResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: list["ToolCall"] | None = None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


class RetryableProviderError(RuntimeError):
    """供应商已响应但没有可发送结果；允许模型组切换备用线路。"""


class OpenAICompatibleProvider:
    """OpenAI 兼容供应商。base_url/api_key 可覆盖（用户 BYOK 时注入解密后的值）。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ):
        self._base_url = (base_url or settings.openai_compatible_base_url).rstrip("/")
        self._api_key = api_key if api_key is not None else settings.openai_compatible_api_key
        self._timeout = timeout if timeout is not None else settings.request_timeout_seconds

    async def complete(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
        *,
        tools: list[dict] | None = None,
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
        # SenseNova 6.8 Flash-Lite 默认会进入长推理并把响应预算耗尽，返回空 content；
        # 显式关闭推理可稳定拿到直接文本输出，同时避免 thinking 预算吃满。
        # 仅对 SenseNova 系模型生效，不影响 DeepSeek 等供应商。
        if model and model.startswith("sensenova"):
            payload["reasoning_effort"] = "none"
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                self._base_url + "/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RetryableProviderError("模型响应缺少可用候选内容")
        message = choices[0].get("message") or {}
        content = message.get("content")
        parsed_tool_calls: list[ToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            if not isinstance(raw_call, dict) or raw_call.get("type") != "function":
                continue
            function = raw_call.get("function") or {}
            call_id = raw_call.get("id")
            name = function.get("name")
            arguments = function.get("arguments")
            if (
                all(isinstance(item, str) and item for item in (call_id, name, arguments))
                and len(call_id) <= 200
                and len(name) <= 80
                and len(arguments) <= 4000
            ):
                parsed_tool_calls.append(
                    ToolCall(id=call_id, name=name, arguments=arguments)
                )
        # 部分兼容供应商会在较长推理仍未产出最终文本时返回空 content。
        # 这不是可发送的客服回复：抛出可重试错误，让 Outbox 按退避策略等待，
        # 而不是把“暂时没有生成可发送的内容”发给用户。
        if (not isinstance(content, str) or not content.strip()) and not parsed_tool_calls:
            raise RetryableProviderError("模型尚未生成可发送的最终内容")
        return CompletionResult(
            content=content.strip() if isinstance(content, str) else "",
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            tool_calls=parsed_tool_calls,
        )


def provider_for(
    name: str,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
):
    if name == "openai-compatible":
        return OpenAICompatibleProvider(base_url=base_url, api_key=api_key, timeout=timeout)
    raise ValueError(f"不支持的供应商：{name}")
