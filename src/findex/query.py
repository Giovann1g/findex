"""The query language: a recursive-descent parser that builds a tree of nodes.

Grammar, from the lowest precedence to the highest (``OR`` < ``AND`` < ``NOT``)::

    or_expr  := and_expr ("OR" and_expr)*
    and_expr := not_expr (["AND"] not_expr)*       # two operands side by side = AND
    not_expr := "NOT" not_expr | atom
    atom     := WORD | "quoted phrase" | "(" or_expr ")"

So ``a OR b c`` is ``Or(a, And(b, c))``: ``or_expr`` asks ``and_expr`` for its
operands, and ``and_expr`` swallows ``b c`` before control returns to the
``OR`` loop. Each grammar rule is one method; precedence *is* the call depth.

Operators are written in upper case (``and``/``or``/``not`` are plain words).
Words and phrases go through the corpus tokenizer, so ``Straße`` -> ``strasse``;
a word that splits into several tokens (``3.13``, ``state-of-the-art``) is the
AND of its parts. Phrases (``"event loop"``) need an index built with
``--positions``.

Nodes overload ``&``, ``|`` and ``~``, so the parser literally builds trees
with them, and tests compare trees, not strings::

    parse('a OR b c') == Term('a') | (Term('b') & Term('c'))
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from findex.merge import MERGE, Engine, intersect
from findex.tokenize import tokenize

if TYPE_CHECKING:
    from findex.index import Index

OPERATORS = frozenset({"AND", "OR", "NOT"})


class QuerySyntaxError(ValueError):
    """A malformed query, with the offending position for a caret display."""

    def __init__(self, message: str, query: str, pos: int) -> None:
        super().__init__(message)
        self.message, self.query, self.pos = message, query, pos

    def __str__(self) -> str:
        return f"{self.message}\n  {self.query}\n  {' ' * self.pos}^"


class PhraseNeedsPositions(ValueError):
    pass


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
class Node:
    """Base of the query tree. Subclasses are frozen slotted dataclasses, so
    nodes are immutable, hashable (usable as ``lru_cache`` keys) and compared
    structurally by the generated ``__eq__``."""

    __slots__ = ()

    def __and__(self, other: Node) -> And:
        return And(self, other)

    def __or__(self, other: Node) -> Or:
        return Or(self, other)

    def __invert__(self) -> Not:
        return Not(self)

    def evaluate(self, index: Index, engine: Engine = MERGE) -> Sequence[int]:
        """Sorted ids of the matching documents."""
        raise NotImplementedError

    def terms(self) -> frozenset[str]:
        """Terms that should contribute to the ranking score (not under NOT)."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True, repr=False)
class Term(Node):
    text: str

    def evaluate(self, index: Index, engine: Engine = MERGE) -> Sequence[int]:
        return index.doc_ids(self.text)

    def terms(self) -> frozenset[str]:
        return frozenset({self.text})

    def __repr__(self) -> str:
        return f"Term({self.text!r})"

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True, repr=False)
class Phrase(Node):
    words: tuple[str, ...]

    def evaluate(self, index: Index, engine: Engine = MERGE) -> Sequence[int]:
        if len(self.words) == 1:
            return index.doc_ids(self.words[0])
        if not index.has_positions:
            raise PhraseNeedsPositions(
                f"phrase {self} needs token positions: rebuild with "
                "`python -m findex.index ... --positions` (pickle or json)"
            )
        if any(w not in index for w in self.words):
            return []
        # 1. documents that contain every word
        docs: Sequence[int] = index.doc_ids(self.words[0])
        for w in self.words[1:]:
            docs = engine.and_(docs, index.doc_ids(w))
        if not docs:
            return []
        wanted = set(docs)
        # has_positions was checked above, so `positions` is never None here;
        # `or ()` turns the Optional into a plain tuple for the type checker.
        where = [
            {p.doc_id: p.positions or () for p in index[w] if p.doc_id in wanted}
            for w in self.words
        ]
        # 2. in each, a start p with word i at p + i: intersect the start
        #    candidates with every later word's positions shifted back by i —
        #    the same two-pointer merge as for doc ids.
        out: list[int] = []
        for d in docs:
            starts: Sequence[int] = where[0][d]
            for i in range(1, len(self.words)):
                starts = intersect(starts, [p - i for p in where[i][d]])
                if not starts:
                    break
            if starts:
                out.append(d)
        return out

    def terms(self) -> frozenset[str]:
        return frozenset(self.words)

    def __repr__(self) -> str:
        return f"Phrase({' '.join(self.words)!r})"

    def __str__(self) -> str:
        return f'"{" ".join(self.words)}"'


@dataclass(frozen=True, slots=True, repr=False)
class And(Node):
    left: Node
    right: Node

    def evaluate(self, index: Index, engine: Engine = MERGE) -> Sequence[int]:
        left, right = self.left, self.right
        # x AND NOT y = difference — no need to build the complement of y
        if isinstance(right, Not) and not isinstance(left, Not):
            return engine.not_(left.evaluate(index, engine), right.child.evaluate(index, engine))
        if isinstance(left, Not) and not isinstance(right, Not):
            return engine.not_(right.evaluate(index, engine), left.child.evaluate(index, engine))
        return engine.and_(left.evaluate(index, engine), right.evaluate(index, engine))

    def terms(self) -> frozenset[str]:
        return self.left.terms() | self.right.terms()

    def __repr__(self) -> str:
        return f"And({self.left!r}, {self.right!r})"

    def __str__(self) -> str:
        return f"({self.left} AND {self.right})"


@dataclass(frozen=True, slots=True, repr=False)
class Or(Node):
    left: Node
    right: Node

    def evaluate(self, index: Index, engine: Engine = MERGE) -> Sequence[int]:
        return engine.or_(self.left.evaluate(index, engine), self.right.evaluate(index, engine))

    def terms(self) -> frozenset[str]:
        return self.left.terms() | self.right.terms()

    def __repr__(self) -> str:
        return f"Or({self.left!r}, {self.right!r})"

    def __str__(self) -> str:
        return f"({self.left} OR {self.right})"


@dataclass(frozen=True, slots=True, repr=False)
class Not(Node):
    child: Node

    def evaluate(self, index: Index, engine: Engine = MERGE) -> Sequence[int]:
        everything = range(index.num_docs)  # doc ids are dense 0..N-1
        return engine.not_(everything, self.child.evaluate(index, engine))

    def terms(self) -> frozenset[str]:
        return frozenset()  # excluded words don't score

    def __repr__(self) -> str:
        return f"Not({self.child!r})"

    def __str__(self) -> str:
        return f"NOT {self.child}"


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------
_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "«": '"', "»": '"'})
_LEX_RE = re.compile(
    r"""\s*(?:
        (?P<lparen>\()
      | (?P<rparen>\))
      | "(?P<phrase>[^"]*)(?P<closed>")?
      | (?P<word>[^\s()"]+)
    )""",
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class Token:
    kind: str  # "(" | ")" | "AND" | "OR" | "NOT" | "WORD" | "PHRASE"
    pos: int
    terms: tuple[str, ...] = ()


def lex(query: str) -> Iterator[Token]:
    query = query.translate(_QUOTES)
    pos = 0
    while pos < len(query):
        m = _LEX_RE.match(query, pos)
        if m is None or m.end() == pos:  # only trailing whitespace left
            break
        start = m.start(m.lastgroup) if m.lastgroup else pos
        if m["lparen"]:
            yield Token("(", start)
        elif m["rparen"]:
            yield Token(")", start)
        elif m["phrase"] is not None:
            if m["closed"] is None:
                raise QuerySyntaxError("unclosed quote", query, start - 1)
            words = tuple(tokenize(m["phrase"]))
            if words:  # an empty or all-punctuation phrase is skipped
                yield Token("PHRASE", start - 1, words)
        elif (word := m["word"]) in OPERATORS:
            yield Token(word, start)
        else:
            words = tuple(tokenize(word))
            if words:  # pure punctuation ("—", "...") is not a search word
                yield Token("WORD", start, words)
        pos = m.end()


# ---------------------------------------------------------------------------
# Parser: one method per grammar rule
# ---------------------------------------------------------------------------
class _Parser:
    def __init__(self, query: str) -> None:
        self.query = query
        self.tokens = list(lex(query))
        self.i = 0

    def peek(self) -> str | None:
        return self.tokens[self.i].kind if self.i < len(self.tokens) else None

    def take(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def error(self, message: str) -> QuerySyntaxError:
        pos = self.tokens[self.i].pos if self.i < len(self.tokens) else len(self.query)
        return QuerySyntaxError(message, self.query, pos)

    def parse(self) -> Node | None:
        if not self.tokens:
            return None
        node = self.or_expr()
        if self.peek() is not None:  # only a stray ")" can stop or_expr early
            raise self.error("unmatched ')'")
        return node

    def or_expr(self) -> Node:
        node = self.and_expr()
        while self.peek() == "OR":
            self.take()
            node = node | self.and_expr()
        return node

    def and_expr(self) -> Node:
        node = self.not_expr()
        while self.peek() not in (None, "OR", ")"):
            if self.peek() == "AND":
                self.take()
            node = node & self.not_expr()
        return node

    def not_expr(self) -> Node:
        if self.peek() == "NOT":
            self.take()
            return ~self.not_expr()
        return self.atom()

    def atom(self) -> Node:
        kind = self.peek()
        if kind == "(":
            self.take()
            if self.peek() == ")":
                raise self.error("empty parentheses")
            node = self.or_expr()
            if self.peek() != ")":
                raise self.error("expected ')'")
            self.take()
            return node
        if kind == "WORD":
            words = self.take().terms
            node: Node = Term(words[0])
            for w in words[1:]:  # "3.13" -> 3 AND 13
                node = node & Term(w)
            return node
        if kind == "PHRASE":
            return Phrase(self.take().terms)
        if kind is None:
            raise self.error("query ends where a word, phrase or '(' was expected")
        raise self.error(f"expected a word, phrase or '(' but found {kind!r}")


def parse(query: str) -> Node | None:
    """Parse a query string into a tree; ``None`` for an empty query."""
    return _Parser(query).parse()
