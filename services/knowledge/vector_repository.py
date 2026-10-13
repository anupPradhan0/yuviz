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
        model: str | None = None,
        notes: list[str] | None = None,
    ) -> list[VectorMatch]: ...

    async def lexical_search(
        self,
        conn: asyncpg.Connection,
        kb_ids: list[str],
        query: str,
        limit: int,
        minimum_score: float,
        approx_chunks: int | None = None,
        notes: list[str] | None = None,
    ) -> list[VectorMatch]: ...


_STOPWORDS = frozenset(
    "a an and are as at be but by do does for from had has have how i if in is it its me my of on or so than "
    "that the their them then there they this to was we were what when where which who why will with you your".split()
)
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_MAX_QUERY_TERMS = 24


def query_terms(query: str) -> list[str]:
    """Distinct content words of the query, in order (stopwords and 1-letter tokens dropped)."""
    terms: list[str] = []
    for token in _TOKEN_RE.findall(query.casefold()):
        if token in _STOPWORDS or (len(token) < 2 and not token.isdigit()) or token in terms:
            continue
        terms.append(token)
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    return terms


def tsquery_from_terms(terms: list[str], op: str = " | ") -> str | None:
    """OR of the terms as a to_tsquery('simple', ...) string; None if there are none.
    OR, not plainto_tsquery's AND: a spoken question rarely contains every word of the passage that answers it,
    and ts_rank_cd already prefers the chunks that match more of the terms. Tokens are \\w+ only, so quoting
    them is enough to keep user text out of tsquery syntax."""
    return op.join(f"'{t}'" for t in terms) if terms else None


def build_tsquery(query: str) -> str | None:
    return tsquery_from_terms(query_terms(query))


# On a KB bigger than _SMALL_KB_CHUNKS a term found in >= _MAX_TERM_DF (2000) chunks is too common to discriminate and
# ranking every chunk it matches costs more than the lexical budget (a common-word query on 100k chunks took
# 0.4-2.7 s). Such terms are dropped and the rarest _MAX_LEXICAL_TERMS of the rest are kept. A query made only of
# common words keeps its 2 rarest (longest on a tie, document frequencies are capped) and ANDs them over at most
# _FALLBACK_CANDIDATES arbitrary matches, so the cost stays bounded; the caller is told via the
# "lexical_terms_dropped" note whenever any term was dropped.
_SMALL_KB_CHUNKS = 3000
_MAX_TERM_DF = int(os.environ.get("KNOWLEDGE_LEXICAL_MAX_TERM_DF", "2000"))
_MAX_LEXICAL_TERMS = 4
_MAX_TOTAL_DF = 6000  # bound on the rows ranked: the kept terms' document frequencies sum to at most this
_DF_TTL_S = 600.0
_DF_ABSENT_TTL_S = 60.0  # df == 0: re-check soon, a document containing the word may be ingested any minute
_FALLBACK_CANDIDATES = 2000

_DF_SQL = """
    SELECT t AS term, (SELECT count(*) FROM (
        SELECT 1 FROM kb_chunks c
        WHERE c.kb_id = ANY($2::uuid[]) AND NOT c.is_parent AND c.tsv @@ to_tsquery('simple', quote_literal(t))
        LIMIT $3) capped) AS df
    FROM unnest($1::text[]) AS t
"""

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
    LEFT JOIN kb_chunks p ON p.id = c.parent_id AND p.document_id = c.document_id
                         AND p.tenant_id = c.tenant_id AND p.is_parent
"""

_DENSE_SQL = f"""
    SELECT {_SELECT_COLUMNS}, 1 - (c.embedding <=> $1::vector) AS score
    FROM kb_chunks c {_DOC_JOINS}
    WHERE c.kb_id = ANY($2::uuid[]) AND c.embedding IS NOT NULL AND NOT c.is_parent
      AND (c.embedding <=> $1::vector) <= 1 - $5::float8  -- distance form: a zero vector gives NaN, which must not pass
      -- Only vectors made by the model the query was embedded with (legacy rows with no model still count): a plan
      -- that outlived a re-embed swap must not compare an old-model query with new-model vectors.
      AND ($6::text IS NULL OR c.embedding_model IS NULL OR c.embedding_model = $6::text)
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


# The capped variant (all-common-word queries) takes its candidate cap AFTER the document filters: soft delete keeps
# a document's chunks, so capping first could fill the candidate set with deleted/prompt/not-ready chunks and
# leave the live match out.
_LEXICAL_CAPPED_SQL = f"""
    WITH q AS (SELECT to_tsquery('simple', $1) AS tsq),
    hits AS (
        SELECT c.id, ts_rank_cd(c.tsv, q.tsq, 32) AS score
        FROM kb_chunks c
        JOIN kb_documents dd ON dd.id = c.document_id AND dd.deleted_at IS NULL AND dd.usage_mode = 'auto'
                            AND dd.status = ANY($4::text[]),
             q
        WHERE c.kb_id = ANY($2::uuid[]) AND NOT c.is_parent AND c.tsv @@ q.tsq
        LIMIT $6
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
        from .cache import TTLCache

        # Capped document frequency per (KB set, term). The key carries the KB UUIDs, so it is tenant-scoped.
        self._df = TTLCache(ttl_s=_DF_TTL_S, max_entries=20000)
        self._df_absent = TTLCache(ttl_s=_DF_ABSENT_TTL_S, max_entries=20000)
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

    async def _discriminating_terms(
        self, conn: asyncpg.Connection, kb_ids: list[str], terms: list[str],
    ) -> tuple[list[str], bool, int]:
        """(terms to search, True if they must be ANDed over a capped candidate set, how many were dropped)."""
        scope = ",".join(sorted(kb_ids))  # KB UUIDs: the key is tenant-scoped
        known: dict[str, int] = {}
        unknown: list[str] = []
        for term in terms:
            if self._df_absent.get(f"df:{scope}:{term}") is not None:
                known[term] = 0
                continue
            df = self._df.get(f"df:{scope}:{term}")
            if df is None:
                unknown.append(term)
            else:
                known[term] = df
        if unknown:  # one statement for all unseen terms; later queries hit the caches
            rows = await conn.fetch(_DF_SQL, unknown, kb_ids, _MAX_TERM_DF)
            for row in rows:
                known[row["term"]] = row["df"]
                (self._df if row["df"] > 0 else self._df_absent).set(f"df:{scope}:{row['term']}", row["df"])
        present = [t for t in terms if known.get(t, 0) > 0]  # df 0 matches nothing: no need to search it
        present.sort(key=lambda t: (known[t], -len(t)))
        usable = [t for t in present if known[t] < _MAX_TERM_DF]
        kept: list[str] = []
        total = 0
        for term in usable[:_MAX_LEXICAL_TERMS]:
            if kept and total + known[term] > _MAX_TOTAL_DF:
                break
            kept.append(term)
            total += known[term]
        if kept:
            return kept, False, len(present) - len(kept)
        if present:  # only common words: AND the 2 rarest over a capped candidate set instead of searching nothing
            return present[:2], True, len(present) - min(2, len(present))
        return [], False, 0

    async def search(
        self,
        conn: asyncpg.Connection,
        kb_ids: list[str],
        embedding: list[float],
        top_k: int,
        minimum_score: float,
        model: str | None = None,
        notes: list[str] | None = None,
    ) -> list[VectorMatch]:
        if not kb_ids:
            return []
        await self._prepare_hnsw(conn)
        vector_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
        rows = await conn.fetch(
            _DENSE_SQL, vector_literal, kb_ids, top_k, SEARCHABLE_STATUSES, minimum_score, model,
        )
        if not rows and model is not None and notes is not None:
            # Nothing came back: either nothing matched, or every chunk was excluded because the KB moved to
            # another model (a stale plan after a re-embed swap). One sampled row tells which, at index cost.
            sample = await conn.fetchval(
                "SELECT embedding_model FROM kb_chunks WHERE kb_id = ANY($1::uuid[]) AND NOT is_parent "
                "AND embedding IS NOT NULL LIMIT 1", kb_ids)
            if sample is not None and sample != model:
                notes.append("embedding_model_mismatch")
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
        approx_chunks: int | None = None,
        notes: list[str] | None = None,
    ) -> list[VectorMatch]:
        terms = query_terms(query)
        capped_and = False
        dropped = 0
        if kb_ids and terms and (approx_chunks is None or approx_chunks > _SMALL_KB_CHUNKS):
            terms, capped_and, dropped = await self._discriminating_terms(conn, kb_ids, terms)
        if (dropped or capped_and) and notes is not None:  # the fallback itself is a degradation, even with 1-2 terms
            notes.append("lexical_terms_dropped")
        tsquery = tsquery_from_terms(terms, " & " if capped_and else " | ")
        if not kb_ids or tsquery is None:  # nothing searchable in the query: no round trip
            return []
        if capped_and:
            rows = await conn.fetch(_LEXICAL_CAPPED_SQL, tsquery, kb_ids, limit, SEARCHABLE_STATUSES, minimum_score,
                                    _FALLBACK_CANDIDATES)
        else:
            rows = await conn.fetch(_LEXICAL_SQL, tsquery, kb_ids, limit, SEARCHABLE_STATUSES, minimum_score)
        return [_to_match(row, "lexical") for row in rows]
