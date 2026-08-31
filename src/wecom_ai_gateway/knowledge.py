"""用户私有知识库：加密文档、确定性本地向量、混合检索与安全导入。"""

import hashlib
import hmac
import html
import io
import ipaddress
import math
import multiprocessing
import re
import socket
import zipfile
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

import httpx
from docx import Document
from pypdf import PdfReader
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .models import KnowledgeChunk, KnowledgeItem
from .security import decrypt_secret, encrypt_secret

_CHUNK_OVERLAP = 40
_EMBEDDING_DIMENSIONS = 256
_ALLOWED_MIME_TYPES = {
    "text/plain",
    "text/markdown",
    "text/html",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_MAX_DOCUMENT_CHARS = 500_000
_MAX_PDF_PAGES = 500
_MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_MAX_DOCX_COMPRESSION_RATIO = 100
_DOCUMENT_PARSE_TIMEOUT_SECONDS = 15


def _content_fingerprint(value: str) -> str:
    """带平台密钥的内容指纹，避免数据库泄露后离线猜测低熵正文。"""
    return hmac.new(
        settings.identity_hmac_key.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


class _VisibleHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _normalize_text(value: str) -> str:
    value = value.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\t ]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()[:_MAX_DOCUMENT_CHARS]


def chunk_text(content: str, chunk_chars: int) -> list[str]:
    content = _normalize_text(content or "")
    if not content:
        return []
    if len(content) <= chunk_chars:
        return [content]
    chunks: list[str] = []
    step = max(chunk_chars - _CHUNK_OVERLAP, 1)
    start = 0
    while start < len(content):
        end = min(start + chunk_chars, len(content))
        chunks.append(content[start:end].strip())
        if end >= len(content):
            break
        start += step
    return [chunk for chunk in chunks if chunk]


def _tokens(text: str) -> list[str]:
    normalized = text.lower()
    tokens = re.findall(r"[a-z0-9_\-]+", normalized)
    for segment in re.findall(r"[\u3400-\u9fff]+", normalized):
        tokens.extend(segment[index : index + 2] for index in range(max(len(segment) - 1, 1)))
    return tokens


def deterministic_embedding(text: str) -> list[float]:
    """无需外部服务的带密钥 256 维特征哈希向量；不会传出用户文档。"""
    vector = [0.0] * _EMBEDDING_DIMENSIONS
    for token, count in Counter(_tokens(text)).items():
        digest = hmac.new(
            settings.identity_hmac_key.encode(), token.encode("utf-8"), hashlib.sha256
        ).digest()[:8]
        bucket = int.from_bytes(digest[:4], "big") % _EMBEDDING_DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


def _bigrams(text: str) -> Counter:
    counter: Counter = Counter()
    for token in re.findall(r"[a-z0-9_\-]+", text.lower()):
        counter[token] += 2
    for segment in re.findall(r"[\u3400-\u9fff]+", text.lower()):
        for index in range(len(segment) - 1):
            counter[segment[index : index + 2]] += 1
    return counter


def _similarity(query: Counter, candidate: Counter) -> float:
    if not query or not candidate:
        return 0.0
    overlap = sum((query & candidate).values())
    total = sum(query.values()) + sum(candidate.values())
    return 2 * overlap / total if total else 0.0


def item_text(item: KnowledgeItem) -> str:
    if item.content_encrypted:
        try:
            return decrypt_secret(item.content_encrypted)
        except Exception:  # noqa: BLE001 - 密钥轮换时旧内容不可读
            return ""
    return item.content or ""


def _index_item(db: Session, item: KnowledgeItem, content: str, chunk_chars: int = 800) -> None:
    db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.item_id == item.id))
    for index, chunk in enumerate(chunk_text(content, chunk_chars), start=1):
        db.add(
            KnowledgeChunk(
                item_id=item.id,
                user_id=item.user_id,
                chunk_index=index,
                content_encrypted=encrypt_secret(chunk),
                content_sha256=_content_fingerprint(chunk),
                embedding=deterministic_embedding(chunk),
                char_count=len(chunk),
            )
        )


def add_item(
    db: Session,
    user_id: str,
    title: str,
    content: str,
    *,
    source_type: str = "text",
    source_name: str | None = None,
    source_url: str | None = None,
    mime_type: str | None = None,
    chunk_chars: int = 800,
) -> KnowledgeItem:
    title = title.strip()[:255]
    content = _normalize_text(content or "")
    if not title or not content:
        raise ValueError("标题与内容不能为空")
    digest = _content_fingerprint(content)
    item = db.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.user_id == user_id,
            KnowledgeItem.title == title,
        )
    )
    if item is None:
        item = KnowledgeItem(user_id=user_id, title=title, content="")
        db.add(item)
        db.flush()
    item.content = ""
    item.content_encrypted = encrypt_secret(content)
    item.content_sha256 = digest
    item.source_type = source_type[:30]
    item.source_name = (source_name or title)[:255]
    item.source_url = source_url[:1000] if source_url else None
    item.mime_type = mime_type[:160] if mime_type else None
    item.content_chars = len(content)
    from datetime import UTC, datetime

    item.indexed_at = datetime.now(UTC)
    _index_item(db, item, content, chunk_chars)
    db.commit()
    return item


def list_items(db: Session, user_id: str) -> list[KnowledgeItem]:
    return list(
        db.scalars(
            select(KnowledgeItem)
            .where(KnowledgeItem.user_id == user_id)
            .order_by(KnowledgeItem.updated_at.desc())
        )
    )


def migrate_legacy_items(db: Session, *, chunk_chars: int = 800) -> int:
    """把旧明文或旧裸哈希条目原地加密并重建带密钥索引；可重复执行。"""
    rows = list(
        db.scalars(
            select(KnowledgeItem).where(
                (KnowledgeItem.content_encrypted.is_(None)) | (KnowledgeItem.content != "")
            )
        )
    )
    # 已加密条目无法仅凭指纹区分旧 SHA-256 与新 HMAC；逐条解密比较并仅重建旧格式。
    for encrypted in db.scalars(
        select(KnowledgeItem).where(KnowledgeItem.content_encrypted.is_not(None))
    ):
        content = item_text(encrypted)
        if content and encrypted.content_sha256 != _content_fingerprint(content):
            rows.append(encrypted)
    rows = list({row.id: row for row in rows}.values())
    for row in rows:
        content = _normalize_text(row.content or item_text(row))
        row.content = ""
        row.content_encrypted = encrypt_secret(content)
        row.content_sha256 = _content_fingerprint(content)
        row.content_chars = len(content)
        row.source_type = row.source_type or "legacy"
        row.source_name = row.source_name or row.title
        _index_item(db, row, content, chunk_chars)
    db.commit()
    return len(rows)


def delete_item(db: Session, user_id: str, title: str) -> bool:
    row = db.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.user_id == user_id,
            KnowledgeItem.title == title.strip(),
        )
    )
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def delete_item_by_id(db: Session, user_id: str, item_id: str) -> bool:
    row = db.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.id == item_id,
            KnowledgeItem.user_id == user_id,
        )
    )
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def reindex_item(db: Session, user_id: str, item_id: str, *, chunk_chars: int = 800) -> bool:
    row = db.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.id == item_id,
            KnowledgeItem.user_id == user_id,
        )
    )
    if not row:
        return False
    content = item_text(row)
    if not content:
        return False
    _index_item(db, row, content, chunk_chars)
    from datetime import UTC, datetime

    row.indexed_at = datetime.now(UTC)
    db.commit()
    return True


def _ensure_chunks(db: Session, item: KnowledgeItem, chunk_chars: int) -> list[KnowledgeChunk]:
    chunks = list(
        db.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.item_id == item.id)
            .order_by(KnowledgeChunk.chunk_index)
        )
    )
    if chunks:
        return chunks
    content = item_text(item)
    if not content:
        return []
    # 旧版明文条目首次读取时原地加密并建立索引。
    item.content = ""
    item.content_encrypted = encrypt_secret(content)
    item.content_sha256 = _content_fingerprint(content)
    item.content_chars = len(content)
    item.source_type = item.source_type or "legacy"
    _index_item(db, item, content, chunk_chars)
    db.commit()
    return list(
        db.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.item_id == item.id)
            .order_by(KnowledgeChunk.chunk_index)
        )
    )


def vector_candidate_query(user_id: str, embedding: list[float], limit: int):
    """构造 PostgreSQL pgvector 余弦候选查询；最终仍在应用层做词法融合与解密。"""
    return (
        select(KnowledgeChunk)
        .where(KnowledgeChunk.user_id == user_id)
        .order_by(KnowledgeChunk.embedding.cosine_distance(embedding))
        .limit(limit)
    )


def search(
    db: Session,
    user_id: str,
    query: str,
    limit: int = 3,
    chunk_chars: int = 800,
) -> list[dict]:
    query = _normalize_text(query)
    if not query:
        return []
    query_counter = _bigrams(query)
    query_vector = deterministic_embedding(query)
    candidates: list[tuple[float, KnowledgeItem, KnowledgeChunk, str]] = []
    items = list_items(db, user_id)
    item_map = {item.id: item for item in items}
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        # 先由 pgvector 缩小候选集，再解密并做词法+向量融合；不会全表扫描。
        chunks_by_item: dict[str, list[KnowledgeChunk]] = {}
        for chunk in db.scalars(
            vector_candidate_query(user_id, query_vector, max(limit * 8, 24))
        ):
            chunks_by_item.setdefault(chunk.item_id, []).append(chunk)
    else:
        chunks_by_item = {
            item.id: _ensure_chunks(db, item, chunk_chars)
            for item in items
        }
    for item_id, chunks in chunks_by_item.items():
        item = item_map.get(item_id)
        if item is None:
            continue
        for chunk in chunks:
            try:
                text = decrypt_secret(chunk.content_encrypted)
            except Exception:  # noqa: BLE001, S112 - 坏块静默跳过且绝不记录私密内容
                continue
            lexical = _similarity(query_counter, _bigrams(text))
            vector_score = max(0.0, _cosine(query_vector, list(chunk.embedding or [])))
            title_bonus = 0.15 if query.lower() in item.title.lower() else 0.0
            score = lexical * 0.7 + vector_score * 0.3 + title_bonus
            # n-gram 向量不是外部语义模型；要求最低词法证据，避免无关内容误注入。
            if lexical >= 0.04 or title_bonus:
                candidates.append((score, item, chunk, text))
    candidates.sort(key=lambda value: value[0], reverse=True)
    return [
        {
            "item_id": item.id,
            "title": item.title,
            "text": text,
            "chunk_index": chunk.chunk_index,
            "source_type": item.source_type,
            "source_name": item.source_name or item.title,
            "source_url": item.source_url,
            "score": round(score, 6),
            "citation": f"[KB:{item.title}#{chunk.chunk_index}]",
        }
        for score, item, chunk, text in candidates[:limit]
    ]


def build_injection(
    db: Session,
    user_id: str,
    query: str,
    *,
    max_chunks: int,
    chunk_chars: int,
) -> str:
    results = search(db, user_id, query, limit=max_chunks, chunk_chars=chunk_chars)
    if not results:
        return ""
    return "\n\n".join(
        f"{result['citation']} 来源：{result['source_name']}\n{result['text']}"
        for result in results
    )


def extract_document_text(data: bytes, mime_type: str, filename: str = "") -> str:
    if len(data) > _MAX_DOCUMENT_BYTES:
        raise ValueError("文档不能超过 10MB")
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if mime not in _ALLOWED_MIME_TYPES:
        mime = {
            "txt": "text/plain",
            "md": "text/markdown",
            "markdown": "text/markdown",
            "html": "text/html",
            "htm": "text/html",
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }.get(suffix, mime)
    if mime not in _ALLOWED_MIME_TYPES:
        raise ValueError("仅支持 TXT、Markdown、HTML、PDF 和 DOCX")
    if mime in {"text/plain", "text/markdown"}:
        return _normalize_text(data.decode("utf-8-sig", errors="replace"))
    if mime == "text/html":
        parser = _VisibleHtml()
        parser.feed(data.decode("utf-8", errors="replace"))
        return _normalize_text(html.unescape("".join(parser.parts)))
    if mime == "application/pdf":
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > _MAX_PDF_PAGES:
            raise ValueError(f"PDF 页数不能超过 {_MAX_PDF_PAGES} 页")
        return _normalize_text("\n\n".join(page.extract_text() or "" for page in reader.pages))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            total = 0
            for info in archive.infolist():
                total += info.file_size
                if total > _MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise ValueError("DOCX 解压后内容不能超过 50MB")
                if info.file_size and info.compress_size == 0:
                    raise ValueError("DOCX 解压比例异常")
                if info.compress_size and info.file_size / info.compress_size > _MAX_DOCX_COMPRESSION_RATIO:
                    raise ValueError("DOCX 解压比例异常")
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX 文件结构无效") from exc
    document = Document(io.BytesIO(data))
    return _normalize_text("\n".join(paragraph.text for paragraph in document.paragraphs))


def _document_parse_worker(connection, data: bytes, mime_type: str, filename: str) -> None:
    """隔离解析不可信文档；子进程只返回文本或公开错误。"""
    try:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        except (ImportError, OSError, ValueError):
            pass
        connection.send((True, extract_document_text(data, mime_type, filename)))
    except Exception as exc:  # noqa: BLE001 - 只跨进程返回异常类型
        connection.send((False, type(exc).__name__))
    finally:
        connection.close()


def extract_document_text_isolated(data: bytes, mime_type: str, filename: str = "") -> str:
    """在受限子进程解析文档，超时或崩溃均安全拒绝。"""
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_document_parse_worker,
        args=(child_connection, data, mime_type, filename),
        daemon=True,
    )
    process.start()
    child_connection.close()
    if not parent_connection.poll(_DOCUMENT_PARSE_TIMEOUT_SECONDS):
        process.terminate()
        process.join(2)
        parent_connection.close()
        raise ValueError("文档解析超时")
    try:
        ok, value = parent_connection.recv()
    except EOFError as exc:
        raise ValueError("文档解析失败") from exc
    finally:
        parent_connection.close()
        process.join(2)
    if not ok:
        raise ValueError(f"文档解析失败：{value}")
    return str(value)


def _resolve_public_addresses(hostname: str, port: int = 443) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """解析并返回全部公网地址；调用方必须使用返回地址直连，不能再次解析域名。"""
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = list(
                dict.fromkeys(
                    ipaddress.ip_address(info[4][0])
                    for info in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
                )
            )
        except socket.gaierror as exc:
            raise ValueError("网址域名无法解析") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("网址不能指向内网、环回或保留地址")
    return addresses


def _validate_public_https_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("网址必须是无凭据的 HTTPS 地址")
    _resolve_public_addresses(parsed.hostname, parsed.port or 443)
    return parsed.geturl()


async def fetch_public_document(url: str) -> tuple[bytes, str, str]:
    safe_url = _validate_public_https_url(url)
    parsed = urlsplit(safe_url)
    hostname = parsed.hostname or ""
    port = parsed.port or 443
    # 直接连接刚刚验证过的 IP，并通过 Host/SNI 保留原域名证书校验，封闭 DNS 重绑定窗口。
    address = _resolve_public_addresses(hostname, port)[0]
    pinned_host = f"[{address}]" if address.version == 6 else str(address)
    pinned_netloc = f"{pinned_host}:{port}" if port != 443 else pinned_host
    pinned_url = urlunsplit(("https", pinned_netloc, parsed.path, parsed.query, ""))
    host_header = f"{hostname}:{port}" if port != 443 else hostname
    headers = {"Accept": ", ".join(_ALLOWED_MIME_TYPES), "Host": host_header}
    content = bytearray()
    async with (
        httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client,
        client.stream(
            "GET",
            pinned_url,
            headers=headers,
            extensions={"sni_hostname": hostname},
        ) as response,
    ):
        if 300 <= response.status_code < 400:
            raise ValueError("网址不能重定向")
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and int(declared) > _MAX_DOCUMENT_BYTES:
            raise ValueError("文档不能超过 10MB")
        mime = response.headers.get("content-type", "text/html").split(";", 1)[0].strip()
        if mime not in _ALLOWED_MIME_TYPES:
            raise ValueError("网址返回了不支持的文档类型")
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > _MAX_DOCUMENT_BYTES:
                raise ValueError("文档不能超过 10MB")
    # 展示/持久化 URL 去除 query 和 fragment，避免签名参数进入数据库或 API 响应。
    display_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return bytes(content), mime, display_url
