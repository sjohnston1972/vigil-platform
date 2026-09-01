"""
Claude tool definitions — one per specialist agent, following the "Coordinator Tool
Registration Pattern" documented in CLAUDE.md. Passed to every /chat/stream Claude
call in agent_loop.py.

Tool `name` values must match the keys in `agent_loop._get_agent_url()` and any entry
in a tenant's `step_up_policy` (services/coordinator/step_up.py) — that's what decides
whether a given tool call is gated (serial, human-approved) or non-gated (parallel).
"""

NETWORK_AGENT_TOOL = {
    "name": "network_agent",
    "description": (
        "Connects to network devices and retrieves configuration, interface status, "
        "routing tables, and ACLs. Use when the user asks about device configuration, "
        "connectivity, or network state."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "device_host": {
                "type": "string",
                "description": "IP address or hostname of the target device",
            },
            "query_type": {
                "type": "string",
                "enum": ["running_config", "interfaces", "routing_table", "acl"],
                "description": "Type of information to retrieve",
            },
        },
        "required": ["device_host", "query_type"],
    },
}

RAG_AGENT_TOOL = {
    "name": "rag_agent",
    "description": (
        "Queries the VIGIL knowledge base (Azure AI Search) for compliance checks, "
        "best-practice guidance, and policy validation grounded in indexed "
        "documentation. Use when the user asks whether something complies with a "
        "standard or best practice."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language question to answer from the knowledge base",
            },
        },
        "required": ["query"],
    },
}

ITSM_AGENT_TOOL = {
    "name": "itsm_agent",
    "description": (
        "Creates, queries, and updates Jira tickets. Use when the user asks to raise "
        "a ticket for a finding, check ticket status, or update an existing ticket."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "query", "update"],
                "description": "Which ITSM operation to perform",
            },
            "summary": {
                "type": "string",
                "description": "Ticket summary — required for action=create",
            },
            "ticket_id": {
                "type": "string",
                "description": "Existing ticket ID — required for action=query/update",
            },
        },
        "required": ["action"],
    },
}

ENRICHMENT_AGENT_TOOL = {
    "name": "enrichment_agent",
    "description": (
        "Looks up CVE details, Cisco EoX lifecycle status, and (optionally) Shodan "
        "exposure data for a device or software version. Use when the user asks "
        "about vulnerabilities, end-of-life status, or external exposure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "product_id": {
                "type": "string",
                "description": "Cisco product ID, software version, or CVE identifier to enrich",
            },
            "device_host": {
                "type": "string",
                "description": "IP address or hostname of the device being enriched, if applicable",
            },
        },
        "required": ["product_id"],
    },
}

APPLY_CHANGE_TOOL = {
    "name": "apply_change",
    "description": (
        "Applies a previously proposed and reviewed network change. Step-up gated — "
        "requires human approval before dispatch. Use only after propose_change and "
        "change review have produced an approved change_id."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "change_id": {
                "type": "string",
                "description": "ID of the approved change_records document to apply",
            },
        },
        "required": ["change_id"],
    },
}

ROLLBACK_CHANGE_TOOL = {
    "name": "rollback_change",
    "description": (
        "Rolls back a previously applied network change to its pre-change "
        "configuration. Step-up gated — requires human approval before dispatch."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "change_id": {
                "type": "string",
                "description": "ID of the applied change_records document to roll back",
            },
        },
        "required": ["change_id"],
    },
}

TOOL_DEFINITIONS = [
    NETWORK_AGENT_TOOL,
    RAG_AGENT_TOOL,
    ITSM_AGENT_TOOL,
    ENRICHMENT_AGENT_TOOL,
    APPLY_CHANGE_TOOL,
    ROLLBACK_CHANGE_TOOL,
]
