"""The per-TTS-instance cache for fixed phrases (greeting, fillers)."""

from __future__ import annotations

from .. import phrase_audio


class _Tts:
    pass


def test_a_stored_phrase_is_replayed_for_the_same_instance_language_and_rate():
    tts = _Tts()
    phrase_audio.store(tts, ("en", 16000, "Mm-hmm, one moment."), [b"\x01\x02", b"\x03"])

    assert phrase_audio.lookup(tts, ("en", 16000, "Mm-hmm, one moment.")) == [b"\x01\x02", b"\x03"]


def test_a_different_text_language_or_rate_is_a_miss():
    tts = _Tts()
    phrase_audio.store(tts, ("en", 16000, "Hello."), [b"\x01"])

    assert phrase_audio.lookup(tts, ("hi", 16000, "Hello.")) is None
    assert phrase_audio.lookup(tts, ("en", 8000, "Hello.")) is None
    assert phrase_audio.lookup(tts, ("en", 16000, "Hello!")) is None


def test_another_tts_instance_never_sees_this_instances_audio():
    a, b = _Tts(), _Tts()
    phrase_audio.store(a, (None, 16000, "Hello."), [b"\x01"])

    assert phrase_audio.lookup(b, (None, 16000, "Hello.")) is None


def test_empty_audio_is_not_stored_because_it_would_replay_silence():
    tts = _Tts()
    phrase_audio.store(tts, (None, 16000, "Hello."), [])
    phrase_audio.store(tts, (None, 16000, "Hi."), [b"", b""])

    assert len(phrase_audio._caches.get(tts, ())) == 0


def test_the_oldest_phrase_is_dropped_past_the_bound(monkeypatch):
    monkeypatch.setattr(phrase_audio, "MAX_PHRASES_PER_TTS", 2)
    tts = _Tts()
    for text in ("one", "two", "three"):
        phrase_audio.store(tts, (None, 16000, text), [text.encode()])

    assert phrase_audio.lookup(tts, (None, 16000, "one")) is None
    assert phrase_audio.lookup(tts, (None, 16000, "three")) == [b"three"]


def test_a_lookup_refreshes_the_phrase_so_a_used_one_survives_eviction(monkeypatch):
    monkeypatch.setattr(phrase_audio, "MAX_PHRASES_PER_TTS", 2)
    tts = _Tts()
    phrase_audio.store(tts, (None, 16000, "one"), [b"1"])
    phrase_audio.store(tts, (None, 16000, "two"), [b"2"])
    phrase_audio.lookup(tts, (None, 16000, "one"))
    phrase_audio.store(tts, (None, 16000, "three"), [b"3"])

    assert phrase_audio.lookup(tts, (None, 16000, "one")) == [b"1"]
    assert phrase_audio.lookup(tts, (None, 16000, "two")) is None


def test_the_returned_list_is_a_copy_so_a_caller_cannot_corrupt_the_cache():
    tts = _Tts()
    phrase_audio.store(tts, (None, 16000, "Hi."), [b"\x01"])
    phrase_audio.lookup(tts, (None, 16000, "Hi.")).append(b"junk")

    assert phrase_audio.lookup(tts, (None, 16000, "Hi.")) == [b"\x01"]


def test_the_cache_dies_with_the_tts_instance():
    import gc
    tts = _Tts()
    phrase_audio.store(tts, (None, 16000, "Hi."), [b"\x01"])
    assert len(phrase_audio._caches.get(tts, ())) == 1
    del tts
    gc.collect()

    assert len(phrase_audio._caches) == 0


def test_a_tts_that_cannot_be_weakly_referenced_is_simply_not_cached():
    class Slotted:
        __slots__ = ()

    tts = Slotted()
    phrase_audio.store(tts, (None, 16000, "Hi."), [b"\x01"])
    assert phrase_audio.lookup(tts, (None, 16000, "Hi.")) is None


async def test_a_second_caller_waits_for_the_synthesis_already_running():
    tts = _Tts()
    key = (None, 16000, "One moment.")
    flight = phrase_audio.begin(tts, key)
    assert phrase_audio.in_flight(tts, key) is flight
    phrase_audio.finish(tts, key, flight, [b"\x01"])

    assert await flight == [b"\x01"]
    assert phrase_audio.in_flight(tts, key) is None
    assert phrase_audio.lookup(tts, key) == [b"\x01"]


async def test_a_failed_synthesis_wakes_waiters_with_nothing_and_caches_nothing():
    tts = _Tts()
    key = (None, 16000, "One moment.")
    flight = phrase_audio.begin(tts, key)
    phrase_audio.finish(tts, key, flight, None)

    assert await flight is None
    assert phrase_audio.lookup(tts, key) is None
