"""Pure reconciliation of a ledger sample against vendor bills."""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from scripts.cost_ledger.model import (
    CategoryQuantity,
    Gap,
    HouseholdLedger,
    IntensityBucket,
    LedgerSample,
    OpsLine,
    ReconciliationReport,
    UsageEvent,
    VendorBillLine,
)


def reconcile_sample(sample: LedgerSample) -> ReconciliationReport:
    gaps: list[Gap] = []
    usage_by_household = _group_usage(sample.usage_events)
    bill_index = _index_bills(sample.vendor_bills, gaps)
    global_usage = _aggregate_usage(sample.usage_events)
    _check_global_bills(global_usage, bill_index, gaps)

    receipts = {row.household_id: row for row in sample.receipts}
    variable_ops = _variable_ops_by_household(sample.ops_lines, gaps)
    unallocated_ops = tuple(
        line
        for line in sample.ops_lines
        if line.cost_class in {"fixed", "unallocated"}
    )
    for line in unallocated_ops:
        if line.verification == "未验":
            gaps.append(
                Gap(
                    code="ops_未验",
                    detail=(
                        f"{line.cost_class} ops line {line.line_id} "
                        f"({line.category}) has no verified receipt"
                    ),
                    category=line.category,
                    quantity=line.quantity,
                    unit=line.unit,
                )
            )

    scores = {
        household.household_id: _intensity_score(
            usage_by_household.get(household.household_id, ())
        )
        for household in sample.households
    }
    intensities = _assign_intensities(sample, scores)
    allocations = _allocate_cloud(usage_by_household, global_usage, bill_index, gaps)

    households: list[HouseholdLedger] = []
    for household in sample.households:
        hid = household.household_id
        events = usage_by_household.get(hid, ())
        cloud_rows = allocations.get(hid, ())
        ops_amount, ops_verification = variable_ops.get(hid, (0, "measured"))
        receipt = receipts.get(hid)
        if receipt is None:
            gaps.append(
                Gap(
                    code="receipt_missing",
                    detail="no net receipt row for household in observation window",
                    household_id=hid,
                )
            )
            net_minor: int | None = None
            net_verification = "未验"
        else:
            net_minor = receipt.amount_minor
            net_verification = receipt.verification
            if receipt.verification == "未验":
                gaps.append(
                    Gap(
                        code="receipt_未验",
                        detail="net receipts not verified for sample period",
                        household_id=hid,
                    )
                )

        contribution_minor, contribution_verification = _contribution(
            net_minor=net_minor,
            net_verification=net_verification,
            ops_amount=ops_amount,
            ops_verification=ops_verification,
            cloud_rows=cloud_rows,
        )
        households.append(
            HouseholdLedger(
                household_id=hid,
                intensity=intensities[hid],
                cloud_by_category=cloud_rows,
                variable_ops_amount_minor=ops_amount,
                variable_ops_verification=ops_verification,  # type: ignore[arg-type]
                net_receipts_minor=net_minor,
                net_receipts_verification=net_verification,  # type: ignore[arg-type]
                contribution_minor=contribution_minor,
                contribution_verification=contribution_verification,  # type: ignore[arg-type]
                flagged_quantity=_flag_totals(events),
            )
        )

    distribution: dict[IntensityBucket, int] = {"low": 0, "mid": 0, "high": 0}
    for row in households:
        distribution[row.intensity] += 1

    return ReconciliationReport(
        window=sample.observation_window,
        handling_rules=sample.handling_rules,
        households=tuple(households),
        intensity_distribution=distribution,
        unallocated_ops=unallocated_ops,
        gaps=tuple(gaps),
        invented_prices=False,
    )


def _group_usage(
    events: tuple[UsageEvent, ...],
) -> dict[str, tuple[UsageEvent, ...]]:
    buckets: dict[str, list[UsageEvent]] = defaultdict(list)
    for event in events:
        buckets[event.household_id].append(event)
    return {key: tuple(value) for key, value in buckets.items()}


def _aggregate_usage(
    events: tuple[UsageEvent, ...],
) -> dict[tuple[str, str, str], Decimal]:
    totals: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for event in events:
        totals[(event.vendor, event.category, event.unit)] += event.quantity
    return dict(totals)


def _index_bills(
    bills: tuple[VendorBillLine, ...],
    gaps: list[Gap],
) -> dict[tuple[str, str, str], VendorBillLine]:
    index: dict[tuple[str, str, str], VendorBillLine] = {}
    for bill in bills:
        key = (bill.vendor, bill.category, bill.unit)
        if key in index:
            gaps.append(
                Gap(
                    code="duplicate_bill_key",
                    detail=f"duplicate vendor bill for {key}",
                    vendor=bill.vendor,
                    category=bill.category,
                    unit=bill.unit,
                )
            )
            continue
        if bill.verification == "未验":
            gaps.append(
                Gap(
                    code="bill_未验",
                    detail=f"vendor bill {bill.bill_id} marked 未验",
                    vendor=bill.vendor,
                    category=bill.category,
                    quantity=bill.quantity,
                    unit=bill.unit,
                )
            )
        index[key] = bill
    return index


def _check_global_bills(
    global_usage: dict[tuple[str, str, str], Decimal],
    bill_index: dict[tuple[str, str, str], VendorBillLine],
    gaps: list[Gap],
) -> None:
    for key, usage_qty in sorted(global_usage.items()):
        vendor, category, unit = key
        bill = bill_index.get(key)
        if bill is None:
            gaps.append(
                Gap(
                    code="vendor_bill_missing",
                    detail=(
                        f"no vendor bill for {vendor}/{category}/{unit}; "
                        "quantity kept, amount 未验 (no invented price)"
                    ),
                    vendor=vendor,
                    category=category,
                    quantity=usage_qty,
                    unit=unit,
                )
            )
            continue
        if bill.verification == "未验":
            continue
        if bill.quantity != usage_qty:
            gaps.append(
                Gap(
                    code="quantity_mismatch",
                    detail=(
                        f"sample usage {usage_qty} != bill {bill.quantity} "
                        f"for {vendor}/{category}/{unit}"
                    ),
                    vendor=vendor,
                    category=category,
                    quantity=usage_qty,
                    unit=unit,
                )
            )
    for key, bill in bill_index.items():
        if key not in global_usage and bill.verification != "未验":
            gaps.append(
                Gap(
                    code="bill_without_usage",
                    detail=(
                        f"bill {bill.bill_id} has no matching usage events "
                        f"for {bill.vendor}/{bill.category}/{bill.unit}"
                    ),
                    vendor=bill.vendor,
                    category=bill.category,
                    quantity=bill.quantity,
                    unit=bill.unit,
                )
            )


def _allocate_cloud(
    usage_by_household: dict[str, tuple[UsageEvent, ...]],
    global_usage: dict[tuple[str, str, str], Decimal],
    bill_index: dict[tuple[str, str, str], VendorBillLine],
    gaps: list[Gap],
) -> dict[str, tuple[CategoryQuantity, ...]]:
    """Allocate verified bill amounts by measured household quantity share.

    Share allocation uses bill amounts that already exist. It does not invent
    unit prices. Missing or 未验 bills leave household amount_minor as None.
    """
    per_household: dict[str, dict[tuple[str, str, str], Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )
    for household_id, events in usage_by_household.items():
        for event in events:
            key = (event.vendor, event.category, event.unit)
            per_household[household_id][key] += event.quantity

    allocated_amounts: dict[str, dict[tuple[str, str, str], int | None]] = defaultdict(
        dict
    )
    for key, bill in bill_index.items():
        total_usage = global_usage.get(key, Decimal("0"))
        if bill.verification != "bill_matched" or bill.amount_minor is None:
            for household_id, quantities in per_household.items():
                if key in quantities:
                    allocated_amounts[household_id][key] = None
            continue
        if total_usage == 0:
            continue
        remaining = bill.amount_minor
        shares = [
            (household_id, quantities[key])
            for household_id, quantities in per_household.items()
            if key in quantities
        ]
        shares.sort(key=lambda item: item[0])
        for index, (household_id, quantity) in enumerate(shares):
            if index == len(shares) - 1:
                share_amount = remaining
            else:
                share_amount = int(
                    (
                        Decimal(bill.amount_minor) * quantity / total_usage
                    ).to_integral_value(rounding=ROUND_HALF_UP)
                )
                remaining -= share_amount
            allocated_amounts[household_id][key] = share_amount

    result: dict[str, tuple[CategoryQuantity, ...]] = {}
    for household_id, quantities in per_household.items():
        rows: list[CategoryQuantity] = []
        for key, usage_qty in sorted(quantities.items()):
            vendor, category, unit = key
            bill = bill_index.get(key)
            if bill is None or bill.verification == "未验":
                rows.append(
                    CategoryQuantity(
                        vendor=vendor,
                        category=category,
                        unit=unit,
                        usage_quantity=usage_qty,
                        bill_quantity=None if bill is None else bill.quantity,
                        bill_amount_minor=None,
                        currency=None,
                        verification="未验",
                    )
                )
                continue
            rows.append(
                CategoryQuantity(
                    vendor=vendor,
                    category=category,
                    unit=unit,
                    usage_quantity=usage_qty,
                    bill_quantity=bill.quantity,
                    bill_amount_minor=allocated_amounts[household_id].get(key),
                    currency=bill.currency,
                    verification="bill_matched",
                )
            )
        result[household_id] = tuple(rows)
    return result


def _variable_ops_by_household(
    lines: tuple[OpsLine, ...],
    gaps: list[Gap],
) -> dict[str, tuple[int | None, str]]:
    totals: dict[str, int] = defaultdict(int)
    status: dict[str, str] = {}
    seen: set[str] = set()
    for line in lines:
        if line.cost_class != "variable" or line.household_id is None:
            continue
        hid = line.household_id
        seen.add(hid)
        if line.verification == "未验":
            status[hid] = "未验"
            gaps.append(
                Gap(
                    code="ops_未验",
                    detail=f"variable ops line {line.line_id} unverified",
                    household_id=hid,
                    category=line.category,
                    quantity=line.quantity,
                    unit=line.unit,
                )
            )
            continue
        if line.amount_minor is None:
            status[hid] = "未验"
            gaps.append(
                Gap(
                    code="ops_amount_missing",
                    detail=(
                        f"variable ops line {line.line_id} is {line.verification} "
                        "but has no amount_minor; refusing to invent a price"
                    ),
                    household_id=hid,
                    category=line.category,
                    quantity=line.quantity,
                    unit=line.unit,
                )
            )
            continue
        totals[hid] += line.amount_minor
        if status.get(hid) == "未验":
            continue
        status.setdefault(
            hid,
            "bill_matched" if line.verification == "bill_matched" else "measured",
        )
        if line.verification == "measured" and status[hid] == "bill_matched":
            status[hid] = "measured"
    result: dict[str, tuple[int | None, str]] = {}
    for hid in seen:
        if status.get(hid) == "未验":
            result[hid] = (None, "未验")
        else:
            result[hid] = (totals.get(hid, 0), status.get(hid, "measured"))
    return result


def _contribution(
    *,
    net_minor: int | None,
    net_verification: str,
    ops_amount: int | None,
    ops_verification: str,
    cloud_rows: tuple[CategoryQuantity, ...],
) -> tuple[int | None, str]:
    """net receipts − variable ops − allocated cloud bill amounts.

    Returns None with 未验 when any money leg is unverified or missing.
    """
    if net_verification == "未验" or net_minor is None:
        return None, "未验"
    if ops_verification == "未验" or ops_amount is None:
        return None, "未验"
    cloud_total = 0
    for row in cloud_rows:
        if row.verification != "bill_matched":
            return None, "未验"
        if row.bill_amount_minor is None:
            return None, "未验"
        cloud_total += row.bill_amount_minor
    return net_minor - ops_amount - cloud_total, "bill_matched"


def _flag_totals(events: tuple[UsageEvent, ...]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for event in events:
        for flag in event.flags:
            totals[flag] += event.quantity
    return dict(totals)


def _intensity_score(events: tuple[UsageEvent, ...]) -> Decimal:
    score = Decimal("0")
    for event in events:
        if event.category == "base_ops":
            continue
        score += event.quantity
    return score


def _assign_intensities(
    sample: LedgerSample,
    scores: dict[str, Decimal],
) -> dict[str, IntensityBucket]:
    assigned: dict[str, IntensityBucket] = {}
    autos: list[tuple[str, Decimal]] = []
    for household in sample.households:
        hint = household.intensity_hint
        if hint in {"low", "mid", "high"}:
            assigned[household.household_id] = hint  # type: ignore[assignment]
        else:
            autos.append((household.household_id, scores[household.household_id]))
    if not autos:
        return assigned
    autos.sort(key=lambda item: item[1])
    count = len(autos)
    for index, (household_id, _) in enumerate(autos):
        if count == 1:
            assigned[household_id] = "mid"
        elif index < count / 3:
            assigned[household_id] = "low"
        elif index < (2 * count) / 3:
            assigned[household_id] = "mid"
        else:
            assigned[household_id] = "high"
    return assigned
