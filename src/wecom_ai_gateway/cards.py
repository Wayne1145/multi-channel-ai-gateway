"""角色卡系统：SOUL.md（OpenClaw 标准）与 SillyTavern v2/v3 双格式解析与注入。

- soul_md：Markdown 人格文件（Core Truths / Boundaries / Vibe / Continuity），原文即指令。
- st_v2 / st_v3：SillyTavern 角色卡 JSON（v2 顶层含 data 字段，v3 同构扩展），
  解析后按统一字段组装成 system prompt 前缀。

卡内容入库即密文（content_encrypted），本模块不提供任何解密出口给管理端。
"""

import json
import re
import struct
import zlib

from .security import decrypt_secret, encrypt_secret

SOUL_SECTIONS = ["Core Truths", "Boundaries", "Vibe", "Continuity"]

# SillyTavern v2/v3 中参与 system prompt 组装的字段
ST_FIELDS = [
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "system_prompt",
    "post_history_instructions",
]


def detect_format(content: str) -> str:
    """自动检测卡格式：JSON（SillyTavern）或 Markdown（SOUL.md）。"""
    stripped = content.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return "soul_md"
        spec = data.get("spec", "")
        if spec == "chara_card_v3":
            return "st_v3"
        if spec == "chara_card_v2" or "char_name" in data or "data" in data:
            return "st_v2"
        return "soul_md"
    return "soul_md"


def extract_card_from_png(data: bytes) -> str | None:
    """从 SillyTavern PNG 角色卡提取内嵌 JSON。

    - v2：tEXt chunk，keyword=`chara`，文本即角色卡 JSON；
    - v3：iTXt chunk，keyword=`ccv3`，可能带 zlib 压缩（compression_flag=1）。

    返回内嵌 JSON 字符串；不是 PNG 或没有角色卡 chunk 时返回 None。
    """
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        if length < 0 or pos + 8 + length > len(data):
            return None
        if ctype in (b"tEXt", b"iTXt"):
            keyword, sep, text = payload.partition(b"\x00")
            if not sep:
                pos += 12 + length
                continue
            if keyword in (b"chara", b"ccv3"):
                if ctype == b"tEXt":
                    return text.decode("utf-8", errors="replace")
                # iTXt 布局：keyword\0 compression_flag compression_method
                #         language_tag\0 translated_keyword\0 text
                if len(text) < 2:
                    return None
                comp_flag = text[0:1]
                rest = text[2:].split(b"\x00", 2)
                if len(rest) < 3:
                    return None
                body = rest[2]
                if comp_flag == b"\x01":
                    try:
                        body = zlib.decompress(body)
                    except zlib.error:
                        return None
                return body.decode("utf-8", errors="replace")
        pos += 12 + length
    return None


def parse_soul_md(text: str) -> dict:
    """解析 SOUL.md 为 {section: content}；未识别的小节归入 extra。"""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^#+\s*(.+?)\s*$", line)
        if match:
            if current:
                sections[current] = "\n".join(buffer).strip()
            current = match.group(1).strip()
            buffer = []
        elif current:
            buffer.append(line)
    if current:
        sections[current] = "\n".join(buffer).strip()
    known: dict[str, str | dict] = {k: sections.get(k, "") for k in SOUL_SECTIONS}
    known["extra"] = {k: v for k, v in sections.items() if k not in SOUL_SECTIONS}
    return known


def parse_st_json(text: str) -> dict:
    """解析 SillyTavern 角色卡 JSON，返回统一字段字典（空字段过滤）。"""
    data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        inner = data["data"]
        return {k: inner.get(k, "") for k in ST_FIELDS if inner.get(k)}
    return {k: data.get(k, "") for k in ST_FIELDS if data.get(k)}


def card_to_system_prompt(fmt: str, content: str) -> str:
    """把卡内容转换为注入 system prompt 的文本。

    - soul_md：原文即人格指令，直接使用；
    - st_v2/v3：按字段组装为中文人格描述，优先 system_prompt / description。
    """
    if fmt == "soul_md":
        return content.strip()
    try:
        fields = parse_st_json(content)
    except json.JSONDecodeError:
        return content.strip()
    if fields.get("system_prompt"):
        return fields["system_prompt"].strip()
    lines: list[str] = []
    if fields.get("name"):
        lines.append(f"你是{fields['name']}。")
    if fields.get("description"):
        lines.append(str(fields["description"]).strip())
    if fields.get("personality"):
        lines.append(f"性格：{fields['personality']}".strip())
    if fields.get("scenario"):
        lines.append(f"场景设定：{fields['scenario']}".strip())
    if fields.get("post_history_instructions"):
        lines.append(str(fields["post_history_instructions"]).strip())
    return "\n".join(lines)


def export_card_text(fmt: str, content: str) -> str:
    """导出为用户可读文本：soul_md 原样；ST JSON 格式化输出。"""
    if fmt == "soul_md":
        return content
    try:
        return json.dumps(json.loads(content), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return content


def encrypt_card_content(content: str) -> str:
    return encrypt_secret(content)


def decrypt_card_content(content_encrypted: str) -> str:
    return decrypt_secret(content_encrypted)
