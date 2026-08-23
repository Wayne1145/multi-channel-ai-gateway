"""受限工具执行：供应商协议、白名单执行器与模型循环测试。"""

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx

from wecom_ai_gateway.models import (
    AuditLog,
    Conversation,
    Message,
    MessageStatus,
    UsageRecord,
    User,
    UserSettings,
)
from wecom_ai_gateway.providers import CompletionResult, OpenAICompatibleProvider, ToolCall
from wecom_ai_gateway.runtime_settings import update_settings
from wecom_ai_gateway.services import _complete_ai
from wecom_ai_gateway.tool_execution import (
    ToolValidationError,
    execute_tool,
    tool_definitions,
)


@respx.mock
@pytest.mark.anyio
async def test_provider_sends_tools_and_parses_structured_tool_calls():
    route = respx.post("https://model.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-time-1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_current_time",
                                        "arguments": '{"timezone":"Asia/Shanghai"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )
    )
    provider = OpenAICompatibleProvider(
        base_url="https://model.example/v1", api_key="test-key", timeout=10
    )

    result = await provider.complete(
        [{"role": "user", "content": "现在几点？"}],
        "test-model",
        0.2,
        256,
        tools=tool_definitions({"get_current_time"}),
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == "get_current_time"
    assert result.content == ""
    assert result.tool_calls == [
        ToolCall(
            id="call-time-1",
            name="get_current_time",
            arguments='{"timezone":"Asia/Shanghai"}',
        )
    ]
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 4


@respx.mock
@pytest.mark.anyio
async def test_plain_text_function_syntax_is_never_treated_as_a_tool_call():
    respx.post("https://model.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": 'get_current_time(timezone="Asia/Shanghai")',
                        }
                    }
                ]
            },
        )
    )
    result = await OpenAICompatibleProvider(
        base_url="https://model.example/v1", api_key="test-key"
    ).complete([], "test-model", 0.2, 256, tools=tool_definitions({"get_current_time"}))

    assert result.tool_calls == []
    assert result.content.startswith("get_current_time(")


def test_tool_definitions_only_expose_explicit_allowlist():
    definitions = tool_definitions({"get_current_time"})
    assert [item["function"]["name"] for item in definitions] == ["get_current_time"]
    with pytest.raises(ToolValidationError, match="不在白名单"):
        tool_definitions({"delete_file"})


@pytest.mark.anyio
async def test_time_tool_rejects_unknown_timezone_and_extra_arguments():
    with pytest.raises(ToolValidationError, match="时区"):
        await execute_tool("get_current_time", {"timezone": "Mars/Olympus"}, timeout=2)
    with pytest.raises(ToolValidationError, match="参数"):
        await execute_tool(
            "get_current_time",
            {"timezone": "Asia/Shanghai", "command": "whoami"},
            timeout=2,
        )


@respx.mock
@pytest.mark.anyio
async def test_weather_tool_uses_fixed_open_meteo_endpoints_and_bounded_result():
    geo = respx.get("https://geocoding-api.open-meteo.com/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "上海",
                        "country": "中国",
                        "admin1": "上海",
                        "latitude": 31.23,
                        "longitude": 121.47,
                        "timezone": "Asia/Shanghai",
                    }
                ]
            },
        )
    )
    forecast = respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(
            200,
            json={
                "timezone": "Asia/Shanghai",
                "current": {
                    "time": "2026-08-18T21:00",
                    "temperature_2m": 29.1,
                    "apparent_temperature": 32.0,
                    "weather_code": 2,
                    "wind_speed_10m": 8.4,
                },
                "daily": {
                    "time": ["2026-08-18", "2026-08-19"],
                    "weather_code": [2, 61],
                    "temperature_2m_max": [33.0, 31.0],
                    "temperature_2m_min": [26.0, 25.0],
                    "precipitation_probability_max": [20, 70],
                },
            },
        )
    )

    result = await execute_tool(
        "get_weather", {"location": "上海", "forecast_days": 2}, timeout=5
    )

    assert result["ok"] is True
    assert result["location"] == "上海, 上海, 中国"
    assert result["current"]["weather"] == "多云"
    assert len(result["forecast"]) == 2
    assert geo.calls[0].request.url.host == "geocoding-api.open-meteo.com"
    assert forecast.calls[0].request.url.host == "api.open-meteo.com"
    assert len(json.dumps(result, ensure_ascii=False)) < 4000


def _message_context(db):
    user = User()
    db.add(user)
    db.flush()
    user_settings = UserSettings(user_id=user.id)
    conversation = Conversation(user_id=user.id)
    db.add_all([user_settings, conversation])
    db.flush()
    row = Message(
        user_id=user.id,
        conversation_id=conversation.id,
        channel="wecom_kf",
        external_message_id="tool-loop-message",
        direction="inbound",
        message_type="text",
        content="上海天气如何？",
        status=MessageStatus.processing,
    )
    db.add(row)
    db.flush()
    return user_settings, conversation, row


@pytest.mark.anyio
async def test_complete_ai_executes_structured_tool_call_and_audits(db, monkeypatch):
    update_settings(
        db,
        {
            "tools_enabled": True,
            "tools_allowed": "get_current_time,get_weather",
            "tool_max_calls": 3,
            "tool_timeout_seconds": 5,
        },
    )
    user_settings, conversation, row = _message_context(db)
    first = CompletionResult(
        content="",
        tool_calls=[
            ToolCall(
                id="call-weather-1",
                name="get_weather",
                arguments='{"location":"上海","forecast_days":1}',
            )
        ],
        prompt_tokens=15,
        completion_tokens=6,
    )
    second = CompletionResult(
        content="上海今天多云，29℃。", prompt_tokens=30, completion_tokens=12
    )
    provider = AsyncMock()
    provider.complete.side_effect = [first, second]
    factory = Mock(return_value=provider)
    tool_runner = AsyncMock(return_value={"ok": True, "location": "上海", "temperature_c": 29})
    monkeypatch.setattr(
        "wecom_ai_gateway.services.resolve_provider",
        Mock(return_value=("openai-compatible", "https://model.example/v1", "key")),
    )
    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", factory)
    monkeypatch.setattr("wecom_ai_gateway.services.execute_tool", tool_runner)

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert answer == "上海今天多云，29℃。"
    assert provider.complete.await_count == 2
    first_call = provider.complete.await_args_list[0]
    assert first_call.kwargs["tools"][0]["function"]["name"] == "get_current_time"
    second_messages = provider.complete.await_args_list[1].args[0]
    assert second_messages[-2]["role"] == "assistant"
    assert second_messages[-2]["tool_calls"][0]["id"] == "call-weather-1"
    assert second_messages[-1]["role"] == "tool"
    assert second_messages[-1]["tool_call_id"] == "call-weather-1"
    assert tool_runner.await_args.args[0] == "get_weather"
    assert tool_runner.await_args.args[1] == {"location": "上海", "forecast_days": 1}
    usages = db.query(UsageRecord).order_by(UsageRecord.created_at).all()
    assert sum(item.prompt_tokens for item in usages) == 45
    assert sum(item.completion_tokens for item in usages) == 18
    audit = db.query(AuditLog).filter_by(action="tool.execute").one()
    assert audit.user_id == row.user_id
    assert audit.detail["tool"] == "get_weather"
    assert audit.detail["ok"] is True
    assert "上海" not in str(audit.detail)


@pytest.mark.anyio
async def test_tool_loop_stops_at_configured_limit(db, monkeypatch):
    update_settings(
        db,
        {
            "tools_enabled": True,
            "tools_allowed": "get_current_time",
            "tool_max_calls": 1,
            "tool_timeout_seconds": 5,
        },
    )
    user_settings, conversation, row = _message_context(db)
    provider = AsyncMock()
    provider.complete.side_effect = [
        CompletionResult(
            content="",
            tool_calls=[ToolCall(id="call-1", name="get_current_time", arguments="{}")],
        ),
        CompletionResult(
            content="",
            tool_calls=[ToolCall(id="call-2", name="get_current_time", arguments="{}")],
        ),
    ]
    monkeypatch.setattr(
        "wecom_ai_gateway.services.resolve_provider",
        Mock(return_value=("openai-compatible", "https://model.example/v1", "key")),
    )
    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", Mock(return_value=provider))
    monkeypatch.setattr(
        "wecom_ai_gateway.services.execute_tool",
        AsyncMock(return_value={"ok": True, "timezone": "Asia/Shanghai"}),
    )

    with pytest.raises(RuntimeError, match="调用次数上限"):
        await _complete_ai(db, row, conversation, user_settings)

    assert provider.complete.await_count == 2


@pytest.mark.anyio
async def test_tool_batch_over_limit_executes_nothing(db, monkeypatch):
    update_settings(
        db,
        {
            "tools_enabled": True,
            "tools_allowed": "get_current_time",
            "tool_max_calls": 1,
            "tool_timeout_seconds": 5,
        },
    )
    user_settings, conversation, row = _message_context(db)
    provider = AsyncMock()
    provider.complete.return_value = CompletionResult(
        content="",
        tool_calls=[
            ToolCall(id="call-1", name="get_current_time", arguments="{}"),
            ToolCall(id="call-2", name="get_current_time", arguments="{}"),
        ],
    )
    runner = AsyncMock()
    monkeypatch.setattr(
        "wecom_ai_gateway.services.resolve_provider",
        Mock(return_value=("openai-compatible", "https://model.example/v1", "key")),
    )
    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", Mock(return_value=provider))
    monkeypatch.setattr("wecom_ai_gateway.services.execute_tool", runner)

    with pytest.raises(RuntimeError, match="调用次数上限"):
        await _complete_ai(db, row, conversation, user_settings)

    runner.assert_not_awaited()
    assert db.query(UsageRecord).count() == 1


@pytest.mark.anyio
async def test_tool_failure_is_audited_and_only_generic_error_reaches_model(db, monkeypatch):
    update_settings(
        db,
        {
            "tools_enabled": True,
            "tools_allowed": "get_weather",
            "tool_max_calls": 2,
            "tool_timeout_seconds": 5,
        },
    )
    user_settings, conversation, row = _message_context(db)
    provider = AsyncMock()
    provider.complete.side_effect = [
        CompletionResult(
            content="",
            tool_calls=[
                ToolCall(
                    id="call-failed",
                    name="get_weather",
                    arguments='{"location":"秘密地点"}',
                )
            ],
        ),
        CompletionResult(content="暂时无法查询天气。"),
    ]
    runner = AsyncMock(side_effect=RuntimeError("upstream payload contains secret-location"))
    monkeypatch.setattr(
        "wecom_ai_gateway.services.resolve_provider",
        Mock(return_value=("openai-compatible", "https://model.example/v1", "key")),
    )
    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", Mock(return_value=provider))
    monkeypatch.setattr("wecom_ai_gateway.services.execute_tool", runner)

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert answer == "暂时无法查询天气。"
    tool_message = provider.complete.await_args_list[1].args[0][-1]
    assert tool_message["content"] == '{"ok": false, "error": "工具执行失败或参数无效"}'
    assert "secret-location" not in str(tool_message)
    audit = db.query(AuditLog).filter_by(action="tool.execute").one()
    assert audit.detail["ok"] is False
    assert audit.detail["error_type"] == "RuntimeError"
    assert "秘密地点" not in str(audit.detail)


def test_runtime_settings_reject_unknown_tool_names(db):
    errors = update_settings(db, {"tools_allowed": "get_weather,run_shell"})
    assert "tools_allowed" in errors


@respx.mock
@pytest.mark.anyio
async def test_web_search_tool_uses_fixed_bing_endpoint_and_parses_results():
    """web_search 固定走必应端点，返回前 5 条标题/链接/摘要。"""
    page = '''
<html><body>
<li class="b_algo"><h2><a href="https://example.com/alpha">Alpha 最新消息</a></h2>
<div class="b_caption"><p>Alpha 公司今天发布了新产品，主打 AI 芯片。</p></div></li>
<li class="b_algo"><h2><a href="https://example.com/beta">Beta 教程</a></h2>
<div class="b_caption"><p>Beta 入门教程，涵盖安装与配置。</p></div></li>
<li class="b_algo"><h2><a href="https://example.com/gamma">Gamma 分析</a></h2>
<div class="b_caption"><p>Gamma 市场分析报告。</p></div></li>
</body></html>
'''
    route = respx.get("https://www.bing.com/search").mock(
        return_value=httpx.Response(200, text=page)
    )

    result = await execute_tool("web_search", {"query": "alpha 最新消息"}, timeout=5)

    assert result["ok"] is True
    assert result["source"] == "Bing"
    assert len(result["results"]) == 3
    first = result["results"][0]
    assert first["title"] == "Alpha 最新消息"
    assert first["url"] == "https://example.com/alpha"
    assert "新产品" in first["snippet"]
    # 固定端点：查询参数只能带 q/mkt/ensearch，不允许自定义 URL
    assert route.calls.last.request.url.host == "www.bing.com"


@respx.mock
@pytest.mark.anyio
async def test_web_search_rejects_bad_arguments_and_handles_no_results():
    """参数校验：空查询/超长/多余参数都拒绝；无结果时返回空列表不报错。"""
    with pytest.raises(ToolValidationError, match="关键词"):
        await execute_tool("web_search", {"query": "   "}, timeout=5)
    with pytest.raises(ToolValidationError, match="参数"):
        await execute_tool(
            "web_search", {"query": "a", "url": "https://evil.example"}, timeout=5
        )
    with pytest.raises(ToolValidationError, match="200"):
        await execute_tool("web_search", {"query": "x" * 201}, timeout=5)

    respx.get("https://www.bing.com/search").mock(
        return_value=httpx.Response(200, text="<html><body>没有结果</body></html>")
    )
    result = await execute_tool("web_search", {"query": "不存在的东西"}, timeout=5)
    assert result["ok"] is True
    assert result["results"] == []


def test_web_search_available_in_allowlist_schema():
    """web_search 应出现在可用工具列表中，可被白名单选择。"""
    from wecom_ai_gateway.tool_execution import available_tool_names

    assert "web_search" in available_tool_names()
    definitions = tool_definitions({"web_search"})
    assert definitions[0]["function"]["name"] == "web_search"
