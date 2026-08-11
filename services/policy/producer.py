"""Lifecycle guard for Policy-owned canonical producers.

This module deliberately delegates lifecycle authority to the generated
contract.  It does not duplicate the generated allow/deny table.  Every new
Policy decision, receipt, or runtime-profile producer calls this seam before
constructing output.
"""

from __future__ import annotations

from packages.contracts.generated.python import multi_subject_contracts as contracts


def require_policy_producer(contract_name: str) -> None:
    """Fail closed unless ``contract_name`` is a generated current producer."""
    contracts.require_new_producer_contract(contract_name)
