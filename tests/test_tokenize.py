"""Checks for the tokenizer rules documented in findex/tokenize.py."""

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
