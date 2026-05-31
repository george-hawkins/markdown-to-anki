"""Characterization tests for markdown_to_anki.

These lock in the *current* behavior of the pure (non-Anki) logic so that
refactoring cannot silently change output. A few assertions encode known
quirks; those are marked with a NOTE comment. They are intentionally
asserting "what the code does today", not "what would be ideal".

Run with:  ./venv/bin/python -m pytest
"""

import re

import pytest

import markdown_to_anki as mta


@pytest.fixture(autouse=True)
def anki_env():
    """Provide the global state the note parsers/formatters read.

    In production these are populated from AnkiConnect; here we stub them so
    the pure logic can run offline. Reset around every test to avoid leakage.
    """
    mta.App.FIELDS_DICT = {
        "Basic": ["Front", "Back"],
        "Cloze": ["Text", "Extra"],
    }
    mta.App.ADDED_MEDIA = []
    mta.MEDIA.clear()
    mta.CONFIG_DATA["CurlyCloze"] = False
    # The deck/tag defaults the template would otherwise inherit from config.
    mta.NOTE_DICT_TEMPLATE["tags"] = ["Markdown_to_Anki"]
    mta.NOTE_DICT_TEMPLATE["deckName"] = "Default"
    yield


# --------------------------------------------------------------------------
# string_insert
# --------------------------------------------------------------------------

def test_string_insert_basic():
    assert mta.string_insert("abc", [(0, "X"), (3, "Y")]) == "XabcY"


def test_string_insert_sorts_unordered_positions():
    # Inserts must be applied in position order regardless of input order.
    assert mta.string_insert("abcdef", [(5, "!"), (0, ">")]) == ">abcde!f"


def test_string_insert_accepts_zip_iterable():
    # Real callers pass a zip(); string_insert must handle a bare iterable.
    inserts = zip([0, 3], ["X", "Y"])
    assert mta.string_insert("abc", inserts) == "XabcY"


# --------------------------------------------------------------------------
# has_clozes / note_has_clozes
# --------------------------------------------------------------------------

def test_has_clozes_true():
    assert mta.has_clozes("see {{c1::this}}") is True


def test_has_clozes_false():
    assert mta.has_clozes("nothing here") is False


def test_note_has_clozes_checks_all_fields():
    assert mta.note_has_clozes({"fields": {"a": "plain", "b": "{{c1::x}}"}})
    assert not mta.note_has_clozes({"fields": {"a": "plain", "b": "also"}})


# --------------------------------------------------------------------------
# Math conversion
# --------------------------------------------------------------------------

def test_inline_and_block_math():
    out = mta.FormatConverter.markdown_to_anki_math(
        "Inline $x^2$ and block $$y=z$$"
    )
    assert out == r"Inline \(x^2\) and block \[y=z\]"


# --------------------------------------------------------------------------
# Cloze conversion
# --------------------------------------------------------------------------

def test_curly_to_cloze_plain_gets_c1():
    assert mta.FormatConverter.curly_to_cloze("{hello}") == "{{c1::hello}}"


def test_curly_to_cloze_explicit_number():
    assert mta.FormatConverter.curly_to_cloze("{2:keep}") == "{{c2::keep}}"


def test_curly_to_cloze_auto_increments():
    assert mta.FormatConverter.curly_to_cloze("{a} then {b}") == (
        "{{c1::a}} then {{c2::b}}"
    )


def test_curly_to_cloze_counter_resets_between_calls():
    # The unset-number counter must reset after each call, so independent
    # texts both start at c1.
    assert mta.FormatConverter.curly_to_cloze("{a}") == "{{c1::a}}"
    assert mta.FormatConverter.curly_to_cloze("{b}") == "{{c1::b}}"


def test_curly_to_cloze_explicit_then_auto_interaction():
    # NOTE: explicit ids do not advance the auto counter, so the following
    # {x}/{y} restart at c1/c2 -- this is the current (quirky) behavior.
    assert mta.FormatConverter.curly_to_cloze("{2:keep} {x} {y}") == (
        "{{c2::keep}} {{c1::x}} {{c2::y}}"
    )


# --------------------------------------------------------------------------
# is_url
# --------------------------------------------------------------------------

def test_is_url_true():
    assert mta.FormatConverter.is_url("https://example.com/a.png") is True


def test_is_url_false():
    assert mta.FormatConverter.is_url("local/a.png") is False


# --------------------------------------------------------------------------
# format() end-to-end (markdown -> Anki HTML)
# --------------------------------------------------------------------------

def test_format_strips_wrapping_paragraph_and_renders_bold():
    assert mta.FormatConverter.format("**bold** word") == (
        "<strong>bold</strong> word"
    )


def test_format_inline_code():
    assert mta.FormatConverter.format("use `code` here") == (
        "use <code>code</code> here"
    )


def test_format_inline_math_passthrough():
    assert mta.FormatConverter.format("eq $x^2$ done") == r"eq \(x^2\) done"


def test_format_block_math():
    assert mta.FormatConverter.format("$$a+b$$") == r"\[a+b\]"


def test_format_cloze_when_enabled():
    assert mta.FormatConverter.format("the {answer} is", cloze=True) == (
        "the {{c1::answer}} is"
    )


# --------------------------------------------------------------------------
# File.id_to_str
# --------------------------------------------------------------------------

def test_id_to_str_plain():
    assert mta.File.id_to_str(123) == "ID: 123\n"


def test_id_to_str_comment():
    assert mta.File.id_to_str(123, comment=True) == "<!--ID: 123-->\n"


def test_id_to_str_inline_comment():
    assert mta.File.id_to_str(123, inline=True, comment=True) == (
        "<!--ID: 123--> "
    )


# --------------------------------------------------------------------------
# Note parsing
# --------------------------------------------------------------------------

def test_note_parse_fields_id_tags_deck():
    note = mta.Note(
        "Basic\nFront: hello\nBack: world\nTags: a b\n<!--ID: 42-->"
    )
    parsed = note.parse("MyDeck")
    assert parsed.id == 42
    assert parsed.note["fields"] == {"Front": "hello", "Back": "world"}
    assert parsed.note["modelName"] == "Basic"
    assert parsed.note["deckName"] == "MyDeck"
    assert parsed.note["tags"] == ["Markdown_to_Anki", "a", "b"]


def test_note_parse_no_id_no_tags():
    note = mta.Note("Basic\nFront: q\nBack: a")
    parsed = note.parse("D")
    assert parsed.id is None
    assert parsed.note["fields"] == {"Front": "q", "Back": "a"}
    assert parsed.note["tags"] == ["Markdown_to_Anki"]


def test_note_continuation_line_appends_to_current_field():
    # A line that does not start with "Field:" continues the previous field.
    note = mta.Note("Basic\nFront: line one\nstill front\nBack: the back")
    parsed = note.parse("D")
    # nl2br turns the internal newline into a <br /> (XHTML-style output).
    assert parsed.note["fields"]["Front"] == "line one<br />\nstill front"
    assert parsed.note["fields"]["Back"] == "the back"


def test_note_cloze_enabled_via_config():
    mta.CONFIG_DATA["CurlyCloze"] = True
    note = mta.Note("Cloze\nText: the {answer} here")
    parsed = note.parse("D")
    assert parsed.note["fields"]["Text"] == "the {{c1::answer}} here"


def test_note_cloze_disabled_leaves_curly_untouched():
    mta.CONFIG_DATA["CurlyCloze"] = False
    note = mta.Note("Cloze\nText: the {answer} here")
    parsed = note.parse("D")
    assert parsed.note["fields"]["Text"] == "the {answer} here"


# --------------------------------------------------------------------------
# InlineNote parsing
# --------------------------------------------------------------------------

def test_inline_note_parse():
    inline = mta.InlineNote("[Basic] Front: q Back: a Tags: t1 <!--ID: 7-->")
    parsed = inline.parse("D")
    assert parsed.id == 7
    assert parsed.note["fields"] == {"Front": "q", "Back": "a"}
    assert parsed.note["modelName"] == "Basic"
    # NOTE: the trailing '' is a current quirk -- the captured tag string has a
    # trailing space (before the ID comment) which split(" ") turns into "".
    assert parsed.note["tags"] == ["Markdown_to_Anki", "t1", ""]


def test_inline_note_no_id_no_tags():
    inline = mta.InlineNote("[Basic] Front: q Back: a")
    parsed = inline.parse("D")
    assert parsed.id is None
    assert parsed.note["fields"] == {"Front": "q", "Back": "a"}


# --------------------------------------------------------------------------
# RegexNote parsing (regex-mode notes)
# --------------------------------------------------------------------------

def test_regexnote_basic_maps_groups_to_fields():
    mta.CONFIG_DATA["CurlyCloze"] = False
    match = re.compile(r"(.*)").match("frontval")
    parsed = mta.RegexNote(match, "Basic").parse("D")
    assert parsed.note["fields"] == {"Front": "frontval", "Back": ""}
    assert parsed.note["modelName"] == "Basic"


def test_regexnote_cloze_present_renders_cloze():
    mta.CONFIG_DATA["CurlyCloze"] = True
    match = re.compile(r"(.*)").match("this {answer} here")
    parsed = mta.RegexNote(match, "Cloze").parse("D")
    assert parsed.note["fields"]["Text"] == "this {{c1::answer}} here"


def test_regexnote_cloze_without_clozes_is_skipped():
    # A Cloze-type note with no actual cloze markers is skipped: parse()
    # returns None (the sentinel callers use to `continue`).
    mta.CONFIG_DATA["CurlyCloze"] = True
    match = re.compile(r"(.*)").match("just text, no clozes here")
    assert mta.RegexNote(match, "Cloze").parse("D") is None


# --------------------------------------------------------------------------
# AnkiConnect.parse
# --------------------------------------------------------------------------

def test_anki_parse_returns_result():
    assert mta.AnkiConnect.parse({"result": 42, "error": None}) == 42


def test_anki_parse_raises_on_error():
    with pytest.raises(mta.AnkiConnectError):
        mta.AnkiConnect.parse({"result": None, "error": "boom"})


def test_anki_parse_raises_on_malformed_response():
    with pytest.raises(mta.AnkiConnectError):
        mta.AnkiConnect.parse({"result": 1})  # missing 'error' field
