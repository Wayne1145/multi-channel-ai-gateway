"""用户知识库：条目管理、分块与关键词检索。

检索为本地实现（字符 bigram 相似度 + 关键词命中），不依赖外部 embedding
服务，配合 pgvector 可后续替换为向量检索。

安全：知识库内容按用户隔离；检索结果只注入到该用户自己的模型上下文。
"""

import logging
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import KnowledgeItem

log = logging.getLogger(__name__)

_CHUNK_OVERLAP = 40  # 相邻分块重叠字符数，避免切断语义


def chunk_text(content: str, chunk_chars: int) -> list[str]:
    """按字符数分块，块间保留少量重叠；空内容返回空列表。"""
    content = (content or "").strip()
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
    return [c for c in chunks if c]


def _bigrams(text: str) -> Counter:
    """中英文友好的字符 bigram 计数（英文按词、中文按字符）。"""
    text = text.lower()
    words = re.findall(r"[a-z0-9]+", text)
    counter: Counter = Counter()
    for word in words:
        counter[word] += 2  # 英文单词权重更高
    # 中文部分按相邻字符 bigram
    cjk = re.findall(r"[\u4e00-\u9fff]+", text)
    for segment in cjk:
        for i in range(len(segment) - 1):
            counter[segment[i : i + 2]] += 1
    return counter


def _similarity(query: Counter, candidate: Counter) -> float:
    if not query or not candidate:
        return 0.0
    overlap = sum((query & candidate).values())
    total = sum(query.values()) + sum(candidate.values())
    return 2 * overlap / total if total else 0.0


def add_item(db: Session, user_id: str, title: str, content: str) -> KnowledgeItem:
    title = title.strip()[:255]
    content = (content or "").strip()
    if not title or not content:
        raise ValueError("标题与内容不能为空")
    existing = db.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.user_id == user_id, KnowledgeItem.title == title
        )
    )
    if existing:
        existing.content = content
        item = existing
    else:
        item = KnowledgeItem(user_id=user_id, title=title, content=content)
        db.add(item)
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


def delete_item(db: Session, user_id: str, title: str) -> bool:
    row = db.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.user_id == user_id, KnowledgeItem.title == title.strip()
        )
    )
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def search(
    db: Session,
    user_id: str,
    query: str,
    limit: int = 3,
    chunk_chars: int = 800,
) -> list[dict]:
    """检索最相关的知识块；返回 {title, text}。"""
    query_counter = _bigrams(query)
    candidates: list[tuple[float, str, str]] = []
    for item in list_items(db, user_id):
        for chunk in chunk_text(item.content, chunk_chars):
            score = _similarity(query_counter, _bigrams(chunk))
            if score >= 0.12:  # 过低相似度视为无关，避免注入噪声
                # 标题命中加成
                if query.lower() in item.title.lower():
                    score += 0.15
                candidates.append((score, item.title, chunk))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [
        {"title": title, "text": chunk}
        for _, title, chunk in candidates[:limit]
    ]


def build_injection(
    db: Session,
    user_id: str,
    query: str,
    *,
    max_chunks: int,
    chunk_chars: int,
) -> str:
    """生成注入系统提示的知识库段落；无结果返回空串。"""
    results = search(db, user_id, query, limit=max_chunks, chunk_chars=chunk_chars)
    if not results:
        return ""
    sections = []
    for result in results:
        sections.append(f"【{result['title']}】\n{result['text']}")
    return "\n\n".join(sections)
