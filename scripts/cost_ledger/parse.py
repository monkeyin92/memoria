"""Parse ledger JSON at the boundary into domain types."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from scripts.cost_ledger.model import (
    CLOUD_CATEGORIES,
    DEFAULT_HANDLING_RULES,
    OPS_CATEGORIES,
    HandlingRules,
    Household,
    LedgerSample,
    NetReceipt,
    ObservationWindow,
    OpsLine,
    UsageEvent,
    VendorBillLine,
)

_HOUSEHOLD_PREFIX = "hh_"
_USAGE_UNITS = frozenset({"tokens", "characters", "seconds", "bytes", "requests"})
_OPS_UNITS = frozenset({"units", "minutes", "cents", "shipments"})
_USAGE_FLAGS = frozenset(
    {
        "retry",
        "speculation",
        "cancel_still_billed",
        "generated_unplayed",
        "free_tier",
    }
)
_OPS_FLAGS = frozenset({"human_rescue"})
_COST_CLASSES = frozenset({"variable", "fixed", "unallocated"})
_VERIFICATIONS = frozenset({"measured", "bill_matched", "未验"})
_CURRENCIES = frozenset({"CNY", "USD"})
_INTENSITY_HINTS = frozenset({"low", "mid", "high", "auto"})


class LedgerParseError(ValueError):
    """Raised when a ledger sample fails boundary validation."""


def parse_ledger_sample(payload: Mapping[str, Any]) -> LedgerSample:
    if not isinstance(payload, Mapping):
        raise LedgerParseError("ledger sample must be an object")
    if payload.get("schema_version") != 1:
        raise LedgerParseError("schema_version must be 1")

    window = _parse_window(payload.get("observation_window"))
    rules = _parse_rules(payload.get("handling_rules"))
    households = tuple(_parse_household(item) for item in _require_list(payload, "households"))
    household_ids = {household.household_id for household in households}
    if len(household_ids) != len(households):
        raise LedgerParseError("household_id values must be unique")

    usage_events = tuple(
        _parse_usage(item, household_ids) for item in _require_list(payload, "usage_events")
    )
    vendor_bills = tuple(_parse_bill(item) for item in _require_list(payload, "vendor_bills"))
    ops_lines = tuple(
        _parse_ops(item, household_ids) for item in _require_list(payload, "ops_lines")
    )
    receipts_obj = payload.get("receipts")
    if not isinstance(receipts_obj, Mapping):
        raise LedgerParseError("receipts must be an object")
    receipts = tuple(
        _parse_receipt(item, household_ids)
        for item in _require_list(receipts_obj, "net_receipts")
    )
    return LedgerSample(
        schema_version=1,
        observation_window=window,
        handling_rules=rules,
        households=households,
        usage_events=usage_events,
        vendor_bills=vendor_bills,
        ops_lines=ops_lines,
        receipts=receipts,
    )


def _require_list(payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise LedgerParseError(f"{key} must be an array")
    return value


def _parse_window(raw: Any) -> ObservationWindow:
    if not isinstance(raw, Mapping):
        raise LedgerParseError("observation_window must be an object")
    try:
        start = date.fromisoformat(str(raw["start"]))
        end = date.fromisoformat(str(raw["end"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerParseError("observation_window dates must be YYYY-MM-DD") from exc
    try:
        return ObservationWindow(start=start, end=end)
    except ValueError as exc:
        raise LedgerParseError(str(exc)) from exc


def _parse_rules(raw: Any) -> HandlingRules:
    if raw is None:
        raw = DEFAULT_HANDLING_RULES
    if not isinstance(raw, Mapping):
        raise LedgerParseError("handling_rules must be an object")
    required = ("unallocated_fees", "free_tiers", "cancel_after_bill", "human_rescue")
    values: dict[str, str] = {}
    for key in required:
        text = raw.get(key)
        if not isinstance(text, str) or not text.strip():
            raise LedgerParseError(f"handling_rules.{key} must be a non-empty string")
        values[key] = text.strip()
    return HandlingRules(**values)


def _parse_household(raw: Any) -> Household:
    if not isinstance(raw, Mapping):
        raise LedgerParseError("household entry must be an object")
    household_id = _household_id(raw.get("household_id"))
    hint = raw.get("intensity_hint", "auto")
    if hint not in _INTENSITY_HINTS:
        raise LedgerParseError(f"invalid intensity_hint: {hint!r}")
    return Household(household_id=household_id, intensity_hint=hint)  # type: ignore[arg-type]


def _parse_usage(raw: Any, household_ids: set[str]) -> UsageEvent:
    if not isinstance(raw, Mapping):
        raise LedgerParseError("usage_events entry must be an object")
    event_id = _nonempty_str(raw.get("event_id"), "event_id")
    household_id = _household_id(raw.get("household_id"))
    if household_id not in household_ids:
        raise LedgerParseError(f"usage event {event_id} references unknown household")
    category = raw.get("category")
    if category not in CLOUD_CATEGORIES:
        raise LedgerParseError(f"invalid cloud category: {category!r}")
    vendor = _nonempty_str(raw.get("vendor"), "vendor")
    unit = raw.get("unit")
    if unit not in _USAGE_UNITS:
        raise LedgerParseError(f"invalid usage unit: {unit!r}")
    quantity = _nonnegative_decimal(raw.get("quantity"), "quantity")
    flags = _parse_flags(raw.get("flags"), _USAGE_FLAGS, "usage flags")
    return UsageEvent(
        event_id=event_id,
        household_id=household_id,
        category=category,  # type: ignore[arg-type]
        vendor=vendor,
        unit=unit,  # type: ignore[arg-type]
        quantity=quantity,
        flags=flags,  # type: ignore[arg-type]
    )


def _parse_bill(raw: Any) -> VendorBillLine:
    if not isinstance(raw, Mapping):
        raise LedgerParseError("vendor_bills entry must be an object")
    bill_id = _nonempty_str(raw.get("bill_id"), "bill_id")
    vendor = _nonempty_str(raw.get("vendor"), "vendor")
    category = raw.get("category")
    if category not in CLOUD_CATEGORIES:
        raise LedgerParseError(f"invalid bill category: {category!r}")
    unit = raw.get("unit")
    if unit not in _USAGE_UNITS:
        raise LedgerParseError(f"invalid bill unit: {unit!r}")
    quantity = _nonnegative_decimal(raw.get("quantity"), "quantity")
    amount_minor = _optional_int(raw.get("amount_minor"), "amount_minor")
    currency = _optional_currency(raw.get("currency"))
    verification = raw.get("verification")
    if verification not in _VERIFICATIONS:
        raise LedgerParseError(f"invalid bill verification: {verification!r}")
    if verification == "未验" and amount_minor is not None:
        raise LedgerParseError(
            f"bill {bill_id} is 未验 and must not carry an invented amount_minor"
        )
    return VendorBillLine(
        bill_id=bill_id,
        vendor=vendor,
        category=category,  # type: ignore[arg-type]
        unit=unit,  # type: ignore[arg-type]
        quantity=quantity,
        amount_minor=amount_minor,
        currency=currency,  # type: ignore[arg-type]
        verification=verification,  # type: ignore[arg-type]
    )


def _parse_ops(raw: Any, household_ids: set[str]) -> OpsLine:
    if not isinstance(raw, Mapping):
        raise LedgerParseError("ops_lines entry must be an object")
    line_id = _nonempty_str(raw.get("line_id"), "line_id")
    category = raw.get("category")
    if category not in OPS_CATEGORIES:
        raise LedgerParseError(f"invalid ops category: {category!r}")
    cost_class = raw.get("cost_class")
    if cost_class not in _COST_CLASSES:
        raise LedgerParseError(f"invalid cost_class: {cost_class!r}")
    household_raw = raw.get("household_id")
    household_id: str | None
    if household_raw is None:
        household_id = None
    else:
        household_id = _household_id(household_raw)
        if household_id not in household_ids:
            raise LedgerParseError(f"ops line {line_id} references unknown household")
    if cost_class == "variable" and household_id is None:
        raise LedgerParseError(f"variable ops line {line_id} requires household_id")
    if cost_class in {"fixed", "unallocated"} and household_id is not None:
        raise LedgerParseError(
            f"{cost_class} ops line {line_id} must not set household_id"
        )
    unit = raw.get("unit")
    if unit not in _OPS_UNITS:
        raise LedgerParseError(f"invalid ops unit: {unit!r}")
    quantity = _nonnegative_decimal(raw.get("quantity"), "quantity")
    amount_minor = _optional_int(raw.get("amount_minor"), "amount_minor")
    currency = _optional_currency(raw.get("currency"))
    flags = _parse_flags(raw.get("flags"), _OPS_FLAGS, "ops flags")
    verification = raw.get("verification")
    if verification not in _VERIFICATIONS:
        raise LedgerParseError(f"invalid ops verification: {verification!r}")
    if verification == "未验" and amount_minor is not None:
        raise LedgerParseError(
            f"ops line {line_id} is 未验 and must not carry an invented amount_minor"
        )
    return OpsLine(
        line_id=line_id,
        category=category,  # type: ignore[arg-type]
        cost_class=cost_class,  # type: ignore[arg-type]
        household_id=household_id,
        unit=unit,  # type: ignore[arg-type]
        quantity=quantity,
        amount_minor=amount_minor,
        currency=currency,  # type: ignore[arg-type]
        flags=flags,  # type: ignore[arg-type]
        verification=verification,  # type: ignore[arg-type]
    )


def _parse_receipt(raw: Any, household_ids: set[str]) -> NetReceipt:
    if not isinstance(raw, Mapping):
        raise LedgerParseError("net_receipts entry must be an object")
    household_id = _household_id(raw.get("household_id"))
    if household_id not in household_ids:
        raise LedgerParseError(f"receipt references unknown household {household_id}")
    amount_minor = _optional_int(raw.get("amount_minor"), "amount_minor")
    currency = _optional_currency(raw.get("currency"))
    verification = raw.get("verification")
    if verification not in {"measured", "未验"}:
        raise LedgerParseError(f"invalid receipt verification: {verification!r}")
    if verification == "未验" and amount_minor is not None:
        raise LedgerParseError(
            f"receipt for {household_id} is 未验 and must not carry amount_minor"
        )
    if verification == "measured" and amount_minor is None:
        raise LedgerParseError(
            f"measured receipt for {household_id} requires amount_minor"
        )
    return NetReceipt(
        household_id=household_id,
        amount_minor=amount_minor,
        currency=currency,  # type: ignore[arg-type]
        verification=verification,  # type: ignore[arg-type]
    )


def _household_id(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.startswith(_HOUSEHOLD_PREFIX):
        raise LedgerParseError(f"household_id must start with hh_: {raw!r}")
    suffix = raw[len(_HOUSEHOLD_PREFIX) :]
    if len(suffix) < 6 or len(suffix) > 32 or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789" for ch in suffix):
        raise LedgerParseError(f"household_id suffix must be 6-32 [a-z0-9]: {raw!r}")
    return raw


def _nonempty_str(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise LedgerParseError(f"{field} must be a non-empty string")
    return raw.strip()


def _nonnegative_decimal(raw: Any, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LedgerParseError(f"{field} must be a number") from exc
    if value < 0:
        raise LedgerParseError(f"{field} must be >= 0")
    return value


def _optional_int(raw: Any, field: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise LedgerParseError(f"{field} must be an integer or null")
    return raw


def _optional_currency(raw: Any) -> str | None:
    if raw is None:
        return None
    if raw not in _CURRENCIES:
        raise LedgerParseError(f"invalid currency: {raw!r}")
    return raw


def _parse_flags(raw: Any, allowed: frozenset[str], label: str) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise LedgerParseError(f"{label} must be an array")
    flags: set[str] = set()
    for item in raw:
        if item not in allowed:
            raise LedgerParseError(f"invalid {label} value: {item!r}")
        flags.add(item)
    return frozenset(flags)
