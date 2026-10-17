import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response, status

app = FastAPI(
    title="Fake ServiceNow Server",
    description="Simulates ServiceNow Table and Attachment APIs",
)


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
    recorded_requests: list[RecordedRequest] = field(default_factory=list)

    def reset(self) -> None:
        self.servicenow_incidents.clear()
        self.servicenow_attachments.clear()
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
            parsed_body = body_bytes.decode("utf-8", errors="replace")

    state.recorded_requests.append(
        RecordedRequest(
            method=request.method,
            path=request.url.path,
            headers=dict(request.headers),
            query_params=dict(request.query_params),
            body=parsed_body,
        )
    )

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body_bytes}

    request = Request(request.scope, receive=receive)
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "fake-ticket-server"}


@app.post("/test/reset")
def reset_state() -> dict[str, str]:
    state.reset()
    return {"status": "reset"}


@app.get("/api/now/table/incident")
def query_servicenow_incidents(request: Request) -> dict[str, Any]:
    sysparm_query = request.query_params.get("sysparm_query", "")
    results = []
    if "correlation_id=" in sysparm_query:
        target_corr_id = sysparm_query.split("correlation_id=", 1)[1].split("^")[0]
        for inc in state.servicenow_incidents.values():
            if inc.get("correlation_id") == target_corr_id:
                results.append(inc)
    elif not sysparm_query:
        results = list(state.servicenow_incidents.values())
    return {"result": results}


@app.post("/api/now/table/incident", status_code=status.HTTP_201_CREATED)
async def create_servicenow_incident(request: Request) -> dict[str, Any]:
    body = await request.json()
    count = len(state.servicenow_incidents) + 1
    sys_id = f"sys_{count:06d}"
    number = f"INC001{count:04d}"

    incident = {
        "sys_id": sys_id,
        "number": number,
        "state": "1",
        **body,
    }
    state.servicenow_incidents[sys_id] = incident
    return {"result": incident}


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
    if "caller_id" in body:
        incident["caller_id"] = str(body["caller_id"])
    if "assignment_group" in body:
        incident["assignment_group"] = str(body["assignment_group"])
    if "work_notes" in body:
        if isinstance(incident.get("work_notes"), list):
            incident["work_notes"].append(body["work_notes"])
        else:
            incident["work_notes"] = [body["work_notes"]]

    return {"result": incident}


@app.get("/api/now/table/sys_user")
def query_sys_user(request: Request) -> dict[str, Any]:
    sysparm_query = request.query_params.get("sysparm_query", "")
    if "admin" in sysparm_query:
        return {
            "result": [
                {
                    "sys_id": "6816f79cc0a8016401c5a33be04be441",
                    "user_name": "admin",
                    "email": "admin@example.com",
                }
            ]
        }
    return {"result": []}


@app.get("/api/now/table/incident/{sys_id}")
def get_servicenow_incident(sys_id: str) -> dict[str, Any]:
    incident = state.servicenow_incidents.get(sys_id)
    if not incident:
        return {"result": {"sys_id": sys_id, "state": "1", "work_notes": []}}
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
