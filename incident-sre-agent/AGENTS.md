# AGENTS.md — AI Coding Assistant Guide for `incident-sre-agent`

## 1. Purpose & Scope

`incident-sre-agent` is an autonomous Site Reliability Engineering (SRE) diagnostic system that investigates production incidents triggered by ticketing webhooks (ServiceNow, Jira, or generic alerting).

The system consists of two collaborating Cloud Run services:
1. **`incident-bridge`**: Ingests, authenticates, and normalizes incoming incident webhooks, acknowledges tickets immediately, enqueues deduplicated Cloud Tasks, and delivers rich HTML work notes and ADF comments back to ticketing platforms upon completion.
2. **`sre-agent`**: Executes multi-agent telemetry investigations (Logs, Metrics, Changes, Resources) across Google Cloud Platform, correlates symptom onset with recent deployments or infrastructure drift, and synthesizes an executive-grade root cause report using Vertex AI Gemini.

---

## 2. Multi-Agent & Review Architecture Map

The core diagnostic engine in `agent/app/` is structured as a hierarchical Google ADK multi-agent system:

```
Dispatcher (BaseAgent)
├── TriageAgent (LlmAgent)
├── ParallelSpecialists (ParallelAgent)
│   ├── ChangesSpecialist (LlmAgent)
│   ├── LogsSpecialist (LlmAgent)
│   ├── MetricsSpecialist (LlmAgent)
│   └── ResourcesSpecialist (LlmAgent)
├── ReviewEvaluator (Python Review Loop, max_rounds=1)
└── SynthesizerAgent (LlmAgent / Vertex AI Gemini REST)
```

- **Root Orchestrator**: `Dispatcher` ([`agent/app/dispatcher.py`](agent/app/dispatcher.py)) supervises the investigation lifecycle.
- **Triage**: `TriageAgent` ([`agent/app/triage.py`](agent/app/triage.py)) analyzes the incident ticket to formulate an `InvestigationPlan` selecting specialists and establishing query time windows.
- **Specialists Fan-Out**: Four concurrent diagnostic specialists in [`agent/app/specialists/`](agent/app/specialists/):
  - `ChangesSpecialist` ([`changes.py`](agent/app/specialists/changes.py)): Queries Cloud Audit Logs, Cloud Run revision diffs, and Cloud Build history.
  - `LogsSpecialist` ([`logs.py`](agent/app/specialists/logs.py)): Queries Cloud Logging, analyzes stack traces, and aggregates Cloud Run request status codes.
  - `MetricsSpecialist` ([`metrics.py`](agent/app/specialists/metrics.py)): Analyzes Cloud Monitoring time series, latency percentiles, and active alert policies.
  - `ResourcesSpecialist` ([`resources.py`](agent/app/specialists/resources.py)): Inspects Cloud Run, GKE, Compute Engine, and Cloud SQL configurations (with strict credential redaction).
- **Two-Pass Execution**: Specialists execute Pass 1 (Skill-Led LLM Drill-Down) and Pass 2 (Deterministic Baseline Sweep) concurrently. If LLM reasoning times out or degrades, the baseline sweep guarantees essential telemetry is preserved.
- **Review Evaluator Loop**: `ReviewEvaluator` ([`agent/app/review.py`](agent/app/review.py)) examines initial specialist findings. If gaps or adjacent services require correlation, it executes at most one targeted follow-up query round (`max_rounds = 1`).
- **Synthesis Engine**: `SynthesizerAgent` ([`agent/app/synthesizer.py`](agent/app/synthesizer.py)) and Vertex AI REST client ([`agent/app/llm.py`](agent/app/llm.py)) compile findings into the canonical `Report` contract.

---

## 3. Where Instructions Live

All runtime system instructions, prompt templates, and agent definitions are externalized from Python code:

- **Declarative Architecture & System Instructions**: [`agent/app/agent.yaml`](agent/app/agent.yaml)
  - Defines the ADK agent tree, agent names, models, descriptions, output schemas, and system instructions.
  - Single source of truth for `triage`, `changes_specialist`, `logs_specialist`, `metrics_specialist`, `resources_specialist`, and `synthesizer` instructions.
  - Uses model environment references (`${MODEL_DISPATCHER}`, `${MODEL_SPECIALIST}`, `${MODEL_SYNTHESIZER}`) with zero hardcoded model literals.
- **Configuration Loader**: [`agent/app/agent_config.py`](agent/app/agent_config.py)
  - Loads and strictly validates `agent.yaml` using `yaml.safe_load`.
  - Enforces fail-fast startup: if `agent.yaml` is missing, unreadable, or malformed, the service immediately raises `AgentConfigError` at startup. No silent defaults or fallbacks.
- **Runbook Skills (Progressive Disclosure)**: [`agent/skills/*/SKILL.md`](agent/skills/)
  - Domain-specific runbooks loaded on-demand by specialists via ADK `SkillToolset`.

---

## 4. Environment & Verification Commands

All commands below are verified and must pass cleanly before opening a pull request.

### Virtual Environment Setup

```bash
# From incident-sre-agent root:
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e "./bridge[dev]" -e "./agent[dev]" -e "./demo[dev]"
```

### Running Tests

```bash
# Run all unit and component tests across bridge, agent, and demo:
pytest bridge/tests agent/tests demo/tests

# Run agent tests only:
pytest agent/tests/

# Run golden configuration tests:
pytest agent/tests/test_agent_config.py
```

### Code Formatting & Typechecking

```bash
# Run Ruff lint checks:
ruff check bridge agent demo

# Run Ruff format check:
ruff format --check bridge agent demo

# Run Mypy static typecheck:
mypy bridge agent demo
```

### Guardrail Scripts

```bash
# Verify byte-identical contract synchronization:
bash scripts/check-contracts.sh

# Verify zero forbidden project literals or un-externalized Gemini models:
bash scripts/check-no-literal-projects.sh

# Verify zero demo scenario overfit across code, prompts, and YAML:
bash scripts/check-no-demo-overfit.sh

# Verify no post-LLM root cause template overrides or regex classifiers:
python3 scripts/check-no-rootcause-overrides.py

# Verify zero demo log hints in logging statements:
python3 scripts/check-no-demo-log-hints.py

# Verify all relative markdown links resolve successfully:
python3 scripts/check-markdown-links.py

# Verify portable sed syntax across the repository (run from repo root):
bash scripts/check-portable-sed.sh
```

---

## 5. Non-Negotiable Rules & Guardrails

1. **No Model Literals in Code or YAML**:
   - `gemini-[0-9]` strings are strictly prohibited in `agent/app/agent.yaml` and Python code outside of `config.py` default settings.
   - Reference models using environment variable expressions (`${MODEL_SPECIALIST}`).
2. **No Hardcoded Project Identifiers**:
   - GCP project IDs and ticketing instance URLs must be dynamic or configurable (`check-no-literal-projects.sh`).
3. **De-overfit Integrity**:
   - Never hardcode demo service names (`shop-api`, `inventory-svc`), scenario numbers (`Scenario 1`), or canned diagnostic answers in `agent/app/`, `agent.yaml`, or `agent/skills/`.
4. **Canned Override Ban**:
   - All root cause explanations, confidence ratings, and remediations must be authored 100% by the LLM. AST guards prohibit post-LLM template overrides (`check-no-rootcause-overrides.py`).
5. **Byte-Identical Contract Synchronization**:
   - Pydantic models in `bridge/app/contracts/` and `agent/app/contracts/` must remain byte-identical (`check-contracts.sh`).
6. **No Fallbacks for Missing or Malformed `agent.yaml`**:
   - If `agent.yaml` is absent or malformed, the agent must fail immediately at startup with `AgentConfigError`. Never provide silent defaults or inline Python fallback prompts.
7. **Single Follow-Up Review Cap**:
   - `ReviewEvaluator.max_rounds` must never exceed `1` to avoid unbounded query loops.
8. **Strict Read-Only Guarantee**:
   - All diagnostic specialist tools must be read-only (`assert_all_readonly()`). Mutating tools are strictly forbidden.
9. **Portable Shell Syntax**:
   - Scripts must follow `scripts/check-portable-sed.sh` rules: never use non-portable GNU `sed -i` extensions without portable argument handling (`sedi_args`).

---

## 6. Coding Do's and Don'ts

### DO:
- **DO** verify that all system instructions moved to `agent.yaml` remain byte-identical to their historical constants.
- **DO** run `pytest bridge/tests agent/tests demo/tests` offline to ensure hermetic execution without GCP network dependencies.
- **DO** ensure all markdown links are relative and resolve cleanly (`scripts/check-markdown-links.py`).
- **DO** preserve docstrings and architectural comments when making targeted edits.

### DON'T:
- **DON'T** re-introduce inline prompt strings or `.txt` prompt files into Python modules.
- **DON'T** push directly to the remote Git repository; the coordinator handles branch pushes.
- **DON'T** deploy to Cloud Run or Cloud Tasks from local developer environments during feature development.
- **DON'T** bypass specialist execution protocols or disable tool callbacks.
