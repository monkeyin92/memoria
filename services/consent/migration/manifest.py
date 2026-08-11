"""Rollback manifest generator for the dry-run plan.

The manifest is deterministic jsonl (one line per plan item) and every line is
marked ``executed: false`` with the note ``迁移未执行`` — a migration that has
never been executed must never claim otherwise.
"""

from __future__ import annotations

import json

from services.consent.migration.plan import DryRunPlan

MANIFEST_NOTE = "迁移未执行"


def render_manifest(plan: DryRunPlan) -> str:
    """Render the plan as reproducible jsonl text, ordered by ``old_id``."""
    lines = [
        json.dumps(
            {
                "old_id": item.old_id,
                "new_id": item.new_id,
                "action": item.action,
                "checksum": item.row_checksum,
                "executed": False,
                "note": MANIFEST_NOTE,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        for item in sorted(plan.items, key=lambda item: item.old_id)
    ]
    return "\n".join(lines)
