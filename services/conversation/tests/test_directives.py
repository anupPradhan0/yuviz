from services.conversation.directives import DirectiveParser, EndCallDirective, StreamBuffer


def test_parse_strips_markdown_characters():
    result = DirectiveParser.parse("- **Date:** Tomorrow\n- **Time:** 10:00 AM")
    assert "*" not in result.clean_text
    assert result.clean_text == "- Date: Tomorrow\n- Time: 10:00 AM"


def test_parse_strips_markdown_and_directives_together():
    result = DirectiveParser.parse("**Confirmed.** [[END_CALL]]")
    assert result.clean_text == "Confirmed. "
    assert result.directives == [EndCallDirective()]


def test_streambuffer_split_markdown_marker_across_chunks():
    buf = StreamBuffer()
    safe = buf.feed("The time is *") + buf.feed("*10:00 AM**")
    result = DirectiveParser.parse(safe + buf.flush())
    assert "*" not in result.clean_text
    assert result.clean_text == "The time is 10:00 AM"
