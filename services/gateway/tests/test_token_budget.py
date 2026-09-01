import pytest
from unittest.mock import AsyncMock

from middleware.token_budget import check_budget, deduct_tokens


def _mock_container(read_item_result=None, read_item_side_effect=None):
    container = AsyncMock()
    if read_item_side_effect is not None:
        container.read_item = AsyncMock(side_effect=read_item_side_effect)
    else:
        container.read_item = AsyncMock(return_value=read_item_result)
    container.replace_item = AsyncMock(return_value={})
    return container


class TestCheckBudgetAllows:
    @pytest.mark.asyncio
    async def test_within_daily_and_monthly_budget_is_allowed(self):
        container = _mock_container({
            "id": "tenant-a", "tenant_id": "tenant-a",
            "token_budget_daily": 1000, "token_budget_monthly": 20000,
            "tokens_used_today": 100, "tokens_used_month": 500,
        })
        status = await check_budget("tenant-a", container)
        assert status.allowed is True

    @pytest.mark.asyncio
    async def test_no_configured_limits_is_allowed(self):
        container = _mock_container({"id": "tenant-a", "tenant_id": "tenant-a"})
        status = await check_budget("tenant-a", container)
        assert status.allowed is True


class TestCheckBudgetRejects:
    @pytest.mark.asyncio
    async def test_daily_budget_exceeded_is_rejected(self):
        container = _mock_container({
            "id": "tenant-a", "tenant_id": "tenant-a",
            "token_budget_daily": 1000, "tokens_used_today": 1000,
        })
        status = await check_budget("tenant-a", container)
        assert status.allowed is False
        assert status.reason == "daily_budget_exceeded"

    @pytest.mark.asyncio
    async def test_monthly_budget_exceeded_is_rejected(self):
        container = _mock_container({
            "id": "tenant-a", "tenant_id": "tenant-a",
            "token_budget_monthly": 5000, "tokens_used_month": 5001,
        })
        status = await check_budget("tenant-a", container)
        assert status.allowed is False
        assert status.reason == "monthly_budget_exceeded"

    @pytest.mark.asyncio
    async def test_lookup_error_fails_closed(self):
        """
        If the tenant's budget document can't be read at all (Cosmos DB down,
        credentials issue, etc.) the check must reject the request rather than
        silently letting it through — a budget gate that fails open defeats its
        entire purpose.
        """
        container = _mock_container(read_item_side_effect=Exception("Cosmos DB unavailable"))
        status = await check_budget("tenant-a", container)
        assert status.allowed is False
        assert status.reason == "budget_check_unavailable"

    @pytest.mark.asyncio
    async def test_missing_tenant_document_fails_closed(self):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        container = _mock_container(read_item_side_effect=CosmosResourceNotFoundError())
        status = await check_budget("tenant-a", container)
        assert status.allowed is False

    @pytest.mark.asyncio
    async def test_tenant_isolation_one_tenants_budget_never_affects_another(self):
        container_a = _mock_container({
            "id": "tenant-a", "tenant_id": "tenant-a",
            "token_budget_daily": 100, "tokens_used_today": 100,
        })
        container_b = _mock_container({
            "id": "tenant-b", "tenant_id": "tenant-b",
            "token_budget_daily": 100, "tokens_used_today": 5,
        })
        status_a = await check_budget("tenant-a", container_a)
        status_b = await check_budget("tenant-b", container_b)
        assert status_a.allowed is False
        assert status_b.allowed is True


class TestDeductTokens:
    @pytest.mark.asyncio
    async def test_deducts_from_both_daily_and_monthly_totals(self):
        tenant_doc = {
            "id": "tenant-a", "tenant_id": "tenant-a",
            "tokens_used_today": 100, "tokens_used_month": 500,
        }
        container = _mock_container(tenant_doc)

        await deduct_tokens("tenant-a", 50, container)

        written = container.replace_item.call_args.kwargs["body"]
        assert written["tokens_used_today"] == 150
        assert written["tokens_used_month"] == 550

    @pytest.mark.asyncio
    async def test_zero_or_none_tokens_used_is_a_no_op(self):
        container = _mock_container({"id": "tenant-a", "tenant_id": "tenant-a"})

        await deduct_tokens("tenant-a", 0, container)
        await deduct_tokens("tenant-a", None, container)

        container.read_item.assert_not_called()
        container.replace_item.assert_not_called()

    @pytest.mark.asyncio
    async def test_deduction_failure_is_swallowed_not_raised(self):
        """
        Deduction runs after the client has already received the full response —
        a failure here must be logged, not raised, since there is no request left
        to fail.
        """
        container = _mock_container(read_item_side_effect=Exception("Cosmos DB unavailable"))
        await deduct_tokens("tenant-a", 50, container)  # must not raise
