from __future__ import annotations


class ConfigSDKError(Exception):
    """Base class — callers that want to catch anything from this package
    can catch this rather than enumerating every concrete subclass."""


class RepositoryUnavailableError(ConfigSDKError):
    """Raised by a repository when its transport is unreachable (Redis down,
    Config Service unreachable/5xx). CacheAsideConfigProvider catches this
    internally and falls through to the next repository in the chain — it
    should never escape to a caller of IConfigProvider. Exists as a distinct
    type (not a bare Exception catch) so a repository's "I couldn't reach my
    backend" is never confused with "the config genuinely doesn't exist"
    (which is None, not an exception — matching every other lookup in this
    codebase's convention)."""
