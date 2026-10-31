# Standalone Deployment Context MCP Protocol Contract

**Version:** 1.0.0  
**Transport:** Streamable HTTP (`POST /mcp`, `GET /health`)  
**Protocol Standard:** Model Context Protocol (FastMCP / MCP JSON-RPC 2.0)

---

## 1. Overview & Protocol Specification

The **Deployment Context MCP Server** provides an agent-agnostic, read-only interface for resolving alert target indicators (IP addresses, monitoring check identifiers, service hints) to service topologies and correlating incidents with recent deployments, releases, and workload scaling actions.

Consuming agents interact with this service strictly over Streamable HTTP using standard MCP JSON-RPC 2.0 protocol tooling (`tools/list` and `tools/call`).

---

## 2. Endpoints & Transport

| Endpoint | Method | Protocol | Description | Auth Required |
| :--- | :--- | :--- | :--- | :--- |
| `/mcp` | `POST` | MCP JSON-RPC 2.0 (Streamable HTTP) | Primary MCP tool execution and discovery endpoint | Yes (Bearer ID token) |
| `/health` | `GET` | HTTP / JSON | Service liveness and container health check probe | No |

### Request Headers for `/mcp`
- `Content-Type: application/json`
- `Accept: application/json, text/event-stream`
- `Authorization: Bearer <ID_TOKEN>` (Google OIDC token when authentication is enabled)

---

## 3. Tool Specifications

### 3.1. `lookup_alert_context`

Resolves ambiguous or opaque alert target indicators to canonical service topology, workload metadata, and team ownership.

#### Input Schema
```json
{
  "type": "object",
  "properties": {
    "target_ip": {
      "type": "string",
      "description": "Target IPv4 or IPv6 address associated with alert"
    },
    "check_id": {
      "type": "string",
      "description": "Monitoring or uptime check identifier"
    },
    "service_hint": {
      "type": "string",
      "description": "Service name hint from alert summary or description"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema (`LookupAlertContextResponse`)
```json
{
  "type": "object",
  "required": ["found"],
  "properties": {
    "found": {
      "type": "boolean",
      "description": "Whether alert context was resolved"
    },
    "context": {
      "type": ["object", "null"],
      "description": "Resolved alert context topology and ownership, or null if not found",
      "required": ["service_name", "cluster_name", "namespace", "workload_name", "owner_team"],
      "properties": {
        "service_name": {"type": "string"},
        "cluster_name": {"type": "string"},
        "cluster": {"type": "string"},
        "namespace": {"type": "string"},
        "workload_name": {"type": "string"},
        "workload": {"type": "string"},
        "owner_team": {"type": "string"},
        "environment": {"type": "string"},
        "platform": {"type": "string"},
        "workload_type": {"type": "string"},
        "release": {"type": "string"},
        "target_ip": {"type": "string"},
        "port": {"type": "integer"},
        "check_id": {"type": "string"},
        "service_hint": {"type": "string"},
        "repo_url": {"type": "string"},
        "runbook_url": {"type": "string"}
      }
    }
  }
}
```

#### Unmatched Key Semantics
When an indicator cannot be matched to an active context, the server returns a valid JSON-RPC result with `found: false` and `context: null` rather than an RPC fault:
```json
{
  "found": false,
  "context": null
}
```

---

### 3.2. `get_recent_deployments`

Fetches recent deployments, revisions, rollouts, or scaling actions for a specified workload or cluster.

#### Input Schema
```json
{
  "type": "object",
  "properties": {
    "workload": {
      "type": "string",
      "description": "Target workload name"
    },
    "workload_name": {
      "type": "string",
      "description": "Alias for workload"
    },
    "cluster": {
      "type": "string",
      "description": "Cluster identifier"
    },
    "cluster_name": {
      "type": "string",
      "description": "Alias for cluster"
    },
    "namespace": {
      "type": "string",
      "description": "Kubernetes namespace"
    },
    "start_time": {
      "type": "string",
      "description": "ISO-8601 start timestamp filter"
    },
    "end_time": {
      "type": "string",
      "description": "ISO-8601 end timestamp filter"
    },
    "limit": {
      "type": "integer",
      "description": "Maximum number of deployment events to return (default: 5, max: 20)",
      "default": 5,
      "minimum": 1,
      "maximum": 20
    }
  },
  "additionalProperties": false
}
```

#### Output Schema (`GetRecentDeploymentsResponse`)
```json
{
  "type": "object",
  "required": ["deployments", "total_count"],
  "properties": {
    "deployments": {
      "type": "array",
      "description": "List of recent deployment and change events (ordered newest first)",
      "items": {
        "type": "object",
        "required": ["event_id", "timestamp", "status"],
        "properties": {
          "event_id": {"type": "string"},
          "timestamp": {"type": "string"},
          "status": {"type": "string"},
          "service_name": {"type": "string"},
          "workload_name": {"type": "string"},
          "workload": {"type": "string"},
          "environment": {"type": "string"},
          "cluster_name": {"type": "string"},
          "cluster": {"type": "string"},
          "namespace": {"type": "string"},
          "deployment_tool": {"type": "string"},
          "tool": {"type": "string"},
          "author": {"type": "string"},
          "deployer": {"type": "string"},
          "revision": {"type": "string"},
          "release": {"type": "string"},
          "image": {"type": "string"},
          "container_images": {
            "type": "array",
            "items": {"type": "string"}
          },
          "pipeline_id": {"type": ["string", "null"]},
          "git_commit": {"type": ["string", "null"]},
          "git_commit_message": {"type": ["string", "null"]},
          "change_summary": {"type": "string"}
        }
      }
    },
    "total_count": {
      "type": "integer",
      "description": "Total count of matching events before pagination limit"
    }
  }
}
```

#### Unmatched Query Semantics
Queries that yield no matching events return a typed empty response:
```json
{
  "deployments": [],
  "total_count": 0
}
```

---

## 4. Authentication Specification

### Google Cloud Run Deployment (Native OIDC)
- Incoming requests to `/mcp` must include an `Authorization: Bearer <ID_TOKEN>` header.
- The token is verified against Google's public OIDC keys (`google.oauth2.id_token.verify_oauth2_token`).
- The caller service account email extracted from the token's `email` claim must match one of the entries in `AUTHORIZED_CALLER_SERVICE_ACCOUNTS`.
- Status codes:
  - `401 Unauthorized`: Missing or invalid Bearer token.
  - `403 Forbidden`: Token is valid, but the caller identity is not in the authorized service account allowlist.

### Local Development / Evaluation Mode
- Set `AUTH_DISABLED=true` to bypass OIDC token verification for automated local test suites and offline evaluations.

### External Customer Implementations (Future Cutover)
- Candidate production servers outside Google Cloud (e.g., GitHub Actions, ArgoCD, ServiceNow, GitLab) implement an **OAuth2 Client Credentials Grant** returning standard Bearer tokens conforming to RFC 6749.

---

## 5. Schema Validation
All request and response payloads can be formally validated against the JSON Schema definitions located in `schemas/`:
- `schemas/lookup_alert_context_request.json`
- `schemas/lookup_alert_context_response.json`
- `schemas/get_recent_deployments_request.json`
- `schemas/get_recent_deployments_response.json`
