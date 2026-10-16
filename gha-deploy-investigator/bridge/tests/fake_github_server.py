import base64
import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response, status

app = FastAPI(
    title="Fake GitHub Server",
    description="Simulates GitHub REST API for Actions, Check Runs, Contents, and Comments",
)


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    query_params: dict[str, str]
    body: Any


@dataclass
class FakeGithubServerState:
    jobs: dict[int, dict[str, Any]] = field(default_factory=dict)
    logs: dict[int, str] = field(default_factory=dict)
    check_runs: list[dict[str, Any]] = field(default_factory=list)
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)
    contents: dict[str, str] = field(default_factory=dict)
    comments: list[dict[str, Any]] = field(default_factory=list)
    recorded_requests: list[RecordedRequest] = field(default_factory=list)

    def reset(self) -> None:
        self.jobs.clear()
        self.logs.clear()
        self.check_runs.clear()
        self.commits.clear()
        self.contents.clear()
        self.comments.clear()
        self.recorded_requests.clear()


state = FakeGithubServerState()


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
    return {"status": "ok", "service": "fake-github-server"}


@app.post("/test/reset")
def reset_state() -> dict[str, str]:
    state.reset()
    return {"status": "reset"}


@app.post("/app/installations/{installation_id}/access_tokens", status_code=status.HTTP_201_CREATED)
def post_installation_access_token(installation_id: int) -> dict[str, Any]:
    return {
        "token": f"ghs_fake_token_for_inst_{installation_id}",
        "expires_at": "2026-10-17T00:00:00Z",
        "permissions": {"actions": "read", "contents": "read", "checks": "write"},
    }


@app.get("/repos/{owner}/{repo}/actions/runs/{run_id}/jobs")
def get_run_jobs(owner: str, repo: str, run_id: int) -> dict[str, Any]:
    if run_id in state.jobs:
        return state.jobs[run_id]

    default_jobs = {
        "total_count": 1,
        "jobs": [
            {
                "id": 101,
                "run_id": run_id,
                "name": "lint",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "2026-10-16T21:00:00Z",
                "completed_at": "2026-10-16T21:01:00Z",
                "steps": [
                    {
                        "name": "Set up Python",
                        "status": "completed",
                        "conclusion": "success",
                        "number": 1,
                    },
                    {
                        "name": "Run Ruff Lint",
                        "status": "completed",
                        "conclusion": "failure",
                        "number": 2,
                    },
                ],
            }
        ],
    }
    return default_jobs


@app.get("/repos/{owner}/{repo}/actions/jobs/{job_id}/logs")
def get_job_logs(owner: str, repo: str, job_id: int) -> Response:
    if job_id in state.logs:
        return Response(content=state.logs[job_id], media_type="text/plain")

    default_log = (
        "2026-10-16T21:00:10.0000000Z Run ruff check .\n"
        "2026-10-16T21:00:11.0000000Z app/main.py:42:5: F841 "
        "Local variable `unused_token` is assigned to but never used\n"
        "2026-10-16T21:00:11.0000000Z Found 1 error.\n"
        "2026-10-16T21:00:11.0000000Z Process completed with exit code 1.\n"
    )
    return Response(content=default_log, media_type="text/plain")


@app.get("/repos/{owner}/{repo}/commits/{head_sha}")
def get_commit(owner: str, repo: str, head_sha: str) -> dict[str, Any]:
    if head_sha in state.commits:
        return state.commits[head_sha]

    return {
        "sha": head_sha,
        "commit": {
            "message": "Update microservice implementation",
            "author": {"name": "Octocat", "date": "2026-10-16T21:00:00Z"},
        },
        "files": [
            {
                "filename": "app/main.py",
                "status": "modified",
                "additions": 5,
                "deletions": 2,
                "patch": "@@ -40,4 +40,5 @@\n+    unused_token = 'secret'\n",
            }
        ],
    }


@app.get("/repos/{owner}/{repo}/contents/{path:path}")
def get_contents(owner: str, repo: str, path: str, ref: str = "main") -> dict[str, Any]:
    key = f"{path}@{ref}"
    raw = state.contents.get(key) or state.contents.get(path)
    if raw is None:
        raw = f"# Content for {path} at {ref}\n"

    encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    return {
        "name": path.split("/")[-1],
        "path": path,
        "sha": "fakecontenthash123",
        "size": len(raw),
        "encoding": "base64",
        "content": encoded,
    }


@app.post("/repos/{owner}/{repo}/check-runs", status_code=status.HTTP_201_CREATED)
async def post_check_run(owner: str, repo: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    record = {
        "id": len(state.check_runs) + 100,
        "owner": owner,
        "repo": repo,
        **body,
    }
    state.check_runs.append(record)
    return record


@app.post(
    "/repos/{owner}/{repo}/issues/{issue_number}/comments",
    status_code=status.HTTP_201_CREATED,
)
async def post_issue_comment(
    owner: str, repo: str, issue_number: int, request: Request
) -> dict[str, Any]:
    body = await request.json()
    record = {
        "id": len(state.comments) + 100,
        "owner": owner,
        "repo": repo,
        "issue_number": issue_number,
        **body,
    }
    state.comments.append(record)
    return record


@app.post(
    "/repos/{owner}/{repo}/commits/{commit_sha}/comments",
    status_code=status.HTTP_201_CREATED,
)
async def post_commit_comment(
    owner: str, repo: str, commit_sha: str, request: Request
) -> dict[str, Any]:
    body = await request.json()
    record = {
        "id": len(state.comments) + 100,
        "owner": owner,
        "repo": repo,
        "commit_sha": commit_sha,
        **body,
    }
    state.comments.append(record)
    return record
