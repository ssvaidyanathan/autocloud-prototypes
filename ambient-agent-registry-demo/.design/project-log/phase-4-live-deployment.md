# Project Log: Phase 4 - Live GCP Deployment & Hot-Reload Demonstration

**Date**: 2026-10-21  
**Phase**: Phase 4: Live GCP Deployment & Live Demonstration  
**Agent**: `ambient-dev-p4`  
**Branch**: `feat/ambient-agent-registry-demo`  

---

## 1. Summary of Changes & Achievements

Phase 4 completed full live deployment of `ambient-agent-registry-demo` to Google Cloud Platform (`cloud-assist-fde-7`, `us-central1`, global skill registry), followed by live end-to-end hot-reload validation, negative boundary verification, and Cloud Logging audit.

### 1. Terraform Infrastructure Provisioning (`infra/terraform/`)
- Initialized remote state backend in GCS bucket `cloud-assist-fde-7-tfstate` under prefix `ambient-agent-registry-demo`.
- Provisioned Artifact Registry repository `ambient-docker-repo` using staged plan `tfplan-ar`.
- Provisioned full infrastructure using staged plan `tfplan-full` without `-auto-approve`:
  - Service accounts: `ambient-agent-sa` and `ambient-calc-sa`.
  - IAM bindings for Vertex AI user, Cloud Logging writer, and Agent Registry viewer.
  - Cloud Run v2 services: `ambient-agent-service` and `ambient-calculator-service`.
  - Agent Registry service binding: `ambient-calculator` pointing to the calculator's `/mcp` endpoint.

### 2. Container Build & Packaging
- Built and published production Docker images to Artifact Registry via Google Cloud Build:
  - `us-central1-docker.pkg.dev/cloud-assist-fde-7/ambient-docker-repo/ambient-calculator-service:latest` (digest: `sha256:77b8f4cd5b00aba3722df67e2535c4e19fa116b27ac0637304c465c11c729335`)
  - `us-central1-docker.pkg.dev/cloud-assist-fde-7/ambient-docker-repo/ambient-agent-service:latest` (digest: `sha256:b49c853f5c9fdc70b9a9b9d19ce2564a1ea202a3cbbf80b0e6b5da84c255ec28`)

### 3. Registry Registration & Skill Publishing
- Registered FastMCP tool `ambient-calculator` in Agent Registry (`us-central1`).
- Published Skill `ambient-privilege-audit-skill` (`private-ambient-privilege-audit-skill`) in `global` location:
  - **Revision 1 (`rev-r1`)**: Configured with `add` and `multiply` tools.
  - **Revision 2 (`rev-r2`)**: Configured with `add`, `multiply`, and `power` tools, along with emoji formatting directives.

### 4. End-to-End Live Demonstration (`scripts/run_demo.sh`)
- Executed full live demonstration against real Cloud Run microservices and Vertex AI Gemini 2.5 Flash:
  - **Step 1 (Health Probe)**: Verified HTTP 200 on `/health`.
  - **Step 2 (Act 1)**: Executed `POST /run` with Revision 1 prompt. Successfully invoked `multiply` and `add` via FastMCP over HTTPS. Verified model response: `"Result: 70 (Computed via multiply, add)"`. Captured provenance metadata in event trace confirming skill revision `r1` and MCP server URL.
  - **Step 3 (Live Skill Mutation)**: Published `rev-r2` to Agent Registry and set as default. Triggered atomic in-memory cache flush via `POST /admin/cache/invalidate`.
  - **Step 4 (Act 2)**: Executed `POST /run` with Revision 2 prompt. Successfully invoked newly registered `power` tool via FastMCP over HTTPS. Verified model response: `"🧮 [CALCULATOR-V2] Computed value: **256**"`. Captured provenance metadata confirming skill revision `r2`.
  - **Step 5 (Uptime Check)**: Confirmed continuous uptime with zero container restarts or redeployments.

### 5. Resiliency & Live Negative Tests
- Executed 7 automated negative test scenarios:
  1. Unknown agent app name rejected with HTTP 404.
  2. Non-existent skill lookup rejected with HTTP 404 NOT_FOUND.
  3. Legacy/un-prefixed skill lookup correctly rejected with HTTP 404.
  4. Unregistered tool call on live MCP server returned `isError: true`.
  5. Boundary/domain math error on live MCP server returned `isError: true`.
  6. Unreachable downstream MCP server returned HTTP 502 Bad Gateway with ZERO fallback arithmetic calculation.
  7. Directory traversal attack attempt on skill resolution blocked by validation guardrail.

### 6. Cloud Logging Audit
- Queried Cloud Logging for `ambient-calculator-service` proving real inbound `POST /mcp` JSON-RPC traffic for all tool calls executed during the demonstration.

---

## 2. Key Technical Findings & Compatibility Resolutions

1. **Private Skill Prefixing in GCP Agent Registry**:
   - GCP Agent Registry automatically creates private skills with a `private-` resource name prefix (e.g., `private-ambient-privilege-audit-skill`).
   - Enhanced `app/registry/client.py` with seamless 404 fallback logic to search for both `name` and `private-{name}`, ensuring full compatibility with public and private skill namespaces.

2. **Scalar String Validation in Agent Registry Metadata**:
   - The GCP Agent Registry API strictly enforces that all `metadata` key-value pairs must be scalar strings.
   - Removed deprecated `adk_additional_tools` from `metadata` in favor of standard frontmatter `allowed-tools: add multiply power` and `metadata.tools: "add,multiply,power"`, satisfying both GCP Agent Registry validation and ADK tool declaration requirements.

3. **ADK Event Trace Provenance Middleware**:
   - Google ADK's `ApiServer` returns `list[Event]` without default skill URI annotations.
   - Added an HTTP middleware to `app/main.py` that enriches event traces with `custom_metadata` containing the active skill resource name, revision, and MCP server endpoint URL, providing verifiable cryptographic and trace evidence in every API response.

---

## 3. Verification Artifacts & Evidence Files

- **Live Demonstration Log**: `/scion-volumes/scratchpad/projects/ambient-agent-registry-demo/reviews/phase-4-live-demo.log`
- **Negative & Resiliency Test Log**: `/scion-volumes/scratchpad/projects/ambient-agent-registry-demo/reviews/phase-4-negative-tests.log`
- **Phase 4 Deployment Summary**: `/scion-volumes/scratchpad/projects/ambient-agent-registry-demo/reviews/phase-4-deployment-summary.md`

---

## 4. Operational URLs & Cloud Resources

| Resource | Value / Identifier |
|---|---|
| **GCP Project** | `cloud-assist-fde-7` |
| **Region** | `us-central1` |
| **Agent Cloud Run Service** | `https://ambient-agent-service-btcbwtmvvq-uc.a.run.app` |
| **Calculator Cloud Run Service** | `https://ambient-calculator-service-btcbwtmvvq-uc.a.run.app` |
| **Artifact Registry** | `us-central1-docker.pkg.dev/cloud-assist-fde-7/ambient-docker-repo` |
| **Agent Registry Skill** | `projects/cloud-assist-fde-7/locations/global/skills/private-ambient-privilege-audit-skill` |
| **Agent Registry Tool Service** | `projects/cloud-assist-fde-7/locations/us-central1/services/ambient-calculator` |
| **Terraform Remote State** | `gs://cloud-assist-fde-7-tfstate/ambient-agent-registry-demo` |
