"""受限只读工具注册表与执行器。

安全约束：仅暴露静态注册的只读工具；参数使用逐字段白名单；所有外部请求目标固定，
调用者不能提供 URL、请求头、文件路径或命令。
"""

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


class ToolValidationError(ValueError):
    """工具名或参数不符合白名单约束。"""


_TOOL_SCHEMAS: dict[str, dict] = {
    "get_current_time": {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "查询指定 IANA 时区的当前日期、时间和星期。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA 时区，例如 Asia/Shanghai；默认 Asia/Shanghai。",
                        "maxLength": 64,
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    "get_weather": {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询地点的当前天气和最多 7 天预报。数据来自 Open-Meteo，只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "城市或地区名称，例如 上海、Tokyo。",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "forecast_days": {
                        "type": "integer",
                        "description": "预报天数，1 到 7；默认 3。",
                        "minimum": 1,
                        "maximum": 7,
                    },
                },
                "required": ["location"],
                "additionalProperties": False,
            },
        },
    },
}


def tool_definitions(allowed: set[str]) -> list[dict]:
    """按稳定顺序返回显式白名单中的 OpenAI 工具定义。"""
    unknown = allowed - _TOOL_SCHEMAS.keys()
    if unknown:
        raise ToolValidationError(f"工具不在白名单：{','.join(sorted(unknown))}")
    return [_TOOL_SCHEMAS[name] for name in sorted(allowed)]


def available_tool_names() -> set[str]:
    return set(_TOOL_SCHEMAS)


def parse_tool_allowlist(raw: str) -> set[str]:
    if not isinstance(raw, str):
        raise ToolValidationError("工具白名单必须是逗号分隔文本")
    names = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = names - available_tool_names()
    if unknown:
        raise ToolValidationError(f"工具不在白名单：{','.join(sorted(unknown))}")
    return names


def _validate_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
    if not isinstance(arguments, dict):
        raise ToolValidationError("工具参数必须是 JSON 对象")
    extra = set(arguments) - allowed
    if extra:
        raise ToolValidationError(f"包含不允许的参数：{','.join(sorted(extra))}")


def _current_time(arguments: dict[str, Any]) -> dict:
    _validate_keys(arguments, {"timezone"})
    timezone = arguments.get("timezone", "Asia/Shanghai")
    if not isinstance(timezone, str) or not timezone or len(timezone) > 64:
        raise ToolValidationError("时区必须是有效的 IANA 时区文本")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolValidationError("时区不是有效的 IANA 时区") from exc
    now = datetime.now(zone)
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return {
        "ok": True,
        "timezone": timezone,
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M:%S"),
        "weekday": weekdays[now.weekday()],
        "iso": now.isoformat(timespec="seconds"),
    }


_WEATHER_CODES = {
    0: "晴",
    1: "大致晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴强冰雹",
}


def _weather_text(code: Any) -> str:
    try:
        return _WEATHER_CODES.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"


async def _weather(arguments: dict[str, Any], timeout: float) -> dict:
    _validate_keys(arguments, {"location", "forecast_days"})
    location = arguments.get("location")
    days = arguments.get("forecast_days", 3)
    if not isinstance(location, str) or not location.strip() or len(location.strip()) > 100:
        raise ToolValidationError("地点必须是 1 到 100 字符的文本")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 7:
        raise ToolValidationError("预报天数必须是 1 到 7 的整数")

    async with httpx.AsyncClient(timeout=timeout) as client:
        geo_response = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": location.strip(),
                "count": 1,
                "language": "zh",
                "format": "json",
            },
        )
        geo_response.raise_for_status()
        candidates = (geo_response.json() or {}).get("results") or []
        if not candidates:
            raise ToolValidationError("没有找到该地点")
        place = candidates[0]
        latitude = place.get("latitude")
        longitude = place.get("longitude")
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            raise TypeError("天气服务返回了无效坐标")
        forecast_response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "timezone": "auto",
                "forecast_days": days,
                "current": (
                    "temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
                ),
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
            },
        )
        forecast_response.raise_for_status()
        data = forecast_response.json() or {}

    current = data.get("current") or {}
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    precipitation = daily.get("precipitation_probability_max") or []
    forecast = []
    for index, date in enumerate(dates[:days]):
        forecast.append(
            {
                "date": date,
                "weather": _weather_text(codes[index] if index < len(codes) else None),
                "temperature_max_c": highs[index] if index < len(highs) else None,
                "temperature_min_c": lows[index] if index < len(lows) else None,
                "precipitation_probability_percent": (
                    precipitation[index] if index < len(precipitation) else None
                ),
            }
        )
    display_parts = [place.get("name"), place.get("admin1"), place.get("country")]
    return {
        "ok": True,
        "location": ", ".join(str(part)[:120] for part in display_parts if part)[:360],
        "timezone": str(data.get("timezone") or place.get("timezone") or "")[:64],
        "current": {
            "time": current.get("time"),
            "temperature_c": current.get("temperature_2m"),
            "apparent_temperature_c": current.get("apparent_temperature"),
            "weather": _weather_text(current.get("weather_code")),
            "wind_speed_kmh": current.get("wind_speed_10m"),
        },
        "forecast": forecast,
        "source": "Open-Meteo",
    }


async def execute_tool(name: str, arguments: dict[str, Any], *, timeout: float) -> dict:
    """执行白名单只读工具，并对整个执行过程施加超时。"""
    if name not in _TOOL_SCHEMAS:
        raise ToolValidationError("工具不在白名单")
    if timeout <= 0 or timeout > 60:
        raise ToolValidationError("工具超时必须在 1 到 60 秒之间")
    if name == "get_current_time":
        return _current_time(arguments)
    return await asyncio.wait_for(_weather(arguments, timeout), timeout=timeout)
