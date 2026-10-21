# autocloud-prototypes
A repo of Autocloud prototypes

## Prototypes

### Agent Registry Demo
Autonomous agent prototype built with the Google Agent Development Kit (ADK) and Gemini that loads its skills from Google Cloud Agent Registry. Discovers tools through a registry-registered Model Context Protocol (MCP) calculator server, picks up skill updates without a redeploy using an in-memory TTL cache and cache invalidation endpoint, and returns structured provenance in responses while operating fail-closed with no fallbacks. See [agent-registry-demo/](agent-registry-demo/) for documentation and setup instructions.

### Ambient Agent Registry Demo
Autonomous agent prototype built on Google Agent Development Kit (ADK) exposing native synchronous `/run` execution traces (`list[Event]`). Loads and updates skills dynamically from Google Cloud Agent Registry, discovers remote FastMCP calculator tools, supports zero-downtime hot-reloads via `/admin/cache/invalidate`, and enforces fail-closed execution with zero fallbacks and production safety startup guards. See [ambient-agent-registry-demo/](ambient-agent-registry-demo/) for documentation and setup instructions.

### Incident SRE Agent
Autonomous SRE agent prototype for Google Cloud Platform incident investigation, root cause diagnosis, and bidirectional ITSM ticket enrichment (ServiceNow and Jira). See [incident-sre-agent/](incident-sre-agent/) for documentation and setup instructions.

### GitHub Actions Deployment Failure Investigator
Autonomous CI/CD and SRE diagnostic platform for GitHub Actions pipeline failures. Ingests failure webhooks, triages multi-stage failures across lint, test, build, and deploy stages, correlates logs and Cloud Run runtime health with Vertex AI Gemini, creates enriched ServiceNow incident records, and writes diagnostic findings and ticket deep-links directly back to GitHub Check Runs and Pull Requests. See [gha-deploy-investigator/](gha-deploy-investigator/) for documentation and setup instructions.

## Development Guidelines

### Shell Script Portability
All shell scripts and CI workflow `run:` blocks must remain portable across macOS (BSD) and Linux (GNU). Any in-place `sed` editing must use the uname-based `sedi_args` array pattern (`sedi_args=("-i")`; on Darwin `sedi_args=("-i" "")`). Non-portable variants (bare `sed -i`, `sed -i ''`, `sed -i.bak`, or `sed --in-place`) are blocked by `scripts/check-portable-sed.sh`.

Note: Heredoc bodies (e.g. `cat << 'EOF'`) are scanned as shell code. If a script generates documentation or user instructions referencing `sed`, reword the text (e.g., describe in-place editing conceptually or avoid literal `-i` flags) to satisfy the guard.

