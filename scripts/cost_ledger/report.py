"""Serialize a reconciliation report for CLI output."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from scripts.cost_ledger.model import ReconciliationReport


def report_to_dict(report: ReconciliationReport) -> dict[str, Any]:
    return {
        "observation_window": {
            "start": report.window.start.isoformat(),
            "end": report.window.end.isoformat(),
        },
        "invented_prices": report.invented_prices,
        "handling_rules": {
            "unallocated_fees": report.handling_rules.unallocated_fees,
            "free_tiers": report.handling_rules.free_tiers,
            "cancel_after_bill": report.handling_rules.cancel_after_bill,
            "human_rescue": report.handling_rules.human_rescue,
        },
        "intensity_distribution": dict(report.intensity_distribution),
        "households": [
            {
                "household_id": row.household_id,
                "intensity": row.intensity,
                "cloud_by_category": [
                    {
                        "vendor": item.vendor,
                        "category": item.category,
                        "unit": item.unit,
                        "usage_quantity": _num(item.usage_quantity),
                        "bill_quantity": (
                            None if item.bill_quantity is None else _num(item.bill_quantity)
                        ),
                        "bill_amount_minor": item.bill_amount_minor,
                        "currency": item.currency,
                        "verification": item.verification,
                    }
                    for item in row.cloud_by_category
                ],
                "variable_ops_amount_minor": row.variable_ops_amount_minor,
                "variable_ops_verification": row.variable_ops_verification,
                "net_receipts_minor": row.net_receipts_minor,
                "net_receipts_verification": row.net_receipts_verification,
                "contribution_minor": row.contribution_minor,
                "contribution_verification": row.contribution_verification,
                "flagged_quantity": {
                    flag: _num(quantity) for flag, quantity in sorted(row.flagged_quantity.items())
                },
            }
            for row in report.households
        ],
        "unallocated_ops": [
            {
                "line_id": line.line_id,
                "category": line.category,
                "cost_class": line.cost_class,
                "unit": line.unit,
                "quantity": _num(line.quantity),
                "amount_minor": line.amount_minor,
                "currency": line.currency,
                "flags": sorted(line.flags),
                "verification": line.verification,
            }
            for line in report.unallocated_ops
        ],
        "gaps": [
            {
                "code": gap.code,
                "detail": gap.detail,
                "household_id": gap.household_id,
                "vendor": gap.vendor,
                "category": gap.category,
                "quantity": None if gap.quantity is None else _num(gap.quantity),
                "unit": gap.unit,
            }
            for gap in report.gaps
        ],
    }


def _num(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)
