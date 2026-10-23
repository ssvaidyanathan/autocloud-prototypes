# AGENTS.md — AI Coding Assistant Guide for `gha-deploy-investigator`

This document provides architectural guidance, repository conventions, verification commands, and guardrails for AI coding assistants and developers working on the `gha-deploy-investigator` codebase.

---

## 1. Purpose & Scope

`gha-deploy-investigator` (GDI) is an autonomous Site Reliability Engineering (SRE) diagnostic system designed to investigate failed GitHub Actions CI/CD workflows and Google Cloud Run deployments:
- **Ingestion**: Ingests GitHub webhook events (`workflow_job.completed`, `workflow_run.completed`) via `gdi-bridge`.
- **Deduplication & Queueing**: Enqueues idempotent, deduplicated analysis requests into Google Cloud Tasks.
- **Multi-Agent Diagnosis**: Dispatches `gdi-agent` which runs triage, invokes parallel specialists with progressive disclosure runbook skills, and synthesizes root cause analyses.
- **Dual Write-Back**: Creates and enriches enterprise ServiceNow incident records with styled HTML dossiers, and updates GitHub Check Runs and Pull Request comments.

---

## 2. Multi-Agent Architecture Map

The system uses Google ADK in a hybrid declarative/orchestrated structure:

- **Root Orchestrator**: `Dispatcher` (`BaseAgent`) in [`agent/app/dispatcher.py`](agent/app/dispatcher.py)
  - Coordinates multi-phase workflow: Triage $\rightarrow$ Specialist Fan-Out $\rightarrow$ Root Cause Synthesis.
- **Triage Agent**: `TriageAgent` (`LlmAgent`) in [`agent/app/triage.py`](agent/app/triage.py)
  - Analyzes failure context, identifies pipeline stage, selects specialists, and formulates an `InvestigationPlan`.
- **Specialists Fan-Out**: `ParallelAgent` (`specialists_parallel`) in [`agent/app/dispatcher.py`](agent/app/dispatcher.py)
  - **Pipeline Logs Specialist**: `PipelineLogsSpecialist` in [`agent/app/specialists/pipeline_logs.py`](agent/app/specialists/pipeline_logs.py) — parses GitHub Actions step logs for lint, unit test, and container build failures.
  - **Runtime Health Specialist**: `RuntimeHealthSpecialist` in [`agent/app/specialists/runtime_health.py`](agent/app/specialists/runtime_health.py) — correlates Cloud Run deployment failures, container OOM crashes (exit 137), and revision health telemetry.
  - **IaC Config Specialist**: `IaCConfigSpecialist` in [`agent/app/specialists/iac_config.py`](agent/app/specialists/iac_config.py) — analyzes Terraform manifests and configuration regressions against commit diffs.
- **Report Synthesizer**: `SynthesizerAgent` in [`agent/app/synthesizer.py`](agent/app/synthesizer.py) and Gemini REST synthesis in [`agent/app/llm.py`](agent/app/llm.py)
  - Generates structured, grounded incident reports adhering to the canonical schema.
- **Progressive Disclosure Skills**: Modular runbooks in [`agent/skills/`](agent/skills/)
  - Injected on Turn 1 via `SkillToolset` and [`agent/app/skills_loader.py`](agent/app/skills_loader.py).

---

## 3. Where Instructions Live

All agent system instructions and prompt templates are externalized and managed through declarative configuration:

- **Declarative Agent Instructions**: [`agent/app/agent.yaml`](agent/app/agent.yaml)
  - Defines the agent hierarchy (`dispatcher`, `triage`, `specialists_parallel`, `synthesizer`), agent descriptions, model references (`${GEMINI_MODEL:-default}`), default `LlmAgent` instructions, 3-turn drill-down protocols, and prompt templates.
- **Configuration Loader & Schema Validation**: [`agent/app/agent_config.py`](agent/app/agent_config.py)
  - Loads, validates, and indexes `agent.yaml` using typed Pydantic models.
  - **No Fallbacks**: If `agent.yaml` is missing or malformed, the agent **fails fast at startup** with `FileNotFoundError` or `ValueError`. No silent defaults or inline copies of prompts are permitted in Python code.
- **Domain Diagnostic Runbooks**: [`agent/skills/{skill_name}/SKILL.md`](agent/skills/)
  - Markdown-based progressive disclosure playbooks (`lint-troubleshooting`, `test-failure-triage`, `container-build-troubleshooting`, `cloudrun-deploy-troubleshooting`).
  - **Runbooks Stay as Skills**: `SKILL.md` runbooks represent domain troubleshooting knowledge loaded dynamically on Turn 1 via `SkillsLoader` / `load_skill`, distinct from agent identity and behavioral instructions. They remain as standalone skills rather than being merged into `agent.yaml`.
- **Specialist Runner & SRE Synthesis Prompt Templates**: Key `prompt_templates` in [`agent/app/agent.yaml`](agent/app/agent.yaml)
  - `sre_synthesis`: Formatted at runtime with runtime arguments (`job_name`, `step_name`, `findings_context`, `log_excerpt`) in [`agent/app/llm.py`](agent/app/llm.py).
  - `pipeline_logs_runner`: Formatted at runtime in [`agent/app/specialists/pipeline_logs.py`](agent/app/specialists/pipeline_logs.py) with incident and error details.
  - `runtime_health_runner`: Formatted at runtime in [`agent/app/specialists/runtime_health.py`](agent/app/specialists/runtime_health.py) with target service, region, and revision coordinates.
  - `iac_config_runner`: Formatted at runtime in [`agent/app/specialists/iac_config.py`](agent/app/specialists/iac_config.py) with incident and IaC log excerpt.

---

## 4. Environment & Verification Commands

All commands must be run from the `gha-deploy-investigator/` directory (or use the active virtual environment at `/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/`):

```bash
# Set up environment path if needed
export PATH="/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin:$PATH"

# 1. Byte-identical contract verification between Bridge and Agent
bash scripts/check-contracts.sh

# 2. AST static guard against post-LLM root cause template overrides
python3 scripts/check-no-rootcause-overrides.py

# 3. AST static guard against hardcoded system instructions and prompt templates
python3 scripts/check-no-hardcoded-prompts.py

# 4. Anti-tamper de-overfitting guard across agent source, YAML, and skills
bash scripts/check-no-demo-overfit.sh

# 5. Forbidden literal scanner & markdown relative link checker
bash scripts/check-no-literal-projects.sh

# 6. Automated test suite (unit and component tests across bridge, agent, demo)
pytest bridge/tests agent/tests demo/tests

# 7. Linting checks
ruff check bridge agent demo

# 8. Portable sed syntax check (run from repository root)
../scripts/check-portable-sed.sh

# 9. Terraform formatting and validation across stacks
for stack in agent/infra/terraform bridge/infra/terraform demo/infra/terraform; do
  (cd "$stack" && terraform fmt -check && terraform init -backend=false && terraform validate)
done
```

---

## 5. Repository Guardrails & Non-Negotiable Conventions

1. **No Hardcoded Project IDs or Model Literals**:
   - Zero `gemini-[0-9]` literals in code, skills, or `agent.yaml`.
   - Model names belong only in `config.py` defaults or environment variable references (`${GEMINI_MODEL:-default}`).
   - Scanned and enforced by `scripts/check-no-literal-projects.sh`.
2. **De-Overfit & Anti-Tamper Rules**:
   - Zero hardcoded demo scenarios, project names (`sample-pipeline-app`), memory numbers (`2048`, `256Mi`), or canned answers in `agent/app/` (including `agent.yaml`) or `agent/skills/`.
   - Scanned and enforced by `scripts/check-no-demo-overfit.sh`.
3. **Byte-Identical Contracts**:
   - Data transfer contracts in `bridge/app/contracts/` and `agent/app/contracts/` must remain byte-identical.
   - Enforced by `scripts/check-contracts.sh`.
4. **No Fallback / Fail-Fast Configuration**:
   - Missing or malformed `agent.yaml` must raise an error and abort startup immediately.
   - Do not maintain secondary default prompts inside Python source files.
5. **Portable Shell Syntax (`sedi_args`)**:
   - All shell scripts modifying files in place must use portable `sed` arguments compatible with both BSD/macOS and GNU `sed`.
   - Enforced by `scripts/check-portable-sed.sh`.
6. **Strict Read-Only Tool Enforcement**:
   - Mutating tools are strictly prohibited. All specialist tools must be decorated with `@readonly_tool` and verified via `assert_all_readonly()`.

---

## 6. Coding Do's and Don'ts

### Do's:
- **DO** preserve key terminology in instructions: tests assert that `"InvestigationPlan"` is in triage instructions and `"Report"` is in synthesizer instructions.
- **DO** follow the 3-turn specialist protocol: Turn 1 `load_skill`, Turn 2 query read-only telemetry/diff tools, Turn 3 conclude with structured `Findings`.
- **DO** keep formatting logic and runtime placeholders in Python while storing instruction and template strings in `agent.yaml`.
- **DO** ensure all relative markdown links resolve correctly (`python3 scripts/check-markdown-links.py`).

### Don'ts:
- **DON'T** reintroduce hardcoded prompt strings or fallback instructions in Python files.
- **DON'T** hardcode GCP project IDs, ServiceNow instance URLs, or GitHub tokens.
- **DON'T** use GNU `sed -i` extensions without portable backup argument handling.
- **DON'T** bypass pre-commit hooks, linters, or test suites (`--no-verify`).
