# Phase 8.1 Architecture Design: Agentic Specialists & Skill Loading

## Problem & Goals
**Problem:** Currently, domain specialists (logs, metrics, changes, resources) construct `LlmAgent` instances but do not execute them. They rely on hardcoded Python queries instead. This prevents them from loading and utilizing their assigned domain skills dynamically, limits discovery, and violates the multi-agent design intent.

**Goals:** Make domain specialists genuinely agentic by executing them as `LlmAgent`s via the ADK Runner. Implement a two-pass investigation (fixed baseline sweep followed by agentic LLM drill-down). Ensure specialists dynamically load domain skills using ADK `SkillToolset` with progressive disclosure, run in parallel using `asyncio.gather`, track budget limits, and maintain strict guardrails.

## Non-Goals
- No integration of Firestore or Redis (in-memory deduplication only).
- No write/mutate tools; the SRE agent remains strictly read-only against GCP infra.
- No alteration or relaxation of existing guardrails (de-overfitting, root cause overrides, read-only guarantees).
- No dependency graph traversal or request-log tools (deferred to later phases).

## Proposed Design

### A. Concurrent Baseline Sweep & Skill-Led Investigation
Specialists will inherit from a common base that implements a two-pronged mechanism in `run_investigation()`:

1. **Skill-Led Investigation (LLM Pass)**
   - On Turn 1, the specialist invokes `load_skill()` for its assigned domain skill and applicable service runbook.
   - The agent strictly follows the diagnostic steps, query cookbook, and scoping rules defined in the skill playbook.
   - The specialist executes as an ADK `LlmAgent` via the ADK `Runner`.
   - **Inputs:** Triage hypotheses, available read-only tools, and domain skills are passed in the initial user prompt or via `model_input_context` in `RunConfig`.
   - The agent iteratively calls tools based on reasoning to deepen the investigation.
   - **Output:** Because ADK 2.11 `LlmAgent` supports both `tools` (or toolsets) AND an `output_schema` concurrently, the agent directly returns a structured `Findings` object after completing its tool loops.
2. **Baseline Completeness Sweep (Concurrent Safety Net)**
   - Runs concurrently with the skill-led LLM pass via `asyncio.gather`.
   - Automatically verifies and sweeps any missing standard baseline telemetry (CPU, memory, 5xx baseline comparison, deployment revisions).
   - **Performance:** It costs 0 LLM turns and adds no latency.
   - **Cache Reuse:** The shared in-memory query cache allows the skill-led agent to instantly reuse baseline queries rather than re-querying GCP.
   - **Merging:** At completion, findings are merged so the sweep fills any gaps not covered by the skill-led agent. It serves as the automated fallback floor if the LLM pass hits budget limits or errors.

### B. Skills vs Tools Disambiguation
- **Tools** are executable Python functions registered in `ToolRegistry` (e.g., `query_logs`, `compare_to_baseline`) that interact with external systems.
- **Skills** are instruction/knowledge markdown files loaded dynamically via `SkillToolset` to guide the LLM on *how* to use the tools or interpret data. Only existing skills from `/workspace/incident-sre-agent/agent/skills/skills.yaml` are used.

| Specialist | Assigned Tools (Python Functions) | Assigned Skills (Markdown in SkillToolset) |
|------------|-----------------------------------|--------------------------------------------|
| **Logs** | `query_logs`, `summarize_error_groups`, `list_log_names` | `cloud-logging-queries`, `service-<service_name> runbook (e.g. service-shop-api)` |
| **Metrics** | `query_time_series`, `compare_to_baseline`, `list_alert_policies`, `list_open_alerts` | `cloud-monitoring-metrics`, `service-<service_name> runbook (e.g. service-shop-api)` |
| **Changes** | `query_audit_logs`, `list_cloud_run_revisions`, `list_cloud_builds` | `change-correlation`, `service-<service_name> runbook (e.g. service-shop-api)` |
| **Resources**| `describe_cloud_run_service`, `describe_gke_clusters`, `describe_compute_instances`, `describe_cloud_sql_instances` | `cloud-run-troubleshooting`, `gke-troubleshooting`, `cloud-sql-troubleshooting`, `service-<service_name> runbook (e.g. service-shop-api)` |

*(Note: Triage uses `triage-playbook` and Synthesizer uses `report-style`.)*

Service runbooks are resolved dynamically at runtime based on the affected service identified during triage, ensuring no hardcoded service names exist in code or static skill mappings (enforcing the de-overfit guard).

### C. SkillToolset Mechanics & Progressive Disclosure
- **Initialization:** Each specialist initializes a `SkillToolset` pointing to their specific markdown skills.
- **Tracking Skills & Verifiable `skill_first` Enforcement:** 
  - The specialist prompt requires `load_skill` as Turn 1.
  - We use the `LlmAgent.after_tool_callback` to intercept tool calls. The signature is `Callable[[BaseTool, dict[str, Any], ToolContext, dict[str, Any]], ...]`. 
  - When the tool name matches the skill loading tool (e.g., `load_skill`), the callback records the skill name in a closure-captured list or a custom state object attached to the Context.
  - `after_tool_callback` verifies whether Turn 1 was a `load_skill` invocation and records `Findings.skill_first: bool`.
- Upon completion, the recorded skills are appended to `Findings.skills_used`.

### D. Parallel Execution Model Decision
- **Decision:** Use `asyncio.gather(..., return_exceptions=True)` wrapping individual `Runner` executions instead of ADK `ParallelAgent`.
- **Justification:** `asyncio.gather` provides exact control over error isolation. If one specialist fails (e.g., hits a timeout or raises an exception), `return_exceptions=True` ensures the other specialists continue uninterrupted. The Dispatcher can inspect the exceptions, extract the degraded Pass 1 findings from the failed specialist, and aggregate successfully completed drill-downs. `ParallelAgent` abstractions limit this fine-grained error and state handling.

### E. Execution Budgets
- **Budget as LLM Turns:** `max_llm_calls` counts LLM turns, not tool calls. Each `load_skill` call requires an LLM turn. Sizing: `load_skill` (1-2 turns) + drill-down tool calls (2-3 turns) + final structured output (1 turn) = `max_llm_calls=8`.
- **Tool Call Cap:** Cap at 5 tool calls per specialist, enforced separately via `before_tool_callback` or `after_tool_callback` counter raising early termination/stop.
- **Timeout:** 60s wall clock per specialist via `asyncio.wait_for`.

### F. Agentic Safety & Argument Validation
To ensure the LLM does not generate harmful or out-of-bounds tool arguments:
- **Explicit Safety Decision:** The LLM NEVER generates raw Cloud Logging or Cloud Monitoring filter strings. It passes strictly typed, structured arguments: `service` (validated against discovered services allowlist), `severity` (enum: INFO, WARNING, ERROR, CRITICAL), `status_code_range` (e.g. 500-599), `text_contains` (string literal, sanitized), and `lookback_minutes` (clamped to [1, 60]).
- **Filter Construction:** The tool functions internally construct and parameterize the GCP filter strings based on the structured arguments.
- **GCP Project ID:** Strictly pinned to the current project environment variable; overriding is blocked.
- **Root Cause Overrides:** The tools only return data; they do not dictate the final root cause, ensuring the Synthesizer retains objective authority.

### G. Fallbacks & Guardrails
- **Drill-down Failure:** If the drill-down fails, times out, or errors (e.g., hits `max_llm_calls`), the specialist catches the exception.
- **Graceful Degradation:** The specialist returns the Pass 1 evidence with a `fallback_used=True` flag set in the `Findings` object.
- Re-verified: No code path writes canned root causes. Read-only limits remain fully intact.

## Alternatives Considered
1. **ADK ParallelAgent:** 
   - *Rejected:* Less explicit control over per-agent exception handling and partial state recovery compared to `asyncio.gather`.
2. **Two-Step Agent Pattern (Drill-down -> Extractor):**
   - *Rejected:* Our spike confirmed ADK 2.11 `LlmAgent` supports `tools` and `output_schema` simultaneously. A two-step pattern is unnecessary overhead.

## Implementation Phases
0. **Phase 0: Prerequisites** - Backlog Item 1: Structured JSON logging in demo apps (`demo/shop-api/shop_app.py`, `demo/inventory-svc/inventory_app.py`). Backlog Item 11: Inventory restock loop and `POST /inventory/restock` endpoint in `demo/inventory-svc/inventory_app.py` plus reset script integration.
1. **Phase 1: Tool & Skill Wiring** - Define explicit tool and skill assignments. Implement strict argument validation in tools.
2. **Phase 2: Progressive Skill Loading** - Wire `SkillToolset` and implement the `after_tool_callback` tracking mechanism.
3. **Phase 3: The Two-Pass Specialist** - Refactor `LogsSpecialist` as the vertical slice. Implement Pass 1 and Pass 2. Validate end-to-end with budgets and timeouts.
4. **Phase 4: Fan-out & Parallel Execution** - Roll out the pattern to all specialists using `asyncio.gather`.

## Testing Strategy
- **Hermetic Tests:** Specialists will be unit-tested using stubbed LLMs (mocked responses returning predefined JSON tool calls and structured outputs) for fast, deterministic behavior.
- **Fixtures:** GCP tool results will be mocked via static JSON fixtures corresponding to specific anomaly scenarios.
- **Live Testing:** Validated via `pytest -m live` to ensure end-to-end integration against real GCP endpoints and the actual LLM.

## Acceptance Criteria
1. Each specialist dynamically loads its assigned skill on Turn 1 in live runs (`skill_first=true`).
2. Per-agent `skills_used` is accurately tracked and recorded in `Findings.skills_used` and `InvestigationResult.skills_used` (proven by test).
3. Drill-down executes >= 1 hypothesis-driven tool call beyond Pass 1 sweep.
4. Zero regression on Scenarios 1 and 4.
5. Budgets (call count and timeout) strictly enforced with graceful fallback.

## Decided Details
- **Cache Key:** `sha256(tool_name + sorted_json_args)` for the in-memory deduplication cache.
- **Execution Strategy:** `asyncio.gather(..., return_exceptions=True)`.
