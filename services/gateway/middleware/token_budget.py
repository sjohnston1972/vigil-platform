"""
Per-tenant token budget enforcement.

Budget limits and running usage totals live on each tenant's `tenant_config`
Cosmos DB document (partition key: tenant_id):

    {
        "id": "tenant-a",
        "tenant_id": "tenant-a",
        "token_budget_daily": 200000,      # null/absent = no daily limit
        "token_budget_monthly": 4000000,   # null/absent = no monthly limit
        "tokens_used_today": 15234,
        "tokens_used_month": 812004,
        ...
    }

This module provides:
  - check_budget: a best-effort pre-flight check against the tenant's current
    balance, run before the Gateway opens the streaming proxy to the Coordinator.
  - deduct_tokens: adds a completed request's tokens_used to the running totals,
    called once the proxied stream's `done` event has been observed.

Fail closed: if the tenant's budget document can't be read at all (Cosmos DB
unreachable, credentials problem, tenant has no config document yet), the
pre-flight check reports the tenant as over budget. A budget check that cannot
positively verify the tenant is within budget must never silently allow the
request through — that would defeat the purpose of a cost control gate.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class BudgetStatus:
    allowed: bool
    reason: str | None = None  # None when allowed=True


async def check_budget(tenant_id: str, tenant_config_container) -> BudgetStatus:
    """
    Best-effort pre-flight budget check against the tenant's current balance.

    Returns BudgetStatus(allowed=False, reason=...) if the tenant is at or over
    its daily or monthly token budget, OR if the budget document could not be
    read at all (fail closed — see module docstring).
    """
    try:
        tenant_doc = await tenant_config_container.read_item(item=tenant_id, partition_key=tenant_id)
    except Exception as exc:
        logger.error(
            "Budget lookup failed for tenant — failing closed",
            extra={"tenant_id": tenant_id, "error": str(exc)},
        )
        return BudgetStatus(allowed=False, reason="budget_check_unavailable")

    daily_limit = tenant_doc.get("token_budget_daily")
    monthly_limit = tenant_doc.get("token_budget_monthly")
    used_today = tenant_doc.get("tokens_used_today", 0) or 0
    used_month = tenant_doc.get("tokens_used_month", 0) or 0

    if daily_limit is not None and used_today >= daily_limit:
        logger.info(
            "Tenant over daily token budget",
            extra={"tenant_id": tenant_id, "used_today": used_today, "daily_limit": daily_limit},
        )
        return BudgetStatus(allowed=False, reason="daily_budget_exceeded")

    if monthly_limit is not None and used_month >= monthly_limit:
        logger.info(
            "Tenant over monthly token budget",
            extra={"tenant_id": tenant_id, "used_month": used_month, "monthly_limit": monthly_limit},
        )
        return BudgetStatus(allowed=False, reason="monthly_budget_exceeded")

    return BudgetStatus(allowed=True)


async def deduct_tokens(tenant_id: str, tokens_used: int, tenant_config_container) -> None:
    """
    Add tokens_used to the tenant's running daily/monthly totals.

    Best-effort: this runs after the response has already been streamed to the
    client, so failures here are logged rather than raised — they cannot change
    an outcome the client has already received. The Coordinator's own Cosmos DB
    conversation + audit record (written before it emits `done`) remains the
    authoritative record of tokens consumed for this interaction regardless of
    whether this deduction succeeds.
    """
    if tokens_used is None or tokens_used <= 0:
        return
    try:
        tenant_doc = await tenant_config_container.read_item(item=tenant_id, partition_key=tenant_id)
        tenant_doc["tokens_used_today"] = (tenant_doc.get("tokens_used_today", 0) or 0) + tokens_used
        tenant_doc["tokens_used_month"] = (tenant_doc.get("tokens_used_month", 0) or 0) + tokens_used
        await tenant_config_container.replace_item(item=tenant_id, body=tenant_doc, partition_key=tenant_id)
    except Exception as exc:
        logger.warning(
            "Failed to deduct tokens from tenant budget",
            extra={"tenant_id": tenant_id, "tokens_used": tokens_used, "error": str(exc)},
        )
