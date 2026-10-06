"""Declared dimensions for the session-fee sheet.

Addition is defined only when both sides have the same dimension, including
the same conversion factor. Multiplication combines exponents. A pure factor
cannot represent an affine conversion such as Fahrenheit to Celsius.

Dimensions are taken from the column schema. Header words are not inferred.
Ordered comparison uses the same equal-dimension precondition as addition;
that comparison rule is a local extension and is not a rule stated for
relational operators in the dimension paper's addition semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class DimensionError(Exception):
    """Base error for dimension rules."""


class DimensionMismatch(DimensionError):
    """Raised when an operation is undefined for the operand dimensions."""


class UnsupportedConversion(DimensionError):
    """Raised when a conversion needs more than a pure factor."""


class UndeclaredDimension(DimensionError):
    """Raised when a column has no declared dimension."""


def _as_decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _factor_text(value: Decimal | int | str) -> str:
    """Stable factor text, so 1, 1.0, and 1.00 are the same factor."""

    return format(_as_decimal(value).normalize(), "f")


@dataclass(frozen=True)
class Dimension:
    """Exponent vector over the bases money, time, and date.

    An empty vector is the dimensionless dimension. It is a real dimension:
    operations still compare it and do not skip the check.
    ``factors`` maps each present base to its conversion factor relative to
    the base unit (JPY, hour, or civil day).
    """

    exponents: tuple[tuple[str, int], ...]
    factors: tuple[tuple[str, str], ...]

    @staticmethod
    def base(name: str, factor: Decimal | int | str = 1) -> "Dimension":
        factor_text = _factor_text(factor)
        return Dimension(exponents=((name, 1),), factors=((name, factor_text),))

    @staticmethod
    def dimensionless() -> "Dimension":
        return Dimension(exponents=(), factors=())

    def exponent(self, base: str) -> int:
        for name, power in self.exponents:
            if name == base:
                return power
        return 0

    def factor(self, base: str) -> Decimal | None:
        for name, value in self.factors:
            if name == base:
                return Decimal(value)
        return None

    def label(self) -> str:
        if not self.exponents:
            return "dimensionless"
        parts = []
        for name, power in self.exponents:
            factor = self.factor(name)
            unit = {"money": "JPY", "time": "hour", "date": "date"}.get(name, name)
            if factor is not None and factor != Decimal("1"):
                unit = f"{unit}*{format(factor, 'f')}"
            if power == 1:
                parts.append(unit)
            else:
                parts.append(f"{unit}^{power}")
        return "*".join(parts)


JPY = Dimension.base("money", 1)
HOUR = Dimension.base("time", 1)
DATE = Dimension.base("date", 1)
DIMENSIONLESS = Dimension.dimensionless()
CENT = Dimension.base("money", Decimal("0.01"))


def _combine_product(left: Dimension, right: Dimension) -> Dimension:
    powers: dict[str, int] = {}
    factors: dict[str, str] = {}
    for source in (left, right):
        for name, power in source.exponents:
            incoming = source.factor(name)
            if incoming is None:
                raise DimensionMismatch(f"{name} is missing a conversion factor")
            if name in factors and Decimal(factors[name]) != incoming:
                raise DimensionMismatch(
                    f"conversion factors for {name} differ: {factors[name]} and {incoming}"
                )
            factors[name] = _factor_text(incoming)
            powers[name] = powers.get(name, 0) + power
    exponents = tuple(sorted((name, power) for name, power in powers.items() if power != 0))
    kept = tuple(sorted((name, factors[name]) for name, _power in exponents))
    return Dimension(exponents=exponents, factors=kept)


def multiply_dimensions(left: Dimension, right: Dimension) -> Dimension:
    return _combine_product(left, right)


def inverse(dimension: Dimension) -> Dimension:
    exponents = tuple((name, -power) for name, power in dimension.exponents)
    return Dimension(exponents=exponents, factors=dimension.factors)


JPY_PER_HOUR = multiply_dimensions(JPY, inverse(HOUR))


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    dimension: Dimension

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            object.__setattr__(self, "value", _as_decimal(self.value))


def add(left: Quantity, right: Quantity) -> Quantity:
    if left.dimension != right.dimension:
        raise DimensionMismatch(
            f"addition is undefined for {left.dimension.label()} and {right.dimension.label()}"
        )
    return Quantity(left.value + right.value, left.dimension)


def multiply(left: Quantity, right: Quantity) -> Quantity:
    dimension = multiply_dimensions(left.dimension, right.dimension)
    return Quantity(left.value * right.value, dimension)


def compare(left: Quantity, right: Quantity, operator: str) -> bool:
    """Compare values only when both dimensions are the same.

    Unequal dimensions are undefined, by the same precondition the addition
    rule uses. This is a local extension to ordered comparison.
    """

    if left.dimension != right.dimension:
        raise DimensionMismatch(
            f"comparison is undefined for {left.dimension.label()} and {right.dimension.label()}"
        )
    operations = {
        ">": lambda a, b: a > b,
        "<": lambda a, b: a < b,
        ">=": lambda a, b: a >= b,
        "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b,
    }
    if operator not in operations:
        raise DimensionError(f"unsupported comparison {operator}")
    return operations[operator](left.value, right.value)


def numeric_add(left: Decimal | int | str, right: Decimal | int | str) -> Decimal:
    """Baseline that adds bare numbers and ignores units."""

    return _as_decimal(left) + _as_decimal(right)


def fahrenheit_to_celsius(degrees_f: Decimal | int | str) -> Quantity:
    """Affine temperature conversion. Intentionally unsupported.

    A pure factor cannot carry the offset this conversion needs.
    ``degrees_f`` is accepted so callers can see the value is not converted.
    """

    _as_decimal(degrees_f)
    raise UnsupportedConversion(
        "Fahrenheit to Celsius needs an offset; a pure factor does not represent it"
    )


DECLARED_COLUMNS = {
    "operation_id": DIMENSIONLESS,
    "session_date": DATE,
    "desk_code": DIMENSIONLESS,
    "hours": HOUR,
    "rate_jpy_per_hour": JPY_PER_HOUR,
    "amount_jpy": JPY,
    "fingerprint": DIMENSIONLESS,
}


def dimension_for_column(name: str) -> Dimension:
    """Return the declared dimension. Never infer one from the header text."""

    try:
        return DECLARED_COLUMNS[name]
    except KeyError as exc:
        raise UndeclaredDimension(name) from exc
