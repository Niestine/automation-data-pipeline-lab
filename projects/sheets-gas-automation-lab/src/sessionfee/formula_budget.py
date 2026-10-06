"""Local budget for staging formulas the planner is allowed to emit.

The gates are local policy, not cutoffs from the understandability study:
parse-tree height at most 2, at most one reference group, and no conditional
operation. The raw reference count is measured and is not a gate. A separate
local ceiling of 4 applies to the longest formula-to-formula chain.

Python functions are outside this budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LOCAL_MAX_HEIGHT = 2
LOCAL_MAX_RANGES = 1
LOCAL_CHAIN_CEILING = 4

_CONDITIONAL_FUNCTIONS = frozenset({"IF", "IFS", "AND", "OR", "NOT", "SWITCH"})


class FormulaSyntaxError(Exception):
    """Raised when a staging formula is outside the small parsed subset."""


class FormulaCycle(Exception):
    """Raised when staging formulas refer to each other in a cycle."""


@dataclass(frozen=True)
class FormulaMetrics:
    reference_count: int
    range_count: int
    conditional: bool
    height: int
    accepted: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class ChainResult:
    per_formula_accepted: bool
    length: int
    ceiling: int
    ceiling_accepted: bool


@dataclass
class _Node:
    kind: str
    height: int = 0
    conditional: bool = False
    range_count: int = 0
    reference_count: int = 0
    refs: tuple[str, ...] = ()
    children: tuple["_Node", ...] = field(default_factory=tuple)


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.length = len(text)
        self.index = 0

    def parse(self) -> _Node:
        self._skip()
        if self.index >= self.length or self.text[self.index] != "=":
            raise FormulaSyntaxError("staging formula must start with '='")
        self.index += 1
        node = self._comparison()
        self._skip()
        if self.index != self.length:
            raise FormulaSyntaxError(f"unexpected trailing text at {self.index}")
        return node

    def _skip(self) -> None:
        while self.index < self.length and self.text[self.index].isspace():
            self.index += 1

    def _comparison(self) -> _Node:
        left = self._term()
        self._skip()
        for operator in (">=", "<=", "<>", ">", "<", "="):
            if self.text.startswith(operator, self.index):
                self.index += len(operator)
                right = self._term()
                return _binary(operator, left, right)
        return left

    def _term(self) -> _Node:
        node = self._factor()
        while True:
            self._skip()
            if self.index < self.length and self.text[self.index] in "+-":
                operator = self.text[self.index]
                self.index += 1
                right = self._factor()
                node = _binary(operator, node, right)
                continue
            return node

    def _factor(self) -> _Node:
        node = self._unary()
        while True:
            self._skip()
            if self.index < self.length and self.text[self.index] in "*/":
                operator = self.text[self.index]
                self.index += 1
                right = self._unary()
                node = _binary(operator, node, right)
                continue
            return node

    def _unary(self) -> _Node:
        self._skip()
        if self.index < self.length and self.text[self.index] == "-":
            self.index += 1
            child = self._unary()
            return _Node(
                kind="unary",
                height=child.height + 1,
                conditional=child.conditional,
                range_count=child.range_count,
                reference_count=child.reference_count,
                refs=child.refs,
                children=(child,),
            )
        return self._primary()

    def _primary(self) -> _Node:
        self._skip()
        if self.index >= self.length:
            raise FormulaSyntaxError("unexpected end of formula")
        current = self.text[self.index]
        if current == "(":
            self.index += 1
            node = self._comparison()
            self._skip()
            if self.index >= self.length or self.text[self.index] != ")":
                raise FormulaSyntaxError("missing ')'")
            self.index += 1
            return node
        if current == '"':
            return self._string()
        if current.isdigit():
            return self._number()
        if current == "$" or current.isalpha():
            return self._identifier_or_ref()
        raise FormulaSyntaxError(f"unexpected character {current!r}")

    def _string(self) -> _Node:
        self.index += 1
        while self.index < self.length and self.text[self.index] != '"':
            self.index += 1
        if self.index >= self.length:
            raise FormulaSyntaxError("unterminated string")
        self.index += 1
        return _Node(kind="string", height=0)

    def _number(self) -> _Node:
        start = self.index
        while self.index < self.length and self.text[self.index].isdigit():
            self.index += 1
        if self.index < self.length and self.text[self.index] == ".":
            self.index += 1
            while self.index < self.length and self.text[self.index].isdigit():
                self.index += 1
        if start == self.index:
            raise FormulaSyntaxError("expected number")
        return _Node(kind="number", height=0)

    def _identifier_or_ref(self) -> _Node:
        if self.text[self.index] == "$":
            return self._cell_or_range()
        start = self.index
        while self.index < self.length and self.text[self.index].isalpha():
            self.index += 1
        letters = self.text[start:self.index]
        saved = self.index
        if self.index < self.length and self.text[self.index] == "$":
            self.index = start
            return self._cell_or_range()
        if self.index < self.length and self.text[self.index].isdigit():
            self.index = start
            return self._cell_or_range()
        self.index = saved
        self._skip()
        if self.index < self.length and self.text[self.index] == "(":
            return self._function(letters.upper())
        raise FormulaSyntaxError(f"unexpected name {letters}")

    def _function(self, name: str) -> _Node:
        self.index += 1
        args: list[_Node] = []
        self._skip()
        if self.index < self.length and self.text[self.index] != ")":
            while True:
                args.append(self._comparison())
                self._skip()
                if self.index < self.length and self.text[self.index] == ",":
                    self.index += 1
                    continue
                break
        if self.index >= self.length or self.text[self.index] != ")":
            raise FormulaSyntaxError(f"missing ')' after {name}")
        self.index += 1
        conditional = name in _CONDITIONAL_FUNCTIONS or any(arg.conditional for arg in args)
        return _Node(
            kind="call",
            height=1 + max((arg.height for arg in args), default=0),
            conditional=conditional,
            range_count=sum(arg.range_count for arg in args),
            reference_count=sum(arg.reference_count for arg in args),
            refs=tuple(ref for arg in args for ref in arg.refs),
            children=tuple(args),
        )

    def _cell_or_range(self) -> _Node:
        start_cell = self._cell()
        self._skip()
        if self.index < self.length and self.text[self.index] == ":":
            self.index += 1
            end_cell = self._cell()
            return _Node(
                kind="range",
                height=0,
                range_count=1,
                reference_count=_cells_in_range(start_cell, end_cell),
                refs=(start_cell, end_cell),
            )
        return _Node(kind="ref", height=0, range_count=1, reference_count=1, refs=(start_cell,))

    def _cell(self) -> str:
        self._skip()
        if self.index < self.length and self.text[self.index] == "$":
            self.index += 1
        start = self.index
        while self.index < self.length and self.text[self.index].isalpha():
            self.index += 1
        letters = self.text[start:self.index].upper()
        if not letters:
            raise FormulaSyntaxError("expected a cell reference")
        if self.index < self.length and self.text[self.index] == "$":
            self.index += 1
        row_start = self.index
        while self.index < self.length and self.text[self.index].isdigit():
            self.index += 1
        if row_start == self.index:
            raise FormulaSyntaxError("expected a row number")
        row = int(self.text[row_start:self.index])
        if row < 1:
            raise FormulaSyntaxError("row numbers start at 1")
        return f"{letters}{row}"


def _binary(operator: str, left: _Node, right: _Node) -> _Node:
    return _Node(
        kind="binop",
        height=1 + max(left.height, right.height),
        conditional=left.conditional or right.conditional,
        range_count=left.range_count + right.range_count,
        reference_count=left.reference_count + right.reference_count,
        refs=left.refs + right.refs,
        children=(left, right),
    )


def _column_index(letters: str) -> int:
    index = 0
    for character in letters:
        index = index * 26 + (ord(character) - 64)
    return index


def _split_cell(cell: str) -> tuple[int, int]:
    letters = "".join(character for character in cell if character.isalpha())
    row = int("".join(character for character in cell if character.isdigit()))
    return _column_index(letters), row


def _cells_in_range(start: str, end: str) -> int:
    left_column, left_row = _split_cell(start)
    right_column, right_row = _split_cell(end)
    return (abs(right_column - left_column) + 1) * (abs(right_row - left_row) + 1)


def parse_formula(formula: str) -> _Node:
    return _Parser(formula).parse()


def evaluate_formula(formula: str) -> FormulaMetrics:
    """Measure one formula and apply the local per-formula gates.

    A syntax error is a rejection. Reference count is reported and does not
    decide acceptance.
    """

    try:
        node = parse_formula(formula)
    except FormulaSyntaxError as exc:
        return FormulaMetrics(0, 0, False, 0, False, (f"syntax: {exc}",))
    failures: list[str] = []
    if node.height > LOCAL_MAX_HEIGHT:
        failures.append("height")
    if node.range_count > LOCAL_MAX_RANGES:
        failures.append("ranges")
    if node.conditional:
        failures.append("conditional")
    return FormulaMetrics(
        reference_count=node.reference_count,
        range_count=node.range_count,
        conditional=node.conditional,
        height=node.height,
        accepted=not failures,
        failures=tuple(failures),
    )


def formula_references(formula: str) -> tuple[str, ...]:
    node = parse_formula(formula)
    return _precedents(node)


def _precedents(node: _Node) -> tuple[str, ...]:
    if node.kind == "ref":
        return node.refs
    if node.kind == "range":
        return _expand_range(node.refs[0], node.refs[1])
    collected: list[str] = []
    for child in node.children:
        collected.extend(_precedents(child))
    return tuple(collected)


def _expand_range(start: str, end: str) -> tuple[str, ...]:
    left_column, left_row = _split_cell(start)
    right_column, right_row = _split_cell(end)
    columns = range(min(left_column, right_column), max(left_column, right_column) + 1)
    rows = range(min(left_row, right_row), max(left_row, right_row) + 1)
    return tuple(f"{_column_name(column)}{row}" for column in columns for row in rows)


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def chain_length(formulas: dict[str, str]) -> int:
    """Local M1.5: formula cells on the longest formula-to-formula path.

    The starting formula counts. A value cell that is not itself a formula
    does not count. This is an operationalization of "cells one has to
    traverse," restricted to formula cells so a chain of eight formulas
    measures 8.
    """

    if not formulas:
        return 0
    memo: dict[str, int] = {}

    def length(cell: str, stack: tuple[str, ...]) -> int:
        if cell in stack:
            raise FormulaCycle(cell)
        if cell not in formulas:
            return 0
        if cell in memo:
            return memo[cell]
        precedents = [ref for ref in formula_references(formulas[cell]) if ref in formulas]
        if not precedents:
            memo[cell] = 1
            return 1
        best = 1 + max(length(ref, stack + (cell,)) for ref in precedents)
        memo[cell] = best
        return best

    return max(length(cell, ()) for cell in formulas)


def evaluate_chain(
    formulas: dict[str, str],
    ceiling: int = LOCAL_CHAIN_CEILING,
) -> ChainResult:
    per_formula = [evaluate_formula(text) for text in formulas.values()]
    per_pass = all(item.accepted for item in per_formula)
    try:
        length = chain_length(formulas)
    except (FormulaSyntaxError, FormulaCycle):
        return ChainResult(False, 0, ceiling, False)
    return ChainResult(per_pass, length, ceiling, length <= ceiling)


def staging_sum_formula(column: str, start_row: int, end_row: int) -> str:
    if end_row < start_row:
        raise ValueError("staging sum needs a non-empty row span")
    if not column.isalpha():
        raise ValueError("staging column must be letters")
    return f"=SUM({column.upper()}{start_row}:{column.upper()}{end_row})"
