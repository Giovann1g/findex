"""Checks for the tokenizer rules documented in findex/tokenize.py."""

import functools
import inspect

from findex.tokenize import tokenize


def toks(text: str) -> list[str]:
    return list(tokenize(text))


def test_is_a_generator():
    assert inspect.isgeneratorfunction(tokenize)
    gen = tokenize("one two")
    assert next(gen) == "one"  # lazy: one token at a time


def test_mixed_case_casefold():
    assert toks("Hello WORLD hElLo") == ["hello", "world", "hello"]
    assert toks("Straße STRASSE") == ["strasse", "strasse"]  # lower() would fail


def test_cyrillic():
    assert toks("Привіт, Світе! Їжак і ґанок.") == ["привіт", "світе", "їжак", "і", "ґанок"]


def test_combining_mark_accent_nfc():
    composed = "café"  # é as one code point
    decomposed = "café"  # e + COMBINING ACUTE ACCENT
    assert composed != decomposed
    assert toks(composed) == toks(decomposed) == ["café"]


def test_stray_combining_mark_does_not_split_word():
    # Ukrainian stress mark has no precomposed form, NFC leaves it combining
    assert toks("мо́ва") == ["мова"]
    # й must survive (NFD would be и + U+0306)
    assert toks("йти край") == ["йти", "край"]


def test_punctuation_is_separator():
    assert toks("Hi!!! (yes), no; maybe... ok?") == ["hi", "yes", "no", "maybe", "ok"]


def test_apostrophes():
    assert toks("don't stop") == ["don't", "stop"]
    assert toks("п’ять м'ята пʼять") == ["п'ять", "м'ята", "п'ять"]  # 3 apostrophe forms
    assert toks("'quoted'") == ["quoted"]


def test_hyphens_split():
    assert toks("state-of-the-art") == ["state", "of", "the", "art"]
    assert toks("Івано-Франківськ") == ["івано", "франківськ"]


def test_digits_and_identifiers():
    assert toks("Python 3.13 in 2026, utf8") == ["python", "3", "13", "in", "2026", "utf8"]
    assert toks("sys_path __init__") == ["sys_path", "init"]


def test_empty_and_whitespace():
    assert toks("") == []
    assert toks("   \n\t  ") == []
    assert toks("—…!?") == []


# --- Lab 8: the faster normalization must be the same normalization ---------------
def test_combining_table_matches_this_unicode_version():
    import unicodedata

    from findex import _marks

    assert unicodedata.unidata_version == _marks.UNIDATA_VERSION
    assert _marks.scan() == _marks.RANGES  # regenerate with findex._marks if this fails


@functools.cache
def reference_marks() -> str:
    import re
    import sys
    import unicodedata

    return "".join(
        re.escape(chr(cp))
        for cp in range(0x0300, min(sys.maxunicode, 0xE01EF) + 1)
        if unicodedata.combining(chr(cp))
    )


def reference_normalize(text: str) -> str:
    """The Lab 1–7 algorithm, verbatim: scan the code points, sub the whole text."""
    import re
    import unicodedata

    marks = reference_marks()
    apostrophes: dict[int, str | None] = {ord(c): "'" for c in "’‘ʼ`"}
    apostrophes[0x00AD] = None
    text = unicodedata.normalize("NFC", text).casefold()
    if not text.isascii():
        text = text.translate(apostrophes)
        text = re.sub(f"[{marks}]+", "", text)
    return text


def test_fast_normalize_equals_the_reference():
    from hypothesis import given, settings
    from hypothesis import strategies as st

    from findex.tokenize import normalize

    alphabet = st.sampled_from(list("aZé́̆мо́\nİ’ ̇ß—­⃝日"))

    @settings(max_examples=300, deadline=None)
    @given(st.text(alphabet=alphabet, max_size=40) | st.text(max_size=40))
    def same(text: str) -> None:
        assert normalize(text) == reference_normalize(text)

    same()
