"""Domain types for the offline household cost ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

CloudCategory = Literal[
    "asr",
    "tts",
    "chat",
    "plan",
    "end",
    "safety",
    "summary",
    "memory",
    "storage",
    "backup",
    "bandwidth",
    "base_ops",
]

OpsCategory = Literal[
    "manufacturing",
    "assembly_test",
    "packaging_ship",
    "returns_repairs",
    "support",
    "acquisition",
    "rd_fixed",
]

UsageUnit = Literal["tokens", "characters", "seconds", "bytes", "requests"]
OpsUnit = Literal["units", "minutes", "cents", "shipments"]
CostClass = Literal["variable", "fixed", "unallocated"]
IntensityBucket = Literal["low", "mid", "high"]
IntensityHint = Literal["low", "mid", "high", "auto"]
Verification = Literal["measured", "bill_matched", "未验"]
Currency = Literal["CNY", "USD"]
UsageFlag = Literal[
    "retry",
    "speculation",
    "cancel_still_billed",
    "generated_unplayed",
    "free_tier",
]
OpsFlag = Literal["human_rescue"]

CLOUD_CATEGORIES: frozenset[str] = frozenset(
    {
        "asr",
        "tts",
        "chat",
        "plan",
        "end",
        "safety",
        "summary",
        "memory",
        "storage",
        "backup",
        "bandwidth",
        "base_ops",
    }
)
OPS_CATEGORIES: frozenset[str] = frozenset(
    {
        "manufacturing",
        "assembly_test",
        "packaging_ship",
        "returns_repairs",
        "support",
        "acquisition",
        "rd_fixed",
    }
)

DEFAULT_HANDLING_RULES: dict[str, str] = {
    "unallocated_fees": (
        "Keep unallocated and fixed/base_ops fees in a separate pool. "
        "Do not force-split them into household variable totals. "
        "Report the pool beside household lines."
    ),
    "free_tiers": (
        "Record free-tier quantity with flag free_tier. "
        "Amount is 0 when the bill marks the line free. "
        "Quantity still counts toward intensity."
    ),
    "cancel_after_bill": (
        "Include cancel_still_billed and generated_unplayed quantity in "
        "vendor reconciliation. Playback non-ack does not erase a billed call."
    ),
    "human_rescue": (
        "Attribute support minutes with flag human_rescue to the household "
        "when the ticket names one. Otherwise leave the line unallocated "
        "and list it under gaps if verification is 未验."
    ),
}


@dataclass(frozen=True, slots=True)
class ObservationWindow:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("observation_window.end must be on or after start")


@dataclass(frozen=True, slots=True)
class HandlingRules:
    unallocated_fees: str
    free_tiers: str
    cancel_after_bill: str
    human_rescue: str


@dataclass(frozen=True, slots=True)
class Household:
    household_id: str
    intensity_hint: IntensityHint


@dataclass(frozen=True, slots=True)
class UsageEvent:
    event_id: str
    household_id: str
    category: CloudCategory
    vendor: str
    unit: UsageUnit
    quantity: Decimal
    flags: frozenset[UsageFlag]


@dataclass(frozen=True, slots=True)
class VendorBillLine:
    bill_id: str
    vendor: str
    category: CloudCategory
    unit: UsageUnit
    quantity: Decimal
    amount_minor: int | None
    currency: Currency | None
    verification: Verification


@dataclass(frozen=True, slots=True)
class OpsLine:
    line_id: str
    category: OpsCategory
    cost_class: CostClass
    household_id: str | None
    unit: OpsUnit
    quantity: Decimal
    amount_minor: int | None
    currency: Currency | None
    flags: frozenset[OpsFlag]
    verification: Verification


@dataclass(frozen=True, slots=True)
class NetReceipt:
    household_id: str
    amount_minor: int | None
    currency: Currency | None
    verification: Verification


@dataclass(frozen=True, slots=True)
class LedgerSample:
    schema_version: int
    observation_window: ObservationWindow
    handling_rules: HandlingRules
    households: tuple[Household, ...]
    usage_events: tuple[UsageEvent, ...]
    vendor_bills: tuple[VendorBillLine, ...]
    ops_lines: tuple[OpsLine, ...]
    receipts: tuple[NetReceipt, ...]


@dataclass(frozen=True, slots=True)
class Gap:
    code: str
    detail: str
    household_id: str | None = None
    vendor: str | None = None
    category: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class CategoryQuantity:
    vendor: str
    category: str
    unit: str
    usage_quantity: Decimal
    bill_quantity: Decimal | None
    bill_amount_minor: int | None
    currency: Currency | None
    verification: Verification


@dataclass(frozen=True, slots=True)
class HouseholdLedger:
    household_id: str
    intensity: IntensityBucket
    cloud_by_category: tuple[CategoryQuantity, ...]
    variable_ops_amount_minor: int | None
    variable_ops_verification: Verification
    net_receipts_minor: int | None
    net_receipts_verification: Verification
    contribution_minor: int | None
    contribution_verification: Verification
    flagged_quantity: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    window: ObservationWindow
    handling_rules: HandlingRules
    households: tuple[HouseholdLedger, ...]
    intensity_distribution: dict[IntensityBucket, int]
    unallocated_ops: tuple[OpsLine, ...]
    gaps: tuple[Gap, ...]
    invented_prices: bool = False
