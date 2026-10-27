# ata-tools FastMCP Server

FastMCP read-only telemetry and topology service for Google Cloud Platform incident triage.

## Endpoints
- `GET /health`: Health probe endpoint.
- `POST /mcp`: JSON-RPC protocol endpoint for MCP tools (stateless, JSON responses). The agent calls `get_change_timeline` here with an ID token whose audience is the service URL (`TOOLS_URL` in the agent); see CONFIGURATION.md §1.3.1.

## Exposed Read-Only Tools
1. `list_open_alerts`: Queries Cloud Monitoring API `GET /v3/projects/{project}/alerts?filter=state="OPEN"`.
2. `get_topology_graph`: Returns directed DAG nodes and caller -> callee edges.
3. `get_blast_radius`: Computes downstream reachable nodes and hop distances.
4. `get_apigee_metrics`: Queries Cloud Monitoring for proxy response counts and target latencies.
5. `get_apigee_logs`: Queries Cloud Logging log `projects/{project}/logs/<NAME_PREFIX>-apigee-access`.
6. `get_proxy_bundle`: In-memory unpacker and XML policy parser for Apigee revision bundles.
7. `get_change_timeline`: Queries recent Cloud Run service revisions and Apigee proxy deployments.
