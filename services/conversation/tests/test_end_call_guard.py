"""The [[END_CALL]] guard's pure decision, against the deployed phrase tables."""

from __future__ import annotations

import pytest

from libs.config_sdk.languages import LANGUAGES

from ..i18n import has_phrase_tables, t
from ..pipeline import _end_call_rejection, _phrase_tokens


def test_phrase_tokens_pads_and_normalises():
    assert _phrase_tokens("  That’s ALL, folks! ") == " that's all folks "
    assert _phrase_tokens("बस इतना ही।") == " बस इतना ही "


@pytest.mark.parametrize("reply", ["Anything else?", "Anything else？", "کیا کچھ اور؟", '"Anything else?"'])
def test_a_question_vetoes_even_a_closing_caller(reply):
    assert _end_call_rejection(reply, "goodbye", "en") == "question"


def test_only_the_last_sentence_decides_the_question_veto():
    assert _end_call_rejection("Anything else? Bye.", "ok", "en") is None


@pytest.mark.parametrize(
    "reply, caller, expected",
    [
        ("Your booking is confirmed. Goodbye.", "I want to book", None),
        ("Your booking is confirmed.", "I want to book", "no_closure"),
        ("All set.", "that's all", None),
        ("Take care now", "thanks", None),
        ("", "Okay, bye-bye.", None),
        ("Thanks for calling!", "I want to book", None),
        ("All set\u2014goodbye!", "I want to book", None),
        ("All set\u2013goodbye", "I want to book", None),
        ("Done (goodbye)", "I want to book", None),
    ],
)
def test_english_closure_and_farewell(reply, caller, expected):
    assert _end_call_rejection(reply, caller, "en") == expected


@pytest.mark.parametrize(
    "language, reply, caller, expected",
    [
        ("hi", "", "बस इतना ही", None),
        ("hi", "All set.", "that's all", None),
        ("hi", "All set.", "tell me more", "no_closure"),
        ("hi", t("fallback_goodbye", "hi"), "मुझे बुकिंग करनी है", None),
        ("hi", "धन्यवाद, आपका दिन शुभ हो।", "मुझे बुकिंग करनी है", None),
        ("hi", "आपकी बुकिंग हो गई।", "नमस्ते, मुझे बुकिंग करनी है", "no_closure"),
    ],
)
def test_language_tables_and_english_union(language, reply, caller, expected):
    assert _end_call_rejection(reply, caller, language) == expected


def test_an_empty_reply_to_a_non_closing_caller_is_marker_only():
    assert _end_call_rejection("", "I want to book", "en") == "marker_only"


@pytest.mark.parametrize(
    "reply, caller",
    [
        ("Your appointment is booked for Tuesday at 3. Have a wonderful day!", "Great, thanks."),
        ("Your appointment is booked.", "Okay thanks"),
        ("Your appointment is booked. Talk to you soon.", "I want to book"),
        ("Your appointment is booked. Have a lovely day.", "I want to book"),
        ("Your appointment is booked. See you then.", "I want to book"),
    ],
)
def test_english_thanks_and_wider_farewells_end_the_call(reply, caller):
    assert _end_call_rejection(reply, caller, "en") is None


def test_a_thanking_caller_does_not_defeat_the_question_veto():
    assert _end_call_rejection("Anything else I can help with?", "Great, thanks.", "en") == "question"


# Registered languages with no phrase table: the guard trusts the marker (see _end_call_rejection).
_FAIL_OPEN_LANGUAGES = {"es", "fr", "de", "pt", "it", "ja", "zh"}


def test_every_registered_language_has_a_phrase_table_or_fails_open():
    unaccounted = {
        code for code in LANGUAGES if not has_phrase_tables(code) and code not in _FAIL_OPEN_LANGUAGES
    }
    assert not unaccounted, f"add a phrase table or list in _FAIL_OPEN_LANGUAGES: {sorted(unaccounted)}"
    # A fail-open entry that gained a table, or left the registry, is stale.
    assert all(code in LANGUAGES and not has_phrase_tables(code) for code in _FAIL_OPEN_LANGUAGES)


@pytest.mark.parametrize(
    "language, reply, caller",
    [
        ("es", "Su cita está confirmada. Adiós.", "quiero reservar"),
        ("es", "Gracias por llamar.", "quiero reservar"),
        ("fr", "Votre rendez-vous est confirmé. Au revoir.", "je veux réserver"),
        ("fr", "Merci d'avoir appelé.", "je veux réserver"),
        ("de", "Ihr Termin ist bestätigt. Auf Wiedersehen.", "ich möchte buchen"),
        ("de", "Danke für Ihren Anruf.", "ich möchte buchen"),
        ("pt", "Sua consulta está confirmada. Obrigado por ligar.", "quero marcar"),
        ("ja", "ご予約を承りました。お電話ありがとうございました。", "予約したいです"),
        ("zh", "您的预约已确认。感谢来电，再见。", "我想预约"),
    ],
)
def test_languages_without_a_table_honour_a_native_goodbye(language, reply, caller):
    assert _end_call_rejection(reply, caller, language) is None


@pytest.mark.parametrize(
    "language, reply",
    [
        ("es", "¿Hay algo más en lo que pueda ayudarle?"),
        ("fr", "Puis-je vous aider avec autre chose ?"),
        ("de", "Kann ich sonst noch etwas für Sie tun?"),
        ("ja", "他にご用件はございますか？"),
        ("zh", "还有其他需要帮助的吗？"),
    ],
)
def test_languages_without_a_table_keep_the_question_veto(language, reply):
    assert _end_call_rejection(reply, "adiós", language) == "question"


def test_a_closure_earlier_in_the_caller_turn_does_not_count():
    caller = "Thank you. I also want to reschedule my Tuesday appointment."
    assert _end_call_rejection("Okay, I have noted that.", caller, "en") == "no_closure"
    assert _end_call_rejection("Okay, I have noted that.", "Great, thanks.", "en") is None


def test_an_unknown_language_trusts_the_marker_after_the_question_veto():
    assert _end_call_rejection("Okay, noted.", "I want to book", None) is None
    assert _end_call_rejection("Anything else?", "adiós", None) == "question"
