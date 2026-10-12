"""IVectorRepository — dense (pgvector) and lexical (tsvector) search over the child chunks of auto-mode docs.
Both return one VectorMatch per child hit with its parent section already joined in (no second round trip).
Dense score = 1 - cosine distance; lexical score = ts_rank_cd(..., 32), i.e. rank/(rank+1), also 0..1.
Parents (is_parent), prompt-mode, deleted and not-ready documents never appear; ready_degraded is retrievable."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import asyncpg

# Documents whose chunks may be searched. ready_degraded = indexed, but part of the source was unreadable.
SEARCHABLE_STATUSES = ["ready", "ready_degraded"]

_HNSW_MAX_SCAN_TUPLES = int(os.environ.get("KNOWLEDGE_HNSW_MAX_SCAN_TUPLES", "20000"))
# Older pgvector cannot keep scanning past ef_search under a filter; widening the candidate list is the
# nearest equivalent (hnsw.ef_search tops out at 1000).
_HNSW_FALLBACK_EF_SEARCH = 400


@dataclass(frozen=True)
class VectorMatch:
    chunk_id: str
    document_id: str
    kb_id: str
    content: str  # the parent section when the chunk has one, else the chunk itself
    score: float  # dense: cosine similarity; lexical: normalised ts_rank_cd; higher = more relevant
    token_count: int | None  # of `content`
    page: int | None
    language: str | None
    tags: dict[str, Any]
    version: int
    document_title: str
    kb_slug: str
    # Parent expansion bookkeeping (defaults keep prompt-mode matches and old call sites unchanged).
    child_content: str | None = None
    child_token_count: int | None = None
    parent_id: str | None = None
    section_path: str | None = None
    found_by: tuple[str, ...] = field(default_factory=tuple)


class IVectorRepository(Protocol):
    async def search(
        self,
        conn: asyncpg.Connection,
        kb_ids: list[str],
        embedding: list[float],
        top_k: int,
        minimum_score: float,
    ) -> list[VectorMatch]: ...

    async def lexical_search(
        self,
        conn: asyncpg.Connection,
        kb_ids: list[str],
        query: str,
        limit: int,
        minimum_score: float,
    ) -> list[VectorMatch]: ...


_STOPWORDS = frozenset(
    "a an and are as at be but by do does for from had has have how i if in is it its me my of on or so than "
    "that the their them then there they this to was we were what when where which who why will with you your".split()
)
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_MAX_QUERY_TERMS = 24


def build_tsquery(query: str) -> str | None:
    """OR of the query's content words as a to_tsquery('simple', ...) string; None if nothing is left.
    OR, not plainto_tsquery's AND: a spoken question rarely contains every word of the passage that answers it,
    and ts_rank_cd already prefers the chunks that match more of the terms. Tokens are \\w+ only, so quoting
    them is enough to keep user text out of tsquery syntax."""
    terms: list[str] = []
    for token in _TOKEN_RE.findall(query.casefold()):
        if token in _STOPWORDS or (len(token) < 2 and not token.isdigit()) or token in terms:
            continue
        terms.append(token)
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    if not terms:
        return None
    return " | ".join(f"'{t}'" for t in terms)


_SELECT_COLUMNS = """
    c.id AS chunk_id, c.document_id, c.kb_id, c.content AS child_content, c.token_count AS child_tokens,
    c.page, c.language, c.tags, c.version, c.parent_id, c.section_path,
    d.title AS document_title, kb.slug AS kb_slug,
    p.content AS parent_content, p.token_count AS parent_tokens
"""

_DOC_JOINS = """
    JOIN kb_documents d ON d.id = c.document_id AND d.deleted_at IS NULL AND d.usage_mode = 'auto'
                       AND d.status = ANY($4::text[])
    JOIN knowledge_bases kb ON kb.id = c.kb_id
    LEFT JOIN kb_chunks p ON p.id = c.parent_id
"""

_DENSE_SQL = f"""
    SELECT {_SELECT_COLUMNS}, 1 - (c.embedding <=> $1::vector) AS score
    FROM kb_chunks c {_DOC_JOINS}
    WHERE c.kb_id = ANY($2::uuid[]) AND c.embedding IS NOT NULL AND NOT c.is_parent
      AND (c.embedding <=> $1::vector) <= 1 - $5::float8  -- distance form: a zero vector gives NaN, which must not pass
    ORDER BY c.embedding <=> $1::vector
    LIMIT $3
"""

_LEXICAL_SQL = f"""
    WITH q AS (SELECT to_tsquery('simple', $1) AS tsq),
    hits AS (
        SELECT c.id, ts_rank_cd(c.tsv, q.tsq, 32) AS score
        FROM kb_chunks c, q
        WHERE c.kb_id = ANY($2::uuid[]) AND NOT c.is_parent AND c.tsv @@ q.tsq
    )
    SELECT {_SELECT_COLUMNS}, h.score
    FROM hits h
    JOIN kb_chunks c ON c.id = h.id {_DOC_JOINS}
    WHERE h.score >= $5
    ORDER BY h.score DESC
    LIMIT $3
"""


def _to_match(row: asyncpg.Record, found_by: str) -> VectorMatch:
    has_parent = row["parent_content"] is not None
    content = row["parent_content"] if has_parent else row["child_content"]
    tokens = row["parent_tokens"] if has_parent else row["child_tokens"]
    return VectorMatch(
        chunk_id=str(row["chunk_id"]),
        document_id=str(row["document_id"]),
        kb_id=str(row["kb_id"]),
        content=content,
        score=float(row["score"]),
        token_count=tokens,
        page=row["page"],
        language=row["language"],
        tags=json.loads(row["tags"]) if row["tags"] else {},
        version=row["version"],
        document_title=row["document_title"],
        kb_slug=row["kb_slug"],
        child_content=row["child_content"],
        child_token_count=row["child_tokens"],
        parent_id=str(row["parent_id"]) if row["parent_id"] is not None else None,
        section_path=row["section_path"],
        found_by=(found_by,),
    )


class PgVectorRepository:
    """Stateless apart from a one-time pgvector capability probe; the caller passes a tenant-scoped
    connection so every query stays under its RLS scope."""

    def __init__(self) -> None:
        # None = not probed; "session" = iterative scan already on for every connection (db.get_pool sets it
        # as a startup parameter, so it survives asyncpg's RESET ALL); "local" = set it per transaction;
        # "fallback" = pgvector < 0.8, widen ef_search instead.
        self._hnsw_mode: str | None = None

    async def _probe(self, conn: asyncpg.Connection) -> str:
        # Loading the vector library first makes its hnsw.* settings visible to current_setting().
        await conn.fetchval("SELECT '[1]'::vector::text")
        current = await conn.fetchval("SELECT current_setting('hnsw.iterative_scan', true)")
        version = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        parts = [int(p) for p in re.findall(r"\d+", version or "0")][:2]
        if parts < [0, 8]:
            return "fallback"
        return "session" if current in ("relaxed_order", "strict_order") else "local"

    async def _prepare_hnsw(self, conn: asyncpg.Connection) -> None:
        if self._hnsw_mode is None:
            self._hnsw_mode = await self._probe(conn)
        if self._hnsw_mode == "local":
            await conn.execute(
                "SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true), "
                "set_config('hnsw.max_scan_tuples', $1, true)", str(_HNSW_MAX_SCAN_TUPLES),
            )
        elif self._hnsw_mode == "fallback":
            await conn.execute("SELECT set_config('hnsw.ef_search', $1, true)", str(_HNSW_FALLBACK_EF_SEARCH))

    async def search(
        self,
        conn: asyncpg.Connection,
        kb_ids: list[str],
        embedding: list[float],
        top_k: int,
        minimum_score: float,
    ) -> list[VectorMatch]:
        if not kb_ids:
            return []
        await self._prepare_hnsw(conn)
        vector_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
        rows = await conn.fetch(
            _DENSE_SQL, vector_literal, kb_ids, top_k, SEARCHABLE_STATUSES, minimum_score,
        )
        # relaxed_order can hand rows back slightly out of order; the outer sort makes it exact.
        matches = [_to_match(row, "dense") for row in rows]
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches

    async def lexical_search(
        self,
        conn: asyncpg.Connection,
        kb_ids: list[str],
        query: str,
        limit: int,
        minimum_score: float,
    ) -> list[VectorMatch]:
        tsquery = build_tsquery(query)
        if not kb_ids or tsquery is None:  # nothing searchable in the query: no round trip
            return []
        rows = await conn.fetch(_LEXICAL_SQL, tsquery, kb_ids, limit, SEARCHABLE_STATUSES, minimum_score)
        return [_to_match(row, "lexical") for row in rows]
