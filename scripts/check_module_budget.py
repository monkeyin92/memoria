#!/usr/bin/env python3
"""Enforce downward-only line budgets for high-risk modules.

``check`` is read-only and requires every measured line count to match the
budget in ``architecture-status.yaml``. ``update`` only records lower counts;
it never turns an over-budget module into the new baseline.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_HEADER_RE = re.compile(r"^module_budgets:\s*(?:#.*)?$")
_ENTRY_RE = re.compile(
    r"^(?P<indent> +)(?P<module>[^:#][^:]*?):(?P<spacing>\s*)"
    r"(?P<budget>[0-9]+)(?P<suffix>\s*(?:#.*)?)$"
)


class ModuleBudgetError(ValueError):
    """Raised when the budget configuration or a measured module is invalid."""


@dataclass(frozen=True)
class ModuleBudget:
    module: str
    budget: int
    actual: int


@dataclass(frozen=True)
class _BudgetEntry:
    module: str
    budget: int
    line_index: int
    budget_start: int
    budget_end: int


@dataclass(frozen=True)
class _BudgetDocument:
    lines: tuple[str, ...]
    entries: tuple[_BudgetEntry, ...]


def _load_budget_document(path: Path) -> _BudgetDocument:
    try:
        lines = tuple(path.read_text(encoding="utf-8").splitlines(keepends=True))
    except (OSError, UnicodeError) as exc:
        raise ModuleBudgetError(f"cannot read architecture status: {path}") from exc

    header_indices = [
        index for index, line in enumerate(lines) if _HEADER_RE.fullmatch(line.rstrip("\r\n"))
    ]
    if len(header_indices) != 1:
        raise ModuleBudgetError("architecture status must contain one module_budgets mapping")

    entries: list[_BudgetEntry] = []
    seen: set[str] = set()
    for index in range(header_indices[0] + 1, len(lines)):
        raw = lines[index].rstrip("\r\n")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith(" "):
            break
        match = _ENTRY_RE.fullmatch(raw)
        if match is None:
            raise ModuleBudgetError(f"invalid module budget entry on line {index + 1}")
        module = match.group("module").strip()
        if module in seen:
            raise ModuleBudgetError(f"duplicate module budget: {module}")
        seen.add(module)
        entries.append(
            _BudgetEntry(
                module=module,
                budget=int(match.group("budget")),
                line_index=index,
                budget_start=match.start("budget"),
                budget_end=match.end("budget"),
            )
        )
    if not entries:
        raise ModuleBudgetError("module_budgets must not be empty")
    return _BudgetDocument(lines=lines, entries=tuple(entries))


def _module_path(root: Path, module: str) -> Path:
    relative = PurePosixPath(module)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ModuleBudgetError(f"module budget path must stay inside the repository: {module}")
    candidate = root.joinpath(*relative.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ModuleBudgetError(
            f"module budget path must stay inside the repository: {module}"
        ) from exc
    if not candidate.is_file():
        raise ModuleBudgetError(f"budgeted module does not exist: {module}")
    return candidate


def _line_count(path: Path) -> int:
    try:
        with path.open(encoding="utf-8") as stream:
            return sum(1 for _ in stream)
    except (OSError, UnicodeError) as exc:
        raise ModuleBudgetError(f"cannot read budgeted module: {path}") from exc


def _measure(root: Path, document: _BudgetDocument) -> tuple[ModuleBudget, ...]:
    return tuple(
        ModuleBudget(
            module=entry.module,
            budget=entry.budget,
            actual=_line_count(_module_path(root, entry.module)),
        )
        for entry in document.entries
    )


def check_module_budgets(*, root: Path, status_path: Path) -> tuple[ModuleBudget, ...]:
    """Return exact measurements or reject growth and unrecorded reductions."""
    root = root.expanduser().resolve()
    document = _load_budget_document(status_path.expanduser().resolve())
    measurements = _measure(root, document)
    violations: list[str] = []
    for item in measurements:
        if item.actual > item.budget:
            violations.append(f"{item.module}: {item.actual} lines exceeds budget {item.budget}")
        elif item.actual < item.budget:
            violations.append(
                f"{item.module}: {item.actual} lines is below budget {item.budget}; "
                "run check_module_budget.py update to tighten it"
            )
    if violations:
        raise ModuleBudgetError("module budget check rejected:\n- " + "\n- ".join(violations))
    return measurements


def update_module_budgets(*, root: Path, status_path: Path) -> tuple[ModuleBudget, ...]:
    """Persist lower measured counts while refusing to raise any budget."""
    root = root.expanduser().resolve()
    status_path = status_path.expanduser().resolve()
    document = _load_budget_document(status_path)
    measurements = _measure(root, document)
    over_budget = [item for item in measurements if item.actual > item.budget]
    if over_budget:
        details = "\n- ".join(
            f"{item.module}: {item.actual} lines exceeds budget {item.budget}"
            for item in over_budget
        )
        raise ModuleBudgetError(
            "module budget update rejected; update mode never raises a budget:\n- " + details
        )

    measured_by_module = {item.module: item for item in measurements}
    lines = list(document.lines)
    changed = False
    for entry in document.entries:
        actual = measured_by_module[entry.module].actual
        if actual >= entry.budget:
            continue
        line = lines[entry.line_index]
        lines[entry.line_index] = (
            line[: entry.budget_start] + str(actual) + line[entry.budget_end :]
        )
        changed = True
    if changed:
        try:
            status_path.write_text("".join(lines), encoding="utf-8")
        except OSError as exc:
            raise ModuleBudgetError(f"cannot update architecture status: {status_path}") from exc
        document = _load_budget_document(status_path)
        measurements = _measure(root, document)
    return measurements


def _resolve_status_path(root: Path, status_path: Path) -> Path:
    return status_path if status_path.is_absolute() else root / status_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "update"))
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument(
        "--status-file",
        type=Path,
        default=Path("architecture-status.yaml"),
        help="absolute path or path relative to --root",
    )
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    status_path = _resolve_status_path(root, args.status_file)
    try:
        if args.mode == "check":
            measurements = check_module_budgets(root=root, status_path=status_path)
        else:
            measurements = update_module_budgets(root=root, status_path=status_path)
    except ModuleBudgetError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for item in measurements:
        print(f"module_budget_ok {item.module} lines={item.actual} budget={item.budget}")
    print(f"module_budget_{args.mode}=PASS count={len(measurements)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
