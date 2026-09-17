"""Parse text read off a screen into typed output values."""

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

OutputType = Literal["string", "decimal", "integer", "date"]


class ParseError(ValueError):
    pass


def parse_value(text: str, type_: OutputType) -> str | int | Decimal:
    text = text.strip()
    if type_ == "string" or type_ == "date":
        return text
    cleaned = re.sub(r"[\s$,]", "", text)
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = Decimal(cleaned) if type_ == "decimal" else int(cleaned)
    except (InvalidOperation, ValueError) as e:
        raise ParseError(f"cannot parse {text!r} as {type_}") from e
    return -value if negative else value
