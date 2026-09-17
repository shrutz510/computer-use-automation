"""Semantic target descriptors: how a recorded step names the control it acts on.

These are deliberately surface-agnostic. The web surface resolves them through
the DOM/ARIA; a desktop surface would resolve the same descriptors through the
UIA/AX accessibility tree. Strategies are ordered from most to least robust and
replay takes the first one that matches exactly one element.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class RoleStrategy(BaseModel):
    """Accessible role + accessible name, e.g. button "Search"."""

    by: Literal["role"] = "role"
    role: str
    name: str


class LabelStrategy(BaseModel):
    """The visible field label: a real <label>, or the adjacent table cell on legacy forms."""

    by: Literal["label"] = "label"
    text: str


class CellStrategy(BaseModel):
    """A table cell addressed by its row header and (optionally) column header."""

    by: Literal["cell"] = "cell"
    row_header: str
    column_header: str | None = None


class CssStrategy(BaseModel):
    """Structural fallback. Brittle; only used when nothing semantic is unique."""

    by: Literal["css"] = "css"
    value: str


Strategy = Annotated[RoleStrategy | LabelStrategy | CellStrategy | CssStrategy, Field(discriminator="by")]


class Target(BaseModel):
    frame: str | None = None  # frame name; None means the top-level document
    strategies: list[Strategy]
    description: str = ""  # human-readable, for reviewers
