---
name: topology
description: Explains TOPOLOGY.md single-source-of-truth format, dependency specification, and how to adapt topology to another application without code changes.
---

# Application Topology Skill

> Resource names below use the default prefix `ata`. A stack deployed with another `NAME_PREFIX` uses `<prefix>-` instead of `ata-` (and `/<prefix>/...` base paths).

## Overview

The Alert Triage Agent relies on `TOPOLOGY.md` as its single source of truth for the application dependency graph. No service names, proxy names, or dependency edges are hardcoded in agent or bridge Python code.

The agent reads `TOPOLOGY.md` at startup to:
1. Normalize raw Cloud Monitoring alert resource names and labels to canonical component identifiers.
2. Calculate blast radius (which upstream consumers are affected if a backend component fails).
3. Trace upstream dependency chains to identify probable root cause origins.
4. Compute hop distances between anomalous resources during multi-alert incident grouping.
5. Provide the `get_topology` tool for incident investigation.

---

## The TOPOLOGY.md Format

A valid `TOPOLOGY.md` file contains two markdown tables:

### 1. `## Components` Table

Defines every component that can raise an alert or be affected by an incident.

| Component | Type | Also known as (alert labels) | Notes |
|---|---|---|---|
| ata-orders-v1 | Apigee proxy | ata-apigee-orders-v1, orders-v1 | Public orders API (`/ata/orders/v1`) |
| ata-inventory-v1 | Apigee proxy | ata-apigee-inventory-v1, inventory-v1 | Public inventory API (`/ata/inventory/v1`) |
| ata-orders-api | Cloud Run | orders-api | Places orders and reserves stock |
| ata-inventory-api | Cloud Run | inventory-api | Stock levels and reservations |
| ata-inventory-db | Cloud SQL | inventory-db | PostgreSQL inventory database |

#### Columns:
- **Component** (or **id**): Canonical identifier for the component.
- **Type**: Infrastructure tier type. Supported types:
  - `Apigee proxy` (or `apigee-proxy`)
  - `Cloud Run` (or `cloud-run`)
  - `Cloud SQL` (or `cloud-sql`)
- **Also known as (alert labels)** (or **aliases**): Comma-separated list of alternate names, revision base names, or monitoring label values that reference this component.
- **Notes**: Human-readable description of the component's responsibility.

### 2. `## Dependencies` Table

Defines directed caller $\rightarrow$ callee edges between components.

| From | To | How |
|---|---|---|
| ata-orders-v1 | ata-orders-api | routes traffic to |
| ata-inventory-v1 | ata-inventory-api | routes traffic to |
| ata-orders-api | ata-inventory-api | calls |
| ata-inventory-api | ata-inventory-db | reads and writes |

#### Semantics:
- Read each row as **"From depends on To"**.
- `From` sends traffic to or relies upon `To`.
- If `To` fails or degrades, `From` is in the blast radius and experiences downstream impact.

---

## Validation Rules (Fails Loudly)

The topology loader parses and validates `TOPOLOGY.md` upon initialization. Any of the following conditions raises a `TopologyValidationError` and halts startup:
1. **Duplicate IDs:** Each component ID in `## Components` must be unique.
2. **Unknown Edge Endpoints:** Every `From` and `To` entry in `## Dependencies` must reference a valid declared component.
3. **No Cycles:** Dependencies must form a Directed Acyclic Graph (DAG). Cycles are rejected.
4. **Known Types:** Component types must match the supported types (`Apigee proxy`, `Cloud Run`, `Cloud SQL`).

---

## Adapting to Another Application

To adapt the triage agent to a new application, **no Python code changes are required**:

1. **Edit `TOPOLOGY.md`:**
   - Update the `## Components` table with the new application services, proxies, and databases.
   - List any label aliases observed in Cloud Monitoring payloads (such as `proxy_name`, `service_name`, or `database_id`).
   - Update the `## Dependencies` table to reflect the architectural call graph.
2. **Configure Runtime Path (Optional):**
   - By default, the packaged `TOPOLOGY.md` inside the container is loaded.
   - Set the `TOPOLOGY_FILE` environment variable to point to an external or mounted topology file (e.g., `TOPOLOGY_FILE=/etc/config/TOPOLOGY.md`).
3. **Deploy the Agent:**
   - The agent automatically loads, validates, and builds traversal DAGs from the new file at startup.
