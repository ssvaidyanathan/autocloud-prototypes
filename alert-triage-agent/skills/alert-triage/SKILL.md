---
name: alert-triage
description: Multi-factor correlation scoring, topology traversal, pre-analysis locking, duplicate check pipelines, and incident grouping for Google Cloud alert triage.
---

# Alert Triage Skill

> Resource names below use the default prefix `ata`. A stack deployed with another `NAME_PREFIX` uses `<prefix>-` instead of `ata-` (and `/<prefix>/...` base paths).

## Overview
This skill defines operational procedures for ingesting, deduplicating, correlating, and grouping Cloud Monitoring alert events. It specifies exact shell pipeline construction for duplicate detection across incident store records, mathematical scoring for multi-factor alert correlation, and dependency graph traversal for blast radius mapping.

---

## 1. Duplicate Detection & Pre-Analysis Locking

### Verbatim Duplicate Check Instruction
Read Active Incidents: Execute a multi-grep pipeline across the directory to check if a previous session has logged an identical incident with an active status. You can use standard Linux utilities. When constructing grep pipelines with these variables, ensure you: quote policy_name and resource_name and escape internal quotes for the shell (e.g., replacing ' with '\'' in single-quoted strings); use grep -F (fixed strings) so regex meta-characters in variables are matched literally; use grep -Z (null-terminated output) and xargs -0 to safely handle filenames containing spaces; use xargs -r (--no-run-if-empty) so the pipeline does not fail if a grep finds no files; Check for Stale Locks: verify that the timestamp in the frontmatter of any matching file is recent (e.g., within the last 30 minutes). If the timestamp is older, treat the file as a stale lock from a crashed previous session and ignore it to prevent indefinite alert suppression.

### Standard Multi-Grep Pipeline Execution
Execute the pipeline via `run_incident_search`:
```bash
grep -FlZ 'policy_name: "<policy_name>"' /knowledge/incident-manager/*.md 2>/dev/null \
  | xargs -r0 grep -FlZ 'resource_name: "<resource_name>"' \
  | xargs -r0 grep -l 'status: mitigation-in-progress'
```

### Match Evaluation Flow
1. **Active Non-Stale Match Found (Timestamp $\le 30$ minutes):**
   - Classify as active duplicate processing (`is_duplicate: true`).
   - Refresh the `updated_at` timestamp in the file frontmatter and append the `incident_id`.
   - Dispatch update to bridge to append a work note to the existing ServiceNow ticket.
   - Halt further duplicate investigation.
2. **Stale Match Found (Timestamp $> 30$ minutes):**
   - Treat matching file as a stale lock from a crashed or interrupted session.
   - Bypass the match to prevent indefinite alert suppression.
   - Log a warning in investigation trace and proceed to create a new lock file.
3. **No Active Entry Found:**
   - Immediately create an initial incident lock file (`incident-<slug>.md`) with `status: mitigation-in-progress`.
   - Proceed to multi-factor correlation evaluation.

---

## 2. Multi-Factor Correlation Scoring Engine

For alerts that are not identical duplicates, calculate correlation score against candidate open incident groups $G$ created within the last 30 minutes:

$$\text{Score}(A, G) = W_{\text{topo}} \cdot S_{\text{topo}} + W_{\text{causal}} \cdot S_{\text{causal}} + W_{\text{time}} \cdot S_{\text{time}} + W_{\text{change}} \cdot S_{\text{change}} + W_{\text{label}} \cdot S_{\text{label}}$$

### Weights & Factor Rules
| Factor | Weight ($W_i$) | Range ($S_i$) | Scoring Function & Rules |
| :--- | :---: | :---: | :--- |
| **Topology Proximity** ($S_{\text{topo}}$) | **35** | `0.0 – 1.0` | Evaluated against closest resource node $R \in G$ in DAG:<br/>• **0 hops** (Exact same resource): $S_{\text{topo}} = 1.0$<br/>• **1 hop** (Direct neighbor: proxy $\leftrightarrow$ service, service $\leftrightarrow$ service, service $\leftrightarrow$ DB): $S_{\text{topo}} = 0.8$<br/>• **2 hops** (Transitive neighbor: proxy $\leftrightarrow$ downstream service, caller service $\leftrightarrow$ DB): $S_{\text{topo}} = 0.5$<br/>• **$\ge 3$ hops or Disjoint**: $S_{\text{topo}} = 0.0$ |
| **Causal Failure Chain** ($S_{\text{causal}}$) | **30** | `0.0 – 1.0` | Evaluates metric anomaly compatibility across architecture layers:<br/>• **Direct Propagation Chain** ($S_{\text{causal}} = 1.0$):<br/>  - DB Connection Saturation / Lock Latency $\rightarrow$ Backend 5xx / Latency $\rightarrow$ Apigee 5xx / 504<br/>  - Cloud Run OOM / Container Crash $\rightarrow$ Backend 503 $\rightarrow$ Apigee 502/503<br/>  - Backend Latency $\rightarrow$ Apigee 504 Gateway Timeout<br/>• **Plausible Correlation** ($S_{\text{causal}} = 0.5$): General 5xx across multiple microservices.<br/>• **Incompatible Failure Classes** ($S_{\text{causal}} = 0.0$):<br/>  - Client 429 SpikeArrest / Quota Throttling vs DB Connection Saturation<br/>  - Client 401/403 API Key Auth Failure vs Backend 5xx |
| **Temporal Proximity & Decay** ($S_{\text{time}}$) | **20** | `0.0 – 1.0` | Let $\Delta t = \|t_{\text{alert\_onset}} - t_{\text{group\_anchor}}\|$ in minutes with correlation horizon $H = 10.0\text{ min}$:<br/>$$S_{\text{time}} = \max\left(0.0, 1.0 - \frac{\Delta t}{H}\right)$$<br/>Alerts with $\Delta t > 10\text{ min}$ receive $S_{\text{time}} = 0.0$. |
| **Shared Recent Change** ($S_{\text{change}}$) | **10** | `0.0 – 1.0` | Evaluates revision deployments within $[-15\text{ min}, +5\text{ min}]$ of alert onset:<br/>• **Identical Deployment**: Alert resource was part of the same deployment event / release as resources in $G$: $S_{\text{change}} = 1.0$<br/>• **No Shared Change**: Different changes or no recent deployments: $S_{\text{change}} = 0.0$. |
| **Application Label Bonus** ($S_{\text{label}}$) | **5** | `0.0 – 1.0` | User label alignment: $S_{\text{label}} = 1.0$ if alert metadata matches `app == "ata"` or shares an explicit service tier label with $G$; otherwise $0.0$. |

### Decision Thresholds
- **Auto-Attach ($\ge 65$ Points):** Strong topological proximity and compatible causal failure chain. Alert automatically joins group $G$. Update blast radius and reset settle timer (+120s).
- **Auto-New Group ($< 35$ Points):** Weak topology, disparate timing, or incompatible failure classes (e.g., concurrent SpikeArrest 429 on orders while inventory DB experiences lock contention). Create distinct new incident group.
- **LLM Gray-Zone Resolution ($35 \le \text{Score} < 65$ Points):** Invoke LLM with structured evidence prompt (alert metadata, group timeline, topology path, and recent deployment logs). Record decision (`attach` vs `split`) and rationale in the incident record.

---

## 3. Topology Traversal & Blast Radius Mapping

### Application DAG Definition
- `ata-apigee-orders-v1` $\rightarrow$ `ata-orders-api`
- `ata-apigee-inventory-v1` $\rightarrow$ `ata-inventory-api`
- `ata-orders-api` $\rightarrow$ `ata-inventory-api`
- `ata-inventory-api` $\rightarrow$ `ata-inventory-db`

### Probable Origin Identification
1. Find all alert members in group $G$ whose onset timestamp is within 60 seconds of the earliest alert in the group.
2. Select the candidate with the deepest upstream position in the dependency DAG (Database $\rightarrow$ Dependency Service $\rightarrow$ Caller Service $\rightarrow$ Ingress Proxy).

### Blast Radius Calculation
Traverse reverse edges (callee $\rightarrow$ caller) forward from the probable origin to determine all reachable downstream microservices and proxies, recording exact hop distances.
