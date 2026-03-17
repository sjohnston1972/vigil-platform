# Agent Troubleshoot Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `agent-troubleshoot` (unprivileged FastAPI dispatcher with three-tier approval model and extensible vendor connector registry) + `agent-probe` (privileged execution container for nmap, scapy, dig, curl, ssh) + the Gateway budget reconciliation task for interrupted streams.

**Architecture:** `agent-troubleshoot` receives diagnostic tasks from the coordinator, uses an internal Claude call (via `asyncio.to_thread`) to select tools and returns real token counts, classifies each tool invocation into a risk tier (show / invasive / config), dispatches show-tier calls immediately to `agent-probe` or vendor connectors, and writes `step_up_requests` for invasive/config-tier calls before terminating the stream (request-stateless approval gate). Config-tier also creates a Jira ticket via `agent-itsm` unless `emergency=true` (SAML role verified server-side via `json.loads`). `agent-probe` is a minimal privileged container with a single `/execute` endpoint, one call per target. The Gateway gains a background reconciliation task that deducts tokens from interrupted streams via `audit_logs`, using the `tenant_config` container to enumerate tenants (not cross-partition queries).

**Tech Stack:** Python 3.11, FastAPI, Pydantic, `azure-ai-inference` (tool selection Claude call — sync SDK, called via `asyncio.to_thread`), `azure-cosmos` (async), `azure-identity` (sync + async), `httpx`, `azure-keyvault-secrets` (JIT vendor creds). Probe container: `nmap`, `scapy`, `dig` (dnsutils), `curl`, `paramiko` (ssh). pytest + pytest-asyncio.

---

## File Map

**Create:**
- `services/agent-probe/main.py` — FastAPI app, `/health`, `POST /execute`
- `services/agent-probe/models.py` — Pydantic models for probe I/O
- `services/agent-probe/executor.py` — Dispatches to tool handlers
- `services/agent-probe/tools/__init__.py`
- `services/agent-probe/tools/dig_tool.py`
- `services/agent-probe/tools/curl_tool.py`
- `services/agent-probe/tools/nmap_tool.py`
- `services/agent-probe/tools/scapy_tool.py`
- `services/agent-probe/tools/ssh_tool.py`
- `services/agent-probe/requirements.txt`
- `services/agent-probe/Dockerfile`
- `services/agent-probe/tests/conftest.py`
- `services/agent-probe/tests/test_main.py`
- `services/agent-probe/tests/test_executor.py`
- `services/agent-troubleshoot/main.py` — FastAPI app, `/health`, `POST /troubleshoot`
- `services/agent-troubleshoot/models.py` — All Pydantic models
- `services/agent-troubleshoot/triage.py` — Tier classification (show/invasive/config) by tool + params
- `services/agent-troubleshoot/tool_selector.py` — Claude call (sync, called via asyncio.to_thread) to select tools; returns (jobs, tokens_used)
- `services/agent-troubleshoot/probe_client.py` — HTTP client for `agent-probe /execute`
- `services/agent-troubleshoot/itsm_client.py` — HTTP client for `agent-itsm /ticket` (config-tier Jira)
- `services/agent-troubleshoot/step_up_client.py` — Writes/reads `step_up_requests` and validates grants
- `services/agent-troubleshoot/audit.py` — Writes `audit_logs` entries
- `services/agent-troubleshoot/connectors/__init__.py` — Vendor registry `{vendor_id: ConnectorClass}`
- `services/agent-troubleshoot/connectors/base.py` — Abstract `FirewallConnector`
- `services/agent-troubleshoot/connectors/palo_alto.py` — PAN-OS REST API
- `services/agent-troubleshoot/connectors/cisco_asa.py` — ASA REST API / FTD
- `services/agent-troubleshoot/connectors/cisco_meraki.py` — Meraki Dashboard API
- `services/agent-troubleshoot/connectors/fortinet.py` — FortiGate REST API
- `services/agent-troubleshoot/requirements.txt`
- `services/agent-troubleshoot/Dockerfile`
- `services/agent-troubleshoot/tests/conftest.py`
- `services/agent-troubleshoot/tests/test_main.py`
- `services/agent-troubleshoot/tests/test_triage.py`
- `services/agent-troubleshoot/tests/test_connectors.py`
- `services/coordinator/tools/troubleshoot.py` — `troubleshoot_agent` tool definition
- `services/gateway/gateway_reconcile.py` — Budget reconciliation logic
- `.github/workflows/deploy-agent-troubleshoot.yml`
- `.github/workflows/deploy-agent-probe.yml`

**Modify:**
- `services/coordinator/agent_loop.py` — Add `troubleshoot_agent` URL mapping
- `services/gateway/main.py` — Add budget reconciliation background task
- `ARCHITECTURE.md`

---

## Task 1: agent-probe scaffold + health

**Files:**
- Create: `services/agent-probe/main.py`
- Create: `services/agent-probe/requirements.txt`
- Create: `services/agent-probe/tests/conftest.py`
- Create: `services/agent-probe/tests/test_main.py`

- [ ] **Step 1: Write failing health test**

```python
# services/agent-probe/tests/test_main.py
def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "agent-probe"}
```

```python
# services/agent-probe/tests/conftest.py
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from main import app
    yield TestClient(app)
```

- [ ] **Step 2: Write requirements.txt**

```
fastapi
uvicorn
pydantic
python-dotenv
pytest
pytest-asyncio
httpx
paramiko
dnspython
```

- [ ] **Step 3: Write main.py**

```python
# services/agent-probe/main.py
import logging
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "healthy", "service": "agent-probe"}
```

- [ ] **Step 4: Run to confirm pass**

```bash
cd services/agent-probe
pip install -r requirements.txt
pytest tests/test_main.py::test_health -v
```
Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/agent-probe/
git commit -m "feat(agent-probe): scaffold with health endpoint"
```

---

## Task 2: agent-probe models + executor

**Files:**
- Create: `services/agent-probe/models.py`
- Create: `services/agent-probe/executor.py`
- Create: `services/agent-probe/tools/__init__.py`

- [ ] **Step 1: Write models.py**

```python
# services/agent-probe/models.py
from typing import Literal
from pydantic import BaseModel


class ExecuteRequest(BaseModel):
    tool: Literal["nmap", "scapy", "dig", "curl", "ssh"]
    target: str
    params: dict = {}
    timeout_seconds: int = 20


class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    parsed: dict | None = None
```

- [ ] **Step 2: Write failing test for executor dispatch**

```python
# services/agent-probe/tests/test_executor.py
import pytest
from unittest.mock import patch, MagicMock
from models import ExecuteRequest, ExecuteResponse
from executor import execute


def test_executor_routes_to_dig():
    req = ExecuteRequest(tool="dig", target="example.com", params={}, timeout_seconds=20)
    fake_result = ExecuteResponse(stdout="example.com A 1.2.3.4", stderr="", exit_code=0, parsed={"a_records": ["1.2.3.4"]})
    with patch("executor.dig_tool.run", return_value=fake_result) as mock_dig:
        result = execute(req)
    mock_dig.assert_called_once_with("example.com", {}, 20)
    assert result.exit_code == 0


def test_executor_routes_to_curl():
    req = ExecuteRequest(tool="curl", target="https://example.com", params={}, timeout_seconds=20)
    fake_result = ExecuteResponse(stdout="200 OK", stderr="", exit_code=0, parsed={"status_code": 200})
    with patch("executor.curl_tool.run", return_value=fake_result) as mock_curl:
        result = execute(req)
    mock_curl.assert_called_once_with("https://example.com", {}, 20)
    assert result.exit_code == 0


def test_executor_unknown_tool_raises():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ExecuteRequest(tool="telnet", target="192.168.1.1")
```

- [ ] **Step 3: Run to confirm failure**

```bash
cd services/agent-probe
pytest tests/test_executor.py -v
```
Expected: `ImportError: No module named 'executor'`

- [ ] **Step 4: Write executor.py and tools/__init__.py**

```python
# services/agent-probe/tools/__init__.py
from . import dig_tool, curl_tool, nmap_tool, scapy_tool, ssh_tool

__all__ = ["dig_tool", "curl_tool", "nmap_tool", "scapy_tool", "ssh_tool"]
```

```python
# services/agent-probe/executor.py
from models import ExecuteRequest, ExecuteResponse
from tools import dig_tool, curl_tool, nmap_tool, scapy_tool, ssh_tool

_TOOL_MAP = {
    "dig":   dig_tool,
    "curl":  curl_tool,
    "nmap":  nmap_tool,
    "scapy": scapy_tool,
    "ssh":   ssh_tool,
}


def execute(req: ExecuteRequest) -> ExecuteResponse:
    handler = _TOOL_MAP[req.tool]
    return handler.run(req.target, req.params, req.timeout_seconds)
```

- [ ] **Step 5: Create stub tool modules** (real implementations come in Task 3)

```python
# services/agent-probe/tools/dig_tool.py
from models import ExecuteResponse

def run(target: str, params: dict, timeout_seconds: int) -> ExecuteResponse:
    raise NotImplementedError
```

Repeat the same stub for `curl_tool.py`, `nmap_tool.py`, `scapy_tool.py`, `ssh_tool.py`.

- [ ] **Step 6: Run tests to confirm pass**

```bash
pytest tests/test_executor.py -v
```
Expected: all `PASSED`

- [ ] **Step 7: Commit**

```bash
git add services/agent-probe/models.py services/agent-probe/executor.py services/agent-probe/tools/
git commit -m "feat(agent-probe): add executor dispatch and tool stubs"
```

---

## Task 3: Probe tool implementations (dig + curl + nmap)

**Files:**
- Modify: `services/agent-probe/tools/dig_tool.py`
- Modify: `services/agent-probe/tools/curl_tool.py`
- Modify: `services/agent-probe/tools/nmap_tool.py`

These use subprocess with timeout. `scapy` and `ssh` follow in Task 4.

- [ ] **Step 1: Write failing tests**

```python
# services/agent-probe/tests/test_executor.py  (add these)
from unittest.mock import patch
import subprocess


def test_dig_tool_parses_a_record():
    from tools.dig_tool import run
    mock_result = MagicMock()
    mock_result.stdout = "example.com.\t\t300\tIN\tA\t93.184.216.34\n"
    mock_result.stderr = ""
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        result = run("example.com", {}, 20)
    assert result.exit_code == 0
    assert "93.184.216.34" in result.parsed.get("a_records", [])


def test_curl_tool_returns_status_code():
    from tools.curl_tool import run
    mock_result = MagicMock()
    mock_result.stdout = "200"
    mock_result.stderr = ""
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        result = run("https://example.com", {}, 20)
    assert result.exit_code == 0
    assert result.parsed["status_code"] == 200


def test_nmap_basic_scan():
    from tools.nmap_tool import run
    mock_result = MagicMock()
    mock_result.stdout = "80/tcp open  http\n443/tcp open  https\n"
    mock_result.stderr = ""
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        result = run("192.168.1.1", {"profile": "basic"}, 20)
    assert result.exit_code == 0
    assert len(result.parsed.get("open_ports", [])) == 2


def test_nmap_timeout():
    from tools.nmap_tool import run
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("nmap", 20)):
        result = run("192.168.1.1", {}, 20)
    assert result.exit_code == 1
    assert "timeout" in result.stderr.lower()
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_executor.py::test_dig_tool_parses_a_record tests/test_executor.py::test_nmap_timeout -v
```
Expected: `NotImplementedError`

- [ ] **Step 3: Implement dig_tool.py**

Note: the first `cmd` assignment is intentionally absent — use only the final form.

```python
# services/agent-probe/tools/dig_tool.py
import re
import subprocess
from models import ExecuteResponse


def run(target: str, params: dict, timeout_seconds: int) -> ExecuteResponse:
    record_type = params.get("type", "A").upper()
    cmd = ["dig", "+noall", "+answer", target, record_type]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return ExecuteResponse(stdout="", stderr="timeout expired", exit_code=1)

    a_records = re.findall(r'\bIN\s+A\s+(\S+)', result.stdout)
    cname_records = re.findall(r'\bIN\s+CNAME\s+(\S+)', result.stdout)
    return ExecuteResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.returncode,
        parsed={"a_records": a_records, "cname_records": cname_records},
    )
```

- [ ] **Step 4: Implement curl_tool.py**

```python
# services/agent-probe/tools/curl_tool.py
import subprocess
from models import ExecuteResponse


def run(target: str, params: dict, timeout_seconds: int) -> ExecuteResponse:
    cmd = [
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "--max-time", str(timeout_seconds),
        target,
    ]
    headers = params.get("headers", {})
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds + 5)
    except subprocess.TimeoutExpired:
        return ExecuteResponse(stdout="", stderr="timeout expired", exit_code=1)

    try:
        status_code = int(result.stdout.strip())
    except ValueError:
        status_code = -1
    return ExecuteResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.returncode,
        parsed={"status_code": status_code},
    )
```

- [ ] **Step 5: Implement nmap_tool.py**

nmap profiles control aggressiveness. The triage module (in agent-troubleshoot) decides which profile is requested — the probe just executes it.

```python
# services/agent-probe/tools/nmap_tool.py
import re
import subprocess
from models import ExecuteResponse

_PROFILE_FLAGS: dict[str, list[str]] = {
    "ping":       ["-sn"],
    "basic":      ["-sS", "-F", "--open"],
    "service":    ["-sS", "-sV", "-F", "--open"],
    "aggressive": ["-sS", "-A", "-T4"],
}


def run(target: str, params: dict, timeout_seconds: int) -> ExecuteResponse:
    profile = params.get("profile", "basic")
    flags = _PROFILE_FLAGS.get(profile, _PROFILE_FLAGS["basic"])
    cmd = ["nmap"] + flags + [target]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return ExecuteResponse(stdout="", stderr="timeout expired", exit_code=1)

    open_ports = re.findall(r'(\d+)/tcp\s+open\s+(\S+)', result.stdout)
    return ExecuteResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.returncode,
        parsed={"open_ports": [{"port": int(p), "service": s} for p, s in open_ports]},
    )
```

- [ ] **Step 6: Run tests to confirm pass**

```bash
pytest tests/test_executor.py -v
```
Expected: all `PASSED`

- [ ] **Step 7: Commit**

```bash
git add services/agent-probe/tools/dig_tool.py services/agent-probe/tools/curl_tool.py services/agent-probe/tools/nmap_tool.py
git commit -m "feat(agent-probe): implement dig, curl, nmap tool handlers"
```

---

## Task 4: Probe tool implementations (scapy + ssh)

**Files:**
- Modify: `services/agent-probe/tools/scapy_tool.py`
- Modify: `services/agent-probe/tools/ssh_tool.py`

- [ ] **Step 1: Write failing tests**

```python
# Add to services/agent-probe/tests/test_executor.py

def test_ssh_tool_runs_show_command():
    from tools.ssh_tool import run
    with patch("paramiko.SSHClient") as mock_ssh_cls:
        mock_ssh = MagicMock()
        mock_ssh_cls.return_value = mock_ssh
        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"Hostname: router1\n"
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""
        mock_ssh.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

        result = run("192.168.1.1", {"command": "show version", "username": "admin", "password": "pass"}, 20)
    assert result.exit_code == 0
    assert "router1" in result.stdout


def test_scapy_ping_target():
    from tools.scapy_tool import run
    with patch("scapy.layers.inet.IP") as mock_ip, \
         patch("scapy.layers.inet.ICMP") as mock_icmp, \
         patch("scapy.sendrecv.sr1") as mock_sr1:
        mock_sr1.return_value = MagicMock()
        mock_sr1.return_value.summary.return_value = "ICMP Echo Reply"
        result = run("192.168.1.1", {"type": "ping"}, 20)
    assert result.exit_code == 0
```

- [ ] **Step 2: Implement scapy_tool.py**

```python
# services/agent-probe/tools/scapy_tool.py
from models import ExecuteResponse


def run(target: str, params: dict, timeout_seconds: int) -> ExecuteResponse:
    """
    Scapy tool — requires NET_RAW capability in the probe container.
    Supports type: "ping" (default).
    """
    try:
        from scapy.layers.inet import IP, ICMP
        from scapy.sendrecv import sr1

        pkt_type = params.get("type", "ping")
        if pkt_type == "ping":
            pkt = IP(dst=target) / ICMP()
            reply = sr1(pkt, timeout=timeout_seconds, verbose=False)
            if reply:
                summary = reply.summary()
                return ExecuteResponse(
                    stdout=summary, stderr="", exit_code=0,
                    parsed={"alive": True, "summary": summary},
                )
            return ExecuteResponse(
                stdout="", stderr="no reply", exit_code=1,
                parsed={"alive": False},
            )
        return ExecuteResponse(stdout="", stderr=f"unsupported type: {pkt_type}", exit_code=1)
    except Exception as exc:
        return ExecuteResponse(stdout="", stderr=str(exc), exit_code=1)
```

- [ ] **Step 3: Implement ssh_tool.py**

```python
# services/agent-probe/tools/ssh_tool.py
import paramiko
from models import ExecuteResponse


def run(target: str, params: dict, timeout_seconds: int) -> ExecuteResponse:
    """
    SSH tool — runs a single command on a remote host.
    Credentials passed via params (username, password or key_path).
    """
    username = params.get("username", "")
    password = params.get("password", "")
    command = params.get("command", "show version")
    port = int(params.get("port", 22))

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            target, port=port, username=username, password=password,
            timeout=timeout_seconds, banner_timeout=timeout_seconds,
        )
        _, stdout, stderr = client.exec_command(command, timeout=timeout_seconds)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return ExecuteResponse(stdout=out, stderr=err, exit_code=0, parsed={"lines": out.splitlines()})
    except Exception as exc:
        return ExecuteResponse(stdout="", stderr=str(exc), exit_code=1)
    finally:
        client.close()
```

- [ ] **Step 4: Run all probe tests**

```bash
pytest tests/ -v
```
Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/agent-probe/tools/scapy_tool.py services/agent-probe/tools/ssh_tool.py
git commit -m "feat(agent-probe): implement scapy and ssh tool handlers"
```

---

## Task 5: agent-probe /execute endpoint + Dockerfile

**Files:**
- Modify: `services/agent-probe/main.py` — add `POST /execute`
- Create: `services/agent-probe/Dockerfile`

- [ ] **Step 1: Write failing test**

```python
# Add to services/agent-probe/tests/test_main.py
from unittest.mock import patch, MagicMock
from models import ExecuteResponse


def test_execute_endpoint_calls_executor(client):
    fake = ExecuteResponse(stdout="ok", stderr="", exit_code=0, parsed={})
    with patch("main.execute", return_value=fake) as mock_exec:
        response = client.post("/execute", json={
            "tool": "dig",
            "target": "example.com",
            "params": {},
            "timeout_seconds": 20,
        })
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["stdout"] == "ok"
    mock_exec.assert_called_once()


def test_execute_rejects_unknown_tool(client):
    response = client.post("/execute", json={
        "tool": "telnet",
        "target": "192.168.1.1",
    })
    assert response.status_code == 422
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_main.py::test_execute_endpoint_calls_executor -v
```
Expected: `404`

- [ ] **Step 3: Add /execute to main.py**

```python
# Add to services/agent-probe/main.py
from fastapi import FastAPI
from models import ExecuteRequest, ExecuteResponse
from executor import execute as _execute

@app.post("/execute", response_model=ExecuteResponse)
def execute_tool(request: ExecuteRequest) -> ExecuteResponse:
    """Execute a diagnostic tool against a target. One call per target."""
    logger.info("Probe execute", extra={"tool": request.tool, "target": request.target})
    return _execute(request)
```

- [ ] **Step 4: Write Dockerfile**

```dockerfile
# services/agent-probe/Dockerfile
FROM python:3.11-slim

# Install system diagnostic tools
RUN apt-get update && apt-get install -y \
    nmap \
    dnsutils \
    curl \
    openssh-client \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Note: `scapy` is installed via pip (in requirements.txt). `NET_ADMIN`/`NET_RAW` capabilities are granted at the Azure Container Apps level (Dedicated workload profile, Terraform `capabilities` block).

- [ ] **Step 5: Run all probe tests**

```bash
pytest tests/ -v
```
Expected: all `PASSED`

- [ ] **Step 6: Commit**

```bash
git add services/agent-probe/main.py services/agent-probe/Dockerfile
git commit -m "feat(agent-probe): add POST /execute endpoint and Dockerfile"
```

---

## Task 6: agent-troubleshoot scaffold + triage

**Files:**
- Create: `services/agent-troubleshoot/main.py`
- Create: `services/agent-troubleshoot/models.py`
- Create: `services/agent-troubleshoot/triage.py`
- Create: `services/agent-troubleshoot/requirements.txt`
- Create: `services/agent-troubleshoot/tests/conftest.py`
- Create: `services/agent-troubleshoot/tests/test_triage.py`

- [ ] **Step 1: Write models.py**

```python
# services/agent-troubleshoot/models.py
from typing import Literal
from pydantic import BaseModel


class TroubleshootRequest(BaseModel):
    tenant_id: str
    session_id: str
    task: str
    targets: list[str]
    tools_hint: list[str] = []
    emergency: bool = False
    vendor: str | None = None
    step_up_grant_id: str | None = None
    step_up_request_id: str | None = None   # provided on re-submission to cross-check grant


class ProbeJob(BaseModel):
    tool: str
    target: str
    params: dict = {}
    tier: Literal["show", "invasive", "config"]


class TroubleshootResponse(BaseModel):
    findings: list[dict]
    interrupted: bool = False          # True if stream terminated for step-up
    step_up_request_id: str | None = None
    sse_events: list[dict]
```

- [ ] **Step 2: Write failing triage tests**

```python
# services/agent-troubleshoot/tests/test_triage.py
import pytest
from triage import classify_tier, SHOW, INVASIVE, CONFIG


def test_dig_is_show():
    assert classify_tier("dig", {}) == SHOW


def test_curl_is_show():
    assert classify_tier("curl", {}) == SHOW


def test_nmap_ping_is_show():
    assert classify_tier("nmap", {"profile": "ping"}) == SHOW


def test_nmap_basic_is_show():
    assert classify_tier("nmap", {"profile": "basic"}) == SHOW


def test_nmap_service_is_invasive():
    assert classify_tier("nmap", {"profile": "service"}) == INVASIVE


def test_nmap_aggressive_is_invasive():
    assert classify_tier("nmap", {"profile": "aggressive"}) == INVASIVE


def test_scapy_is_invasive():
    assert classify_tier("scapy", {}) == INVASIVE


def test_ssh_show_command_is_show():
    assert classify_tier("ssh", {"command": "show version"}) == SHOW


def test_ssh_config_command_is_config():
    assert classify_tier("ssh", {"command": "interface gi0/1"}) == CONFIG


def test_firewall_show_is_show():
    assert classify_tier("firewall_show", {}) == SHOW


def test_firewall_push_config_is_config():
    assert classify_tier("firewall_push_config", {}) == CONFIG
```

- [ ] **Step 3: Run to confirm failure**

```bash
cd services/agent-troubleshoot
pip install fastapi uvicorn pydantic pytest pytest-asyncio
pytest tests/test_triage.py -v
```
Expected: `ModuleNotFoundError: No module named 'triage'`

- [ ] **Step 4: Write triage.py**

```python
# services/agent-troubleshoot/triage.py
from typing import Literal

SHOW = "show"
INVASIVE = "invasive"
CONFIG = "config"

Tier = Literal["show", "invasive", "config"]

# SSH commands that are write/config operations
_SSH_CONFIG_PREFIXES = (
    "interface", "ip route", "router", "no ", "shutdown",
    "access-list", "vlan", "spanning-tree", "ntp", "logging",
)

# nmap profiles that are invasive
_NMAP_INVASIVE_PROFILES = {"service", "aggressive"}


def classify_tier(tool: str, params: dict) -> Tier:
    """
    Determine the risk tier for a tool invocation.
    This is the authoritative tier classification — not overridable by user or coordinator.
    """
    if tool in ("dig", "curl", "firewall_show", "firewall_get_config_targeted"):
        return SHOW

    if tool in ("firewall_push_config", "firewall_push_config_config"):
        return CONFIG

    if tool == "nmap":
        profile = params.get("profile", "basic")
        return INVASIVE if profile in _NMAP_INVASIVE_PROFILES else SHOW

    if tool == "scapy":
        return INVASIVE

    if tool == "ssh":
        command = params.get("command", "").strip().lower()
        if any(command.startswith(prefix) for prefix in _SSH_CONFIG_PREFIXES):
            return CONFIG
        return SHOW

    # Firewall get_config — invasive if broad scope, show if targeted
    if tool == "firewall_get_config":
        scope = params.get("scope", "targeted")
        return INVASIVE if scope == "broad" else SHOW

    # Default unknown tools to invasive for safety
    return INVASIVE
```

- [ ] **Step 5: Run tests to confirm pass**

```bash
pytest tests/test_triage.py -v
```
Expected: all `PASSED`

- [ ] **Step 6: Write main.py scaffold**

Note: initialize both a sync credential (for `ChatCompletionsClient` in tool_selector) and an async credential (for `CosmosClient`).

```python
# services/agent-troubleshoot/main.py
import logging
import os

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential as AsyncCredential
from azure.identity import DefaultAzureCredential as SyncCredential
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
_cosmos_client = None
_credential = None    # sync credential — for tool_selector's ChatCompletionsClient


@app.on_event("startup")
async def startup():
    global _cosmos_client, _credential
    async_cred = AsyncCredential()
    _cosmos_client = CosmosClient(
        url=os.getenv("COSMOS_ENDPOINT", ""),
        credential=async_cred,
    )
    _credential = SyncCredential()


@app.get("/health")
def health():
    return {"status": "healthy", "service": "agent-troubleshoot"}
```

- [ ] **Step 7: Write requirements.txt**

```
fastapi
uvicorn
pydantic
azure-cosmos
azure-identity
azure-keyvault-secrets
azure-ai-inference
httpx
python-dotenv
pytest
pytest-asyncio
```

- [ ] **Step 8: Write conftest.py**

Patch at the azure module level so startup() never touches Azure.

```python
# services/agent-troubleshoot/tests/conftest.py
import sys
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    sys.modules.pop("main", None)
    with patch("azure.cosmos.aio.CosmosClient") as mock_cosmos_cls, \
         patch("azure.identity.aio.DefaultAzureCredential") as mock_async_cred_cls, \
         patch("azure.identity.DefaultAzureCredential") as mock_sync_cred_cls:
        mock_cosmos_cls.return_value = MagicMock()
        mock_async_cred_cls.return_value = MagicMock()
        mock_sync_cred_cls.return_value = MagicMock()
        from main import app
        yield TestClient(app)
```

- [ ] **Step 9: Commit**

```bash
git add services/agent-troubleshoot/
git commit -m "feat(agent-troubleshoot): scaffold + tier classification"
```

---

## Task 7: Vendor connector registry

**Files:**
- Create: `services/agent-troubleshoot/connectors/base.py`
- Create: `services/agent-troubleshoot/connectors/__init__.py`
- Create: `services/agent-troubleshoot/connectors/palo_alto.py`
- Create: `services/agent-troubleshoot/connectors/cisco_asa.py`
- Create: `services/agent-troubleshoot/connectors/cisco_meraki.py`
- Create: `services/agent-troubleshoot/connectors/fortinet.py`

- [ ] **Step 1: Write failing connector test**

```python
# services/agent-troubleshoot/tests/test_connectors.py
import pytest


def test_registry_has_all_vendors():
    from connectors import get_connector
    for vendor in ["palo_alto", "cisco_asa", "cisco_meraki", "fortinet"]:
        connector_cls = get_connector(vendor)
        assert connector_cls is not None, f"Missing connector for {vendor}"


def test_unknown_vendor_raises():
    from connectors import get_connector
    with pytest.raises(KeyError):
        get_connector("juniper")


def test_palo_alto_has_required_methods():
    from connectors.palo_alto import PaloAltoConnector
    from connectors.base import FirewallConnector
    assert issubclass(PaloAltoConnector, FirewallConnector)
    assert hasattr(PaloAltoConnector, "show")
    assert hasattr(PaloAltoConnector, "get_config")
    assert hasattr(PaloAltoConnector, "push_config")
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_connectors.py -v
```
Expected: `ModuleNotFoundError: No module named 'connectors'`

- [ ] **Step 3: Write base.py**

```python
# services/agent-troubleshoot/connectors/base.py
from abc import ABC, abstractmethod


class FirewallConnector(ABC):
    """
    Abstract base for all vendor firewall connectors.

    Tier mapping (enforced by agent-troubleshoot, not by connectors):
      show()       → SHOW tier — no approval required
      get_config() → SHOW (targeted) or INVASIVE (broad scope) — agent decides
      push_config()→ CONFIG tier — requires active step-up grant before call
    """

    def __init__(self, host: str, credentials: dict):
        self.host = host
        self.credentials = credentials

    @abstractmethod
    def show(self, command: str) -> dict:
        """Run a read-only show command. Returns structured result."""

    @abstractmethod
    def get_config(self, scope: str = "targeted", section: str | None = None) -> dict:
        """Retrieve configuration. scope='targeted' for specific section, 'broad' for full config."""

    @abstractmethod
    def push_config(self, commands: list[str]) -> dict:
        """Apply configuration commands. ONLY called after step-up grant verified."""
```

- [ ] **Step 4: Write connector stubs**

Each connector has the same structure. Write PaloAlto fully; the others follow the same pattern.

```python
# services/agent-troubleshoot/connectors/palo_alto.py
import httpx
from .base import FirewallConnector


class PaloAltoConnector(FirewallConnector):
    """PAN-OS REST API connector. Supports direct device and Panorama."""

    def _get(self, path: str, params: dict = {}) -> dict:
        api_key = self.credentials.get("api_key", "")
        base_url = f"https://{self.host}/restapi/v10.2"
        with httpx.Client(verify=False, timeout=20) as client:
            resp = client.get(
                f"{base_url}{path}",
                headers={"X-PAN-KEY": api_key},
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    def show(self, command: str) -> dict:
        return self._get("/Objects/Addresses", {"name": command})

    def get_config(self, scope: str = "targeted", section: str | None = None) -> dict:
        path = "/Device/VirtualSystems" if scope == "broad" else "/Policies/SecurityRules"
        return self._get(path)

    def push_config(self, commands: list[str]) -> dict:
        raise NotImplementedError("PaloAlto push_config not yet implemented")
```

```python
# services/agent-troubleshoot/connectors/cisco_asa.py
import httpx
from .base import FirewallConnector


class CiscoASAConnector(FirewallConnector):
    def show(self, command: str) -> dict:
        token = self.credentials.get("token", "")
        url = f"https://{self.host}/api/cli"
        with httpx.Client(verify=False, timeout=20) as client:
            resp = client.post(url, json={"commands": [command]}, headers={"X-Auth-Token": token})
            resp.raise_for_status()
            return resp.json()

    def get_config(self, scope: str = "targeted", section: str | None = None) -> dict:
        return self.show(f"show running-config{' ' + section if section else ''}")

    def push_config(self, commands: list[str]) -> dict:
        raise NotImplementedError("CiscoASA push_config not yet implemented")
```

```python
# services/agent-troubleshoot/connectors/cisco_meraki.py
import httpx
from .base import FirewallConnector


class CiscoMerakiConnector(FirewallConnector):
    _BASE = "https://api.meraki.com/api/v1"

    def _get(self, path: str) -> dict:
        api_key = self.credentials.get("api_key", "")
        with httpx.Client(timeout=20) as client:
            resp = client.get(
                f"{self._BASE}{path}",
                headers={"X-Cisco-Meraki-API-Key": api_key},
            )
            resp.raise_for_status()
            return resp.json()

    def show(self, command: str) -> dict:
        return self._get(command)

    def get_config(self, scope: str = "targeted", section: str | None = None) -> dict:
        org_id = self.credentials.get("org_id", "")
        return self._get(f"/organizations/{org_id}/networks")

    def push_config(self, commands: list[str]) -> dict:
        raise NotImplementedError("Meraki push_config not yet implemented")
```

```python
# services/agent-troubleshoot/connectors/fortinet.py
import httpx
from .base import FirewallConnector


class FortinetConnector(FirewallConnector):
    def _get(self, path: str) -> dict:
        token = self.credentials.get("token", "")
        with httpx.Client(verify=False, timeout=20) as client:
            resp = client.get(
                f"https://{self.host}/api/v2/monitor/{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    def show(self, command: str) -> dict:
        return self._get(command)

    def get_config(self, scope: str = "targeted", section: str | None = None) -> dict:
        path = "system/config/backup?destination=file&scope=global" if scope == "broad" else "firewall/policy"
        return self._get(path)

    def push_config(self, commands: list[str]) -> dict:
        raise NotImplementedError("Fortinet push_config not yet implemented")
```

```python
# services/agent-troubleshoot/connectors/__init__.py
from .palo_alto import PaloAltoConnector
from .cisco_asa import CiscoASAConnector
from .cisco_meraki import CiscoMerakiConnector
from .fortinet import FortinetConnector
from .base import FirewallConnector

_REGISTRY: dict[str, type[FirewallConnector]] = {
    "palo_alto":    PaloAltoConnector,
    "cisco_asa":    CiscoASAConnector,
    "cisco_meraki": CiscoMerakiConnector,
    "fortinet":     FortinetConnector,
}


def get_connector(vendor_id: str) -> type[FirewallConnector]:
    """Return the connector class for a vendor. Raises KeyError for unknown vendors."""
    if vendor_id not in _REGISTRY:
        raise KeyError(f"No connector registered for vendor: {vendor_id}")
    return _REGISTRY[vendor_id]
```

- [ ] **Step 5: Run tests to confirm pass**

```bash
pytest tests/test_connectors.py -v
```
Expected: all `PASSED`

- [ ] **Step 6: Commit**

```bash
git add services/agent-troubleshoot/connectors/
git commit -m "feat(agent-troubleshoot): add extensible vendor connector registry (PAN-OS, ASA, Meraki, Fortinet)"
```

---

## Task 8: Probe client + ITSM client + audit logging

**Files:**
- Create: `services/agent-troubleshoot/probe_client.py`
- Create: `services/agent-troubleshoot/itsm_client.py`
- Create: `services/agent-troubleshoot/audit.py`

- [ ] **Step 1: Write failing tests**

```python
# services/agent-troubleshoot/tests/test_main.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_probe_client_calls_execute():
    from probe_client import call_probe
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"stdout": "ok", "stderr": "", "exit_code": 0, "parsed": {}}
        mock_resp.raise_for_status = MagicMock()
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)

        result = await call_probe(
            probe_url="http://agent-probe",
            tool="dig",
            target="example.com",
            params={},
            timeout_seconds=20,
        )
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_itsm_client_creates_ticket():
    from itsm_client import create_jira_ticket
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ticket_id": "VIGIL-123", "url": "https://jira.example.com/VIGIL-123"}
        mock_resp.raise_for_status = MagicMock()
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)

        result = await create_jira_ticket(
            itsm_url="http://agent-itsm",
            tenant_id="tenant-a",
            session_id="session-1",
            summary="Config change: firewall rule update",
            description="Requested by agent-troubleshoot for config-tier tool",
        )
    assert result["ticket_id"] == "VIGIL-123"


@pytest.mark.asyncio
async def test_audit_write_contains_tenant():
    from audit import write_probe_audit

    mock_container = AsyncMock()
    mock_container.create_item = AsyncMock(return_value={})

    await write_probe_audit(
        container=mock_container,
        tenant_id="tenant-a",
        session_id="session-1",
        tool="dig",
        target="example.com",
        tier="show",
        outcome="success",
        duration_ms=120,
        tokens_used=0,
        step_up_request_id=None,
        emergency=False,
    )
    call_args = mock_container.create_item.call_args[1]["body"]
    assert call_args["tenant_id"] == "tenant-a"
    assert call_args["tool"] == "dig"
    assert call_args["budget_deducted"] is True   # success outcome — deducted immediately
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_main.py::test_probe_client_calls_execute tests/test_main.py::test_audit_write_contains_tenant -v
```
Expected: `ImportError`

- [ ] **Step 3: Write probe_client.py**

```python
# services/agent-troubleshoot/probe_client.py
import httpx


async def call_probe(
    probe_url: str,
    tool: str,
    target: str,
    params: dict,
    timeout_seconds: int = 20,
) -> dict:
    payload = {"tool": tool, "target": target, "params": params, "timeout_seconds": timeout_seconds}
    async with httpx.AsyncClient(timeout=timeout_seconds + 5) as client:
        resp = await client.post(f"{probe_url}/execute", json=payload)
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Write itsm_client.py**

```python
# services/agent-troubleshoot/itsm_client.py
"""
Calls agent-itsm to raise a Jira ticket for config-tier tool invocations.
Skipped when emergency=True (caller responsibility).
"""
import httpx


async def create_jira_ticket(
    itsm_url: str,
    tenant_id: str,
    session_id: str,
    summary: str,
    description: str,
) -> dict:
    """Create a Jira ticket via agent-itsm. Returns {'ticket_id': ..., 'url': ...}."""
    payload = {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "summary": summary,
        "description": description,
        "issue_type": "Change",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{itsm_url}/ticket", json=payload)
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 5: Write audit.py**

```python
# services/agent-troubleshoot/audit.py
import uuid
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def write_probe_audit(
    container,
    tenant_id: str,
    session_id: str,
    tool: str,
    target: str,
    tier: str,
    outcome: str,
    duration_ms: int,
    tokens_used: int,
    step_up_request_id: str | None,
    emergency: bool,
) -> None:
    """
    Write an audit_logs entry for a probe invocation.

    budget_deducted is set to True immediately for non-interrupted streams.
    For interrupted streams (outcome='interrupted'), budget_deducted=False —
    the Gateway reconciliation task will deduct and flip it.
    """
    budget_deducted = outcome != "interrupted"
    doc = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "session_id": session_id,
        "event_type": "probe_invocation",
        "tool": tool,
        "target": target,
        "tier": tier,
        "step_up_request_id": step_up_request_id,
        "emergency": emergency,
        "outcome": outcome,
        "duration_ms": duration_ms,
        "tokens_used": tokens_used,
        "budget_deducted": budget_deducted,
        "timestamp": _now_iso(),
    }
    await container.create_item(body=doc)
```

- [ ] **Step 6: Run tests to confirm pass**

```bash
pytest tests/test_main.py -v
```
Expected: all `PASSED`

- [ ] **Step 7: Commit**

```bash
git add services/agent-troubleshoot/probe_client.py services/agent-troubleshoot/itsm_client.py services/agent-troubleshoot/audit.py
git commit -m "feat(agent-troubleshoot): add probe client, itsm client, and audit logging"
```

---

## Task 9: Step-up client + tool_selector + main /troubleshoot endpoint

**Files:**
- Create: `services/agent-troubleshoot/step_up_client.py`
- Create: `services/agent-troubleshoot/tool_selector.py`
- Modify: `services/agent-troubleshoot/main.py` — add `POST /troubleshoot`
- Modify: `services/agent-troubleshoot/tests/test_main.py` — endpoint tests

- [ ] **Step 1: Write step_up_client.py**

```python
# services/agent-troubleshoot/step_up_client.py
import uuid
from datetime import datetime, timezone, timedelta


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def write_step_up_request(
    container,
    tenant_id: str,
    session_id: str,
    user_identity: str,
    tool_name: str,
    context: dict,
    expires_in_seconds: int = 300,
) -> dict:
    """Write a step_up_requests document. Returns the document (includes 'id')."""
    request_id = str(uuid.uuid4())
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)).isoformat()
    doc = {
        "id": request_id,
        "tenant_id": tenant_id,
        "session_id": session_id,
        "tool_name": tool_name,
        "requested_by": user_identity,
        "context": context,
        "status": "pending",
        "expires_at": expires_at,
        "created_at": _now_iso(),
    }
    await container.create_item(body=doc)
    return doc


async def validate_grant(
    grants_container,
    grant_id: str,
    tenant_id: str,
    tool_name: str,
    step_up_request_id: str | None = None,
) -> bool:
    """
    Validate that an active step_up_grant exists for this tenant + tool.
    Optionally cross-checks step_up_request_id to prevent grant reuse across different requests.
    Returns True if valid, False otherwise.
    """
    try:
        grant = await grants_container.read_item(item=grant_id, partition_key=tenant_id)
        valid = (
            grant.get("tenant_id") == tenant_id
            and grant.get("tool_name") == tool_name
            and grant.get("status") == "granted"
        )
        if valid and step_up_request_id is not None:
            # Cross-check: grant must have been issued for this specific request
            valid = grant.get("request_id") == step_up_request_id
        return valid
    except Exception:
        return False
```

- [ ] **Step 2: Write tool_selector.py**

`select_tools` is a plain synchronous function — call it from the async endpoint via `asyncio.to_thread()`. It returns a tuple `(jobs, tokens_used)` so real token counts flow through to audit.

```python
# services/agent-troubleshoot/tool_selector.py
"""
Uses an internal Claude call to select diagnostic tools from a natural language task.
Returns (jobs, tokens_used) where jobs is a list of {tool, target, params} dicts.

IMPORTANT: This is a plain synchronous function — call via asyncio.to_thread() from async endpoints.
"""
import json
import logging
import os

from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage

logger = logging.getLogger(__name__)

_SYSTEM = """You are a network troubleshooting assistant. Given a diagnostic task and a list of targets,
select the appropriate tools to run. Available tools: dig, curl, nmap, scapy, ssh, firewall_show, firewall_get_config, firewall_push_config.

Respond with a JSON array only — no prose, no markdown fences:
[{"tool": "<tool>", "target": "<target>", "params": {<tool-specific params>}}]

nmap params: {"profile": "ping|basic|service|aggressive"}
ssh params: {"command": "<show command>", "username": "<user>"}
firewall_show params: {"command": "<show path>"}
firewall_get_config params: {"scope": "targeted|broad", "section": "<optional section>"}"""


def select_tools(
    task: str,
    targets: list[str],
    tools_hint: list[str],
    credential,
) -> tuple[list[dict], int]:
    """
    Return (jobs, tokens_used) for the given task.
    credential must be a sync azure.identity.DefaultAzureCredential instance.
    """
    hint_text = f"\nPrefer these tools if applicable: {', '.join(tools_hint)}" if tools_hint else ""
    user_content = f"TASK: {task}\nTARGETS: {', '.join(targets)}{hint_text}"

    with ChatCompletionsClient(
        endpoint=os.getenv("AZURE_FOUNDRY_ENDPOINT", ""),
        credential=credential,
    ) as client:
        response = client.complete(
            model=os.getenv("AZURE_FOUNDRY_MODEL", "claude-sonnet-4-6"),
            messages=[SystemMessage(content=_SYSTEM), UserMessage(content=user_content)],
            max_tokens=512,
        )

    tokens_used = response.usage.total_tokens if response.usage else 0
    raw = response.choices[0].message.content

    try:
        jobs = json.loads(raw)
        if not isinstance(jobs, list):
            logger.warning("select_tools: response was not a JSON array, defaulting to empty")
            jobs = []
    except json.JSONDecodeError:
        logger.warning("select_tools: failed to parse JSON response: %s", raw[:200])
        jobs = []

    return jobs, tokens_used
```

- [ ] **Step 3: Write failing endpoint tests**

```python
# Add to services/agent-troubleshoot/tests/test_main.py

@pytest.mark.asyncio
async def test_troubleshoot_show_tier_dispatches_immediately(client):
    """dig is show-tier — no approval needed, runs immediately."""
    with patch("main.select_tools", return_value=(
            [{"tool": "dig", "target": "example.com", "params": {}}], 42
        )), \
         patch("probe_client.call_probe", new_callable=AsyncMock, return_value={
             "stdout": "93.184.216.34", "stderr": "", "exit_code": 0, "parsed": {}
         }), \
         patch("audit.write_probe_audit", new_callable=AsyncMock), \
         patch("main._cosmos_client") as mock_cosmos:
        mock_cosmos.get_database_client.return_value.get_container_client.return_value = AsyncMock()
        resp = client.post("/troubleshoot", json={
            "tenant_id": "tenant-a",
            "session_id": "session-1",
            "task": "Check DNS for example.com",
            "targets": ["example.com"],
            "emergency": False,
        }, headers={"x-user-claims": '{"sub": "user@example.com", "roles": []}'})
    assert resp.status_code == 200
    body = resp.json()
    assert body["interrupted"] is False
    assert any(e["type"] == "probe_complete" for e in body["sse_events"])


@pytest.mark.asyncio
async def test_troubleshoot_invasive_tier_terminates_stream(client):
    """nmap aggressive is invasive — should terminate with probe_warning."""
    with patch("main.select_tools", return_value=(
            [{"tool": "nmap", "target": "192.168.1.0/24", "params": {"profile": "aggressive"}}], 38
        )), \
         patch("step_up_client.write_step_up_request", new_callable=AsyncMock, return_value={"id": "req-1"}), \
         patch("audit.write_probe_audit", new_callable=AsyncMock), \
         patch("main._cosmos_client") as mock_cosmos:
        mock_cosmos.get_database_client.return_value.get_container_client.return_value = AsyncMock()
        resp = client.post("/troubleshoot", json={
            "tenant_id": "tenant-a",
            "session_id": "session-1",
            "task": "Aggressive scan of subnet",
            "targets": ["192.168.1.0/24"],
            "emergency": False,
        }, headers={"x-user-claims": '{"sub": "user@example.com", "roles": []}'})
    assert resp.status_code == 200
    body = resp.json()
    assert body["interrupted"] is True
    assert body["step_up_request_id"] == "req-1"
    assert any(e["type"] == "probe_warning" for e in body["sse_events"])


@pytest.mark.asyncio
async def test_troubleshoot_emergency_flag_rejects_without_role(client):
    """emergency=True without emergency_change role must return 403."""
    resp = client.post("/troubleshoot", json={
        "tenant_id": "tenant-a",
        "session_id": "session-1",
        "task": "Config change",
        "targets": ["192.168.1.1"],
        "emergency": True,
    }, headers={"x-user-claims": '{"sub": "user@example.com", "roles": ["read_only"]}'})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_troubleshoot_emergency_flag_invalid_json_rejects(client):
    """Malformed x-user-claims must return 403, not crash."""
    resp = client.post("/troubleshoot", json={
        "tenant_id": "tenant-a",
        "session_id": "session-1",
        "task": "Config change",
        "targets": ["192.168.1.1"],
        "emergency": True,
    }, headers={"x-user-claims": "emergency_change"})   # not valid JSON
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_troubleshoot_config_tier_creates_jira(client):
    """config-tier tool should call agent-itsm when emergency=False."""
    with patch("main.select_tools", return_value=(
            [{"tool": "firewall_push_config", "target": "fw1.example.com", "params": {}}], 55
        )), \
         patch("step_up_client.write_step_up_request", new_callable=AsyncMock, return_value={"id": "req-2"}), \
         patch("itsm_client.create_jira_ticket", new_callable=AsyncMock, return_value={"ticket_id": "VIGIL-99", "url": "http://jira/VIGIL-99"}) as mock_jira, \
         patch("audit.write_probe_audit", new_callable=AsyncMock), \
         patch("main._cosmos_client") as mock_cosmos:
        mock_cosmos.get_database_client.return_value.get_container_client.return_value = AsyncMock()
        resp = client.post("/troubleshoot", json={
            "tenant_id": "tenant-a",
            "session_id": "session-1",
            "task": "Push firewall config",
            "targets": ["fw1.example.com"],
            "emergency": False,
        }, headers={"x-user-claims": '{"sub": "user@example.com", "roles": []}'})
    assert resp.status_code == 200
    body = resp.json()
    assert body["interrupted"] is True
    mock_jira.assert_called_once()
    assert any(e["type"] == "config_change_jira" for e in body["sse_events"])


@pytest.mark.asyncio
async def test_troubleshoot_config_tier_skips_jira_for_emergency(client):
    """config-tier with emergency=True should skip Jira but still require step-up."""
    with patch("main.select_tools", return_value=(
            [{"tool": "firewall_push_config", "target": "fw1.example.com", "params": {}}], 55
        )), \
         patch("step_up_client.write_step_up_request", new_callable=AsyncMock, return_value={"id": "req-3"}), \
         patch("itsm_client.create_jira_ticket", new_callable=AsyncMock) as mock_jira, \
         patch("audit.write_probe_audit", new_callable=AsyncMock), \
         patch("main._cosmos_client") as mock_cosmos:
        mock_cosmos.get_database_client.return_value.get_container_client.return_value = AsyncMock()
        resp = client.post("/troubleshoot", json={
            "tenant_id": "tenant-a",
            "session_id": "session-1",
            "task": "Emergency firewall config",
            "targets": ["fw1.example.com"],
            "emergency": True,
        }, headers={"x-user-claims": '{"sub": "user@example.com", "roles": ["emergency_change"]}'})
    assert resp.status_code == 200
    body = resp.json()
    assert body["interrupted"] is True
    mock_jira.assert_not_called()
    assert not any(e["type"] == "config_change_jira" for e in body["sse_events"])
```

- [ ] **Step 4: Run to confirm failure**

```bash
pytest tests/test_main.py::test_troubleshoot_show_tier_dispatches_immediately -v
```
Expected: `404`

- [ ] **Step 5: Add /troubleshoot endpoint to main.py**

```python
# Add to services/agent-troubleshoot/main.py
import asyncio
import json
import os
import time

from fastapi import FastAPI, HTTPException, Header
from models import TroubleshootRequest, TroubleshootResponse
from triage import classify_tier, SHOW, INVASIVE, CONFIG
from tool_selector import select_tools as _select_tools_sync
from probe_client import call_probe
from step_up_client import write_step_up_request, validate_grant
import itsm_client as itsm_mod
import audit as audit_mod

_PROBE_TIMEOUT = int(os.getenv("PROBE_TIMEOUT_SECONDS", "20"))


def _get_container(name: str):
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    return db.get_container_client(name)


def _parse_emergency_role(x_user_claims: str) -> bool:
    """
    Parse x-user-claims header (JSON) and check for 'emergency_change' role.
    Returns False if header is missing, malformed, or role is absent.
    Server-side check — UI checkbox is UX convenience only.
    """
    try:
        claims = json.loads(x_user_claims)
        return "emergency_change" in claims.get("roles", [])
    except (json.JSONDecodeError, AttributeError):
        return False


@app.post("/troubleshoot", response_model=TroubleshootResponse)
async def troubleshoot(
    request: TroubleshootRequest,
    x_user_claims: str = Header(default="{}"),   # SAML claims forwarded by Gateway
):
    # Validate emergency flag server-side via JSON parse + role membership check
    has_emergency_role = _parse_emergency_role(x_user_claims)
    if request.emergency and not has_emergency_role:
        raise HTTPException(status_code=403, detail="emergency_change role required")

    audit_container = _get_container("audit_logs")
    step_up_container = _get_container("step_up_requests")
    probe_url = os.getenv("PROBE_URL", "")
    itsm_url = os.getenv("ITSM_AGENT_URL", "")

    sse_events: list[dict] = []
    findings: list[dict] = []

    # Select tools via Claude — sync SDK, called via asyncio.to_thread
    tool_jobs, selector_tokens = await asyncio.to_thread(
        _select_tools_sync,
        request.task,
        request.targets,
        request.tools_hint,
        _credential,
    )

    # Process each job — one call per target
    for job in tool_jobs:
        tool = job["tool"]
        target = job["target"]
        params = job.get("params", {})
        tier = classify_tier(tool, params)

        if tier == INVASIVE or tier == CONFIG:
            # Validate existing grant on re-submission
            if request.step_up_grant_id:
                grants_container = _get_container("step_up_grants")
                grant_valid = await validate_grant(
                    grants_container,
                    request.step_up_grant_id,
                    request.tenant_id,
                    tool,
                    step_up_request_id=request.step_up_request_id,
                )
                if not grant_valid:
                    raise HTTPException(status_code=403, detail="invalid or expired step_up_grant")
                # Grant valid — fall through to probe dispatch below
            else:
                # Config-tier: create Jira ticket unless emergency
                if tier == CONFIG and not request.emergency:
                    try:
                        ticket = await itsm_mod.create_jira_ticket(
                            itsm_url=itsm_url,
                            tenant_id=request.tenant_id,
                            session_id=request.session_id,
                            summary=f"Config change: {tool} on {target}",
                            description=f"Raised by agent-troubleshoot for task: {request.task}",
                        )
                        sse_events.append({
                            "type": "config_change_jira",
                            "ticket_id": ticket.get("ticket_id"),
                            "url": ticket.get("url"),
                            "tool": tool,
                            "target": target,
                        })
                    except Exception as exc:
                        logger.warning("Jira ticket creation failed: %s", exc)

                # Write step_up_request and terminate stream
                step_up_doc = await write_step_up_request(
                    container=step_up_container,
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    user_identity=x_user_claims,
                    tool_name=tool,
                    context={"tool": tool, "target": target, "params": params},
                )
                sse_events.append({
                    "type": "probe_warning",
                    "tool": tool,
                    "target": target,
                    "tier": tier,
                    "reason": f"{tier}-tier tool requires approval",
                    "request_id": step_up_doc["id"],
                })
                # Write audit log for interrupted stream (budget_deducted=False)
                await audit_mod.write_probe_audit(
                    container=audit_container,
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    tool=tool, target=target, tier=tier,
                    outcome="interrupted", duration_ms=0, tokens_used=selector_tokens,
                    step_up_request_id=step_up_doc["id"],
                    emergency=request.emergency,
                )
                return TroubleshootResponse(
                    findings=findings,
                    interrupted=True,
                    step_up_request_id=step_up_doc["id"],
                    sse_events=sse_events,
                )

        # Dispatch to probe
        sse_events.append({"type": "probe_start", "tool": tool, "target": target, "tier": tier})
        start = time.monotonic()
        try:
            result = await call_probe(probe_url, tool, target, params, _PROBE_TIMEOUT)
            duration_ms = int((time.monotonic() - start) * 1000)
            summary = str(result.get("parsed") or result.get("stdout", ""))[:200]
            sse_events.append({"type": "probe_complete", "tool": tool, "target": target,
                                "duration_ms": duration_ms, "summary": summary})
            findings.append({"tool": tool, "target": target, "result": result})
            await audit_mod.write_probe_audit(
                container=audit_container,
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                tool=tool, target=target, tier=tier,
                outcome="success", duration_ms=duration_ms, tokens_used=0,
                step_up_request_id=None, emergency=request.emergency,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            sse_events.append({"type": "probe_error", "tool": tool, "target": target, "error": str(exc)})
            await audit_mod.write_probe_audit(
                container=audit_container,
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                tool=tool, target=target, tier=tier,
                outcome="error", duration_ms=duration_ms, tokens_used=0,
                step_up_request_id=None, emergency=request.emergency,
            )

    sse_events.append({"type": "done", "tokens_used": selector_tokens, "session_id": request.session_id})
    return TroubleshootResponse(findings=findings, interrupted=False, sse_events=sse_events)
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/ -v
```
Expected: all `PASSED`

- [ ] **Step 7: Commit**

```bash
git add services/agent-troubleshoot/step_up_client.py services/agent-troubleshoot/tool_selector.py services/agent-troubleshoot/main.py services/agent-troubleshoot/tests/
git commit -m "feat(agent-troubleshoot): add POST /troubleshoot with three-tier approval, Jira, real token counts"
```

---

## Task 10: Coordinator tool registration (troubleshoot_agent)

**Files:**
- Create: `services/coordinator/tools/troubleshoot.py`
- Modify: `services/coordinator/agent_loop.py` — add troubleshoot_agent URL

- [ ] **Step 1: Write failing test**

```python
# Add to services/coordinator/tests/test_tools.py
def test_troubleshoot_tool_has_required_fields():
    from tools.troubleshoot import TROUBLESHOOT_TOOL
    assert TROUBLESHOOT_TOOL["name"] == "troubleshoot_agent"
    props = TROUBLESHOOT_TOOL["input_schema"]["properties"]
    assert "task" in props
    assert "targets" in props
    required = TROUBLESHOOT_TOOL["input_schema"]["required"]
    assert "task" in required
    assert "targets" in required
    assert "emergency" not in required  # optional, default false


def test_get_agent_url_troubleshoot_agent():
    import os
    from unittest.mock import patch
    with patch.dict(os.environ, {"TROUBLESHOOT_AGENT_URL": "http://vigil-agent-troubleshoot"}):
        from agent_loop import _get_agent_url
        assert _get_agent_url("troubleshoot_agent") == "http://vigil-agent-troubleshoot"
```

- [ ] **Step 2: Write troubleshoot.py**

```python
# services/coordinator/tools/troubleshoot.py
TROUBLESHOOT_TOOL = {
    "name": "troubleshoot_agent",
    "description": (
        "Active network troubleshooting using diagnostic tools (dig, curl, nmap, scapy, SSH, "
        "vendor firewall APIs for Palo Alto, Cisco ASA/FTD, Meraki, Fortinet, and others). "
        "Use when the user asks to diagnose, test connectivity, trace a path, check a firewall "
        "rule, or investigate a network fault. Some tools require step-up approval — the agent "
        "enforces this automatically based on tool risk tier."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Natural language description of what to diagnose or check.",
            },
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IP addresses, hostnames, or device identifiers to investigate.",
            },
            "tools_hint": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: constrain tool selection e.g. ['nmap', 'dig'].",
            },
            "emergency": {
                "type": "boolean",
                "description": "If true, skip Jira ticket for config-tier changes. Requires emergency_change SAML role (server-side verified). Omit unless explicitly needed.",
            },
            "vendor": {
                "type": "string",
                "description": "Firewall vendor for API-based tools: palo_alto, cisco_asa, cisco_meraki, fortinet.",
            },
            "step_up_grant_id": {
                "type": "string",
                "description": "Populate on re-submission after step-up approval was granted.",
            },
            "step_up_request_id": {
                "type": "string",
                "description": "Populate on re-submission together with step_up_grant_id to cross-verify the grant.",
            },
        },
        "required": ["task", "targets"],
    },
}
```

- [ ] **Step 3: Add troubleshoot_agent to agent_loop.py**

```python
# In services/coordinator/agent_loop.py _get_agent_url, add:
"troubleshoot_agent": os.getenv("TROUBLESHOOT_AGENT_URL", ""),
```

- [ ] **Step 4: Run coordinator tests**

```bash
cd services/coordinator
pytest tests/ -v
```
Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/coordinator/tools/troubleshoot.py services/coordinator/agent_loop.py services/coordinator/tests/test_tools.py
git commit -m "feat(coordinator): register troubleshoot_agent tool + agent_loop URL mapping"
```

---

## Task 11: Gateway budget reconciliation task

**Files:**
- Create: `services/gateway/gateway_reconcile.py`
- Modify: `services/gateway/main.py` — add background reconciliation task

The Gateway already has a Cosmos client. Add a background task that:
1. Enumerates tenants from `tenant_config` container (avoids cross-partition `DISTINCT` query on `audit_logs`)
2. Scans `audit_logs` per-tenant for `budget_deducted=false AND tokens_used>0`
3. Deducts from `tenant_config` budget using ETag optimistic concurrency (prevents double-deduction on concurrent runs)
4. Marks each entry `budget_deducted=true` using ETag concurrency
5. Runs every 5 minutes.

- [ ] **Step 1: Read the gateway's current main.py to understand existing patterns**

Read `services/gateway/main.py` before making any modifications.

- [ ] **Step 2: Write failing test**

```python
# services/gateway/tests/test_reconciliation.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_reconcile_deducts_tokens_and_marks_deducted():
    from gateway_reconcile import reconcile_tenant_budget

    mock_audit = AsyncMock()
    mock_budget = AsyncMock()

    # Simulate one undeducted entry
    undeducted = {
        "id": "audit-1", "tenant_id": "tenant-a",
        "tokens_used": 50, "budget_deducted": False,
        "_etag": '"abc123"',
    }

    async def fake_query(*args, **kwargs):
        yield undeducted

    mock_audit.query_items = fake_query

    tenant_doc = {"id": "tenant-a", "tenant_id": "tenant-a", "tokens_used_today": 100, "_etag": '"def456"'}
    mock_budget.read_item = AsyncMock(return_value=tenant_doc)
    mock_budget.replace_item = AsyncMock(return_value={})
    mock_audit.replace_item = AsyncMock(return_value={})

    await reconcile_tenant_budget("tenant-a", mock_audit, mock_budget)

    # Budget should be incremented
    updated_budget = mock_budget.replace_item.call_args[1]["body"]
    assert updated_budget["tokens_used_today"] == 150

    # Budget replace must use ETag for optimistic concurrency
    budget_etag = mock_budget.replace_item.call_args[1]["if_match_etag"]
    assert budget_etag == '"def456"'

    # Audit entry should be marked deducted
    updated_audit = mock_audit.replace_item.call_args[1]["body"]
    assert updated_audit["budget_deducted"] is True

    # Audit replace must also use ETag
    audit_etag = mock_audit.replace_item.call_args[1]["if_match_etag"]
    assert audit_etag == '"abc123"'
```

- [ ] **Step 3: Run to confirm failure**

```bash
cd services/gateway
pytest tests/test_reconciliation.py -v
```
Expected: `ImportError: No module named 'gateway_reconcile'`

- [ ] **Step 4: Create gateway_reconcile.py**

```python
# services/gateway/gateway_reconcile.py
"""Budget reconciliation for interrupted step-up streams.

Scans audit_logs per-tenant for entries where budget_deducted=False and tokens_used>0.
For each, deducts tokens from the tenant budget document and marks the entry deducted.
Uses ETag-based optimistic concurrency to prevent double-deduction on concurrent runs.
"""
import logging

logger = logging.getLogger(__name__)


async def reconcile_tenant_budget(tenant_id: str, audit_container, budget_container) -> None:
    """Reconcile one tenant's undeducted audit entries."""
    undeducted = [
        item async for item in audit_container.query_items(
            query=(
                "SELECT * FROM c WHERE c.tenant_id=@tid "
                "AND c.budget_deducted=false AND c.tokens_used>0"
            ),
            parameters=[{"name": "@tid", "value": tenant_id}],
            partition_key=tenant_id,
        )
    ]
    if not undeducted:
        return

    total_tokens = sum(e["tokens_used"] for e in undeducted)

    try:
        tenant_doc = await budget_container.read_item(item=tenant_id, partition_key=tenant_id)
        tenant_doc["tokens_used_today"] = tenant_doc.get("tokens_used_today", 0) + total_tokens
        await budget_container.replace_item(
            item=tenant_id,
            body=tenant_doc,
            partition_key=tenant_id,
            if_match_etag=tenant_doc.get("_etag"),
        )
    except Exception as exc:
        logger.warning("Budget deduction failed for tenant %s: %s", tenant_id, exc)
        return  # Do not mark entries as deducted if budget update failed

    for entry in undeducted:
        try:
            entry["budget_deducted"] = True
            await audit_container.replace_item(
                item=entry["id"],
                body=entry,
                partition_key=tenant_id,
                if_match_etag=entry.get("_etag"),
            )
        except Exception as exc:
            logger.warning("Failed to mark audit entry %s as deducted: %s", entry["id"], exc)
```

- [ ] **Step 5: Run test to confirm pass**

```bash
pytest tests/test_reconciliation.py -v
```
Expected: `PASSED`

- [ ] **Step 6: Wire reconciliation into gateway startup**

Read `services/gateway/main.py` first. Then add the reconciliation loop. The loop enumerates tenants from `tenant_config` (not a cross-partition `DISTINCT` query on `audit_logs`):

```python
# In services/gateway/main.py — add background loop and call in startup:
async def _run_budget_reconciliation_loop():
    from gateway_reconcile import reconcile_tenant_budget
    while True:
        try:
            db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
            audit_container = db.get_container_client("audit_logs")
            budget_container = db.get_container_client("tenant_config")
            # Enumerate known tenants from tenant_config — avoids cross-partition query
            tenants = [
                item["id"] async for item in budget_container.read_all_items()
            ]
            for tenant_id in tenants:
                await reconcile_tenant_budget(tenant_id, audit_container, budget_container)
        except Exception as exc:
            logger.warning("Reconciliation loop error", exc_info=exc)
        await asyncio.sleep(300)  # 5 minutes

# In startup():
asyncio.ensure_future(_run_budget_reconciliation_loop())
```

- [ ] **Step 7: Run gateway tests**

```bash
cd services/gateway
pytest tests/ -v
```
Expected: all `PASSED` (existing tests still pass; new reconciliation test passes)

- [ ] **Step 8: Commit**

```bash
git add services/gateway/gateway_reconcile.py services/gateway/main.py services/gateway/tests/test_reconciliation.py
git commit -m "feat(gateway): add budget reconciliation task for interrupted step-up streams"
```

---

## Task 12: Dockerfiles + deploy workflows

**Files:**
- Create: `services/agent-troubleshoot/Dockerfile`
- Create: `.github/workflows/deploy-agent-troubleshoot.yml`
- Create: `.github/workflows/deploy-agent-probe.yml`

- [ ] **Step 1: Write agent-troubleshoot Dockerfile**

```dockerfile
# services/agent-troubleshoot/Dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Verify agent-probe Dockerfile from Task 5**

```bash
cat services/agent-probe/Dockerfile
```

- [ ] **Step 3: Write deploy-agent-troubleshoot.yml**

```yaml
# .github/workflows/deploy-agent-troubleshoot.yml
name: Deploy Agent Troubleshoot

on:
  push:
    branches: [main]
    paths:
      - 'services/agent-troubleshoot/**'

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
          docker build -t ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-agent-troubleshoot:${{ github.sha }} services/agent-troubleshoot/
          docker push ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-agent-troubleshoot:${{ github.sha }}

      - name: Deploy to Container Apps
        run: |
          az containerapp update \
            --name vigil-agent-troubleshoot \
            --resource-group rg-vigil-prod \
            --image ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-agent-troubleshoot:${{ github.sha }}
```

- [ ] **Step 4: Write deploy-agent-probe.yml**

```yaml
# .github/workflows/deploy-agent-probe.yml
name: Deploy Agent Probe

on:
  push:
    branches: [main]
    paths:
      - 'services/agent-probe/**'

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
          docker build -t ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-agent-probe:${{ github.sha }} services/agent-probe/
          docker push ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-agent-probe:${{ github.sha }}

      - name: Deploy to Container Apps
        run: |
          az containerapp update \
            --name vigil-agent-probe \
            --resource-group rg-vigil-prod \
            --image ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-agent-probe:${{ github.sha }}
```

- [ ] **Step 5: Commit**

```bash
git add services/agent-troubleshoot/Dockerfile .github/workflows/deploy-agent-troubleshoot.yml .github/workflows/deploy-agent-probe.yml
git commit -m "feat(agent-troubleshoot, agent-probe): add Dockerfiles and deploy workflows"
```

---

## Task 13: Full test suite + ARCHITECTURE.md

**Files:**
- Modify: `ARCHITECTURE.md`

- [ ] **Step 1: Run all service test suites**

```bash
cd services/agent-probe && pytest tests/ -v
cd services/agent-troubleshoot && pytest tests/ -v
cd services/coordinator && pytest tests/ -v
cd services/gateway && pytest tests/ -v
```
Expected: all `PASSED`. Fix any failures before proceeding.

- [ ] **Step 2: Update ARCHITECTURE.md Component Reference**

Add after Design Agent:

```markdown
### Troubleshooting Agent
- **Type:** Containerised (FastAPI + Claude Sonnet 4.6)
- **Service:** `services/agent-troubleshoot`
- **Responsibilities:**
  - Accept diagnostic tasks from the coordinator
  - Use an internal Claude call (via `asyncio.to_thread`) to select appropriate tools (dig, curl, nmap, scapy, SSH, vendor firewall APIs)
  - Classify each tool invocation into a risk tier (show / invasive / config) — not overridable by user
  - Dispatch show-tier calls immediately to `agent-probe` or vendor connectors
  - Write `step_up_requests` and terminate stream for invasive/config-tier (request-stateless approval gate)
  - For config-tier: call `agent-itsm` to raise a Jira ticket (skipped when `emergency=true` + `emergency_change` SAML role, server-side verified via `json.loads`)
  - Validate step-up grants on re-submission (cross-checks `step_up_request_id` to prevent grant reuse)
  - Write `audit_logs` entry for every probe invocation including `tokens_used` and `budget_deducted`
  - Fetch vendor credentials JIT from Key Vault per request (per-tenant, not cached)
- **Vendor connectors:** Palo Alto PAN-OS, Cisco ASA/FTD, Meraki Dashboard API, Fortinet FortiGate (extensible plugin registry)
- **Never called directly by users** — coordinator only

### Network Probe Container
- **Type:** Containerised (FastAPI, Dedicated workload profile)
- **Service:** `services/agent-probe`
- **Linux capabilities:** `NET_ADMIN`, `NET_RAW` (required for scapy raw sockets and nmap SYN scans)
- **Responsibilities:**
  - Pure execution engine — no tenant awareness, no business logic
  - Execute diagnostic tools (nmap, scapy, dig, curl, ssh) against a single target per call
  - Return structured JSON output to `agent-troubleshoot`
  - 20-second default timeout (`PROBE_TIMEOUT_SECONDS` env var)
- **Internal only:** VNet-locked — only reachable from `agent-troubleshoot`
- **Never called directly by users or coordinator** — agent-troubleshoot only
```

In Cosmos DB section, add:
```
- `audit_logs` entries gain two new fields: `tokens_used` (int) and `budget_deducted` (bool) — used by Gateway reconciliation task for interrupted stream budget accounting. Reconciliation enumerates tenants via `tenant_config` (not cross-partition query).
```

In Environment Configuration, add under Coordinator:
```
TROUBLESHOOT_AGENT_URL  # Internal URL of Troubleshoot Agent
```

Add env var sections:
```
**Troubleshoot Agent**
COSMOS_ENDPOINT         # Azure Cosmos DB endpoint
COSMOS_DATABASE         # Database name
AZURE_FOUNDRY_ENDPOINT  # Azure AI Foundry endpoint (for tool selection Claude call)
AZURE_FOUNDRY_MODEL     # Model deployment name (claude-sonnet-4-6)
PROBE_URL               # Internal URL of agent-probe
PROBE_TIMEOUT_SECONDS   # Default: 20 — probe kills process after this duration
ITSM_AGENT_URL          # Internal URL of agent-itsm (for Jira ticket creation on config-tier)
KEY_VAULT_URL           # For JIT vendor credential fetching
```

Add infrastructure acceptance test note:
```
**agent-probe ingress verification (infrastructure acceptance test — not pytest):**
az containerapp show --name vigil-agent-probe --resource-group rg-vigil-prod | jq '.properties.configuration.ingress.external'
→ Must return false. Also confirmed via Terraform plan: ingress.external_enabled = false.
```

- [ ] **Step 3: Final commit**

```bash
git add ARCHITECTURE.md
git commit -m "docs(architecture): add Troubleshoot Agent, Probe container, and audit_log schema updates"
```
