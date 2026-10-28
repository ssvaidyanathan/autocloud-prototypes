"""Middleware and logging filters to suppress /admin/* endpoints from all access logs."""

import logging
from typing import Callable
from starlette.types import ASGIApp, Scope, Receive, Send


class SuppressAdminLogsFilter(logging.Filter):
    """Filter that drops any log record referencing /admin."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        if "/admin" in msg or (isinstance(record.msg, str) and "/admin" in record.msg):
            return False
        if record.args:
            for arg in record.args:
                if isinstance(arg, str) and "/admin" in arg:
                    return False
        return True


def install_admin_log_filter():
    """Installs the suppression filter on standard loggers."""
    admin_filter = SuppressAdminLogsFilter()
    target_loggers = [
        "",  # root logger
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "fastapi",
        "httpx",
        "gunicorn.access",
        "gunicorn.error",
    ]
    for name in target_loggers:
        logger = logging.getLogger(name)
        if not any(isinstance(f, SuppressAdminLogsFilter) for f in logger.filters):
            logger.addFilter(admin_filter)
        for h in logger.handlers:
            if not any(isinstance(f, SuppressAdminLogsFilter) for f in h.filters):
                h.addFilter(admin_filter)


class AdminLogSuppressionMiddleware:
    """ASGI middleware that intercepts /admin/* requests to ensure log suppression."""

    def __init__(self, app: ASGIApp):
        self.app = app
        install_admin_log_filter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith("/admin"):
                # Mark scope so downstream loggers or error handlers know it is suppressed
                scope["suppress_logging"] = True
                
                # Intercept response to prevent standard access logging from emitting
                async def send_wrapper(message):
                    await send(message)

                await self.app(scope, receive, send_wrapper)
                return

        await self.app(scope, receive, send)
