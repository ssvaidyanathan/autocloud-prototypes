import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response, status

app = FastAPI(title="Fake Ticket Server", description="Simulates ServiceNow and Jira Cloud APIs")


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    query_params: dict[str, str]
    body: Any


@dataclass
class FakeTicketServerState:
    servicenow_incidents: dict[str, dict[str, Any]] = field(default_factory=dict)
    servicenow_attachments: list[dict[str, Any]] = field(default_factory=list)
    jira_issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    jira_comments: list[dict[str, Any]] = field(default_factory=list)
    jira_transitions_history: list[dict[str, Any]] = field(default_factory=list)
    jira_attachments: list[dict[str, Any]] = field(default_factory=list)
    recorded_requests: list[RecordedRequest] = field(default_factory=list)

    def reset(self) -> None:
        self.servicenow_incidents.clear()
        self.servicenow_attachments.clear()
        self.jira_issues.clear()
        self.jira_comments.clear()
        self.jira_transitions_history.clear()
        self.jira_attachments.clear()
        self.recorded_requests.clear()


state = FakeTicketServerState()


@app.middleware("http")
async def record_request_middleware(request: Request, call_next: Any) -> Response:
    body_bytes = await request.body()

    parsed_body: Any = None
    if body_bytes:
        try:
            parsed_body = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            parsed_body = body_bytes

    state.recorded_requests.append(
        RecordedRequest(
            method=request.method,
            path=request.url.path,
            headers=dict(request.headers),
            query_params=dict(request.query_params),
            body=parsed_body,
        )
    )

    # Re-wrap receive so downstream handlers can re-read body
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body_bytes}

    request = Request(request.scope, receive=receive)
    response: Response = await call_next(request)
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "fake-ticket-server"}


@app.post("/test/reset")
def reset_state() -> dict[str, str]:
    state.reset()
    return {"status": "reset"}


# -----------------------------------------------------------------------------
# ServiceNow Endpoints
# -----------------------------------------------------------------------------


@app.patch("/api/now/table/incident/{sys_id}")
async def patch_servicenow_incident(sys_id: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    incident = state.servicenow_incidents.setdefault(
        sys_id,
        {
            "sys_id": sys_id,
            "number": f"INC{sys_id[:7]}",
            "state": "1",
            "work_notes": [],
        },
    )

    if "state" in body:
        incident["state"] = str(body["state"])
    if "work_notes" in body:
        incident["work_notes"].append(body["work_notes"])

    return {"result": incident}


@app.post("/api/now/attachment/file")
async def post_servicenow_attachment(request: Request) -> dict[str, Any]:
    table_name = request.query_params.get("table_name", "incident")
    table_sys_id = request.query_params.get("table_sys_id", "")
    file_name = request.query_params.get("file_name", "report.html")
    content_bytes = await request.body()

    att_record = {
        "sys_id": f"att-{len(state.servicenow_attachments) + 1}",
        "table_name": table_name,
        "table_sys_id": table_sys_id,
        "file_name": file_name,
        "content_type": request.headers.get("content-type", "application/octet-stream"),
        "size_bytes": len(content_bytes),
        "content": content_bytes.decode("utf-8", errors="replace"),
    }
    state.servicenow_attachments.append(att_record)
    return {"result": att_record}


@app.get("/api/now/table/incident/{sys_id}")
def get_servicenow_incident(sys_id: str) -> dict[str, Any]:
    incident = state.servicenow_incidents.get(sys_id)
    if not incident:
        return {"result": {"sys_id": sys_id, "state": "1", "work_notes": []}}
    return {"result": incident}


# -----------------------------------------------------------------------------
# Jira Cloud Endpoints
# -----------------------------------------------------------------------------


@app.post("/rest/api/3/issue/{key}/comment")
async def post_jira_comment(key: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    comment_data = {
        "id": f"comment-{len(state.jira_comments) + 1}",
        "issue_key": key,
        "body": body.get("body"),
    }
    state.jira_comments.append(comment_data)

    issue = state.jira_issues.setdefault(
        key,
        {"key": key, "status": "To Do", "comments": [], "attachments": []},
    )
    issue["comments"].append(comment_data)
    return comment_data


@app.get("/rest/api/3/issue/{key}/transitions")
def get_jira_transitions(key: str) -> dict[str, Any]:
    return {
        "transitions": [
            {"id": "21", "name": "In Progress"},
            {"id": "31", "name": "Done"},
            {"id": "41", "name": "Closed"},
        ]
    }


@app.post("/rest/api/3/issue/{key}/transitions")
async def post_jira_transition(key: str, request: Request, response: Response) -> None:
    body = await request.json()
    trans_id = body.get("transition", {}).get("id")

    record = {"issue_key": key, "transition_id": trans_id}
    state.jira_transitions_history.append(record)

    issue = state.jira_issues.setdefault(
        key,
        {"key": key, "status": "To Do", "comments": [], "attachments": []},
    )
    if trans_id == "21":
        issue["status"] = "In Progress"

    response.status_code = status.HTTP_204_NO_CONTENT


@app.post("/rest/api/3/issue/{key}/attachments")
async def post_jira_attachments(key: str, request: Request) -> list[dict[str, Any]]:
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8", errors="replace")

    import re

    filename = f"sre_investigation_{key}.html"
    fn_match = re.search(r'filename="([^"]+)"', body_str)
    if fn_match:
        filename = fn_match.group(1)

    content = body_str
    parts = body_str.split("\r\n\r\n", 1)
    if len(parts) == 2:
        file_part = parts[1]
        boundary_idx = file_part.rfind("\r\n--")
        if boundary_idx != -1:
            content = file_part[:boundary_idx]

    att_record = {
        "id": f"att-{len(state.jira_attachments) + 1}",
        "issue_key": key,
        "filename": filename,
        "content": content,
        "size": len(content.encode("utf-8")),
    }
    state.jira_attachments.append(att_record)
    return [att_record]


@app.get("/rest/api/3/issue/{key}")
def get_jira_issue(key: str) -> dict[str, Any]:
    issue = state.jira_issues.get(key)
    if not issue:
        return {"key": key, "status": "To Do", "comments": [], "attachments": []}
    return issue
