"""Capabilities: typed device attributes with bounds (docs/specs/devices.md §1)."""

from dataclasses import dataclass
from typing import Any, Literal

Kind = Literal["bool", "int", "float", "enum"]
FLOAT_TOLERANCE = 0.05


class CapabilityError(ValueError):
    """Base for errors detected before a command is sent (dispatcher.md §1.2)."""

    code = "invalid"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class UnsupportedAction(CapabilityError):
    code = "unsupported_action"


class ValueOutOfRange(CapabilityError):
    code = "value_out_of_range"


@dataclass(frozen=True)
class Capability:
    name: str
    kind: Kind
    min: float | None = None
    max: float | None = None
    values: tuple[str, ...] = ()  # for enum
    readonly: bool = False
    unit: str | None = None
    default: Any = None

    def coerce(self, value: Any) -> Any:
        """Validate and convert a value for this attribute, or raise ValueOutOfRange."""
        try:
            if self.kind == "bool":
                if not isinstance(value, bool):
                    raise TypeError
                return value
            if self.kind == "enum":
                if value not in self.values:
                    raise ValueOutOfRange(
                        f"{self.name}={value!r} not in {self.values}",
                        attribute=self.name,
                        value=value,
                    )
                return value
            if isinstance(value, bool):
                raise TypeError
            number = round(value) if self.kind == "int" else float(value)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueOutOfRange):
                raise
            raise ValueOutOfRange(
                f"{self.name}={value!r} is not {self.kind}", attribute=self.name, value=value
            ) from None
        if (self.min is not None and number < self.min) or (
            self.max is not None and number > self.max
        ):
            raise ValueOutOfRange(
                f"{self.name}={value} outside {self.min}..{self.max}",
                attribute=self.name,
                value=value,
                min=self.min,
                max=self.max,
            )
        return number

    def clamp(self, number: float) -> float:
        if self.min is not None:
            number = max(self.min, number)
        if self.max is not None:
            number = min(self.max, number)
        return round(number) if self.kind == "int" else number

    def initial(self) -> Any:
        if self.default is not None:
            return self.default
        if self.kind == "bool":
            return False
        if self.kind == "enum":
            return self.values[0]
        return self.min if self.min is not None else 0

    def equal(self, a: Any, b: Any) -> bool:
        if self.kind == "float" and isinstance(a, int | float) and isinstance(b, int | float):
            return abs(a - b) <= FLOAT_TOLERANCE
        return a == b

    def describe(self) -> dict[str, Any]:
        """Capability description for the `meta` message (protocol.md §3)."""
        out: dict[str, Any] = {"type": self.kind}
        for key in ("min", "max", "unit"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.values:
            out["values"] = list(self.values)
        if self.readonly:
            out["readonly"] = True
        return out


def bool_cap(name: str, **kw: Any) -> Capability:
    return Capability(name, "bool", **kw)


def int_cap(name: str, lo: int, hi: int, **kw: Any) -> Capability:
    return Capability(name, "int", lo, hi, **kw)


def float_cap(name: str, lo: float, hi: float, **kw: Any) -> Capability:
    return Capability(name, "float", lo, hi, **kw)


def enum_cap(name: str, *values: str, **kw: Any) -> Capability:
    return Capability(name, "enum", values=values, **kw)
