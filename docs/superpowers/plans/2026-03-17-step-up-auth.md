# Step-Up Auth (Agent MFA) Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a human-in-the-loop approval gate to the VIGIL coordinator so high-risk tool calls (e.g. `apply_change`, `rollback_change`) are paused until an authorised human approves them, with in-chat and out-of-band (email/webhook) notification channels.

**Architecture:** The coordinator's SSE stream generator yields `approval_required` immediately when a high-risk tool is selected, then blocks on a Cosmos DB poll until approved/rejected/expired. `step_up.py` owns all lifecycle logic; the generator calls `prepare_step_up` (creates the record, fires notifications) then yields the SSE event, then calls `resolve_step_up` (polls, fetches Key Vault credential). Gateway adds transparent proxy routes for the approve/reject endpoints.

**Tech Stack:** Python 3.11, FastAPI, azure-cosmos, azure-keyvault-secrets, azure-communication-email, httpx, pytest, unittest.mock

**Spec:** `docs/superpowers/specs/2026-03-17-step-up-auth-design.md`

---

## Chunk 1: Terraform Infrastructure

### Task 1: Add step_up_requests and step_up_grants Cosmos DB containers

**Files:**
- Modify: `infrastructure/terraform/modules/cosmos-db/main.tf`

- [ ] **Step 1.1: Add step_up_requests container resource**

Open (or create) `infrastructure/terraform/modules/cosmos-db/main.tf` and add:

```hcl
resource "azurerm_cosmosdb_sql_container" "step_up_requests" {
  name                = "step_up_requests"
  resource_group_name = var.resource_group_name
  account_name        = var.cosmos_account_name
  database_name       = var.cosmos_database_name
  partition_key_path  = "/tenant_id"
  partition_key_version = 1

  indexing_policy {
    indexing_mode = "consistent"
    included_path { path = "/*" }
  }

  tags = var.tags
}
```

- [ ] **Step 1.2: Add step_up_grants container resource with default_ttl**

```hcl
resource "azurerm_cosmosdb_sql_container" "step_up_grants" {
  name                = "step_up_grants"
  resource_group_name = var.resource_group_name
  account_name        = var.cosmos_account_name
  database_name       = var.cosmos_database_name
  partition_key_path  = "/tenant_id"
  partition_key_version = 1

  # REQUIRED: default_ttl = -1 enables per-document TTL (_ttl field on each document).
  # Without this, the _ttl field is silently ignored and grants never expire in Cosmos DB.
  default_ttl = -1

  indexing_policy {
    indexing_mode = "consistent"
    included_path { path = "/*" }
  }

  tags = var.tags
}
```

- [ ] **Step 1.3: Validate Terraform config**

Run from the root Terraform directory (the module files are referenced from there — running `terraform validate` inside `modules/cosmos-db/` alone will error because modules have no provider configuration):

```bash
cd infrastructure/terraform
terraform init
terraform validate
```

Expected: `Success! The configuration is valid.`

If the root module does not yet exist (greenfield setup), skip this step and rely on CI/CD (`terraform validate` in the GitHub Actions workflow) to catch errors after the root module is wired up.

- [ ] **Step 1.4: Commit**

```bash
git add infrastructure/terraform/modules/cosmos-db/main.tf
git commit -m "feat(infra): add step_up_requests and step_up_grants Cosmos DB containers"
```

---

### Task 2: Set ACA ingress timeout to accommodate long approval polls

**Files:**
- Modify: `infrastructure/terraform/modules/container-apps/main.tf`

- [ ] **Step 2.1: Add ingress_timeout variable**

In `infrastructure/terraform/modules/container-apps/variables.tf` add:

```hcl
variable "ingress_timeout_seconds" {
  type        = number
  description = "HTTP request timeout in seconds. Must exceed the longest step_up pending_ttl_seconds + 60."
  default     = 960  # 900s (15min TTL) + 60s buffer
}
```

- [ ] **Step 2.2: Apply timeout via post-deploy CLI step in GitHub Actions**

The `azurerm_container_app` Terraform provider does not expose an HTTP ingress timeout attribute directly. Set it via the Azure CLI as a post-deploy step in every Container App deploy workflow (`.github/workflows/deploy-*.yml`):

Add this step after the `az containerapp update` image step:

```yaml
- name: Set ACA ingress timeout for step-up approval polls
  run: |
    az containerapp ingress update \
      --name vigil-coordinator \
      --resource-group rg-vigil-prod \
      --timeout 960
  # 960s = 900s max pending_ttl + 60s buffer.
  # Must exceed the longest configured pending_ttl_seconds or SSE stream closes before approval.
```

Also add a comment in `infrastructure/terraform/modules/container-apps/main.tf` above the ingress block to document the out-of-band setting:

```hcl
# NOTE: HTTP ingress timeout is set to 960s via az containerapp ingress update
# in the GitHub Actions deploy workflow (not exposed by the azurerm Terraform provider).
# This must exceed max(pending_ttl_seconds) + 60s across all step-up policies.
ingress {
  # ... existing ingress config ...
}
```

- [ ] **Step 2.3: Commit**

```bash
git add infrastructure/terraform/modules/container-apps/
git commit -m "feat(infra): configure ACA ingress timeout for step-up approval polls"
```

---

## Chunk 2: step_up.py — Core Lifecycle

### Task 3: Create step_up.py with data models and helper functions

**Files:**
- Create: `services/coordinator/step_up.py`
- Create: `services/coordinator/tests/test_step_up.py`
- Modify: `services/coordinator/requirements.txt`

- [ ] **Step 3.1: Add required packages to requirements.txt**

Ensure `services/coordinator/requirements.txt` contains:

```
fastapi
uvicorn
pydantic
azure-cosmos
azure-keyvault-secrets
azure-identity
azure-communication-email
httpx
python-dotenv
pytest
pytest-asyncio
```

- [ ] **Step 3.2: Write failing tests for create_step_up_request**

Create `services/coordinator/tests/test_step_up.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from dataclasses import dataclass


# Minimal ChatRequest stub for tests
@dataclass
class ChatRequest:
    tenant_id: str
    session_id: str
    user_identity: str = "jsmith@client.com"


class TestCreateStepUpRequest:
    @pytest.mark.asyncio
    async def test_creates_record_with_correct_fields(self):
        from step_up import create_step_up_request

        mock_container = AsyncMock()
        mock_container.create_item = AsyncMock(side_effect=lambda body, **_: body)

        policy = {
            "grant_type": "single_use",
            "pending_ttl_seconds": 900,
            "grant_duration_seconds": None,
        }
        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        tool_input = {"change_id": "chg-001", "device_host": "10.0.0.1"}

        with patch("step_up._step_up_container", mock_container):
            record = await create_step_up_request("apply_change", tool_input, request, policy)

        assert record["tenant_id"] == "tenant-a"
        assert record["session_id"] == "s-001"
        assert record["tool_name"] == "apply_change"
        assert record["requested_by"] == "jsmith@client.com"
        assert record["status"] == "pending"
        assert record["grant_type"] == "single_use"
        assert record["id"].startswith("sur-")
        assert "expires_at" in record
        mock_container.create_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_contains_tool_input_summary(self):
        from step_up import create_step_up_request

        mock_container = AsyncMock()
        mock_container.create_item = AsyncMock(side_effect=lambda body, **_: body)

        policy = {"grant_type": "single_use", "pending_ttl_seconds": 300, "grant_duration_seconds": None}
        request = ChatRequest(tenant_id="tenant-a", session_id="s-002")

        with patch("step_up._step_up_container", mock_container):
            record = await create_step_up_request("rollback_change", {"change_id": "chg-002"}, request, policy)

        assert "change_id" in record["context"] or "summary" in record["context"]
```

- [ ] **Step 3.3: Run failing tests**

```bash
cd services/coordinator
pytest tests/test_step_up.py::TestCreateStepUpRequest -v
```

Expected: `ImportError` or `ModuleNotFoundError` — step_up.py does not exist yet.

- [ ] **Step 3.4: Create step_up.py with imports, container setup, and create_step_up_request**

Create `services/coordinator/step_up.py`:

```python
"""
Step-up auth lifecycle — human-in-the-loop approval gate for high-risk coordinator tools.

IMPORTANT: write credentials fetched here are a deliberate exception to the startup-fetch rule.
They are fetched just-in-time after approval (not cached) so revocation takes immediate effect.
See CLAUDE.md for the documented exception.
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient

logger = logging.getLogger(__name__)

# Module-level containers — initialised at startup via init_step_up_containers()
_step_up_container = None
_grants_container = None
_tenant_config_container = None
_kv_client = None


async def init_step_up_containers(cosmos_client: CosmosClient, kv_url: str):
    """Call once at coordinator startup to wire up Cosmos DB containers and Key Vault client."""
    global _step_up_container, _grants_container, _tenant_config_container, _kv_client

    db = cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    _step_up_container = db.get_container_client("step_up_requests")
    _grants_container = db.get_container_client("step_up_grants")
    _tenant_config_container = db.get_container_client("tenant_config")

    credential = DefaultAzureCredential()
    _kv_client = SecretClient(vault_url=kv_url, credential=credential)


@dataclass
class StepUpResult:
    status: str           # "approved" | "rejected" | "expired" | "failed"
    request_id: str
    approved_by: Optional[str]
    credential: Optional[str]   # populated only on "approved"


async def create_step_up_request(
    tool_name: str,
    tool_input: dict,
    request,           # ChatRequest — avoid circular import with type annotation
    policy: dict,
) -> dict:
    """
    Creates a step_up_requests record in Cosmos DB and returns the document.
    The caller should emit approval_required SSE immediately after this returns,
    then call resolve_step_up to block until a decision is made.
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=policy["pending_ttl_seconds"])
    request_id = f"sur-{uuid.uuid4()}"

    # Build a human-readable context summary for the approval prompt
    context = _build_context(tool_name, tool_input)

    record = {
        "id": request_id,
        "tenant_id": request.tenant_id,
        "session_id": request.session_id,
        "tool_name": tool_name,
        "requested_by": request.user_identity,
        "requested_at": now.isoformat(),
        "status": "pending",
        "context": context,
        "approved_by": None,
        "approved_at": None,
        "expires_at": expires_at.isoformat(),
        "grant_type": policy["grant_type"],
        "grant_duration_seconds": policy.get("grant_duration_seconds"),
    }

    await _step_up_container.create_item(body=record, enable_automatic_id_generation=False)

    logger.info(
        "Step-up request created",
        extra={
            "tenant_id": request.tenant_id,
            "request_id": request_id,
            "tool_name": tool_name,
            "expires_at": expires_at.isoformat(),
        },
    )
    return record


def _build_context(tool_name: str, tool_input: dict) -> dict:
    """Extract a safe, human-readable summary from tool input for the approval prompt."""
    context = {"tool": tool_name}
    # Include safe fields — never include raw configs or credentials
    safe_keys = {"change_id", "device_host", "summary", "ticket_id", "action"}
    for key in safe_keys:
        if key in tool_input:
            context[key] = tool_input[key]
    if "summary" not in context:
        context["summary"] = f"Tool: {tool_name}"
    return context
```

- [ ] **Step 3.5: Run tests — should pass**

```bash
cd services/coordinator
pytest tests/test_step_up.py::TestCreateStepUpRequest -v
```

Expected: `2 passed`

- [ ] **Step 3.6: Commit**

```bash
git add services/coordinator/step_up.py services/coordinator/tests/test_step_up.py services/coordinator/requirements.txt
git commit -m "feat(coordinator): add create_step_up_request and data models"
```

---

### Task 4: Add get_active_grant, write_active_grant, mark_step_up_failed

**Files:**
- Modify: `services/coordinator/step_up.py`
- Modify: `services/coordinator/tests/test_step_up.py`

- [ ] **Step 4.1: Write failing tests**

Append to `services/coordinator/tests/test_step_up.py`:

```python
class TestGetActiveGrant:
    @pytest.mark.asyncio
    async def test_returns_grant_when_active(self):
        from step_up import get_active_grant

        mock_grant = {
            "id": "grnt-001",
            "tenant_id": "tenant-a",
            "session_id": "s-001",
            "tool_name": "bulk_close_tickets",
            "approved_by": "approver@client.com",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        mock_container = MagicMock()
        mock_container.query_items = MagicMock(return_value=[mock_grant])

        with patch("step_up._grants_container", mock_container):
            result = await get_active_grant("tenant-a", "s-001", "bulk_close_tickets")

        assert result == mock_grant
        mock_container.query_items.assert_called_once()
        call_kwargs = mock_container.query_items.call_args.kwargs
        assert call_kwargs["partition_key"] == "tenant-a"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_grant(self):
        from step_up import get_active_grant

        mock_container = MagicMock()
        mock_container.query_items = MagicMock(return_value=[])

        with patch("step_up._grants_container", mock_container):
            result = await get_active_grant("tenant-a", "s-001", "apply_change")

        assert result is None


class TestMarkStepUpFailed:
    @pytest.mark.asyncio
    async def test_sets_status_to_failed(self):
        from step_up import mark_step_up_failed

        existing = {"id": "sur-001", "tenant_id": "tenant-a", "status": "approved"}
        mock_container = AsyncMock()
        mock_container.read_item = AsyncMock(return_value=dict(existing))
        mock_container.replace_item = AsyncMock()

        with patch("step_up._step_up_container", mock_container):
            await mark_step_up_failed("sur-001", "tenant-a")

        replaced = mock_container.replace_item.call_args.kwargs["body"]
        assert replaced["status"] == "failed"
        mock_container.read_item.assert_called_once_with(item="sur-001", partition_key="tenant-a")
```

- [ ] **Step 4.2: Run failing tests**

```bash
cd services/coordinator
pytest tests/test_step_up.py::TestGetActiveGrant tests/test_step_up.py::TestMarkStepUpFailed -v
```

Expected: `ImportError` for `get_active_grant`, `mark_step_up_failed`

- [ ] **Step 4.3: Implement get_active_grant, write_active_grant, mark_step_up_failed**

Append to `services/coordinator/step_up.py`:

```python
async def get_active_grant(tenant_id: str, session_id: str, tool_name: str) -> Optional[dict]:
    """
    Returns an active time-window grant for this session+tool, or None.
    Uses partition_key=tenant_id — never a cross-partition query.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    results = list(
        _grants_container.query_items(
            query="""SELECT * FROM c
                     WHERE c.session_id = @session_id
                       AND c.tool_name  = @tool_name
                       AND c.expires_at  > @now""",
            parameters=[
                {"name": "@session_id", "value": session_id},
                {"name": "@tool_name",  "value": tool_name},
                {"name": "@now",        "value": now_iso},
            ],
            partition_key=tenant_id,  # always tenant-scoped
        )
    )
    return results[0] if results else None


async def write_active_grant(
    tool_name: str,
    approved_by: str,
    request,
    policy: dict,
) -> None:
    """Write a time-window grant to step_up_grants after approval. Cosmos DB TTL handles expiry."""
    now = datetime.now(timezone.utc)
    duration = policy["grant_duration_seconds"]
    expires_at = now + timedelta(seconds=duration)
    grant = {
        "id": f"grnt-{uuid.uuid4()}",
        "tenant_id": request.tenant_id,
        "session_id": request.session_id,
        "tool_name": tool_name,
        "approved_by": approved_by,
        "granted_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "_ttl": duration,  # Cosmos DB per-document TTL (requires container default_ttl=-1)
    }
    await _grants_container.create_item(body=grant, enable_automatic_id_generation=False)
    logger.info(
        "Active grant written",
        extra={"tenant_id": request.tenant_id, "tool_name": tool_name, "expires_at": expires_at.isoformat()},
    )


async def mark_step_up_failed(request_id: str, tenant_id: str) -> None:
    """Mark a step-up request as failed (approved but Key Vault credential fetch failed)."""
    record = await _step_up_container.read_item(item=request_id, partition_key=tenant_id)
    record["status"] = "failed"
    await _step_up_container.replace_item(item=record["id"], body=record, partition_key=tenant_id)
    logger.warning(
        "Step-up request marked failed",
        extra={"tenant_id": tenant_id, "request_id": request_id},
    )
```

- [ ] **Step 4.4: Run tests — should pass**

```bash
cd services/coordinator
pytest tests/test_step_up.py -v
```

Expected: all tests pass

- [ ] **Step 4.5: Commit**

```bash
git add services/coordinator/step_up.py services/coordinator/tests/test_step_up.py
git commit -m "feat(coordinator): add grant management and mark_step_up_failed"
```

---

### Task 5: Add await_step_up_decision and fetch_tool_credential

**Files:**
- Modify: `services/coordinator/step_up.py`
- Modify: `services/coordinator/tests/test_step_up.py`

- [ ] **Step 5.1: Write failing tests**

Append to `services/coordinator/tests/test_step_up.py`:

```python
class TestAwaitStepUpDecision:
    @pytest.mark.asyncio
    async def test_returns_approved_when_status_changes(self):
        from step_up import await_step_up_decision

        pending = {"id": "sur-001", "tenant_id": "tenant-a", "status": "pending",
                   "expires_at": "2099-01-01T00:00:00+00:00", "approved_by": None}
        approved = {**pending, "status": "approved", "approved_by": "approver@client.com",
                    "approved_at": "2026-03-17T10:01:00+00:00"}

        call_count = 0
        async def mock_read_item(item, partition_key):
            nonlocal call_count
            call_count += 1
            return pending if call_count < 2 else approved

        mock_container = AsyncMock()
        mock_container.read_item = mock_read_item

        policy = {"pending_ttl_seconds": 900}

        with patch("step_up._step_up_container", mock_container):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await await_step_up_decision("sur-001", "tenant-a", policy)

        assert result["status"] == "approved"
        assert result["approved_by"] == "approver@client.com"

    @pytest.mark.asyncio
    async def test_returns_expired_when_past_expires_at(self):
        from step_up import await_step_up_decision

        expired_record = {
            "id": "sur-002", "tenant_id": "tenant-a", "status": "pending",
            "expires_at": "2000-01-01T00:00:00+00:00",  # already past
            "approved_by": None,
        }
        mock_container = AsyncMock()
        mock_container.read_item = AsyncMock(return_value=expired_record)
        mock_container.replace_item = AsyncMock()

        policy = {"pending_ttl_seconds": 900}

        with patch("step_up._step_up_container", mock_container):
            result = await await_step_up_decision("sur-002", "tenant-a", policy)

        assert result["status"] == "expired"


class TestFetchToolCredential:
    @pytest.mark.asyncio
    async def test_fetches_secret_with_hyphenated_name(self):
        from step_up import fetch_tool_credential

        mock_kv = AsyncMock()
        mock_kv.get_secret = AsyncMock(return_value=MagicMock(value="secret-value"))

        with patch("step_up._kv_client", mock_kv):
            result = await fetch_tool_credential("apply_change", "acme")

        assert result == "secret-value"
        # Underscores replaced with hyphens in Key Vault secret name
        mock_kv.get_secret.assert_called_once_with("tenant-acme-apply-change-write-credential")
```

- [ ] **Step 5.2: Run failing tests**

```bash
cd services/coordinator
pytest tests/test_step_up.py::TestAwaitStepUpDecision tests/test_step_up.py::TestFetchToolCredential -v
```

Expected: `ImportError` for `await_step_up_decision`, `fetch_tool_credential`

- [ ] **Step 5.3: Implement await_step_up_decision and fetch_tool_credential**

Append to `services/coordinator/step_up.py`:

```python
async def await_step_up_decision(request_id: str, tenant_id: str, policy: dict) -> dict:
    """
    Polls step_up_requests until status is no longer 'pending', or expires_at is reached.
    Uses point reads (read_item) with (request_id, tenant_id) — never a cross-partition query.
    Capped at pending_ttl_seconds. Caller is responsible for SSE keepalive heartbeats.
    """
    backoff_seconds = [1, 2, 4, 8, 10]
    backoff_idx = 0

    while True:
        # Point read — tenant-scoped, cheap
        record = await _step_up_container.read_item(item=request_id, partition_key=tenant_id)

        if record["status"] != "pending":
            return record

        # Check wall-clock expiry
        expires_at = datetime.fromisoformat(record["expires_at"])
        if datetime.now(timezone.utc) >= expires_at:
            record["status"] = "expired"
            await _step_up_container.replace_item(item=record["id"], body=record, partition_key=tenant_id)
            logger.info("Step-up request expired", extra={"tenant_id": tenant_id, "request_id": request_id})
            return record

        sleep_for = backoff_seconds[min(backoff_idx, len(backoff_seconds) - 1)]
        backoff_idx += 1
        await asyncio.sleep(sleep_for)


async def fetch_tool_credential(tool_name: str, tenant_id: str) -> str:
    """
    Fetches the write credential for a tool from Azure Key Vault just-in-time.

    DELIBERATE EXCEPTION to the startup-fetch rule: these credentials are only fetched
    after human approval and are never cached, so revocation takes immediate effect.
    Secret name format: tenant-{tenant_id}-{tool-name}-write-credential
    (underscores in tool_name replaced with hyphens — Key Vault names allow only hyphens)
    """
    tool_name_hyphenated = tool_name.replace("_", "-")
    secret_name = f"tenant-{tenant_id}-{tool_name_hyphenated}-write-credential"
    secret = await _kv_client.get_secret(secret_name)
    return secret.value
```

- [ ] **Step 5.4: Run all step_up tests**

```bash
cd services/coordinator
pytest tests/test_step_up.py -v
```

Expected: all tests pass

- [ ] **Step 5.5: Commit**

```bash
git add services/coordinator/step_up.py services/coordinator/tests/test_step_up.py
git commit -m "feat(coordinator): add await_step_up_decision and fetch_tool_credential"
```

---

### Task 6: Add prepare_step_up, resolve_step_up, and recovery task

**Files:**
- Modify: `services/coordinator/step_up.py`
- Modify: `services/coordinator/tests/test_step_up.py`

- [ ] **Step 6.1: Write failing tests**

Append to `services/coordinator/tests/test_step_up.py`:

```python
class TestPrepareStepUp:
    @pytest.mark.asyncio
    async def test_returns_none_when_active_grant_exists(self):
        from step_up import prepare_step_up

        active_grant = {"id": "grnt-001", "approved_by": "approver@client.com"}
        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {
            "grant_type": "time_window",
            "pending_ttl_seconds": 300,
            "notification_channels": ["in_chat"],
            "grant_duration_seconds": 1800,
        }
        tenant_config = {}

        with patch("step_up.get_active_grant", new_callable=AsyncMock, return_value=active_grant):
            with patch("step_up.create_step_up_request", new_callable=AsyncMock) as mock_create:
                result = await prepare_step_up("bulk_close_tickets", {}, request, policy, tenant_config)

        assert result is None
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_request_when_no_active_grant(self):
        from step_up import prepare_step_up

        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {
            "grant_type": "single_use",
            "pending_ttl_seconds": 900,
            "notification_channels": ["in_chat"],
        }
        tenant_config = {}
        mock_record = {"id": "sur-001", "status": "pending"}

        with patch("step_up.create_step_up_request", new_callable=AsyncMock, return_value=mock_record) as mock_create:
            result = await prepare_step_up("apply_change", {"change_id": "chg-001"}, request, policy, tenant_config)

        assert result == mock_record
        mock_create.assert_called_once()


class TestResolveStepUp:
    @pytest.mark.asyncio
    async def test_returns_approved_with_credential(self):
        from step_up import resolve_step_up

        step_up_req = {"id": "sur-001", "tool_name": "apply_change"}
        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {"grant_type": "single_use"}
        decision = {"status": "approved", "approved_by": "approver@client.com"}

        with patch("step_up.await_step_up_decision", new_callable=AsyncMock, return_value=decision):
            with patch("step_up.fetch_tool_credential", new_callable=AsyncMock, return_value="cred-abc"):
                result = await resolve_step_up(step_up_req, request, policy)

        assert result.status == "approved"
        assert result.credential == "cred-abc"
        assert result.approved_by == "approver@client.com"

    @pytest.mark.asyncio
    async def test_returns_rejected_without_credential(self):
        from step_up import resolve_step_up

        step_up_req = {"id": "sur-002", "tool_name": "apply_change"}
        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {"grant_type": "single_use"}
        decision = {"status": "rejected", "approved_by": "approver@client.com"}

        with patch("step_up.await_step_up_decision", new_callable=AsyncMock, return_value=decision):
            result = await resolve_step_up(step_up_req, request, policy)

        assert result.status == "rejected"
        assert result.credential is None

    @pytest.mark.asyncio
    async def test_returns_failed_when_kv_unreachable(self):
        from step_up import resolve_step_up

        step_up_req = {"id": "sur-003", "tool_name": "apply_change"}
        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {"grant_type": "single_use"}
        decision = {"status": "approved", "approved_by": "approver@client.com"}

        with patch("step_up.await_step_up_decision", new_callable=AsyncMock, return_value=decision):
            with patch("step_up.fetch_tool_credential", new_callable=AsyncMock, side_effect=Exception("KV timeout")):
                with patch("step_up.mark_step_up_failed", new_callable=AsyncMock):
                    result = await resolve_step_up(step_up_req, request, policy)

        assert result.status == "failed"
        assert result.credential is None
```

- [ ] **Step 6.2: Run failing tests**

```bash
cd services/coordinator
pytest tests/test_step_up.py::TestPrepareStepUp tests/test_step_up.py::TestResolveStepUp -v
```

Expected: `ImportError` for `prepare_step_up`, `resolve_step_up`

- [ ] **Step 6.3: Implement prepare_step_up, resolve_step_up, and recovery task**

Append to `services/coordinator/step_up.py`:

```python
async def prepare_step_up(
    tool_name: str,
    tool_input: dict,
    request,
    policy: dict,
    tenant_config: dict,
) -> Optional[dict]:
    """
    Creates the step_up_requests record and fires out-of-band notifications.
    Returns the request document so the caller can emit approval_required SSE immediately.
    Returns None if an active time-window grant exists — caller proceeds without approval.
    """
    if policy["grant_type"] == "time_window":
        grant = await get_active_grant(request.tenant_id, request.session_id, tool_name)
        if grant:
            logger.info(
                "Active time-window grant found — skipping step-up",
                extra={"tenant_id": request.tenant_id, "tool_name": tool_name},
            )
            return None

    step_up_req = await create_step_up_request(tool_name, tool_input, request, policy)

    channels = policy.get("notification_channels", [])
    if "email" in channels or "webhook" in channels:
        # Fire-and-forget — notification failure never blocks the in-chat approval path
        asyncio.ensure_future(_notify_out_of_band(step_up_req, tenant_config))

    return step_up_req


async def resolve_step_up(
    step_up_req: dict,
    request,
    policy: dict,
) -> StepUpResult:
    """
    Polls for the approval decision, then fetches the Key Vault credential on approval.
    Writes an active grant for time_window tools.
    Call this after emitting approval_required SSE — it blocks until a decision is made.
    """
    decision = await await_step_up_decision(step_up_req["id"], request.tenant_id, policy)

    if decision["status"] in ("rejected", "expired"):
        return StepUpResult(
            status=decision["status"],
            request_id=step_up_req["id"],
            approved_by=decision.get("approved_by"),
            credential=None,
        )

    # Approved — fetch credential just-in-time (deliberate KV exception, see module docstring)
    try:
        credential = await fetch_tool_credential(step_up_req["tool_name"], request.tenant_id)
    except Exception as exc:
        logger.error(
            "Key Vault credential fetch failed after approval",
            extra={"tenant_id": request.tenant_id, "request_id": step_up_req["id"]},
            exc_info=exc,
        )
        await mark_step_up_failed(step_up_req["id"], request.tenant_id)
        return StepUpResult(
            status="failed",
            request_id=step_up_req["id"],
            approved_by=decision["approved_by"],
            credential=None,
        )

    if policy["grant_type"] == "time_window":
        await write_active_grant(step_up_req["tool_name"], decision["approved_by"], request, policy)

    return StepUpResult(
        status="approved",
        request_id=step_up_req["id"],
        approved_by=decision["approved_by"],
        credential=credential,
    )


async def recover_expired_step_up_requests() -> None:
    """
    Expires step_up_requests records that are still pending past their expires_at.
    Runs at startup and on a 5-minute schedule.

    INTENTIONAL cross-partition read: tenant_config_container.read_all_items() is the
    only permitted cross-partition query in this service — used solely to bootstrap
    the tenant ID list. All subsequent queries use partition_key=tenant_id.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    tenant_ids = [doc["tenant_id"] for doc in _tenant_config_container.read_all_items()]

    for tenant_id in tenant_ids:
        stuck = _step_up_container.query_items(
            query="SELECT * FROM c WHERE c.status = 'pending' AND c.expires_at < @now",
            parameters=[{"name": "@now", "value": now_iso}],
            partition_key=tenant_id,
        )
        for record in stuck:
            record["status"] = "expired"
            await _step_up_container.replace_item(
                item=record["id"], body=record, partition_key=tenant_id
            )
            logger.info(
                "Recovery: expired stale step-up request",
                extra={"tenant_id": tenant_id, "request_id": record["id"]},
            )


async def _notify_out_of_band(step_up_req: dict, tenant_config: dict) -> None:
    """Internal helper — delegates to notifications module. Never raises."""
    try:
        from notifications import notify_approvers
        await notify_approvers(step_up_req, tenant_config)
    except Exception as exc:
        logger.warning(
            "Out-of-band notification failed — in-chat path unaffected",
            extra={"tenant_id": step_up_req["tenant_id"], "request_id": step_up_req["id"]},
            exc_info=exc,
        )
```

- [ ] **Step 6.4: Run all step_up tests**

```bash
cd services/coordinator
pytest tests/test_step_up.py -v
```

Expected: all tests pass

- [ ] **Step 6.5: Commit**

```bash
git add services/coordinator/step_up.py services/coordinator/tests/test_step_up.py
git commit -m "feat(coordinator): add prepare_step_up, resolve_step_up, recovery task"
```

---

## Chunk 3: Notifications + Coordinator Endpoints

### Task 7: Create notifications.py

**Files:**
- Create: `services/coordinator/notifications.py`
- Create: `services/coordinator/tests/test_notifications.py`

- [ ] **Step 7.1: Write failing tests**

Create `services/coordinator/tests/test_notifications.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


STEP_UP_REQ = {
    "id": "sur-001",
    "tenant_id": "tenant-a",
    "tool_name": "apply_change",
    "requested_by": "user@client.com",
    "context": {"change_id": "chg-001", "summary": "Shut down Gi0/1"},
    "expires_at": "2026-03-17T10:15:00+00:00",
}

TENANT_CONFIG_EMAIL = {
    "step_up_notification_email": "approvals@client.com",
    "step_up_webhook_url": None,
}

TENANT_CONFIG_WEBHOOK = {
    "step_up_notification_email": None,
    "step_up_webhook_url": "https://hooks.example.com/vigil",
}


class TestNotifyApprovers:
    @pytest.mark.asyncio
    async def test_sends_email_when_configured(self):
        from notifications import notify_approvers

        with patch("notifications._send_email", new_callable=AsyncMock) as mock_email:
            await notify_approvers(STEP_UP_REQ, TENANT_CONFIG_EMAIL)

        mock_email.assert_called_once()
        args = mock_email.call_args
        assert "approvals@client.com" in args[0]
        body = args[0][1]
        assert body["tool"] == "apply_change"
        assert "approve_url" in body
        assert "reject_url" in body

    @pytest.mark.asyncio
    async def test_sends_webhook_when_configured(self):
        from notifications import notify_approvers

        with patch("notifications._post_webhook", new_callable=AsyncMock) as mock_hook:
            await notify_approvers(STEP_UP_REQ, TENANT_CONFIG_WEBHOOK)

        mock_hook.assert_called_once()
        url_arg = mock_hook.call_args[0][0]
        assert url_arg == "https://hooks.example.com/vigil"

    @pytest.mark.asyncio
    async def test_does_not_raise_on_email_failure(self):
        from notifications import notify_approvers

        with patch("notifications._send_email", new_callable=AsyncMock, side_effect=Exception("SMTP down")):
            # Must not propagate — fire-and-forget
            await notify_approvers(STEP_UP_REQ, TENANT_CONFIG_EMAIL)
```

- [ ] **Step 7.2: Run failing tests**

```bash
cd services/coordinator
pytest tests/test_notifications.py -v
```

Expected: `ImportError` — notifications.py does not exist

- [ ] **Step 7.3: Create notifications.py**

Create `services/coordinator/notifications.py`:

```python
"""
Out-of-band approval notifications for step-up auth.

Fire-and-forget: all functions catch their own exceptions.
Notification failure never blocks the in-chat approval path.
"""

import logging
import os

import httpx
from azure.communication.email.aio import EmailClient
from azure.identity.aio import DefaultAzureCredential

logger = logging.getLogger(__name__)

GATEWAY_EXTERNAL_URL = os.getenv("GATEWAY_EXTERNAL_URL", "")


async def notify_approvers(step_up_request: dict, tenant_config: dict) -> None:
    """Send approval notification to configured out-of-band channels. Never raises."""
    approve_url = f"{GATEWAY_EXTERNAL_URL}/step-up/{step_up_request['id']}/approve"
    reject_url  = f"{GATEWAY_EXTERNAL_URL}/step-up/{step_up_request['id']}/reject"

    body = {
        "tool":         step_up_request["tool_name"],
        "requested_by": step_up_request["requested_by"],
        "context":      step_up_request["context"],
        "expires_at":   step_up_request["expires_at"],
        "approve_url":  approve_url,
        "reject_url":   reject_url,
    }

    email = tenant_config.get("step_up_notification_email")
    if email:
        await _send_email(email, body)

    webhook = tenant_config.get("step_up_webhook_url")
    if webhook:
        await _post_webhook(webhook, body)


async def _send_email(recipient: str, body: dict) -> None:
    """Send approval email via Azure Communication Services (Managed Identity)."""
    try:
        credential = DefaultAzureCredential()
        client = EmailClient(
            endpoint=os.getenv("ACS_ENDPOINT", ""),
            credential=credential,
        )
        message = {
            "senderAddress": os.getenv("ACS_SENDER_ADDRESS", "noreply@vigil"),
            "recipients": {"to": [{"address": recipient}]},
            "content": {
                "subject": f"[VIGIL] Approval required: {body['tool']}",
                "plainText": (
                    f"Tool: {body['tool']}\n"
                    f"Requested by: {body['requested_by']}\n"
                    f"Expires: {body['expires_at']}\n\n"
                    f"Approve: {body['approve_url']}\n"
                    f"Reject:  {body['reject_url']}"
                ),
            },
        }
        async with client:
            await client.begin_send(message)
    except Exception as exc:
        logger.warning(
            "Email notification failed",
            extra={"recipient": recipient, "tool": body.get("tool")},
            exc_info=exc,
        )


async def _post_webhook(url: str, body: dict) -> None:
    """POST approval payload to a webhook URL. Single retry on failure."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=body)
                resp.raise_for_status()
            return
        except Exception as exc:
            if attempt == 0:
                logger.debug("Webhook attempt 1 failed, retrying", exc_info=exc)
            else:
                logger.warning(
                    "Webhook notification failed after retry",
                    extra={"url": url, "tool": body.get("tool")},
                    exc_info=exc,
                )
```

- [ ] **Step 7.4: Run tests**

```bash
cd services/coordinator
pytest tests/test_notifications.py -v
```

Expected: `3 passed`

- [ ] **Step 7.5: Commit**

```bash
git add services/coordinator/notifications.py services/coordinator/tests/test_notifications.py
git commit -m "feat(coordinator): add notifications helper for out-of-band approvals"
```

---

### Task 8: Add approve/reject endpoints to coordinator/main.py

**Files:**
- Modify: `services/coordinator/main.py`
- Create: `services/coordinator/tests/test_endpoints.py`

- [ ] **Step 8.1: Write failing endpoint tests**

Create `services/coordinator/tests/test_endpoints.py`:

```python
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch


# Helper to build a mock app with the step-up endpoints
def make_app():
    from main import app
    return app


class TestStepUpApproveEndpoint:
    def test_approve_returns_200_for_valid_approver(self):
        pending_record = {
            "id": "sur-001", "tenant_id": "tenant-a",
            "status": "pending", "expires_at": "2099-01-01T00:00:00+00:00",
            "requested_by": "user@client.com",
            "tool_name": "apply_change",
            "context": {"change_id": "chg-001"},
            "approved_by": None, "approved_at": None,
        }
        tenant_config = {
            "tenant_id": "tenant-a",
            "step_up_approvers": ["approver@client.com"],
            "step_up_policy": {
                "apply_change": {"self_approve": False}
            },
        }

        with patch("main._get_step_up_request", new_callable=AsyncMock, return_value=pending_record):
            with patch("main._get_tenant_config", new_callable=AsyncMock, return_value=tenant_config):
                with patch("main._write_step_up_decision", new_callable=AsyncMock):
                    with patch("main._propagate_approval_to_change_records", new_callable=AsyncMock):
                        client = TestClient(make_app())
                        # Headers simulating Gateway-extracted tenant_id and user identity
                        response = client.post(
                            "/step-up/sur-001/approve",
                            json={"comment": None},
                            headers={"X-Tenant-Id": "tenant-a", "X-User-Identity": "approver@client.com"},
                        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "approved"
        assert data["decided_by"] == "approver@client.com"

    def test_approve_returns_403_for_self_approval_on_high_risk_tool(self):
        pending_record = {
            "id": "sur-002", "tenant_id": "tenant-a",
            "status": "pending", "expires_at": "2099-01-01T00:00:00+00:00",
            "requested_by": "user@client.com",
            "tool_name": "apply_change",
            "context": {}, "approved_by": None, "approved_at": None,
        }
        tenant_config = {
            "tenant_id": "tenant-a",
            "step_up_approvers": ["approver@client.com", "user@client.com"],
            "step_up_policy": {"apply_change": {"self_approve": False}},
        }

        with patch("main._get_step_up_request", new_callable=AsyncMock, return_value=pending_record):
            with patch("main._get_tenant_config", new_callable=AsyncMock, return_value=tenant_config):
                client = TestClient(make_app())
                response = client.post(
                    "/step-up/sur-002/approve",
                    json={},
                    headers={"X-Tenant-Id": "tenant-a", "X-User-Identity": "user@client.com"},
                )

        assert response.status_code == 403
        data = response.json()
        assert data["error"] == "self_approval_not_permitted"

    def test_approve_returns_403_and_logs_audit(self):
        """Forbidden attempts must be written to the audit log (structured logger.warning)."""
        import logging
        pending_record = {
            "id": "sur-004", "tenant_id": "tenant-a",
            "status": "pending", "expires_at": "2099-01-01T00:00:00+00:00",
            "requested_by": "user@client.com",
            "tool_name": "apply_change",
            "context": {}, "approved_by": None, "approved_at": None,
        }
        tenant_config = {
            "tenant_id": "tenant-a",
            "step_up_approvers": ["approver@client.com"],
            "step_up_policy": {"apply_change": {"self_approve": False}},
        }

        with patch("main._get_step_up_request", new_callable=AsyncMock, return_value=pending_record):
            with patch("main._get_tenant_config", new_callable=AsyncMock, return_value=tenant_config):
                with patch("main.logger") as mock_logger:
                    client = TestClient(make_app())
                    response = client.post(
                        "/step-up/sur-004/approve",
                        json={},
                        headers={"X-Tenant-Id": "tenant-a", "X-User-Identity": "unknown@client.com"},
                    )

        assert response.status_code == 403
        # logger.warning must be called with the audit fields
        mock_logger.warning.assert_called_once()
        call_kwargs = mock_logger.warning.call_args
        extra = call_kwargs[1].get("extra", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {})
        assert extra.get("reason") == "not_authorised_approver"

    def test_approve_returns_409_on_duplicate(self):
        already_approved = {
            "id": "sur-003", "tenant_id": "tenant-a",
            "status": "approved", "expires_at": "2099-01-01T00:00:00+00:00",
            "requested_by": "user@client.com",
            "tool_name": "apply_change",
            "context": {}, "approved_by": "approver@client.com", "approved_at": "...",
        }
        tenant_config = {
            "tenant_id": "tenant-a",
            "step_up_approvers": ["approver@client.com"],
            "step_up_policy": {"apply_change": {"self_approve": False}},
        }

        with patch("main._get_step_up_request", new_callable=AsyncMock, return_value=already_approved):
            with patch("main._get_tenant_config", new_callable=AsyncMock, return_value=tenant_config):
                client = TestClient(make_app())
                response = client.post(
                    "/step-up/sur-003/approve",
                    json={},
                    headers={"X-Tenant-Id": "tenant-a", "X-User-Identity": "approver@client.com"},
                )

        assert response.status_code == 409
        assert response.json()["error"] == "already_decided"
```

- [ ] **Step 8.2: Run failing tests**

```bash
cd services/coordinator
pytest tests/test_endpoints.py -v
```

Expected: `ImportError` or test failures because endpoints don't exist

- [ ] **Step 8.3: Create/update coordinator/main.py with step-up endpoints**

Ensure `services/coordinator/main.py` contains at minimum:

```python
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Cosmos DB client initialised at startup
_cosmos_client = None


@app.on_event("startup")
async def startup():
    global _cosmos_client
    credential = DefaultAzureCredential()
    _cosmos_client = CosmosClient(
        url=os.getenv("COSMOS_ENDPOINT"),
        credential=credential,
    )
    from step_up import init_step_up_containers
    await init_step_up_containers(_cosmos_client, os.getenv("KEY_VAULT_URL", ""))
    # Schedule recovery task
    asyncio.ensure_future(_run_recovery_loop())


async def _run_recovery_loop():
    from step_up import recover_expired_step_up_requests
    while True:
        try:
            await recover_expired_step_up_requests()
        except Exception as exc:
            logger.warning("Recovery loop error", exc_info=exc)
        await asyncio.sleep(300)  # every 5 minutes


@app.get("/health")
def health():
    return {"status": "healthy", "service": "coordinator"}


# ── Pydantic models ────────────────────────────────────────────────────────────

class StepUpDecisionRequest(BaseModel):
    comment: str | None = None


class StepUpDecisionResponse(BaseModel):
    request_id: str
    status: str
    decided_by: str
    decided_at: str


class StepUpErrorResponse(BaseModel):
    error: str
    request_id: str
    detail: str | None = None


# ── Helper: extract tenant_id and user identity from headers ───────────────────
# In production these are set by the Gateway after ISE token validation.

def extract_tenant(x_tenant_id: str = Header(...)) -> str:
    return x_tenant_id


def extract_user(x_user_identity: str = Header(...)) -> str:
    return x_user_identity


# ── Internal helpers (thin wrappers around Cosmos DB) ─────────────────────────

async def _get_step_up_request(request_id: str, tenant_id: str) -> dict:
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    container = db.get_container_client("step_up_requests")
    return await container.read_item(item=request_id, partition_key=tenant_id)


async def _get_tenant_config(tenant_id: str) -> dict:
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    container = db.get_container_client("tenant_config")
    return await container.read_item(item=tenant_id, partition_key=tenant_id)


async def _write_step_up_decision(
    record: dict,
    status: str,
    decided_by: str,
    tenant_id: str,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    record["status"] = status
    record["approved_by"] = decided_by   # set for both approve and reject
    record["approved_at"] = now_iso
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    container = db.get_container_client("step_up_requests")
    await container.replace_item(item=record["id"], body=record, partition_key=tenant_id)
    # Audit log
    logger.info(
        "Step-up decision recorded",
        extra={
            "tenant_id": tenant_id,
            "request_id": record["id"],
            "tool_name": record["tool_name"],
            "status": status,
            "requested_by": record["requested_by"],
            "approved_by": decided_by,
        },
    )


async def _propagate_approval_to_change_records(record: dict, decided_by: str) -> None:
    """
    For apply_change and rollback_change: write approved_by to the change_records document.
    change_id lives in record["context"]["change_id"].
    """
    tool_name = record["tool_name"]
    if tool_name not in ("apply_change", "rollback_change"):
        return
    change_id = record.get("context", {}).get("change_id")
    if not change_id:
        return
    tenant_id = record["tenant_id"]
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    change_container = db.get_container_client("change_records")
    change_record = await change_container.read_item(item=change_id, partition_key=tenant_id)
    change_record["approved_by"] = decided_by
    change_record["approved_at"] = record["approved_at"]
    await change_container.replace_item(item=change_id, body=change_record, partition_key=tenant_id)


def _check_authorisation(record: dict, tenant_config: dict, caller: str, action: str) -> None:
    """
    Raises HTTPException if the caller is not authorised to approve or reject.
    Rules apply symmetrically to both approve and reject.
    Logs all forbidden attempts to the audit log (spec requirement).
    """
    policy = tenant_config.get("step_up_policy", {}).get(record["tool_name"], {})
    approvers = tenant_config.get("step_up_approvers", [])
    self_approve = policy.get("self_approve", False)

    def _log_and_raise(error_code: str) -> None:
        logger.warning(
            "Step-up authorisation denied",
            extra={
                "tenant_id": record["tenant_id"],
                "request_id": record["id"],
                "tool_name": record["tool_name"],
                "caller": caller,
                "action": action,
                "reason": error_code,
            },
        )
        raise HTTPException(
            status_code=403,
            detail=StepUpErrorResponse(
                error=error_code,
                request_id=record["id"],
            ).model_dump(),
        )

    if self_approve:
        # Low-risk: requesting user can self-approve/reject, OR any designated approver can
        if caller != record["requested_by"] and caller not in approvers:
            _log_and_raise("not_authorised_approver")
    else:
        # High-risk: must be a designated approver AND must not be the requester
        if caller not in approvers:
            _log_and_raise("not_authorised_approver")
        if caller == record["requested_by"]:
            _log_and_raise("self_approval_not_permitted")


# ── Step-up approve / reject endpoints ────────────────────────────────────────

@app.post("/step-up/{request_id}/approve", response_model=StepUpDecisionResponse)
async def step_up_approve(
    request_id: str,
    body: StepUpDecisionRequest,
    tenant_id: str = Depends(extract_tenant),
    caller: str = Depends(extract_user),
):
    try:
        record = await _get_step_up_request(request_id, tenant_id)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=StepUpErrorResponse(error="request_not_found", request_id=request_id).model_dump(),
        )

    if record["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=StepUpErrorResponse(error="already_decided", request_id=request_id).model_dump(),
        )

    expires_at = datetime.fromisoformat(record["expires_at"])
    if datetime.now(timezone.utc) >= expires_at:
        raise HTTPException(
            status_code=409,
            detail=StepUpErrorResponse(error="request_expired", request_id=request_id).model_dump(),
        )

    tenant_config = await _get_tenant_config(tenant_id)
    _check_authorisation(record, tenant_config, caller, "approve")

    await _write_step_up_decision(record, "approved", caller, tenant_id)
    await _propagate_approval_to_change_records(record, caller)

    return StepUpDecisionResponse(
        request_id=request_id,
        status="approved",
        decided_by=caller,
        decided_at=record["approved_at"],
    )


@app.post("/step-up/{request_id}/reject", response_model=StepUpDecisionResponse)
async def step_up_reject(
    request_id: str,
    body: StepUpDecisionRequest,
    tenant_id: str = Depends(extract_tenant),
    caller: str = Depends(extract_user),
):
    try:
        record = await _get_step_up_request(request_id, tenant_id)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=StepUpErrorResponse(error="request_not_found", request_id=request_id).model_dump(),
        )

    if record["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=StepUpErrorResponse(error="already_decided", request_id=request_id).model_dump(),
        )

    expires_at = datetime.fromisoformat(record["expires_at"])
    if datetime.now(timezone.utc) >= expires_at:
        raise HTTPException(
            status_code=409,
            detail=StepUpErrorResponse(error="request_expired", request_id=request_id).model_dump(),
        )

    tenant_config = await _get_tenant_config(tenant_id)
    _check_authorisation(record, tenant_config, caller, "reject")

    await _write_step_up_decision(record, "rejected", caller, tenant_id)

    return StepUpDecisionResponse(
        request_id=request_id,
        status="rejected",
        decided_by=caller,
        decided_at=record["approved_at"],
    )
```

- [ ] **Step 8.4: Run endpoint tests**

```bash
cd services/coordinator
pytest tests/test_endpoints.py -v
```

Expected: `4 passed`

- [ ] **Step 8.5: Commit**

```bash
git add services/coordinator/main.py services/coordinator/tests/test_endpoints.py
git commit -m "feat(coordinator): add step-up approve/reject endpoints with authorisation"
```

---

## Chunk 4: Agent Loop Integration

### Task 9: Add step-up gate to _stream_generator in agent_loop.py

**Files:**
- Create: `services/coordinator/agent_loop.py`
- Create: `services/coordinator/tests/test_agent_loop.py`

- [ ] **Step 9.1: Write failing tests for the step-up gate in the loop**

Create `services/coordinator/tests/test_agent_loop.py`:

```python
import json
import pytest
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch, MagicMock


@dataclass
class ChatRequest:
    tenant_id: str
    session_id: str
    user_identity: str = "user@client.com"


def collect_sse(async_gen):
    """Helper: drain an async generator and return parsed SSE event dicts."""
    import asyncio

    async def _drain():
        events = []
        async for chunk in async_gen:
            if isinstance(chunk, str) and chunk.startswith("data: "):
                events.append(json.loads(chunk[6:]))
        return events

    return asyncio.get_event_loop().run_until_complete(_drain())


class TestStepUpGateInLoop:
    def test_approval_required_emitted_before_poll(self):
        """approval_required must appear in the stream before approval_granted."""
        from agent_loop import _stream_tool_call

        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {"grant_type": "single_use", "self_approve": False,
                  "pending_ttl_seconds": 900, "notification_channels": ["in_chat"]}
        tenant_config = {"step_up_policy": {"apply_change": policy}, "step_up_approvers": []}

        mock_req_doc = {"id": "sur-001", "context": {"change_id": "chg-1"},
                        "expires_at": "2099-01-01T00:00:00+00:00"}
        mock_result = MagicMock()
        mock_result.status = "approved"
        mock_result.request_id = "sur-001"
        mock_result.approved_by = "approver@client.com"
        mock_result.credential = "cred-abc"

        with patch("agent_loop.prepare_step_up", new_callable=AsyncMock, return_value=mock_req_doc):
            with patch("agent_loop.resolve_step_up", new_callable=AsyncMock, return_value=mock_result):
                with patch("agent_loop._call_agent", new_callable=AsyncMock, return_value="agent-result"):
                    events = collect_sse(_stream_tool_call("apply_change", {}, request, tenant_config))

        types = [e["type"] for e in events]
        assert "approval_required" in types
        assert "approval_granted" in types
        assert types.index("approval_required") < types.index("approval_granted")

    def test_approval_rejected_skips_tool_call(self):
        from agent_loop import _stream_tool_call

        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {"grant_type": "single_use", "self_approve": False,
                  "pending_ttl_seconds": 900, "notification_channels": ["in_chat"]}
        tenant_config = {"step_up_policy": {"apply_change": policy}, "step_up_approvers": []}

        mock_req_doc = {"id": "sur-002", "context": {}, "expires_at": "2099-01-01T00:00:00+00:00"}
        mock_result = MagicMock()
        mock_result.status = "rejected"
        mock_result.request_id = "sur-002"
        mock_result.approved_by = "approver@client.com"

        with patch("agent_loop.prepare_step_up", new_callable=AsyncMock, return_value=mock_req_doc):
            with patch("agent_loop.resolve_step_up", new_callable=AsyncMock, return_value=mock_result):
                with patch("agent_loop._call_agent", new_callable=AsyncMock) as mock_call:
                    events = collect_sse(_stream_tool_call("apply_change", {}, request, tenant_config))

        types = [e["type"] for e in events]
        assert "approval_rejected" in types
        assert "approval_granted" not in types
        mock_call.assert_not_called()

    def test_no_step_up_for_unregistered_tool(self):
        from agent_loop import _stream_tool_call

        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        tenant_config = {"step_up_policy": {}}  # no entry for "network_agent"

        with patch("agent_loop.prepare_step_up", new_callable=AsyncMock) as mock_prepare:
            with patch("agent_loop._call_agent", new_callable=AsyncMock, return_value="result"):
                events = collect_sse(_stream_tool_call("network_agent", {}, request, tenant_config))

        mock_prepare.assert_not_called()
        types = [e["type"] for e in events]
        assert "approval_required" not in types

    def test_active_grant_skips_approval_prompt(self):
        from agent_loop import _stream_tool_call

        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {"grant_type": "time_window", "self_approve": True,
                  "pending_ttl_seconds": 300, "notification_channels": ["in_chat"],
                  "grant_duration_seconds": 1800}
        tenant_config = {"step_up_policy": {"bulk_close_tickets": policy}}

        # prepare_step_up returns None → active grant found
        with patch("agent_loop.prepare_step_up", new_callable=AsyncMock, return_value=None):
            with patch("agent_loop.fetch_tool_credential", new_callable=AsyncMock, return_value="cred"):
                with patch("agent_loop._call_agent", new_callable=AsyncMock, return_value="result"):
                    events = collect_sse(_stream_tool_call("bulk_close_tickets", {}, request, tenant_config))

        types = [e["type"] for e in events]
        assert "approval_required" not in types

    def test_keepalive_heartbeats_emitted_during_long_poll(self):
        """Keepalive lines must be yielded while waiting for approval to prevent ACA idle timeout."""
        import asyncio
        from agent_loop import _stream_tool_call, _keepalive

        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {"grant_type": "single_use", "self_approve": False,
                  "pending_ttl_seconds": 900, "notification_channels": ["in_chat"]}
        tenant_config = {"step_up_policy": {"apply_change": policy}}

        mock_req_doc = {"id": "sur-001", "context": {}, "expires_at": "2099-01-01T00:00:00+00:00"}
        mock_result = MagicMock()
        mock_result.status = "approved"
        mock_result.request_id = "sur-001"
        mock_result.approved_by = "approver@client.com"
        mock_result.credential = "cred-abc"

        # slow_resolve sleeps 0.05s — longer than the patched _KEEPALIVE_INTERVAL of 0.01s.
        # This guarantees asyncio.wait_for times out (and emits a keepalive) before the
        # coroutine completes. Using sleep(0) and interval=0 is a race condition in CPython.
        async def slow_resolve(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return mock_result

        async def _drain():
            chunks = []
            async for chunk in _stream_tool_call("apply_change", {}, request, tenant_config):
                chunks.append(chunk)
            return chunks

        with patch("agent_loop.prepare_step_up", new_callable=AsyncMock, return_value=mock_req_doc):
            with patch("agent_loop.resolve_step_up", side_effect=slow_resolve):
                with patch("agent_loop._call_agent", new_callable=AsyncMock, return_value="result"):
                    # _KEEPALIVE_INTERVAL = 0.01s — shorter than slow_resolve's 0.05s sleep,
                    # ensuring at least one timeout fires and a keepalive is yielded.
                    with patch("agent_loop._KEEPALIVE_INTERVAL", 0.01):
                        chunks = asyncio.get_event_loop().run_until_complete(_drain())

        assert _keepalive() in chunks, "Expected at least one keepalive heartbeat in stream"
```

- [ ] **Step 9.2: Run failing tests**

```bash
cd services/coordinator
pytest tests/test_agent_loop.py -v
```

Expected: `ImportError` — agent_loop.py does not exist

- [ ] **Step 9.3: Create agent_loop.py with _stream_tool_call and SSE helpers**

Create `services/coordinator/agent_loop.py`:

```python
"""
Agentic loop for the VIGIL coordinator.

SSE event emission follows this order per tool call:
  1. If step-up required: approval_required (immediate, before poll)
  2. If step-up: approval_granted | approval_rejected | approval_expired
  3. agent_start
  4. agent_complete | agent_error

Step-up gated tools run serially. Non-gated tools run in parallel via asyncio.as_completed().
"""

import asyncio
import json
import logging
from typing import AsyncGenerator

import httpx

from step_up import (
    StepUpResult,
    fetch_tool_credential,
    prepare_step_up,
    resolve_step_up,
)

logger = logging.getLogger(__name__)

# SSE keepalive interval (seconds) — sent during long approval polls to prevent ACA idle timeout
_KEEPALIVE_INTERVAL = 30


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _keepalive() -> str:
    """SSE comment line — invisible to event parsers, prevents idle connection timeout."""
    return ": keepalive\n\n"


async def _call_agent(tool_name: str, tool_input: dict, request, credential: str | None = None) -> dict:
    """
    Dispatch a tool call to the appropriate specialist agent.
    credential is passed only for step-up gated write operations.
    """
    # TODO: Implement per-agent routing using tool_name to select agent URL
    # For now, this is a stub that returns an empty result
    agent_url = _get_agent_url(tool_name)
    payload = {**tool_input, "tenant_id": request.tenant_id, "session_id": request.session_id}
    if credential:
        payload["write_credential"] = credential
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(agent_url, json=payload)
        resp.raise_for_status()
        return resp.json()


def _get_agent_url(tool_name: str) -> str:
    """Map tool name to agent internal URL."""
    import os
    urls = {
        "network_agent":     os.getenv("NETWORK_AGENT_URL", ""),
        "rag_agent":         os.getenv("RAG_AGENT_URL", ""),
        "itsm_agent":        os.getenv("ITSM_AGENT_URL", ""),
        "enrichment_agent":  os.getenv("ENRICHMENT_AGENT_URL", ""),
        "apply_change":      os.getenv("NETWORK_AGENT_URL", ""),
        "rollback_change":   os.getenv("NETWORK_AGENT_URL", ""),
    }
    return urls.get(tool_name, "")


async def _stream_tool_call(
    tool_name: str,
    tool_input: dict,
    request,
    tenant_config: dict,
) -> AsyncGenerator[str, None]:
    """
    Yields SSE strings for a single tool call, handling the step-up gate if required.
    Designed to be called directly from _stream_generator for step-up tools.
    Non-gated tools should use asyncio.as_completed() in the outer loop instead.
    """
    policy = tenant_config.get("step_up_policy", {}).get(tool_name)

    if policy is None:
        # No gate — dispatch immediately (caller handles parallelism)
        result = await _call_agent(tool_name, tool_input, request)
        return

    # Step-up path
    step_up_req = await prepare_step_up(tool_name, tool_input, request, policy, tenant_config)

    if step_up_req is not None:
        # Yield approval_required IMMEDIATELY before blocking on the poll
        yield _sse({
            "type": "approval_required",
            "request_id": step_up_req["id"],
            "tool": tool_name,
            "context": step_up_req["context"],
            "approver_type": "self" if policy.get("self_approve") else "designated",
            "expires_at": step_up_req["expires_at"],
        })

        # Poll with keepalive heartbeats every _KEEPALIVE_INTERVAL seconds to prevent
        # ACA idle connection timeout during long approval waits.
        # asyncio.shield() keeps resolve_step_up running even when wait_for raises TimeoutError.
        resolve_task = asyncio.create_task(resolve_step_up(step_up_req, request, policy))
        while not resolve_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(resolve_task), timeout=_KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                yield _keepalive()
        step_up_result = resolve_task.result()

        if step_up_result.status == "rejected":
            yield _sse({
                "type": "approval_rejected",
                "request_id": step_up_result.request_id,
                "tool": tool_name,
                "decided_by": step_up_result.approved_by,
            })
            return

        if step_up_result.status == "expired":
            yield _sse({
                "type": "approval_expired",
                "request_id": step_up_result.request_id,
                "tool": tool_name,
            })
            return

        if step_up_result.status == "failed":
            yield _sse({
                "type": "agent_error",
                "agent": tool_name,
                "error": "credential_fetch_failed",
            })
            return

        yield _sse({
            "type": "approval_granted",
            "request_id": step_up_result.request_id,
            "tool": tool_name,
            "approved_by": step_up_result.approved_by,
        })
        credential = step_up_result.credential

    else:
        # Active time-window grant — fetch credential just-in-time, no approval events
        credential = await fetch_tool_credential(tool_name, request.tenant_id)

    # Dispatch the tool call with the credential
    yield _sse({"type": "agent_start", "agent": tool_name, "detail": tool_input.get("device_host")})
    try:
        import time
        start = time.monotonic()
        await _call_agent(tool_name, tool_input, request, credential=credential)
        duration_ms = int((time.monotonic() - start) * 1000)
        yield _sse({"type": "agent_complete", "agent": tool_name, "duration_ms": duration_ms})
    except Exception as exc:
        logger.error("Agent call failed", extra={"tool": tool_name}, exc_info=exc)
        yield _sse({"type": "agent_error", "agent": tool_name, "error": str(exc)})


```

- [ ] **Step 9.4: Run tests**

```bash
cd services/coordinator
pytest tests/test_agent_loop.py -v
```

Expected: `5 passed`

- [ ] **Step 9.5: Run all coordinator tests**

```bash
cd services/coordinator
pytest tests/ -v
```

Expected: all tests pass

- [ ] **Step 9.6: Commit**

```bash
git add services/coordinator/agent_loop.py services/coordinator/tests/test_agent_loop.py
git commit -m "feat(coordinator): add step-up gate inline in _stream_tool_call"
```

---

## Chunk 5: Gateway + Docs

### Task 10: Add Gateway proxy routes for /step-up/

**Files:**
- Create: `services/gateway/main.py`
- Create: `services/gateway/tests/test_step_up_proxy.py`
- Modify: `services/gateway/requirements.txt`

- [ ] **Step 10.1: Write failing proxy tests**

Create `services/gateway/tests/test_step_up_proxy.py`:

```python
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock
import httpx


class TestStepUpProxy:
    def test_approve_proxies_to_coordinator(self):
        from main import app

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "request_id": "sur-001",
            "status": "approved",
            "decided_by": "approver@client.com",
            "decided_at": "2026-03-17T10:01:00+00:00",
        }
        mock_response.headers = {"content-type": "application/json"}

        with patch("main._proxy_json", new_callable=AsyncMock, return_value=mock_response):
            client = TestClient(app)
            response = client.post(
                "/step-up/sur-001/approve",
                json={"comment": None},
                headers={"Authorization": "Bearer valid-token"},
            )

        # Gateway should forward whatever the coordinator returns
        assert response.status_code == 200
        assert response.json()["status"] == "approved"

    def test_unauthenticated_request_returns_401(self):
        from main import app

        client = TestClient(app)
        response = client.post("/step-up/sur-001/approve", json={})

        assert response.status_code == 401
```

- [ ] **Step 10.2: Run failing tests**

```bash
cd services/gateway
pytest tests/test_step_up_proxy.py -v
```

Expected: `ImportError` or `404`

- [ ] **Step 10.3: Create/update gateway/main.py with step-up proxy routes**

Ensure `services/gateway/main.py` contains the step-up proxy routes:

```python
import logging
import os

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://coordinator:8000")


@app.get("/health")
def health():
    return {"status": "healthy", "service": "gateway"}


def validate_ise_token(request: Request) -> dict:
    """
    Validates the ISE-issued token and extracts claims.
    Returns dict with tenant_id and user_identity.
    Raises HTTPException 401 if invalid.

    STUB — see Step 10.X below. This implementation only checks token presence.
    It MUST be replaced with the real ISE SAML validator before any commit.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth_header.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")
    # STUB — hardcoded values make tests pass but MUST NOT be committed.
    # Replace with call to real ISE SAML validator (services/gateway/middleware/auth.py).
    return {"tenant_id": "tenant-a", "user_identity": "user@client.com"}


async def _proxy_json(method: str, path: str, claims: dict, body: dict) -> httpx.Response:
    """
    Forwards a JSON request to the Coordinator, passing tenant_id and user identity
    as headers (set by Gateway after token validation — never trusted from client).
    """
    headers = {
        "X-Tenant-Id": claims["tenant_id"],
        "X-User-Identity": claims["user_identity"],
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await getattr(client, method)(
            f"{COORDINATOR_URL}{path}",
            json=body,
            headers=headers,
        )
    return resp


class StepUpDecisionRequest(BaseModel):
    comment: str | None = None


@app.post("/step-up/{request_id}/approve")
async def step_up_approve_proxy(
    request_id: str,
    body: StepUpDecisionRequest,
    request: Request,
):
    """
    Proxy for POST /step-up/{request_id}/approve.
    Auth/rate-limit middleware runs before this handler.
    Authorisation logic (approver list, self-approval guard) lives in the Coordinator.
    """
    claims = validate_ise_token(request)
    resp = await _proxy_json("post", f"/step-up/{request_id}/approve", claims, body.model_dump())
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.post("/step-up/{request_id}/reject")
async def step_up_reject_proxy(
    request_id: str,
    body: StepUpDecisionRequest,
    request: Request,
):
    """
    Proxy for POST /step-up/{request_id}/reject.
    Same auth/rate-limit chain as approve.
    """
    claims = validate_ise_token(request)
    resp = await _proxy_json("post", f"/step-up/{request_id}/reject", claims, body.model_dump())
    return JSONResponse(status_code=resp.status_code, content=resp.json())
```

- [ ] **Step 10.4: Run gateway tests**

```bash
cd services/gateway
pytest tests/test_step_up_proxy.py -v
```

Expected: `2 passed`

- [ ] **Step 10.4a: Wire in real ISE token validation before committing**

The `validate_ise_token` stub in Step 10.3 uses hardcoded return values. Per `CLAUDE.md`, the Gateway is the security boundary — replace the stub body with a call to the real ISE SAML validator in `services/gateway/middleware/auth.py`. The real validator must:
- Validate token signature against ISE SAML metadata
- Extract `tenant_id` and `user_identity` from token claims
- Reject expired/invalid tokens with HTTP 401

If `middleware/auth.py` does not yet exist, create it following the `CLAUDE.md` Gateway auth middleware pattern. Do **not** commit `services/gateway/main.py` while `validate_ise_token` returns hardcoded values.

- [ ] **Step 10.5: Commit**

```bash
git add services/gateway/main.py services/gateway/tests/test_step_up_proxy.py services/gateway/requirements.txt
git commit -m "feat(gateway): add step-up approve/reject proxy routes"
```

---

### Task 11: Update ARCHITECTURE.md and CLAUDE.md

**Files:**
- Modify: `ARCHITECTURE.md`
- Modify: `CLAUDE.md`

- [ ] **Step 11.1: Update ARCHITECTURE.md Coordinator endpoint list**

In `ARCHITECTURE.md`, find the Coordinator `**Endpoints:**` section (around line 131) and update:

```markdown
- **Endpoints:**
  - `POST /chat/stream` — primary, returns `text/event-stream`
  - `POST /chat` — non-streaming fallback
  - `POST /step-up/{request_id}/approve` — approve a pending step-up request
  - `POST /step-up/{request_id}/reject` — reject a pending step-up request
  - `POST /changes/{change_id}/acknowledge-drift` — acknowledge config drift and resume apply
  - `POST /changes/{change_id}/abort` — abort in-flight change
```

(Remove `POST /changes/{change_id}/apply` and `POST /changes/{change_id}/reject` — replaced by step-up endpoints)

Also search `ARCHITECTURE.md` for any other references to `/changes/{id}/apply` or `/changes/{id}/reject` (e.g. in the network change lifecycle description or flow sections) and update those references too.

- [ ] **Step 11.2: Update ARCHITECTURE.md Cosmos DB containers list**

In the Multi-Tenancy section, add rows:

```markdown
| `step_up_requests` | Cosmos DB | Partitioned by `tenant_id` — pending/decided approval requests |
| `step_up_grants` | Cosmos DB | Partitioned by `tenant_id`, TTL enabled — active time-window grants |
```

- [ ] **Step 11.3: Update ARCHITECTURE.md SSE event table**

Add the four new events to the SSE event table:

```markdown
| `approval_required` | `request_id`, `tool`, `context`, `approver_type`, `expires_at` | Loop paused awaiting human approval |
| `approval_granted`  | `request_id`, `tool`, `approved_by` | Approval received, tool dispatching |
| `approval_rejected` | `request_id`, `tool`, `decided_by` | Rejected — coordinator continues with partial results |
| `approval_expired`  | `request_id`, `tool` | Approval window elapsed without decision |
```

- [ ] **Step 11.4: Update CLAUDE.md — Key Vault exception and new containers**

In `CLAUDE.md`, find the `## Azure Key Vault Pattern` section and add after the existing code block:

```markdown
**Exception — step-up write credentials:** Write credentials for step-up gated tools
(`apply_change`, `rollback_change`, etc.) are fetched just-in-time after approval, not at
startup, and are not cached in module scope. This is the only approved exception to the
startup-fetch rule. All other secrets follow the startup-fetch pattern above.
```

In `CLAUDE.md`, find the `## Cosmos DB Patterns / Containers` section and add:

```markdown
- `step_up_requests` — partitioned by `tenant_id`, keyed by `request_id` — pending/decided approval gates
- `step_up_grants` — partitioned by `tenant_id`, `default_ttl = -1` required — active time-window grants (TTL = `grant_duration_seconds`)
```

- [ ] **Step 11.5: Commit docs**

```bash
git add ARCHITECTURE.md CLAUDE.md
git commit -m "docs: update ARCHITECTURE.md and CLAUDE.md for step-up auth"
```

---

### Task 12: Full integration smoke test

- [ ] **Step 12.1: Run the complete test suite for coordinator and gateway**

```bash
pytest services/coordinator/tests/ -v && pytest services/gateway/tests/ -v
```

Expected: all tests pass in both services

- [ ] **Step 12.2: Verify health endpoints**

```bash
cd services/coordinator
uvicorn main:app --port 8001 &
curl http://localhost:8001/health
# Expected: {"status": "healthy", "service": "coordinator"}
kill %1

cd ../gateway
uvicorn main:app --port 8002 &
curl http://localhost:8002/health
# Expected: {"status": "healthy", "service": "gateway"}
kill %1
```

- [ ] **Step 12.3: Final commit**

```bash
git add infrastructure/terraform/modules/cosmos-db/main.tf \
        infrastructure/terraform/modules/container-apps/ \
        .github/workflows/ \
        services/coordinator/step_up.py \
        services/coordinator/notifications.py \
        services/coordinator/agent_loop.py \
        services/coordinator/main.py \
        services/coordinator/tests/ \
        services/coordinator/requirements.txt \
        services/gateway/main.py \
        services/gateway/tests/ \
        services/gateway/requirements.txt \
        ARCHITECTURE.md CLAUDE.md
git commit -m "feat: complete step-up auth implementation

- Terraform: step_up_requests and step_up_grants containers, ACA timeout
- coordinator/step_up.py: full lifecycle (prepare, resolve, recovery, grants)
- coordinator/notifications.py: email (ACS) and webhook out-of-band channels
- coordinator/main.py: approve/reject endpoints with Pydantic models
- coordinator/agent_loop.py: inline step-up gate in stream generator
- gateway/main.py: proxy routes for /step-up/ endpoints
- ARCHITECTURE.md + CLAUDE.md: updated docs and KV exception documented"
```
