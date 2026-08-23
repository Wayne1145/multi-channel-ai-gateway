"""受限只读工具注册表与执行器。

安全约束：仅暴露静态注册的只读工具；参数使用逐字段白名单；所有外部请求目标固定，
调用者不能提供 URL、请求头、文件路径或命令。
"""

import asyncio
import html
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

log_tool = logging.getLogger(__name__)


class ToolValidationError(ValueError):
    """工具名或参数不符合白名单约束。"""


_BING_RESULT_RE = re.compile(
    r'<li class="b_algo".*?<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?</h2>'
    r'(?:.*?<p[^>]*>(.*?)</p>)?',
    re.DOTALL,
)


def _clean_html_fragment(raw: str) -> str:
    """去除必应结果标题/摘要中的 HTML 标签并反转义实体。"""
    text = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(text).strip()


# 常见停用词：这些词独立成词即可，不需要引号保护
_BING_STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "等", "一个", "什么", "如何",
    "怎么", "今天", "昨天", "明天", "新闻", "最新", "最新消息", "什么意思", "是什么",
    "为什么", "哪个", "哪些", "哪里", "多少", "介绍", "推荐", "有什么", "怎么样",
}


def _improve_bing_query(query: str) -> str:
    """改进必应中文查询，缓解其分词对无空格长中文查询的漂移。

    策略：
    - 中文词组（含夹带字母数字的如 东方Project）整体加引号，防止必应把
      "广东东方Project同人展" 拆成 "广东/东方/Project/同人展"；
    - 保持英文/数字原样；
    - 常用停用词不引号（避免把 "今天新闻" 括死成短语导致无结果）。
    """
    # 已含引号/操作符则原样返回（模型或用户已精确构造）
    if '"' in query or "site:" in query or "filetype:" in query:
        return query
    parts = query.split()
    if len(parts) <= 1:
        # 单段：若为长中文词组（>=4 字）且不含常见停用词子串，加引号保护；
        # 含停用词（如"今天新闻"）整体加引号反而可能括死成短语导致无结果。
        seg = parts[0].strip() if parts else ""
        if (
            seg
            and re.search(r"[\u4e00-\u9fff]", seg)
            and len(seg) >= 4
            and seg not in _BING_STOPWORDS
            and not any(stop in seg for stop in _BING_STOPWORDS)
        ):
            return f'"{seg}"'
        return query
    improved = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 含中文（可能夹带字母数字）的词段加引号，除非是纯停用词
        if re.search(r"[\u4e00-\u9fff]", part) and part not in _BING_STOPWORDS:
            # 中文+英文混合（如 东方Project）整体加引号
            improved.append(f'"{part}"')
        else:
            improved.append(part)
    return " ".join(improved)


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
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "通过必应搜索互联网，返回结果标题、链接与摘要。用于查询实时新闻、最新信息或任何训练数据可能过时的问题。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，中文搜索建议用中文表达。",
                        "minLength": 1,
                        "maxLength": 200,
                    }
                },
                "required": ["query"],
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


async def _open_websearch(arguments: dict[str, Any], timeout: float) -> dict | None:
    """通过本地 open-websearch daemon 搜索（startpage 引擎优先）。

    daemon 由运维启动（open-websearch serve，端口 3210），聚合多引擎结果、
    免费无 key，且 startpage 代理 Google 结果对海外 VPS IP 不反爬。
    返回 None 表示 daemon 不可用或搜索失败（调用方回退直连引擎）。
    """
    _validate_keys(arguments, {"query"})
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolValidationError("搜索关键词必须是 1 到 200 字符的文本")
    query = query.strip()
    if len(query) > 200:
        raise ToolValidationError("搜索关键词不能超过 200 字符")

    daemon_url = "http://127.0.0.1:3210/search"
    engines = ["startpage", "bing", "sogou", "hackernews"]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                daemon_url,
                json={"query": query, "limit": 5, "engines": engines},
            )
            response.raise_for_status()
            payload = response.json()
        data = payload.get("data") or {}
        results = data.get("results") or []
        if not results:
            return None
        normalized = []
        for item in results[:5]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url.startswith("http"):
                continue
            normalized.append(
                {
                    "title": str(item.get("title") or "")[:200],
                    "url": url[:500],
                    "snippet": str(item.get("description") or item.get("snippet") or "")[:400],
                    "source": str(item.get("source") or item.get("engine") or "")[:80],
                }
            )
        if not normalized:
            return None
        return {"ok": True, "query": query, "results": normalized, "source": "open-websearch"}
    except Exception:
        log_tool.warning("open-websearch daemon 搜索失败，回退直连 query=%s", query, exc_info=True)
        return None


async def _bing_search(arguments: dict[str, Any], timeout: float) -> dict:
    """通过必应（固定端点）搜索互联网，返回前 5 条标题/链接/摘要。

    安全：搜索端点写死为 https://www.bing.com/search，调用者只能提供 query；
    不跟随外部链接、不抓取页面正文，只解析结果列表。中文查询走必应中国。
    """
    _validate_keys(arguments, {"query"})
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolValidationError("搜索关键词必须是 1 到 200 字符的文本")
    query = query.strip()
    if len(query) > 200:
        raise ToolValidationError("搜索关键词不能超过 200 字符")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(
            "https://www.bing.com/search",
            params={"q": _improve_bing_query(query), "mkt": "zh-CN", "ensearch": 0},
            headers=headers,
        )
        response.raise_for_status()
        page = response.text

    results = []
    for match in _BING_RESULT_RE.finditer(page):
        url = html.unescape(match.group(1)).strip()
        title = _clean_html_fragment(match.group(2))
        snippet = _clean_html_fragment(match.group(3) or "")
        # 只保留 http(s) 结果，跳过必应内部导航链接
        if not url.startswith("http"):
            continue
        results.append(
            {
                "title": title[:200],
                "url": url[:500],
                "snippet": snippet[:400],
            }
        )
        if len(results) >= 5:
            break
    if not results:
        return {"ok": True, "query": query, "results": [], "note": "没有搜索到结果"}
    return {"ok": True, "query": query, "results": results, "source": "Bing"}


async def execute_tool(name: str, arguments: dict[str, Any], *, timeout: float) -> dict:
    """执行白名单只读工具，并对整个执行过程施加超时。"""
    if name not in _TOOL_SCHEMAS:
        raise ToolValidationError("工具不在白名单")
    if timeout <= 0 or timeout > 60:
        raise ToolValidationError("工具超时必须在 1 到 60 秒之间")
    if name == "get_current_time":
        return _current_time(arguments)
    if name == "web_search":
        # 优先本地 open-websearch daemon（startpage 等引擎，海外 IP 不反爬、质量高）；
        # daemon 不可用或无结果时回退直连必应（查询改写已内置）。
        daemon_result = await asyncio.wait_for(_open_websearch(arguments, timeout), timeout=timeout)
        if daemon_result is not None:
            return daemon_result
        return await asyncio.wait_for(_bing_search(arguments, timeout), timeout=timeout)
    return await asyncio.wait_for(_weather(arguments, timeout), timeout=timeout)
