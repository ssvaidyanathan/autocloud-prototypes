import logging
from typing import Optional

from fastapi import Depends, FastAPI

from .config import AgentConfig, get_settings
from .contracts.request import InvestigationRequest
from .contracts.result import InvestigationResult
from .dispatcher import InvestigationDispatcher

logger = logging.getLogger(__name__)

app = FastAPI(
    title="GitHub Actions Deployment Investigator Agent",
    description="Analytical engine diagnosing CI/CD workflow failure logs",
    version="0.1.0",
)

_dispatcher: Optional[InvestigationDispatcher] = None


def get_dispatcher(
    settings: AgentConfig = Depends(get_settings),
) -> InvestigationDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = InvestigationDispatcher(config=settings)
    return _dispatcher


def set_dispatcher(dispatcher: Optional[InvestigationDispatcher]) -> None:
    global _dispatcher
    _dispatcher = dispatcher


@app.get("/health")
def health(settings: AgentConfig = Depends(get_settings)) -> dict[str, str]:
    return {"status": "healthy", "service": settings.service_name}


@app.post("/investigate")
def handle_investigate(
    request: InvestigationRequest,
    dispatcher: InvestigationDispatcher = Depends(get_dispatcher),
) -> InvestigationResult:
    """Investigate workflow run failure and return or deliver report."""
    return dispatcher.investigate(request)
