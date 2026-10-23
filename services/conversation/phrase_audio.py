"""Audio for fixed spoken phrases (the greeting and the fillers), kept per TTS instance.

For one TTS instance the voice, model and speed are fixed when it is built, so a phrase's audio depends only on
its text, the language and the sample rate. It is synthesized once and replayed, which takes a repeat call's
greeting and a filler off the network. The cache lives and dies with the instance: a provider config change builds
a new instance, which starts empty, so a stale voice is never replayed.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections import OrderedDict
from typing import Any

from .pipeline_config import env_count

# Per TTS instance. A phrase is a few tens of KB at 16 kHz; the bound keeps a greeting with call variables
# (which makes many distinct texts) from growing without limit.
MAX_PHRASES_PER_TTS = env_count("VOICEAI_PHRASE_CACHE_ENTRIES", 64)

_caches: "weakref.WeakKeyDictionary[Any, OrderedDict[tuple, list[bytes]]]" = weakref.WeakKeyDictionary()
# Phrases being synthesized right now, so concurrent calls on a cold cache share one request.
_in_flight: "weakref.WeakKeyDictionary[Any, dict[tuple, asyncio.Future]]" = weakref.WeakKeyDictionary()
_lock = threading.Lock()

PhraseKey = tuple  # (language or None, sample_rate, text)


def lookup(tts: Any, key: PhraseKey) -> list[bytes] | None:
    """The cached chunks for this phrase on this TTS instance, or None."""
    with _lock:
        try:
            cache = _caches.get(tts)
        except TypeError:
            return None  # an object that cannot be weakly referenced is never cached
        if cache is None or key not in cache:
            return None
        cache.move_to_end(key)
        return list(cache[key])


def store(tts: Any, key: PhraseKey, chunks: list[bytes]) -> None:
    """Remember a fully synthesized phrase. An empty result is never stored: it would replay silence."""
    if not chunks or not any(chunks):
        return
    with _lock:
        try:
            cache = _caches.setdefault(tts, OrderedDict())
        except TypeError:
            return  # an object that cannot be weakly referenced is simply not cached
        cache[key] = list(chunks)
        cache.move_to_end(key)
        while len(cache) > MAX_PHRASES_PER_TTS:
            cache.popitem(last=False)


def in_flight(tts: Any, key: PhraseKey) -> "asyncio.Future | None":
    """Another caller's synthesis of this phrase, still running; it resolves to the chunks, or None if it failed."""
    with _lock:
        try:
            return _in_flight.get(tts, {}).get(key)
        except TypeError:
            return None


def begin(tts: Any, key: PhraseKey) -> "asyncio.Future | None":
    """Claim this phrase's synthesis; pass the result to finish(). None when the instance can't be tracked."""
    future = asyncio.get_running_loop().create_future()
    with _lock:
        try:
            _in_flight.setdefault(tts, {})[key] = future
        except TypeError:
            return None
    return future


def finish(tts: Any, key: PhraseKey, future: "asyncio.Future | None", chunks: list[bytes] | None) -> None:
    """Store a complete synthesis (None when it failed or was cut short) and wake whoever waited on it."""
    if chunks is not None:
        store(tts, key, chunks)
    if future is None:
        return
    with _lock:
        flights = _in_flight.get(tts)
        if flights is not None and flights.get(key) is future:
            del flights[key]
    if not future.done():
        future.set_result(list(chunks) if chunks and any(chunks) else None)
