from __future__ import annotations


class KnowledgeSDKError(Exception):
    """Base class — callers that want to catch anything from this package
    can catch this rather than enumerating every concrete subclass."""


class RepositoryUnavailableError(KnowledgeSDKError):
    """Raised by a repository when its transport is unreachable (Redis down,
    Knowledge Service unreachable/5xx). CacheAsideKnowledgeProvider catches
    this internally — a retrieval-plane outage degrades to "no context
    injected" (None), the same as "no eligible KB", rather than failing the
    caller's turn. See providers/cache_aside.py."""
