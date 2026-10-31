import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Query, Response, status
from fastapi.responses import JSONResponse

from .callback import CallbackClient
from .config import get_settings
from .contracts.request import InvestigationRequest
from .contracts.result import InvestigationResult
from .dispatcher import Dispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager enforcing production fail-fast startup checks."""
    # Enforce fail-fast startup check: agent.yaml must exist and validate
    from .agent_config import get_agent_yaml_config

    get_agent_yaml_config()

    settings = get_settings()
    app_env = os.getenv("APP_ENV", settings.app_env).strip().lower()
    keys_added = []
    if app_env != "local":
        if settings.gcp_project:
            for k, v in [
                ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
                ("GOOGLE_GENAI_USE_ENTERPRISE", "true"),
                ("GOOGLE_CLOUD_PROJECT", settings.gcp_project),
                ("GOOGLE_CLOUD_LOCATION", settings.vertex_location),
            ]:
                if k not in os.environ:
                    os.environ[k] = v
                    keys_added.append(k)

        try:
            import google.genai

            test_client = google.genai.Client()
            if not getattr(test_client, "vertexai", False):
                raise RuntimeError("google.genai.Client did not initialize in Vertex AI mode")
        except Exception as exc:
            for k in keys_added:
                os.environ.pop(k, None)
            raise RuntimeError(
                "Production fail-fast: failed to initialize Vertex AI Gemini client "
                f"at startup: {exc}"
            ) from exc

    try:
        yield
    finally:
        for k in keys_added:
            os.environ.pop(k, None)


app = FastAPI(
    title="SRE Agent",
    description="Multi-agent SRE investigation service",
    version="0.1.0",
    lifespan=lifespan,
)

# Global or injectable dispatcher & callback client
_dispatcher: Optional[Dispatcher] = None
_callback_client: Optional[CallbackClient] = None


def get_dispatcher() -> Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher(config=get_settings())
    return _dispatcher


def get_callback_client() -> CallbackClient:
    global _callback_client
    if _callback_client is None:
        _callback_client = CallbackClient()
    return _callback_client


def set_dispatcher(dispatcher: Optional[Dispatcher]) -> None:
    global _dispatcher
    _dispatcher = dispatcher


def set_callback_client(client: Optional[CallbackClient]) -> None:
    global _callback_client
    _callback_client = client


async def _run_and_callback(
    request: InvestigationRequest,
    inv_id: str,
    dispatcher: Dispatcher,
    callback_client: CallbackClient,
) -> InvestigationResult:
    """Execute investigation and deliver result to callback URL."""
    try:
        result = await dispatcher.investigate(request, investigation_id=inv_id)
    except Exception as exc:
        logger.exception("Dispatcher raised unexpected exception for %s", inv_id)
        result = InvestigationResult(
            investigation_id=inv_id,
            incident_id=request.incident.id,
            incident_source=request.incident.source,
            incident_external_id=request.incident.external_id,
            incident_external_key=request.incident.external_key,
            status="failed",
            error=f"Investigation workflow failed: {str(exc)}",
        )

    try:
        await callback_client.deliver_async(request.callback_url, result)
    except Exception as cb_exc:
        logger.exception("Failed to deliver callback to %s: %s", request.callback_url, cb_exc)

    return result


@app.get("/health")
def health() -> dict[str, str]:
    """Health check probe."""
    return {"status": "healthy", "service": get_settings().service_name}


@app.post("/investigate", status_code=status.HTTP_200_OK, response_model=None)
async def investigate(
    request: InvestigationRequest,
    sync: bool = Query(default=True, description="Synchronous execution is default and only path"),
) -> Response:
    """Accept an investigation request and execute investigation lifecycle synchronously.

    Per DESIGN.md 3.1 & 3.3, Cloud Tasks requests are executed synchronously within the
    15-minute dispatch deadline (max_attempts = 2).
    """
    inv_id = f"inv-{uuid.uuid4().hex[:8]}"
    dispatcher = get_dispatcher()
    callback_client = get_callback_client()

    result = await _run_and_callback(request, inv_id, dispatcher, callback_client)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "completed",
            "investigation_id": inv_id,
            "result_status": result.status,
        },
    )


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port)
