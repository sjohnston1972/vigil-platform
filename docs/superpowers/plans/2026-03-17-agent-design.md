# Agent Design Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `agent-design`, a RAG-enabled FastAPI service that generates expert network design recommendations with an optional Claude-to-Claude critique loop (up to 5 iterations, harshness 1–8), plus wire it into the coordinator as a new tool.

**Companion plan:** `agent-troubleshoot` and `agent-probe` are built in `docs/superpowers/plans/2026-03-17-agent-troubleshoot.md`.

**Architecture:** The Design Agent is a standard JSON-returning specialist agent. It calls the existing RAG Agent internally, runs Claude design + critique calls via `azure-ai-inference` (sync client called in a thread pool to avoid blocking the event loop), writes iteration state to the `design_sessions` Cosmos DB container between iterations (request-stateless critique loop), and returns `sse_events` in its response for the coordinator to relay downstream.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, `azure-ai-inference` (sync, wrapped with `asyncio.to_thread`), `azure-cosmos` (async), `azure-identity`, `httpx`, pytest + pytest-asyncio.

---

## Dependency Note

The coordinator's `POST /chat/stream` endpoint (the outer Claude loop that calls tools) is a separate implementation concern. This plan builds `agent-design` as a standalone testable service and wires the coordinator tool registration + agent_loop dispatch.

---

## File Map

**Create:**
- `services/agent-design/main.py` — FastAPI app, `/health`, `POST /design`, `POST /design/{design_session_id}/accept`
- `services/agent-design/models.py` — All Pydantic request/response models
- `services/agent-design/cosmos.py` — `design_sessions` Cosmos DB read/write (partitioned by `tenant_id`)
- `services/agent-design/rag_client.py` — Async HTTP client wrapping the existing RAG Agent
- `services/agent-design/design_loop.py` — Claude design generation + critique calls (sync, threadpool-safe)
- `services/agent-design/requirements.txt`
- `services/agent-design/Dockerfile`
- `services/agent-design/tests/conftest.py`
- `services/agent-design/tests/test_main.py` — Health, /design, /accept endpoint tests
- `services/agent-design/tests/test_design_loop.py` — Design generation + critique unit tests
- `services/coordinator/tools/__init__.py` — New directory
- `services/coordinator/tools/design.py` — `design_agent` tool definition dict
- `.github/workflows/deploy-agent-design.yml`

**Modify:**
- `services/coordinator/agent_loop.py` — Add `design_agent` URL mapping + sse_events relay + 120s timeout
- `ARCHITECTURE.md` — Add Design Agent to Component Reference + Cosmos DB table + env vars

---

## Task 1: Project scaffold + health endpoint

**Files:**
- Create: `services/agent-design/main.py`
- Create: `services/agent-design/requirements.txt`
- Create: `services/agent-design/tests/conftest.py`
- Create: `services/agent-design/tests/test_main.py`

- [ ] **Step 1: Write the failing health test**

```python
# services/agent-design/tests/test_main.py
import pytest
from fastapi.testclient import TestClient

def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "agent-design"}
```

- [ ] **Step 2: Write conftest.py**

Patch `CosmosClient` and `DefaultAzureCredential` at the module level so `startup()` never touches Azure.

```python
# services/agent-design/tests/conftest.py
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Basic test client — patches Azure startup dependencies."""
    sys.modules.pop("main", None)
    with patch("azure.cosmos.aio.CosmosClient") as mock_cosmos_cls, \
         patch("azure.identity.aio.DefaultAzureCredential") as mock_cred_cls:
        mock_cosmos_cls.return_value = MagicMock()
        mock_cred_cls.return_value = MagicMock()
        from main import app
        yield TestClient(app)
```

- [ ] **Step 3: Run test to confirm it fails**

```bash
cd services/agent-design
pip install fastapi uvicorn pytest pytest-asyncio
pytest tests/test_main.py::test_health -v
```
Expected: `ModuleNotFoundError: No module named 'main'`

- [ ] **Step 4: Write minimal main.py**

```python
# services/agent-design/main.py
import logging
import os

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
_cosmos_client = None
_credential = None   # initialised at startup, reused for all Claude calls


@app.on_event("startup")
async def startup():
    global _cosmos_client, _credential
    _credential = DefaultAzureCredential()
    _cosmos_client = CosmosClient(
        url=os.getenv("COSMOS_ENDPOINT", ""),
        credential=_credential,
    )


@app.get("/health")
def health():
    return {"status": "healthy", "service": "agent-design"}
```

- [ ] **Step 5: Write requirements.txt**

```
fastapi
uvicorn
pydantic
azure-cosmos
azure-identity
azure-ai-inference
httpx
python-dotenv
pytest
pytest-asyncio
```

- [ ] **Step 6: Run test to confirm pass**

```bash
pip install -r requirements.txt
pytest tests/test_main.py::test_health -v
```
Expected: `PASSED`

- [ ] **Step 7: Commit**

```bash
git add services/agent-design/
git commit -m "feat(agent-design): scaffold service with health endpoint"
```

---

## Task 2: Pydantic models

**Files:**
- Create: `services/agent-design/models.py`

- [ ] **Step 1: Write models.py**

Use `Literal` and `Field` constraints to enforce spec semantics — Claude can return unexpected values and Pydantic must reject them before they reach business logic.

```python
# services/agent-design/models.py
from typing import Annotated, Literal
from pydantic import BaseModel, Field


# ── Inbound ──────────────────────────────────────────────────────────────────

class DesignRequest(BaseModel):
    tenant_id: str
    session_id: str
    query: str
    domain_hints: list[str] = []       # inferred from query if empty
    critique_enabled: bool
    critique_level: Annotated[int, Field(ge=1, le=8)]
    design_session_id: str | None = None


class DesignAcceptRequest(BaseModel):
    tenant_id: str


# ── Internal ─────────────────────────────────────────────────────────────────

class DesignDraft(BaseModel):
    narrative: str
    artefacts: list[dict]
    domains_covered: list[str]
    assumptions: list[str]
    open_questions: list[str]


class CritiqueIssue(BaseModel):
    severity: Literal["critical", "major", "minor"]
    domain: str
    description: str
    recommendation: str


class CritiqueResult(BaseModel):
    score: Annotated[int, Field(ge=1, le=10)]
    issues: list[CritiqueIssue]
    verdict: Literal["accept", "revise"]


class DesignIteration(BaseModel):
    n: int
    draft: DesignDraft
    critique: CritiqueResult | None = None
    accepted_at: str | None = None


# ── Outbound ─────────────────────────────────────────────────────────────────

class DesignResponse(BaseModel):
    design_session_id: str
    iteration_n: int
    draft: DesignDraft
    critique: CritiqueResult | None = None
    critique_complete: bool = False
    tokens_used: int = 0
    sse_events: list[dict]


class DesignAcceptResponse(BaseModel):
    design_session_id: str
    accepted: bool
```

- [ ] **Step 2: Commit**

```bash
git add services/agent-design/models.py
git commit -m "feat(agent-design): add Pydantic models with Literal/Field validation"
```

---

## Task 3: Cosmos DB client (design_sessions)

**Files:**
- Create: `services/agent-design/cosmos.py`
- Modify: `services/agent-design/tests/test_main.py`

All Cosmos lookups use `read_item(item=design_session_id, partition_key=tenant_id)`. The partition key is `tenant_id`; the item id is `design_session_id`.

- [ ] **Step 1: Write failing tests**

```python
# Add to services/agent-design/tests/test_main.py
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_create_design_session_returns_id():
    from cosmos import create_design_session
    mock_container = AsyncMock()
    mock_container.create_item = AsyncMock(return_value={})

    result = await create_design_session(
        mock_container, "tenant-a", "session-1", "Design a campus LAN", [], True, 5
    )
    assert isinstance(result, str) and len(result) > 0
    doc = mock_container.create_item.call_args[1]["body"]
    assert doc["tenant_id"] == "tenant-a"
    assert doc["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_get_design_session_uses_partition_key():
    from cosmos import get_design_session
    mock_container = AsyncMock()
    mock_container.read_item = AsyncMock(return_value={
        "id": "dsid-1", "tenant_id": "tenant-a", "iterations": []
    })
    result = await get_design_session(mock_container, "dsid-1", "tenant-a")
    mock_container.read_item.assert_called_with(item="dsid-1", partition_key="tenant-a")
    assert result["id"] == "dsid-1"


@pytest.mark.asyncio
async def test_get_design_session_wrong_tenant_raises_permission_error():
    from cosmos import get_design_session
    mock_container = AsyncMock()
    # Cosmos returns tenant-b doc even though we searched with tenant-a partition key
    # (can't happen in production, but guard defensively)
    mock_container.read_item = AsyncMock(return_value={
        "id": "dsid-1", "tenant_id": "tenant-b", "iterations": []
    })
    with pytest.raises(PermissionError):
        await get_design_session(mock_container, "dsid-1", "tenant-a")


@pytest.mark.asyncio
async def test_get_design_session_not_found_raises_key_error():
    from cosmos import get_design_session
    from azure.core.exceptions import ResourceNotFoundError
    mock_container = AsyncMock()
    mock_container.read_item = AsyncMock(side_effect=ResourceNotFoundError("not found"))
    with pytest.raises(KeyError):
        await get_design_session(mock_container, "dsid-missing", "tenant-a")
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_main.py::test_create_design_session_returns_id -v
```
Expected: `ImportError: cannot import name 'create_design_session' from 'cosmos'`

- [ ] **Step 3: Write cosmos.py**

```python
# services/agent-design/cosmos.py
import uuid
from datetime import datetime, timezone
from azure.core.exceptions import ResourceNotFoundError


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_design_session(
    container, tenant_id: str, session_id: str, query: str,
    domain_hints: list[str], critique_enabled: bool, critique_level: int,
) -> str:
    design_session_id = str(uuid.uuid4())
    now = _now_iso()
    await container.create_item(body={
        "id": design_session_id,
        "tenant_id": tenant_id,
        "session_id": session_id,
        "query": query,
        "domain_hints": domain_hints,
        "critique_enabled": critique_enabled,
        "critique_level": critique_level,
        "iterations": [],
        "final_artefacts": None,
        "created_at": now,
        "updated_at": now,
    })
    return design_session_id


async def get_design_session(container, design_session_id: str, tenant_id: str) -> dict:
    """Fetch design session by (id, partition_key). Raises KeyError if not found, PermissionError if tenant mismatch."""
    try:
        doc = await container.read_item(item=design_session_id, partition_key=tenant_id)
    except ResourceNotFoundError:
        raise KeyError(f"design_session {design_session_id} not found for tenant {tenant_id}")
    if doc["tenant_id"] != tenant_id:
        raise PermissionError("tenant_id mismatch")
    return doc


async def append_iteration(container, doc: dict, iteration: dict) -> None:
    doc["iterations"].append(iteration)
    doc["updated_at"] = _now_iso()
    await container.replace_item(item=doc["id"], body=doc, partition_key=doc["tenant_id"])


async def finalise_session(container, doc: dict, final_artefacts: dict) -> None:
    doc["final_artefacts"] = final_artefacts
    doc["updated_at"] = _now_iso()
    await container.replace_item(item=doc["id"], body=doc, partition_key=doc["tenant_id"])


async def accept_session(container, doc: dict, iteration_n: int) -> None:
    """Mark the nth iteration as accepted and write final_artefacts."""
    accepted_draft = next(
        (it["draft"] for it in doc["iterations"] if it["n"] == iteration_n), None
    )
    if accepted_draft is None:
        raise KeyError(f"iteration {iteration_n} not found in design session")
    for it in doc["iterations"]:
        if it["n"] == iteration_n:
            it["accepted_at"] = _now_iso()
    doc["final_artefacts"] = accepted_draft
    doc["updated_at"] = _now_iso()
    await container.replace_item(item=doc["id"], body=doc, partition_key=doc["tenant_id"])
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
pytest tests/test_main.py -k "design_session" -v
```
Expected: all 4 `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/agent-design/cosmos.py services/agent-design/tests/test_main.py
git commit -m "feat(agent-design): add Cosmos DB design_sessions client with error handling"
```

---

## Task 4: RAG client

**Files:**
- Create: `services/agent-design/rag_client.py`

- [ ] **Step 1: Write failing test**

```python
# Add to services/agent-design/tests/test_main.py
@pytest.mark.asyncio
async def test_rag_client_returns_context():
    from rag_client import query_rag

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "chunks": [{"content": "Campus LAN best practice", "source": "doc1"}],
            "chunks_retrieved": 3,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)

        result = await query_rag(
            rag_url="http://rag-agent/query",
            tenant_id="tenant-a",
            session_id="session-1",
            query="campus LAN design",
            domains=["campus_lan"],
        )
    assert "chunks" in result
    assert result["chunks_retrieved"] == 3
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_main.py::test_rag_client_returns_context -v
```
Expected: `ImportError`

- [ ] **Step 3: Write rag_client.py**

```python
# services/agent-design/rag_client.py
import httpx


async def query_rag(
    rag_url: str, tenant_id: str, session_id: str,
    query: str, domains: list[str],
) -> dict:
    payload = {"tenant_id": tenant_id, "session_id": session_id,
               "query": query, "domain_filter": domains}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(rag_url, json=payload)
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Run test to confirm pass**

```bash
pytest tests/test_main.py::test_rag_client_returns_context -v
```
Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/agent-design/rag_client.py services/agent-design/tests/test_main.py
git commit -m "feat(agent-design): add RAG Agent HTTP client"
```

---

## Task 5: Domain inference

**Files:**
- Create: `services/agent-design/design_loop.py` (domain inference only)

- [ ] **Step 1: Write failing tests**

```python
# services/agent-design/tests/test_design_loop.py
import pytest
from design_loop import infer_domains


def test_infer_campus_lan():
    assert "campus_lan" in infer_domains("Design a campus network for 500 users")

def test_infer_wireless():
    assert "wireless" in infer_domains("WiFi coverage for a hospital")

def test_infer_meraki():
    assert "meraki" in infer_domains("Best practice for Meraki MX deployment")

def test_infer_wan_sdwan():
    assert "wan_sdwan" in infer_domains("SD-WAN architecture for a retail chain")

def test_infer_security_firewall():
    assert "security_firewall" in infer_domains("Firewall policy design for PCI DSS compliance")

def test_infer_sip():
    assert "sip_telephony" in infer_domains("SIP trunk design for a call centre")

def test_infer_multiple():
    domains = infer_domains("Campus LAN with Meraki wireless and SD-WAN edge")
    assert "campus_lan" in domains
    assert "meraki" in domains
    assert "wan_sdwan" in domains

def test_infer_unknown_returns_all():
    domains = infer_domains("Tell me about networking")
    assert len(domains) == 6
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_design_loop.py -v
```
Expected: `ImportError: cannot import name 'infer_domains' from 'design_loop'`

- [ ] **Step 3: Write design_loop.py with infer_domains**

```python
# services/agent-design/design_loop.py
_ALL_DOMAINS = [
    "campus_lan", "wireless", "meraki", "wan_sdwan", "security_firewall", "sip_telephony"
]

_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "campus_lan":        ["campus", "lan", "access layer", "distribution layer", "core layer", "switching", "vlan"],
    "wireless":          ["wifi", "wi-fi", "wireless", "wlan", "access point", "802.11", "hospital wifi"],
    "meraki":            ["meraki", "mx ", "ms ", "mr ", "dashboard"],
    "wan_sdwan":         ["wan", "sd-wan", "sdwan", "mpls", "broadband", "retail chain", "branch", "edge routing"],
    "security_firewall": ["firewall", "security", "pci", "ngfw", "acl", "dmz", "zero trust", "policy"],
    "sip_telephony":     ["sip", "telephony", "voip", "pbx", "call centre", "call center", "trunk", "pstn"],
}


def infer_domains(query: str) -> list[str]:
    """Return domains inferred from query keywords. Returns all domains if none match."""
    q = query.lower()
    matched = [d for d, kws in _DOMAIN_KEYWORDS.items() if any(kw in q for kw in kws)]
    return matched if matched else _ALL_DOMAINS
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
pytest tests/test_design_loop.py -v
```
Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/agent-design/design_loop.py services/agent-design/tests/test_design_loop.py
git commit -m "feat(agent-design): add domain inference from query keywords"
```

---

## Task 6: Design generation (Claude call)

**Files:**
- Modify: `services/agent-design/design_loop.py` — add `generate_design()` and `run_critique()`

`ChatCompletionsClient` from `azure-ai-inference` is **synchronous**. Both functions are plain `def` — the endpoint calls them via `asyncio.to_thread()` to avoid blocking the event loop. The `credential` object is passed in (initialised once at startup in `main.py`).

- [ ] **Step 1: Write failing test for generate_design**

```python
# Add to services/agent-design/tests/test_design_loop.py
from unittest.mock import MagicMock, patch
from design_loop import generate_design


def test_generate_design_returns_draft():
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = """{
        "narrative": "Use a collapsed core design.",
        "artefacts": [{"type": "vlan_table", "data": {}}],
        "domains_covered": ["campus_lan"],
        "assumptions": ["1000 users"],
        "open_questions": ["Redundancy requirements?"]
    }"""
    mock_resp.usage.total_tokens = 420

    with patch("design_loop.ChatCompletionsClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.complete.return_value = mock_resp
        mock_cls.return_value.__enter__.return_value = mock_client

        draft, tokens = generate_design(
            foundry_endpoint="https://fake.foundry",
            model="claude-sonnet-4-6",
            credential=MagicMock(),
            query="Design a campus LAN",
            rag_context={"chunks": []},
            domain_hints=["campus_lan"],
            prior_critique=None,
        )
    assert draft.narrative == "Use a collapsed core design."
    assert tokens == 420
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_design_loop.py::test_generate_design_returns_draft -v
```
Expected: `ImportError: cannot import name 'generate_design'`

- [ ] **Step 3: Implement generate_design in design_loop.py**

```python
# Add to services/agent-design/design_loop.py
import json
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from models import DesignDraft, CritiqueResult, CritiqueIssue

_DESIGN_SYSTEM_PROMPT = """You are a senior network architect with deep expertise across campus LAN,
wireless, Meraki, WAN/SDWAN, security, firewalls, SIP, and telephony.

Always respond with a JSON object matching this exact schema:
{
  "narrative": "<prose HLD/LLD recommendation>",
  "artefacts": [{"type": "<type>", "label": "<label>", "data": {}}],
  "domains_covered": ["<domain>"],
  "assumptions": ["<assumption>"],
  "open_questions": ["<question>"]
}"""


def generate_design(
    foundry_endpoint: str,
    model: str,
    credential,
    query: str,
    rag_context: dict,
    domain_hints: list[str],
    prior_critique: CritiqueResult | None,
) -> tuple["DesignDraft", int]:
    """
    Call Claude to generate a network design draft. Returns (DesignDraft, tokens_used).
    Synchronous — call via asyncio.to_thread() from async endpoints.
    """
    chunks_text = "\n\n".join(c.get("content", "") for c in rag_context.get("chunks", []))
    critique_section = ""
    if prior_critique:
        issues_text = "\n".join(
            f"- [{i.severity.upper()}] {i.domain}: {i.description}. Fix: {i.recommendation}"
            for i in prior_critique.issues
        )
        critique_section = (
            f"\n\nPRIOR CRITIQUE (score {prior_critique.score}/10 — revise to address):\n{issues_text}"
        )

    user_content = (
        f"REFERENCE MATERIAL:\n{chunks_text}\n\n"
        f"DESIGN DOMAINS: {', '.join(domain_hints)}\n\n"
        f"DESIGN REQUEST:\n{query}{critique_section}"
    )

    with ChatCompletionsClient(endpoint=foundry_endpoint, credential=credential) as client:
        response = client.complete(
            model=model,
            messages=[
                SystemMessage(content=_DESIGN_SYSTEM_PROMPT),
                UserMessage(content=user_content),
            ],
            max_tokens=4096,
        )

    data = json.loads(response.choices[0].message.content)
    return DesignDraft(**data), response.usage.total_tokens
```

- [ ] **Step 4: Run test to confirm pass**

```bash
pytest tests/test_design_loop.py::test_generate_design_returns_draft -v
```
Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/agent-design/design_loop.py services/agent-design/tests/test_design_loop.py
git commit -m "feat(agent-design): add Claude design generation (sync + threadpool-safe)"
```

---

## Task 7: Critique loop

**Files:**
- Modify: `services/agent-design/design_loop.py` — add `build_critique_system_prompt()` + `run_critique()`

- [ ] **Step 1: Write failing tests**

```python
# Add to services/agent-design/tests/test_design_loop.py
import json
from design_loop import run_critique, build_critique_system_prompt
from models import DesignDraft


def test_critique_system_prompt_level_1_vs_8():
    gentle = build_critique_system_prompt(1)
    harsh  = build_critique_system_prompt(8)
    assert "critical errors" in gentle.lower()
    assert "adversarial" in harsh.lower() or "challenge everything" in harsh.lower()


def test_run_critique_returns_result():
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = json.dumps({
        "score": 7,
        "issues": [{"severity": "major", "domain": "campus_lan",
                    "description": "No redundant uplinks", "recommendation": "Add LAG"}],
        "verdict": "revise"
    })
    mock_resp.usage.total_tokens = 280

    with patch("design_loop.ChatCompletionsClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.complete.return_value = mock_resp
        mock_cls.return_value.__enter__.return_value = mock_client

        draft = DesignDraft(narrative="Campus", artefacts=[], domains_covered=["campus_lan"],
                            assumptions=[], open_questions=[])
        result, tokens = run_critique(
            foundry_endpoint="https://fake.foundry",
            model="claude-sonnet-4-6",
            credential=MagicMock(),
            draft=draft,
            query="Design a campus LAN",
            critique_level=5,
        )
    assert result.score == 7
    assert result.verdict == "revise"
    assert tokens == 280


def test_run_critique_enforces_revise_when_score_below_5():
    """Score < 5 forces verdict to 'revise' regardless of what Claude returns."""
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = json.dumps({
        "score": 3,
        "issues": [],
        "verdict": "accept"   # Claude incorrectly returned accept with a low score
    })
    mock_resp.usage.total_tokens = 100

    with patch("design_loop.ChatCompletionsClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.complete.return_value = mock_resp
        mock_cls.return_value.__enter__.return_value = mock_client

        draft = DesignDraft(narrative="x", artefacts=[], domains_covered=[], assumptions=[], open_questions=[])
        result, _ = run_critique(
            foundry_endpoint="https://fake.foundry", model="claude-sonnet-4-6",
            credential=MagicMock(), draft=draft, query="q", critique_level=3,
        )
    assert result.verdict == "revise"
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_design_loop.py -k "critique" -v
```
Expected: `ImportError`

- [ ] **Step 3: Add critique functions to design_loop.py**

```python
# Add to services/agent-design/design_loop.py

_CRITIQUE_LEVEL_PROMPTS = {
    1: "Identify only critical errors that would cause the design to fail.",
    2: "Flag significant risks and gaps that could cause outages.",
    3: "Flag significant risks, gaps, and any unclear failure domains.",
    4: "Challenge design choices and suggest better alternatives where they exist.",
    5: "Challenge design choices, suggest alternatives, and verify all assumptions.",
    6: "Demand justification for all decisions. Stress-test every assumption.",
    7: "Demand justification and alternatives for every decision. Accept nothing at face value.",
    8: "Adversarial review. Challenge everything. Require alternative approaches for every design choice.",
}

_CRITIQUE_BASE = """You are a network design critic reviewing a peer's work.
Respond with a JSON object matching this exact schema:
{
  "score": <int 1-10, where 10 = no issues>,
  "issues": [
    {"severity": "critical|major|minor", "domain": "<domain>", "description": "<problem>", "recommendation": "<fix>"}
  ],
  "verdict": "accept|revise"
}
Set verdict to "revise" if score < 8 or any critical/major issues exist."""


def build_critique_system_prompt(critique_level: int) -> str:
    intensity = _CRITIQUE_LEVEL_PROMPTS.get(critique_level, _CRITIQUE_LEVEL_PROMPTS[5])
    return f"{_CRITIQUE_BASE}\n\nCRITIQUE INTENSITY: {intensity}"


def run_critique(
    foundry_endpoint: str,
    model: str,
    credential,
    draft: "DesignDraft",
    query: str,
    critique_level: int,
) -> tuple["CritiqueResult", int]:
    """
    Call Claude to critique a design draft. Returns (CritiqueResult, tokens_used).
    Enforces: score < 5 → verdict = "revise" regardless of Claude's output.
    Synchronous — call via asyncio.to_thread() from async endpoints.
    """
    system_prompt = build_critique_system_prompt(critique_level)
    user_content = (
        f"ORIGINAL REQUEST:\n{query}\n\n"
        f"DESIGN TO REVIEW:\n{draft.narrative}\n\n"
        f"ARTEFACTS:\n{json.dumps(draft.artefacts, indent=2)}\n\n"
        f"ASSUMPTIONS:\n{chr(10).join(draft.assumptions)}"
    )

    with ChatCompletionsClient(endpoint=foundry_endpoint, credential=credential) as client:
        response = client.complete(
            model=model,
            messages=[SystemMessage(content=system_prompt), UserMessage(content=user_content)],
            max_tokens=2048,
        )

    data = json.loads(response.choices[0].message.content)
    # Enforce: low scores must trigger revise regardless of Claude's verdict
    if data["score"] < 5:
        data["verdict"] = "revise"
    issues = [CritiqueIssue(**i) for i in data.get("issues", [])]
    return CritiqueResult(score=data["score"], issues=issues, verdict=data["verdict"]), response.usage.total_tokens
```

- [ ] **Step 4: Run all design loop tests**

```bash
pytest tests/test_design_loop.py -v
```
Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/agent-design/design_loop.py services/agent-design/tests/test_design_loop.py
git commit -m "feat(agent-design): add critique loop with harshness levels and score enforcement"
```

---

## Task 8: POST /design endpoint

**Files:**
- Modify: `services/agent-design/main.py` — add `/design` endpoint
- Modify: `services/agent-design/tests/test_main.py` — endpoint tests

The endpoint is async; it calls the sync `generate_design` and `run_critique` via `asyncio.to_thread()`. Dependencies are injected via FastAPI's `Depends` so tests can override them cleanly.

- [ ] **Step 1: Write failing tests using dependency_overrides**

```python
# Add to services/agent-design/tests/test_main.py
import asyncio
from models import DesignDraft, CritiqueResult


# Override callables injected via Depends in main.py
def _fake_generate_design(*args, **kwargs):
    return DesignDraft(
        narrative="Campus design", artefacts=[], domains_covered=["campus_lan"],
        assumptions=[], open_questions=[],
    ), 420


def _fake_run_critique(*args, **kwargs):
    return None, 0


@pytest.fixture
def design_client(monkeypatch, client):
    """Test client with Claude + RAG calls overridden."""
    monkeypatch.setenv("COSMOS_ENDPOINT", "https://fake.cosmos")
    monkeypatch.setenv("COSMOS_DATABASE", "vigil-db")
    monkeypatch.setenv("RAG_AGENT_URL", "http://rag-agent/query")
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://fake.foundry")
    monkeypatch.setenv("AZURE_FOUNDRY_MODEL", "claude-sonnet-4-6")

    mock_container = AsyncMock()
    mock_container.create_item = AsyncMock(return_value={})
    mock_container.read_item = AsyncMock(return_value={
        "id": "dsid-1", "tenant_id": "tenant-a", "iterations": [],
        "query": "Design a campus LAN", "domain_hints": ["campus_lan"],
        "critique_enabled": False, "critique_level": 5, "final_artefacts": None,
    })
    mock_container.replace_item = AsyncMock(return_value={})

    from main import app, get_design_container, _generate_design_fn, _run_critique_fn
    app.dependency_overrides[get_design_container] = lambda: mock_container
    app.dependency_overrides[_generate_design_fn] = lambda: _fake_generate_design
    app.dependency_overrides[_run_critique_fn] = lambda: _fake_run_critique

    with patch("rag_client.query_rag", new_callable=AsyncMock,
               return_value={"chunks": [], "chunks_retrieved": 0}):
        yield client

    app.dependency_overrides.clear()


def test_design_endpoint_no_critique(design_client):
    resp = design_client.post("/design", json={
        "tenant_id": "tenant-a", "session_id": "session-1",
        "query": "Design a campus LAN", "domain_hints": ["campus_lan"],
        "critique_enabled": False, "critique_level": 5,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["iteration_n"] == 1
    assert body["critique"] is None
    assert any(e["type"] == "design_rag_start" for e in body["sse_events"])
    assert any(e["type"] == "design_draft_ready" for e in body["sse_events"])
    assert any(e["type"] == "done" for e in body["sse_events"])
    assert body["tokens_used"] == 420
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_main.py::test_design_endpoint_no_critique -v
```
Expected: `ImportError` (get_design_container not yet exported)

- [ ] **Step 3: Add dependency providers + /design endpoint to main.py**

```python
# Add to services/agent-design/main.py
import asyncio
import os
from fastapi import Depends, FastAPI, HTTPException
from models import DesignRequest, DesignResponse, DesignAcceptRequest, DesignAcceptResponse
from cosmos import (
    create_design_session, get_design_session,
    append_iteration, finalise_session, accept_session,
)
from rag_client import query_rag
from design_loop import infer_domains, generate_design, run_critique

_MAX_ITERATIONS = 5


# ── Dependency providers (overridable in tests) ──────────────────────────────

def get_design_container():
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    return db.get_container_client("design_sessions")


def _generate_design_fn():
    return generate_design


def _run_critique_fn():
    return run_critique


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/design", response_model=DesignResponse)
async def design(
    request: DesignRequest,
    container=Depends(get_design_container),
    gen_fn=Depends(_generate_design_fn),
    crit_fn=Depends(_run_critique_fn),
):
    domain_hints = request.domain_hints or infer_domains(request.query)
    sse_events: list[dict] = []
    total_tokens = 0

    # Load or create session
    if request.design_session_id:
        try:
            doc = await get_design_session(container, request.design_session_id, request.tenant_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="design_session not found")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        design_session_id = request.design_session_id
        iteration_n = len(doc["iterations"]) + 1
    else:
        design_session_id = await create_design_session(
            container, request.tenant_id, request.session_id,
            request.query, domain_hints, request.critique_enabled, request.critique_level,
        )
        doc = await get_design_session(container, design_session_id, request.tenant_id)
        iteration_n = 1

    # RAG retrieval
    sse_events.append({"type": "design_rag_start", "domains": domain_hints,
                       "design_session_id": design_session_id})
    rag_context = await query_rag(
        rag_url=os.getenv("RAG_AGENT_URL", ""),
        tenant_id=request.tenant_id,
        session_id=request.session_id,
        query=request.query,
        domains=domain_hints,
    )
    sse_events.append({"type": "design_rag_complete", "domains": domain_hints,
                       "chunks_retrieved": rag_context.get("chunks_retrieved", 0),
                       "design_session_id": design_session_id})

    # Prior critique for re-submission
    prior_critique = None
    if doc["iterations"]:
        last = doc["iterations"][-1]
        if last.get("critique"):
            from models import CritiqueResult, CritiqueIssue
            c = last["critique"]
            prior_critique = CritiqueResult(
                score=c["score"],
                issues=[CritiqueIssue(**i) for i in c["issues"]],
                verdict=c["verdict"],
            )

    # Design generation (sync fn → threadpool)
    draft, design_tokens = await asyncio.to_thread(
        gen_fn(),
        foundry_endpoint=os.getenv("AZURE_FOUNDRY_ENDPOINT", ""),
        model=os.getenv("AZURE_FOUNDRY_MODEL", "claude-sonnet-4-6"),
        credential=_credential,
        query=request.query,
        rag_context=rag_context,
        domain_hints=domain_hints,
        prior_critique=prior_critique,
    )
    total_tokens += design_tokens
    sse_events.append({"type": "design_draft_ready", "iteration_n": iteration_n,
                       "design_session_id": design_session_id})

    # Critique (if enabled)
    critique = None
    critique_complete = False
    if request.critique_enabled:
        critique, crit_tokens = await asyncio.to_thread(
            crit_fn(),
            foundry_endpoint=os.getenv("AZURE_FOUNDRY_ENDPOINT", ""),
            model=os.getenv("AZURE_FOUNDRY_MODEL", "claude-sonnet-4-6"),
            credential=_credential,
            draft=draft,
            query=request.query,
            critique_level=request.critique_level,
        )
        total_tokens += crit_tokens
        sse_events.append({
            "type": "critique_iteration", "iteration_n": iteration_n,
            "score": critique.score, "issues": [i.model_dump() for i in critique.issues],
            "verdict": critique.verdict, "design_session_id": design_session_id,
        })
        if critique.verdict == "accept" or iteration_n >= _MAX_ITERATIONS:
            critique_complete = True

    # Write to Cosmos DB before emitting done
    await append_iteration(container, doc, {
        "n": iteration_n,
        "draft": draft.model_dump(),
        "critique": critique.model_dump() if critique else None,
        "accepted_at": None,
    })

    if critique_complete or not request.critique_enabled:
        await finalise_session(container, doc, draft.model_dump())
        if critique:
            sse_events.append({"type": "critique_complete", "final_score": critique.score,
                               "iterations_taken": iteration_n,
                               "design_session_id": design_session_id})

    sse_events.append({"type": "done", "tokens_used": total_tokens,
                       "session_id": request.session_id})

    return DesignResponse(
        design_session_id=design_session_id,
        iteration_n=iteration_n,
        draft=draft,
        critique=critique,
        critique_complete=critique_complete,
        tokens_used=total_tokens,
        sse_events=sse_events,
    )
```

- [ ] **Step 4: Run test to confirm pass**

```bash
pytest tests/test_main.py::test_design_endpoint_no_critique -v
```
Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/agent-design/main.py services/agent-design/tests/test_main.py
git commit -m "feat(agent-design): add POST /design endpoint with critique loop and token accounting"
```

---

## Task 9: POST /design/{id}/accept endpoint

**Files:**
- Modify: `services/agent-design/main.py`
- Modify: `services/agent-design/tests/test_main.py`

- [ ] **Step 1: Write failing tests**

```python
# Add to services/agent-design/tests/test_main.py

def test_accept_endpoint_calls_accept_session(design_client):
    with patch("main.get_design_session", new_callable=AsyncMock) as mock_get, \
         patch("main.accept_session", new_callable=AsyncMock) as mock_accept:
        mock_get.return_value = {
            "id": "dsid-1", "tenant_id": "tenant-a",
            "iterations": [{"n": 1, "draft": {}, "critique": None, "accepted_at": None}],
        }
        resp = design_client.post("/design/dsid-1/accept", json={"tenant_id": "tenant-a"})
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    mock_accept.assert_called_once_with(mock_get.return_value.__class__,
                                        mock_get.return_value, 1)


def test_accept_endpoint_wrong_tenant_returns_403(design_client):
    with patch("main.get_design_session", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = PermissionError("tenant_id mismatch")
        resp = design_client.post("/design/dsid-1/accept", json={"tenant_id": "tenant-b"})
    assert resp.status_code == 403


def test_accept_endpoint_not_found_returns_404(design_client):
    with patch("main.get_design_session", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = KeyError("design_session not found")
        resp = design_client.post("/design/dsid-missing/accept", json={"tenant_id": "tenant-a"})
    assert resp.status_code == 404
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_main.py -k "accept" -v
```
Expected: `404`

- [ ] **Step 3: Add /accept endpoint to main.py**

```python
# Add to services/agent-design/main.py

@app.post("/design/{design_session_id}/accept", response_model=DesignAcceptResponse)
async def accept_design(
    design_session_id: str,
    request: DesignAcceptRequest,
    container=Depends(get_design_container),
):
    try:
        doc = await get_design_session(container, design_session_id, request.tenant_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="design_session not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    latest_n = len(doc["iterations"])
    await accept_session(container, doc, latest_n)
    logger.info("Design session accepted",
                extra={"tenant_id": request.tenant_id, "design_session_id": design_session_id})
    return DesignAcceptResponse(design_session_id=design_session_id, accepted=True)
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/ -v
```
Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/agent-design/main.py services/agent-design/tests/test_main.py
git commit -m "feat(agent-design): add POST /design/{id}/accept with 403/404 handling"
```

---

## Task 10: Coordinator tool registration

**Files:**
- Create: `services/coordinator/tools/__init__.py`
- Create: `services/coordinator/tools/design.py`
- Modify: `services/coordinator/agent_loop.py`

- [ ] **Step 1: Write failing tests**

```python
# services/coordinator/tests/test_tools.py
import os
from unittest.mock import patch


def test_design_tool_schema():
    from tools.design import DESIGN_TOOL
    assert DESIGN_TOOL["name"] == "design_agent"
    props = DESIGN_TOOL["input_schema"]["properties"]
    assert all(k in props for k in ["query", "critique_enabled", "critique_level"])
    required = DESIGN_TOOL["input_schema"]["required"]
    assert "query" in required and "critique_enabled" in required and "critique_level" in required


def test_get_agent_url_design_agent():
    with patch.dict(os.environ, {"DESIGN_AGENT_URL": "http://vigil-agent-design"}):
        from agent_loop import _get_agent_url
        assert _get_agent_url("design_agent") == "http://vigil-agent-design"


def test_call_agent_uses_120s_timeout_for_design():
    """design_agent must use 120s timeout — design generation can be slow."""
    from agent_loop import AGENT_TIMEOUTS
    assert AGENT_TIMEOUTS.get("design_agent", 0) >= 120
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd services/coordinator
pytest tests/test_tools.py -v
```
Expected: `ModuleNotFoundError: No module named 'tools.design'`

- [ ] **Step 3: Create tools/ directory and files**

```python
# services/coordinator/tools/__init__.py
```

```python
# services/coordinator/tools/design.py
DESIGN_TOOL = {
    "name": "design_agent",
    "description": (
        "Expert network design consultant covering campus LAN, wireless, Meraki, WAN/SDWAN, "
        "security, firewalls, SIP, and telephony. Use when the user asks to design, plan, "
        "architect, or recommend a network solution. Returns narrative recommendations and "
        "structured design artefacts. Never makes changes to live devices."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query":             {"type": "string"},
            "domain_hints":      {"type": "array", "items": {"type": "string"}},
            "critique_enabled":  {"type": "boolean"},
            "critique_level":    {"type": "integer", "minimum": 1, "maximum": 8},
            "design_session_id": {"type": "string"},
        },
        "required": ["query", "critique_enabled", "critique_level"],
    },
}
```

- [ ] **Step 4: Update agent_loop.py**

Add to `_get_agent_url` urls dict:
```python
"design_agent": os.getenv("DESIGN_AGENT_URL", ""),
```

Add constant and update `_call_agent` signature:
```python
# Near top of agent_loop.py
AGENT_TIMEOUTS: dict[str, float] = {
    "design_agent": 120.0,
}

# Update _call_agent to accept timeout:
async def _call_agent(tool_name: str, tool_input: dict, request,
                      credential: str | None = None,
                      timeout: float = 30.0) -> dict:
    agent_url = _get_agent_url(tool_name)
    payload = {**tool_input, "tenant_id": request.tenant_id, "session_id": request.session_id}
    if credential:
        payload["write_credential"] = credential
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(agent_url, json=payload)
        resp.raise_for_status()
        return resp.json()
```

In the outer streaming generator (when implemented), relay sse_events with correct timeout:
```python
timeout = AGENT_TIMEOUTS.get(tool_name, 30.0)
result = await _call_agent(tool_name, tool_input, request, timeout=timeout)
for event in result.get("sse_events", []):
    yield _sse(event)
```

- [ ] **Step 5: Run coordinator tests**

```bash
cd services/coordinator
pytest tests/ -v
```
Expected: all `PASSED`

- [ ] **Step 6: Commit**

```bash
git add services/coordinator/tools/ services/coordinator/agent_loop.py services/coordinator/tests/test_tools.py
git commit -m "feat(coordinator): register design_agent tool, 120s timeout, sse_events relay"
```

---

## Task 11: Dockerfile + deploy workflow + full test suite + ARCHITECTURE.md

**Files:**
- Create: `services/agent-design/Dockerfile`
- Create: `.github/workflows/deploy-agent-design.yml`
- Modify: `ARCHITECTURE.md`

- [ ] **Step 1: Write Dockerfile**

```dockerfile
# services/agent-design/Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Write deploy workflow**

```yaml
# .github/workflows/deploy-agent-design.yml
name: Deploy Agent Design
on:
  push:
    branches: [main]
    paths:
      - 'services/agent-design/**'
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Log in to Azure
        uses: azure/login@v1
        with:
          creds: ${{ secrets.AZURE_CREDENTIALS }}
      - name: Log in to ACR
        uses: azure/docker-login@v1
        with:
          login-server: ${{ secrets.REGISTRY_LOGIN_SERVER }}
          username: ${{ secrets.REGISTRY_USERNAME }}
          password: ${{ secrets.REGISTRY_PASSWORD }}
      - name: Build and push
        run: |
          docker build -t ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-agent-design:${{ github.sha }} services/agent-design/
          docker push ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-agent-design:${{ github.sha }}
      - name: Deploy to Container Apps
        run: |
          az containerapp update \
            --name vigil-agent-design \
            --resource-group rg-vigil-prod \
            --image ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-agent-design:${{ github.sha }}
```

- [ ] **Step 3: Run full test suite**

```bash
cd services/agent-design && pytest tests/ -v
cd services/coordinator && pytest tests/ -v
```
Expected: all `PASSED`. Fix any failures before proceeding.

- [ ] **Step 4: Update ARCHITECTURE.md**

Add after Change Reviewer Agent in Component Reference:
```markdown
### Design Agent
- **Type:** Containerised (FastAPI + Claude Sonnet 4.6)
- **Service:** `services/agent-design`
- **Responsibilities:**
  - Query the existing RAG Agent to ground designs in the indexed knowledge base
  - Generate network designs (campus LAN, wireless, Meraki, WAN/SDWAN, security/firewalls, SIP/telephony) via Claude Sonnet 4.6
  - Run an optional Claude-to-Claude critique loop (up to 5 iterations, harshness 1–8 from UI slider)
  - Score < 5 forces `verdict = "revise"` regardless of Claude's output
  - Persists iteration state to `design_sessions` Cosmos DB (request-stateless critique loop)
  - Returns `sse_events` list; coordinator relays these to the UI in order
  - Claude calls are synchronous (`azure-ai-inference`); called via `asyncio.to_thread()` to avoid blocking the event loop
  - Never makes changes to live devices
- **Accept endpoint:** `POST /design/{design_session_id}/accept` — terminates critique loop, writes final artefacts, requires `tenant_id` in request body (standard platform pattern)
- **Never called directly by users** — coordinator only
```

Add to Cosmos DB containers: `design_sessions` — partitioned by `tenant_id`, keyed by `design_session_id`.

Add to coordinator env vars: `DESIGN_AGENT_URL`.

Add Design Agent env vars section:
```
COSMOS_ENDPOINT, COSMOS_DATABASE, RAG_AGENT_URL, AZURE_FOUNDRY_ENDPOINT, AZURE_FOUNDRY_MODEL
```

- [ ] **Step 5: Commit**

```bash
git add services/agent-design/Dockerfile .github/workflows/deploy-agent-design.yml ARCHITECTURE.md
git commit -m "feat(agent-design): add Dockerfile, deploy workflow; update ARCHITECTURE.md"
```
