#!/usr/bin/env python3
"""Merge docs/ Markdown files by document type and rewrite references."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from urllib.parse import urlparse


GROUPS = {
    "acceptance": ("docs/acceptance",),
    "compliance": ("docs/compliance",),
    "design": ("docs/design",),
    "eval": ("docs/eval",),
    "product": ("docs/product", "docs/product-review-f01-307.md"),
    "runbooks": ("docs/runbooks",),
    "strategy": ("docs/strategy",),
    "handoff-archive": ("docs/HANDOFF-archive-*.md",),
}

MARKDOWN_LINK = re.compile(r"(\]\()([^)#\s]+)(#[^)]*)?(\))")
HEADING = re.compile(r"^(#{1,6})(\s+.*)$")


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def source_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    found: set[Path] = set()
    for pattern in patterns:
        path = root / pattern
        if "*" in pattern:
            found.update(root.glob(pattern))
        elif path.is_dir():
            found.update(path.rglob("*.md"))
        elif path.is_file():
            found.add(path)
    return sorted(p for p in found if p.suffix.lower() == ".md")


def demote_headings(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            line = "#" + match.group(1) + match.group(2)
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def canonical_path(root: Path, group: str) -> Path:
    return root / "docs" / f"{group}.md"


def build_mapping(root: Path) -> tuple[dict[str, str], dict[str, list[Path]]]:
    mapping: dict[str, str] = {}
    groups: dict[str, list[Path]] = {}
    for group, patterns in GROUPS.items():
        output = canonical_path(root, group)
        files = [p for p in source_files(root, patterns) if p != output]
        if not files:
            raise SystemExit(f"no source Markdown files found for {group}")
        if output.exists():
            raise SystemExit(f"output already exists, refusing to merge again: {rel(output, root)}")
        groups[group] = files
        for source in files:
            mapping[rel(source, root)] = rel(output, root)
    return mapping, groups


def rewrite_target(
    target: str,
    source_current: Path,
    destination_current: Path,
    root: Path,
    mapping: dict[str, str],
) -> str:
    if not target or target.startswith(("#", "/", "//")):
        return target
    wrapped = target.startswith("<") and target.endswith(">")
    clean_target = target[1:-1] if wrapped else target
    parsed = urlparse(clean_target)
    if parsed.scheme or parsed.netloc:
        return target
    candidate = (source_current.parent / parsed.path).resolve()
    try:
        source_rel = rel(candidate, root)
    except ValueError:
        return target
    destination = mapping.get(source_rel)
    if destination:
        rewritten = os.path.relpath(root / destination, destination_current.parent)
    elif candidate.exists():
        rewritten = os.path.relpath(candidate, destination_current.parent)
    else:
        return target
    rewritten = rewritten.replace(os.sep, "/")
    if parsed.query:
        rewritten += "?" + parsed.query
    if parsed.fragment:
        rewritten += "#" + parsed.fragment
    return f"<{rewritten}>" if wrapped else rewritten


def rewrite_text(
    text: str,
    source_current: Path,
    destination_current: Path,
    root: Path,
    mapping: dict[str, str],
) -> str:
    def replace_link(match: re.Match[str]) -> str:
        target = rewrite_target(
            match.group(2), source_current, destination_current, root, mapping
        )
        return match.group(1) + target + (match.group(3) or "") + match.group(4)

    text = MARKDOWN_LINK.sub(replace_link, text)
    for old, new in sorted(mapping.items(), key=lambda item: -len(item[0])):
        text = text.replace(old, new)
    return text


def merged_text(group: str, files: list[Path], root: Path, mapping: dict[str, str]) -> str:
    output = canonical_path(root, group)
    title = group.replace("-", " ").title()
    lines = [
        f"# Memoria {title}",
        "",
        "本文件由 `scripts/consolidate_docs.py` 按类型归并生成。每个来源章节保留原文和来源路径。",
        "",
        "## 来源文件",
        "",
    ]
    lines.extend(f"- `{rel(path, root)}`" for path in files)
    lines.extend(["", "---", ""])
    for path in files:
        source = path.read_text(encoding="utf-8")
        source = rewrite_text(source, path, output, root, mapping)
        lines.append(f"## 来源：`{rel(path, root)}`")
        lines.append("")
        lines.append(demote_headings(source).rstrip())
        lines.extend(["", "---", ""])
    return "\n".join(lines).rstrip() + "\n"


def apply(root: Path, mapping: dict[str, str], groups: dict[str, list[Path]]) -> None:
    for group, files in groups.items():
        output = canonical_path(root, group)
        output.write_text(merged_text(group, files, root, mapping), encoding="utf-8")

    source_paths = {root / old for old in mapping}
    output_paths = {canonical_path(root, group) for group in groups}
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or ".git" in path.parts
            or path in source_paths
            or path in output_paths
        ):
            continue
        if path.suffix.lower() not in {".md", ".py", ".js", ".ts", ".tsx", ".sh", ".yml", ".yaml", ".json", ".css", ".wxml", ".service"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rewritten = rewrite_text(text, path, path, root, mapping)
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")

    for path in sorted(source_paths, reverse=True):
        path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true", help="write aggregates, rewrite references, and delete sources")
    args = parser.parse_args()
    root = args.root.resolve()
    mapping, groups = build_mapping(root)
    print(f"groups: {len(groups)}")
    print(f"sources: {len(mapping)}")
    for group, files in groups.items():
        print(f"{group}: {len(files)} -> docs/{group}.md")
    if not args.apply:
        print("dry run: pass --apply to make changes")
        return
    apply(root, mapping, groups)
    print("applied")


if __name__ == "__main__":
    main()
