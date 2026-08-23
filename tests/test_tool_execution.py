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
    """工具触顶后强制收尾：追加提示让模型基于已有信息作答，不再死循环重试。"""
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
        # 工具执行后模型又要求调用工具 → 触顶
        CompletionResult(
            content="",
            tool_calls=[ToolCall(id="call-2", name="get_current_time", arguments="{}")],
        ),
        # 强制收尾调用（tools=None）：模型直接给出答案
        CompletionResult(content="现在时间是 14:00", prompt_tokens=3, completion_tokens=2),
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

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert answer == "现在时间是 14:00"
    assert provider.complete.await_count == 3
    # 第三次（收尾）调用不再携带 tools，禁止继续调用工具
    assert provider.complete.await_args.kwargs.get("tools") is None
    # 收尾提示已追加到消息上下文
    assert "工具调用次数已达上限" in provider.complete.await_args.args[0][-1]["content"]


@pytest.mark.anyio
async def test_tool_limit_final_answer_empty_returns_friendly_text(db, monkeypatch):
    """收尾调用仍返回空时，给出友好提示而非把失败抛给重试。"""
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
        CompletionResult(content="", prompt_tokens=1, completion_tokens=0),
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

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert "搜索" in answer
    assert "换一种问法" in answer
    assert provider.complete.await_count == 3


@pytest.mark.anyio
async def test_tool_batch_over_limit_executes_nothing(db, monkeypatch):
    """一批工具调用超过上限时不执行任何工具，直接进入强制收尾。"""
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
            tool_calls=[
                ToolCall(id="call-1", name="get_current_time", arguments="{}"),
                ToolCall(id="call-2", name="get_current_time", arguments="{}"),
            ],
        ),
        CompletionResult(content="收尾回答", prompt_tokens=2, completion_tokens=1),
    ]
    runner = AsyncMock()
    monkeypatch.setattr(
        "wecom_ai_gateway.services.resolve_provider",
        Mock(return_value=("openai-compatible", "https://model.example/v1", "key")),
    )
    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", Mock(return_value=provider))
    monkeypatch.setattr("wecom_ai_gateway.services.execute_tool", runner)

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert answer == "收尾回答"
    # 批次超限：一个工具都不执行
    runner.assert_not_awaited()
    # 首次调用已产生用量；收尾调用也产生用量
    assert db.query(UsageRecord).count() == 2


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
async def test_web_search_daemon_unavailable_returns_friendly_note():
    """web_search 只走 open-websearch daemon；不可用时返回友好提示，不再回退必应。"""
    respx.post("http://open-websearch:3210/search").mock(
        return_value=httpx.Response(503, text="daemon down")
    )

    result = await execute_tool("web_search", {"query": "测试查询"}, timeout=10)

    assert result["ok"] is True
    assert result["results"] == []
    assert "不可用" in result.get("note", "")
    # 不得回退到必应（未 mock bing，若回退会因 respx 未 mock 而报错）
    assert "Bing" not in str(result)


@respx.mock
@pytest.mark.anyio
async def test_web_search_failure_log_does_not_leak_query(caplog):
    """搜索服务异常日志不得记录用户查询正文。"""
    private_query = "我的私人病历编号 123456"
    respx.post("http://open-websearch:3210/search").mock(
        return_value=httpx.Response(503, text="daemon down")
    )

    await execute_tool("web_search", {"query": private_query}, timeout=10)

    assert private_query not in caplog.text
    assert "candidate_index=1" in caplog.text


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

    respx.post("http://open-websearch:3210/search").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": {"results": []}})
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


def test_sensenova_usage_excluded_from_quota(db):
    """SenseNova 用量不计入每日配额，只有 DeepSeek 等计费线路计入。"""
    from datetime import UTC, datetime

    from wecom_ai_gateway.models import UsageRecord, User, UserSettings
    from wecom_ai_gateway.runtime_settings import update_settings
    from wecom_ai_gateway.services import quota_status

    update_settings(db, {"daily_quota_enabled": True, "user_daily_token_quota": 1000})
    user = User()
    db.add(user)
    db.flush()
    us = UserSettings(user_id=user.id, daily_token_quota=1000)
    db.add(us)
    now = datetime.now(UTC)
    # SenseNova 用量（不计入）
    db.add_all([
        UsageRecord(user_id=user.id, provider="Sensenova 主线路", model="sensenova-6.8-flash-lite",
                    prompt_tokens=600, completion_tokens=200, created_at=now),
        UsageRecord(user_id=user.id, provider="Sensenova 主线路", model="sensenova-6.8-flash-lite",
                    prompt_tokens=500, completion_tokens=100, created_at=now),
        # DeepSeek 用量（计入）
        UsageRecord(user_id=user.id, provider="openai-compatible(fallback)", model="deepseek-v4-flash-vision-exp",
                    prompt_tokens=400, completion_tokens=100, created_at=now),
    ])
    db.commit()

    status = quota_status(db, user.id, us)
    # 只有 DeepSeek 的 500 计入，SenseNova 的 1400 不计
    assert status["used"] == 500
    assert status["exceeded"] is False


@pytest.mark.anyio
async def test_tool_limit_on_primary_fails_over_to_fallback_model(db, monkeypatch):
    """主线路（SenseNova）工具触顶时切 DeepSeek 重新处理，DeepSeek 一次答完。"""
    from wecom_ai_gateway.config import settings as cfg

    monkeypatch.setattr(cfg, "fallback_model", "deepseek-v4-flash-vision-exp")
    monkeypatch.setattr(cfg, "fallback_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(cfg, "fallback_api_key", "fallback-key")
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
    sensenova = AsyncMock()
    sensenova.complete.side_effect = [
        CompletionResult(content="", tool_calls=[ToolCall(id="c1", name="get_current_time", arguments="{}")]),
        # SenseNova 执行工具后仍要调用 → 触顶
        CompletionResult(content="", tool_calls=[ToolCall(id="c2", name="get_current_time", arguments="{}")]),
    ]
    deepseek = AsyncMock()
    deepseek.complete.return_value = CompletionResult(
        content="DeepSeek 直接回答", prompt_tokens=5, completion_tokens=3
    )
    calls = []

    def fake_provider(name, base_url, api_key, timeout):
        calls.append((base_url, api_key))
        return sensenova if "sensenova" in (base_url or "") else deepseek

    monkeypatch.setattr("wecom_ai_gateway.services.resolve_provider",
                        Mock(return_value=("openai-compatible", "https://sensenova.example/v1", "sen-key")))
    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", fake_provider)
    monkeypatch.setattr("wecom_ai_gateway.services.execute_tool",
                        AsyncMock(return_value={"ok": True, "timezone": "Asia/Shanghai"}))

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert answer == "DeepSeek 直接回答"
    # 调了 SenseNova 两次（触顶）+ DeepSeek 一次
    assert len(calls) == 3
    assert calls[0][1] == "sen-key"
    assert calls[-1][1] == "fallback-key"
    # 审计记录 fallback 事件
    audit = db.query(AuditLog).filter(AuditLog.action == "model.fallback").all()
    assert audit


@pytest.mark.anyio
async def test_fallback_model_cannot_execute_tools_past_total_limit(db, monkeypatch):
    """主线路触顶后，备用线路仍请求工具时不得突破整条消息的总调用上限。"""
    from wecom_ai_gateway.config import settings as cfg

    monkeypatch.setattr(cfg, "fallback_model", "deepseek-v4-flash-vision-exp")
    monkeypatch.setattr(cfg, "fallback_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(cfg, "fallback_api_key", "fallback-key")
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
    sensenova = AsyncMock()
    sensenova.complete.side_effect = [
        CompletionResult(
            content="",
            tool_calls=[ToolCall(id="primary-1", name="get_current_time", arguments="{}")],
        ),
        CompletionResult(
            content="",
            tool_calls=[ToolCall(id="primary-2", name="get_current_time", arguments="{}")],
        ),
    ]
    deepseek = AsyncMock()
    deepseek.complete.side_effect = [
        CompletionResult(
            content="",
            tool_calls=[ToolCall(id="fallback-1", name="get_current_time", arguments="{}")],
        ),
        CompletionResult(content="根据已有结果直接回答", prompt_tokens=2, completion_tokens=1),
    ]

    def fake_provider(name, base_url, api_key, timeout):
        return sensenova if "sensenova" in (base_url or "") else deepseek

    tool_runner = AsyncMock(return_value={"ok": True, "timezone": "Asia/Shanghai"})
    monkeypatch.setattr(
        "wecom_ai_gateway.services.resolve_provider",
        Mock(return_value=("openai-compatible", "https://sensenova.example/v1", "sen-key")),
    )
    monkeypatch.setattr("wecom_ai_gateway.services.provider_for", fake_provider)
    monkeypatch.setattr("wecom_ai_gateway.services.execute_tool", tool_runner)

    answer = await _complete_ai(db, row, conversation, user_settings)

    assert answer == "根据已有结果直接回答"
    assert tool_runner.await_count == 1, "备用线路不得突破整条消息的工具调用总上限"
    assert deepseek.complete.await_count == 2
    assert deepseek.complete.await_args_list[-1].kwargs.get("tools") is None
    assert db.query(AuditLog).filter(AuditLog.action == "model.fallback").count() == 1


@respx.mock
@pytest.mark.anyio
async def test_web_search_uses_open_websearch_daemon_first():
    """web_search 优先调用本地 open-websearch daemon（startpage 聚合结果）。"""
    daemon_body = {
        "status": "ok",
        "data": {
            "query": "广州东方同人展",
            "engines": ["startpage"],
            "totalResults": 2,
            "results": [
                {
                    "title": "广州·东方同人only东方游剧天2026 - 漫展演出",
                    "url": "https://show.bilibili.com/platform/detail.html?id=1000420",
                    "description": "广东省广州市白云区西城智汇Park 路演中心。电子票，凭购票二维码验证入场。",
                    "source": "show.bilibili.com",
                    "engine": "startpage",
                },
                {
                    "title": "活动| 叮铃铃·东方市场: 同人一站式服务平台",
                    "url": "https://touhou.market/",
                    "description": "东方·即卖会·3000 人。申摊·购票。近期活动。",
                    "source": "touhou.market",
                    "engine": "startpage",
                },
            ],
            "partialFailures": [],
        },
        "error": None,
        "hint": None,
    }
    route = respx.post("http://open-websearch:3210/search").mock(
        return_value=httpx.Response(200, json=daemon_body)
    )

    result = await execute_tool("web_search", {"query": "广州东方同人展"}, timeout=10)

    assert result["ok"] is True
    assert result["source"] == "open-websearch"
    assert len(result["results"]) == 2
    first = result["results"][0]
    assert first["title"] == "广州·东方同人only东方游剧天2026 - 漫展演出"
    assert first["url"].startswith("https://")
    assert "白云区" in first["snippet"]
    # 请求体应包含 startpage 引擎
    sent = route.calls.last.request.content.decode()
    assert "startpage" in sent


@respx.mock
@pytest.mark.anyio
async def test_web_search_daemon_empty_results_falls_back():
    """daemon 返回空结果时返回友好提示，不再回退直连必应。"""
    respx.post("http://open-websearch:3210/search").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": {"results": []}})
    )

    result = await execute_tool("web_search", {"query": "空结果"}, timeout=10)

    assert result["ok"] is True
    assert result["results"] == []
    assert "不可用" in result.get("note", "") or "没有搜索到" in str(result)


def test_sanitize_search_query_removes_noise():
    """净化查询：去掉冗余年份与泛词，避免 startpage 精确匹配返回空。"""
    from wecom_ai_gateway.tool_execution import _sanitize_search_query

    assert _sanitize_search_query("广东 东方同人展 2025 2026 活动") == "广东 东方同人展"
    assert _sanitize_search_query("东方Project 广州 同人展 2025 2026") == "东方Project 广州 同人展"
    assert _sanitize_search_query("汕头 东方THO") == "汕头 东方THO"
    # 单 token（无空格）无法按词拆分，保留原样避免误删核心词
    assert _sanitize_search_query("今天有什么最新活动") == "今天有什么最新活动"
    assert _sanitize_search_query("python tutorial") == "python tutorial"


@respx.mock
@pytest.mark.anyio
async def test_web_search_daemon_retries_sanitized_query_on_empty():
    """daemon 首次空结果时用净化查询重试，成功则不再回退 Bing。"""
    # 用 respx 动态响应：第一次空，第二次有结果
    daemon_route = respx.post("http://open-websearch:3210/search")
    daemon_route.side_effect = [
        httpx.Response(200, json={"status": "ok", "data": {"results": []}}),
        httpx.Response(200, json={
            "status": "ok",
            "data": {"results": [{
                "title": "广州·东方同人only东方游剧天2026 - 漫展演出",
                "url": "https://show.bilibili.com/platform/detail.html?id=1000420",
                "description": "广东省广州市白云区西城智汇Park 路演中心。电子票，凭购票二维码验证入场。",
                "source": "show.bilibili.com",
                "engine": "startpage",
            }]},
        }),
    ]

    result = await execute_tool("web_search", {"query": "广东 东方同人展 2025 2026 活动"}, timeout=15)

    assert result["ok"] is True
    assert result["source"] == "open-websearch"
    assert len(result["results"]) == 1
    assert "东方同人only" in result["results"][0]["title"]
    # 两次请求都应发给 daemon（没有回退 Bing）
    assert len(daemon_route.calls) == 2
