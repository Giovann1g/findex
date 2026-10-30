"""Lab 3: the query parser (trees, not strings) and node evaluation."""

from pathlib import Path

import pytest

from findex.corpus import Document
from findex.index import build_index
from findex.merge import MERGE, SET
from findex.query import (
    And,
    Or,
    Phrase,
    PhraseNeedsPositions,
    QuerySyntaxError,
    Term,
    parse,
)

a, b, c, d = Term("a"), Term("b"), Term("c"), Term("d")


# --- trees ------------------------------------------------------------------
def test_lab_example():
    assert parse("a OR b c") == Or(Term("a"), And(Term("b"), Term("c")))
    assert parse("a OR b c") != And(Or(a, b), c)


@pytest.mark.parametrize(
    ("query", "tree"),
    [
        ("a", a),
        ("a b", a & b),
        ("a AND b", a & b),
        ("a b c", (a & b) & c),  # left-associative, like Python's &
        ("a OR b OR c", (a | b) | c),
        ("a OR b c", a | (b & c)),  # AND binds tighter than OR
        ("a b OR c", (a & b) | c),
        ("(a OR b) c", (a | b) & c),
        ("NOT a", ~a),
        ("NOT a b", ~a & b),  # NOT binds tighter than AND
        ("NOT (a b)", ~(a & b)),
        ("NOT NOT a", ~~a),
        ("a NOT b", a & ~b),
        ("a OR NOT b", a | ~b),
        ('"a b"', Phrase(("a", "b"))),
        ('a "b c" d', (a & Phrase(("b", "c"))) & d),
        ("((a))", a),
        ("a (b OR (c d))", a & (b | (c & d))),
    ],
)
def test_parse_trees(query, tree):
    assert parse(query) == tree


def test_full_lab_query():
    q = 'python AND (async OR await) NOT java "event loop"'
    py, asy, aw, java = map(Term, ["python", "async", "await", "java"])
    assert parse(q) == ((py & (asy | aw)) & ~java) & Phrase(("event", "loop"))


def test_words_are_normalized_like_the_corpus():
    assert parse("Straße CAFÉ") == Term("strasse") & Term("café")
    assert parse("and or not") == (Term("and") & Term("or")) & Term("not")  # lower = words
    assert parse("3.13") == Term("3") & Term("13")
    assert parse("“event loop”") == Phrase(("event", "loop"))  # typographic quotes
    assert parse('"State-of-the-art"') == Phrase(("state", "of", "the", "art"))


def test_empty_and_punctuation():
    assert parse("") is None
    assert parse("   ") is None
    assert parse("— ...") is None
    assert parse('a "" b') == a & b


@pytest.mark.parametrize(
    ("query", "pos"),
    [("a OR", 4), ("(a", 2), ("a)", 1), ("()", 1), ("NOT", 3), ('"open', 0), ("a AND OR b", 6)],
)
def test_syntax_errors_point_at_the_problem(query, pos):
    with pytest.raises(QuerySyntaxError) as err:
        parse(query)
    assert err.value.pos == pos
    assert str(err.value).splitlines()[-1] == "  " + " " * pos + "^"


def test_nodes_are_hashable_values():
    assert len({parse("a b"), parse("a AND b"), a & b}) == 1
    assert hash(parse("(a OR b) NOT c")) == hash((a | b) & ~c)
    assert repr(parse("a OR b c")) == "Or(Term('a'), And(Term('b'), Term('c')))"
    assert str(parse('a OR b NOT "c d"')) == '(a OR (b AND NOT "c d"))'


def test_scoring_terms_skip_negations():
    assert parse('a (b OR "c d") NOT e').terms() == {"a", "b", "c", "d"}


# --- evaluation ---------------------------------------------------------------
TEXTS = [
    "the event loop runs tasks",  # 0
    "loop over every event",  # 1: both words, not as a phrase
    "an event loop and another event loop",  # 2
    "python async await",  # 3
]


@pytest.fixture
def index():
    docs = (Document(f"d{i}", Path(f"d{i}"), t) for i, t in enumerate(TEXTS))
    return build_index(docs, positions=True)


@pytest.mark.parametrize("engine", [MERGE, SET], ids=["merge", "set"])
def test_evaluate(index, engine):
    def ev(q):
        return list(parse(q).evaluate(index, engine))

    assert ev("event loop") == [0, 1, 2]
    assert ev('"event loop"') == [0, 2]
    assert ev('"loop event"') == []
    assert ev('"event loop runs"') == [0]
    assert ev('"loop" NOT "event loop"') == [1]
    assert ev("NOT event") == [3]
    assert ev("async OR runs") == [0, 3]
    assert ev("(async OR runs) NOT python") == [0]
    assert ev('"no such phrase"') == []


def test_phrase_needs_positions():
    docs = (Document(f"d{i}", Path(f"d{i}"), t) for i, t in enumerate(TEXTS))
    plain = build_index(docs)
    with pytest.raises(PhraseNeedsPositions):
        parse('"event loop"').evaluate(plain)
    assert list(parse('"loop"').evaluate(plain)) == [0, 1, 2]  # one word: no positions needed
