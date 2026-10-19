# autocloud-prototypes
A repo of Autocloud prototypes

## Prototypes

### Incident SRE Agent
Autonomous SRE agent prototype for Google Cloud Platform incident investigation, root cause diagnosis, and bidirectional ITSM ticket enrichment (ServiceNow and Jira). See [incident-sre-agent/](incident-sre-agent/) for documentation and setup instructions.

### GitHub Actions Deployment Failure Investigator
Autonomous CI/CD and SRE diagnostic platform for GitHub Actions pipeline failures. Ingests failure webhooks, triages multi-stage failures across lint, test, build, and deploy stages, correlates logs and Cloud Run runtime health with Vertex AI Gemini, creates enriched ServiceNow incident records, and writes diagnostic findings and ticket deep-links directly back to GitHub Check Runs and Pull Requests. See [gha-deploy-investigator/](gha-deploy-investigator/) for documentation and setup instructions.

## Development Guidelines

### Shell Script Portability
All shell scripts and CI workflow `run:` blocks must remain portable across macOS (BSD) and Linux (GNU). Any in-place `sed` editing must use the uname-based `sedi_args` array pattern (`sedi_args=("-i")`; on Darwin `sedi_args=("-i" "")`). Non-portable variants (bare `sed -i`, `sed -i ''`, `sed -i.bak`, or `sed --in-place`) are blocked by `scripts/check-portable-sed.sh`.

