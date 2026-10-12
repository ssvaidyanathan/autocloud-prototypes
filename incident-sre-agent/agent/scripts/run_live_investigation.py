"""Dedicated runner script for full live ADK investigation against GCP and Vertex AI.

Executes ONE complete end-to-end investigation exercising the authentic production pipeline:
1. Dispatcher (BaseAgent) orchestrates the workflow.
2. Triage (LlmAgent) receives incident metadata, consults skills via SkillToolset,
   and generates a validated Pydantic InvestigationPlan using Gemini on Vertex AI.
3. ParallelAgent executes selected specialists concurrently:
   - LogsSpecialist (query_logs, summarize_error_groups)
   - MetricsSpecialist (list_alert_policies, query_time_series)
   - ChangesSpecialist (query_audit_logs, list_cloud_run_revisions, list_cloud_builds)
   - ResourcesSpecialist (describe_cloud_run_service, describe_cloud_sql_instances,
     describe_gke_clusters)
4. Synthesizer (LlmAgent) synthesizes findings using Gemini on Vertex AI
   and outputs a validated Pydantic Report model.
5. Outbound Callback delivers payload to a local capture HTTP server on 127.0.0.1:8088.
6. Ticket Rendering renders Report into ServiceNow HTML work note format.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Literal, Optional, cast

# Set production environment for authentic GCP clients
os.environ["APP_ENV"] = "production"

from agent.app.callback import CallbackClient
from agent.app.config import get_settings
from agent.app.contracts.incident import Incident
from agent.app.contracts.request import InvestigationRequest
from agent.app.contracts.result import InvestigationResult
from agent.app.dispatcher import Dispatcher
from agent.app.llm import GeminiLlmClient
from agent.app.skills_loader import SkillsLoader, get_skill_toolset_for_target
from agent.app.specialists.changes import ChangesSpecialist
from agent.app.specialists.logs import LogsSpecialist
from agent.app.specialists.metrics import MetricsSpecialist
from agent.app.specialists.resources import ResourcesSpecialist
from agent.app.synthesizer import SynthesizerAgent
from agent.app.tools.clients import clear_client_cache
from agent.app.triage import TriageAgent
from bridge.app.adapters.servicenow import render_work_note
from bridge.app.contracts.report import Report as BridgeReport

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("live-investigation")


class CallbackCaptureHandler(BaseHTTPRequestHandler):
    """Local HTTP handler capturing inbound callback payloads."""

    received_payload: Optional[Dict[str, Any]] = None
    received_headers: Optional[Dict[str, str]] = None

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        CallbackCaptureHandler.received_headers = dict(self.headers)
        CallbackCaptureHandler.received_payload = json.loads(body.decode("utf-8"))

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "captured", "ack": true}')

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy HTTP server stdout logs
        pass


def run_local_capture_server(port: int = 8088) -> ThreadingHTTPServer:
    """Start local HTTP capture server in background thread."""
    server = ThreadingHTTPServer(("127.0.0.1", port), CallbackCaptureHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    return server


async def run_live_investigation(
    project_id: Optional[str] = None,
    service: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    incident_id: Optional[str] = None,
    severity: str = "P1",
) -> Dict[str, Any]:
    """Execute complete live investigation against real GCP project."""
    target_project = project_id or os.getenv("GCP_PROJECT") or os.getenv("PROJECT_ID")
    if not target_project:
        raise ValueError(
            "Target GCP project must be specified via --project flag or "
            "GCP_PROJECT / PROJECT_ID environment variable."
        )

    target_service = service or os.getenv("TARGET_SERVICE") or "shop-api"

    now_utc = datetime.now(timezone.utc)
    investigation_end = end_time or now_utc
    investigation_start = start_time or (investigation_end - timedelta(hours=1))
    incident_key = incident_id or f"INC-{int(now_utc.timestamp())}"

    clear_client_cache()
    cfg = get_settings()
    cfg.gcp_project = target_project
    cfg.allowed_callback_hosts = ["127.0.0.1", "localhost"]

    logger.info("Initializing Live Investigation Run")
    logger.info("Target Project: %s", target_project)
    logger.info("Target Service: %s", target_service)
    logger.info("Target Region: %s", cfg.gcp_region)
    logger.info("Vertex Location: %s", cfg.vertex_location)
    logger.info("Dispatcher Model: %s", cfg.model_dispatcher)
    logger.info("Synthesizer Model: %s", cfg.model_synthesizer)

    # Start callback capture server
    callback_port = 8088
    callback_url = f"http://127.0.0.1:{callback_port}/callback"
    capture_server = run_local_capture_server(port=callback_port)
    logger.info("Callback capture server active on %s", callback_url)

    # Create skills loader & skill toolsets
    loader = SkillsLoader()
    disp_skills = get_skill_toolset_for_target("dispatcher", skills_dir=loader.skills_dir)
    synth_skills = get_skill_toolset_for_target("synthesizer", skills_dir=loader.skills_dir)
    logger.info(
        "Loaded skill toolsets: dispatcher (%d skills), synthesizer (%d skills)",
        len(disp_skills.skills),
        len(synth_skills.skills),
    )

    # Instantiate authentic Gemini LLM Client on global endpoint
    llm_client = GeminiLlmClient(
        project_id=target_project,
        location=cfg.vertex_location,
        model=cfg.model_dispatcher,
        config=cfg,
        skills_loader=loader,
    )

    # Instantiate Triage Agent with ADK LlmAgent + SkillToolset + GeminiLlmClient
    triage = TriageAgent(
        skills_loader=loader,
        max_query_window_hours=cfg.max_query_window_hours,
        model=cfg.model_dispatcher,
        llm_client=llm_client,
        config=cfg,
    )

    # Instantiate live Specialists with read-only GCP client SDKs
    specialists = {
        "logs": LogsSpecialist(
            project_id=target_project,
            model=cfg.model_specialist,
            config=cfg,
        ),
        "metrics": MetricsSpecialist(
            project_id=target_project,
            model=cfg.model_specialist,
            config=cfg,
        ),
        "changes": ChangesSpecialist(
            project_id=target_project,
            model=cfg.model_specialist,
            config=cfg,
        ),
        "resources": ResourcesSpecialist(
            project_id=target_project,
            model=cfg.model_specialist,
            config=cfg,
        ),
    }

    # Instantiate Synthesizer Agent with ADK LlmAgent + SkillToolset + GeminiLlmClient
    synthesizer = SynthesizerAgent(
        skills_loader=loader,
        model=cfg.model_synthesizer,
        llm_client=llm_client,
        config=cfg,
    )

    # Wire Dispatcher (BaseAgent)
    dispatcher = Dispatcher(
        config=cfg,
        specialists=specialists,
        triage_agent=triage,
        synthesizer=synthesizer,
        skills_loader=loader,
        llm_client=llm_client,
    )

    valid_severity: Literal["P1", "P2", "P3", "P4"] = (
        cast(Literal["P1", "P2", "P3", "P4"], severity)
        if severity in ("P1", "P2", "P3", "P4")
        else "P1"
    )

    # Create target incident targeting live Cloud Run service in target GCP project
    incident = Incident(
        id=f"inc-live-{int(now_utc.timestamp())}",
        source="servicenow",
        external_id=incident_key,
        external_key=incident_key,
        title=f"Service degradation and 5xx errors on {target_service}",
        description=(
            f"Intermittent HTTP 500 responses and latency spikes detected on Cloud Run service "
            f"'{target_service}' in {cfg.gcp_region}. Critical user workflows impacted."
        ),
        severity=valid_severity,
        service_hint=target_service,
        opened_at=investigation_start,
    )

    request = InvestigationRequest(
        incident=incident,
        callback_url=callback_url,
        requested_at=now_utc,
    )
    logger.info(
        "Created InvestigationRequest for %s: %s",
        request.incident.external_key,
        request.callback_url,
    )

    total_start = time.perf_counter()

    # Step A: Triage (Vertex AI Gemini + SkillToolset)
    triage_start = time.perf_counter()
    logger.info("Executing Step 1: Triage via Vertex AI Gemini...")
    plan = triage.plan(
        incident,
        start_time_override=investigation_start,
        end_time_override=investigation_end,
    )
    triage_elapsed = round(time.perf_counter() - triage_start, 3)
    logger.info(
        "Triage Complete in %.3fs (mode=%s): %s selected specialists: %s",
        triage_elapsed,
        triage.triage_mode,
        len(plan.selected_specialists),
        plan.selected_specialists,
    )

    # Step B: Parallel Fan-out across Specialists via ParallelAgent
    specialists_start = time.perf_counter()
    logger.info("Executing Step 2: Parallel Specialist Fan-out against live GCP telemetry...")
    findings, specialist_errors = await dispatcher._execute_fan_out(plan, incident)
    specialists_elapsed = round(time.perf_counter() - specialists_start, 3)
    logger.info(
        "Specialists Fan-out Complete in %.3fs across %d specialists (errors=%d)",
        specialists_elapsed,
        len(findings),
        len(specialist_errors),
    )

    # Step C: Review Step
    review_start = time.perf_counter()
    logger.info("Executing Step 3: Review evaluation...")
    await dispatcher._execute_review_step(plan, incident, findings, specialist_errors)
    review_elapsed = round(time.perf_counter() - review_start, 3)

    # Step D: Synthesizer (Vertex AI Gemini + SkillToolset)
    synth_start = time.perf_counter()
    report = synthesizer.synthesize(
        incident=incident,
        plan=plan,
        findings=findings,
        specialist_errors=specialist_errors,
        skills_used=list(loader.loaded_skills),
    )
    synth_elapsed = round(time.perf_counter() - synth_start, 3)
    logger.info(
        "Synthesizer Complete in %.3fs (mode=%s): summary='%s...'",
        synth_elapsed,
        synthesizer.synthesizer_mode,
        report.summary[:80],
    )

    total_elapsed = round(time.perf_counter() - total_start, 3)

    result = InvestigationResult(
        investigation_id=f"inv-{int(now_utc.timestamp())}",
        incident_id=incident.id,
        incident_source=incident.source,
        incident_external_id=incident.external_id,
        incident_external_key=incident.external_key,
        status="completed",
        report=report,
    )

    # Step E: Outbound Callback Delivery to Local Capture Server
    cb_start = time.perf_counter()
    logger.info("Executing Step 5: Delivering outbound callback to %s...", callback_url)
    callback_client = CallbackClient(
        config=cfg,
        token_provider=lambda aud: "live-test-oidc-bearer-token",
    )
    cb_resp = callback_client.deliver(callback_url, result)
    cb_elapsed = round(time.perf_counter() - cb_start, 3)
    logger.info("Callback Delivered in %.3fs: HTTP %d", cb_elapsed, cb_resp.status_code)

    # Verify capture server received payload
    assert CallbackCaptureHandler.received_payload is not None, (
        "Capture server did not receive payload"
    )
    captured_data = CallbackCaptureHandler.received_payload

    # Step F: Ticket Rendering in ServiceNow HTML format
    logger.info("Executing Step 6: Rendering Report to ServiceNow HTML work note...")
    bridge_report = BridgeReport.model_validate(report.model_dump())
    service_now_html = render_work_note(bridge_report)
    logger.info("ServiceNow HTML Rendered: %d characters", len(service_now_html))

    # Shutdown capture server
    capture_server.shutdown()

    evidence_summary = {
        "timestamp_utc": now_utc.isoformat(),
        "target_project": target_project,
        "region": cfg.gcp_region,
        "vertex_location": cfg.vertex_location,
        "model_dispatcher": cfg.model_dispatcher,
        "model_synthesizer": cfg.model_synthesizer,
        "service_inspected": target_service,
        "total_duration_seconds": total_elapsed,
        "latencies": {
            "triage_seconds": triage_elapsed,
            "parallel_specialists_seconds": specialists_elapsed,
            "review_seconds": review_elapsed,
            "synthesizer_seconds": synth_elapsed,
            "callback_seconds": cb_elapsed,
        },
        "plan": plan.model_dump(mode="json"),
        "report_json": report.model_dump_json(indent=2),
        "servicenow_html": service_now_html,
        "captured_callback_status": captured_data.get("status"),
        "specialists_status": [s.model_dump(mode="json") for s in report.specialists_status],
    }

    return evidence_summary


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for live investigation."""
    parser = argparse.ArgumentParser(
        description="Run live ADK investigation against Google Cloud telemetry."
    )
    parser.add_argument(
        "--project",
        default=os.getenv("GCP_PROJECT") or os.getenv("PROJECT_ID"),
        help="GCP Project ID to investigate (or set GCP_PROJECT/PROJECT_ID env var)",
    )
    parser.add_argument(
        "--service",
        default=os.getenv("TARGET_SERVICE", "shop-api"),
        help="Target Cloud Run service name (default: shop-api)",
    )
    parser.add_argument(
        "--start-time",
        default=os.getenv("START_TIME"),
        help="Investigation start time in ISO 8601 format (e.g. 2026-10-11T00:00:00Z)",
    )
    parser.add_argument(
        "--end-time",
        default=os.getenv("END_TIME"),
        help="Investigation end time in ISO 8601 format (e.g. 2026-10-11T01:00:00Z)",
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=int(os.getenv("INVESTIGATION_WINDOW_MINUTES", "60")),
        help="Investigation lookback window in minutes if start-time not specified (default: 60)",
    )
    parser.add_argument(
        "--incident-id",
        default=os.getenv("INCIDENT_ID"),
        help="Incident ticket key/identifier (default: auto-generated)",
    )
    parser.add_argument(
        "--severity",
        default=os.getenv("INCIDENT_SEVERITY", "P1"),
        help="Incident severity level (default: P1)",
    )
    parser.add_argument(
        "--output",
        default=os.getenv(
            "OUTPUT_FILE",
            os.path.join(os.path.dirname(__file__), "live_evidence_output.json"),
        ),
        help="Path to output JSON file (default: agent/scripts/live_evidence_output.json)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.project:
        logger.error(
            "GCP Project ID must be provided via --project or "
            "GCP_PROJECT/PROJECT_ID environment variable."
        )
        sys.exit(1)

    start_dt = None
    if args.start_time:
        start_dt = datetime.fromisoformat(args.start_time.replace("Z", "+00:00"))

    end_dt = None
    if args.end_time:
        end_dt = datetime.fromisoformat(args.end_time.replace("Z", "+00:00"))

    if start_dt is None and end_dt is not None:
        start_dt = end_dt - timedelta(minutes=args.window_minutes)

    try:
        evidence = asyncio.run(
            run_live_investigation(
                project_id=args.project,
                service=args.service,
                start_time=start_dt,
                end_time=end_dt,
                incident_id=args.incident_id,
                severity=args.severity,
            )
        )
        print("\n" + "=" * 70)
        print("LIVE ADK INVESTIGATION EXECUTION SUCCESS")
        print("=" * 70)
        print(f"Project:          {evidence['target_project']}")
        print(f"Service:          {evidence['service_inspected']}")
        print(f"Region:           {evidence['region']}")
        print(f"Vertex Location:  {evidence['vertex_location']}")
        print(f"Model:            {evidence['model_dispatcher']}")
        print(f"Total Duration:   {evidence['total_duration_seconds']}s")
        print(f"Triage Latency:   {evidence['latencies']['triage_seconds']}s")
        print(f"Specialists Lat:  {evidence['latencies']['parallel_specialists_seconds']}s")
        print(f"Synthesis Lat:    {evidence['latencies']['synthesizer_seconds']}s")
        print(f"Callback Lat:     {evidence['latencies']['callback_seconds']}s")
        print("-" * 70)
        print("REPORT JSON:")
        print(evidence["report_json"][:600] + "\n... [truncated for display]")
        print("-" * 70)
        print("SERVICENOW HTML WORK NOTE EXCERPT:")
        print(evidence["servicenow_html"][:600] + "\n... [truncated for display]")
        print("=" * 70)

        output_file = args.output
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(evidence, f, indent=2)
        print(f"Full evidence saved to {output_file}")

    except Exception as exc:
        logger.exception("Live investigation failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
