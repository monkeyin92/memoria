"""SQL helpers shared by the memory catalog and its review mixin."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _bind_dollar(parameters: list[Any]) -> Callable[[str], str]:
    """PostgreSQL placeholder binder for :func:`subject_lineage_predicates`."""

    def bind(value: str) -> str:
        parameters.append(value)
        return f"${len(parameters)}"

    return bind


