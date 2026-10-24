"""Entrypoint for ata-tools ASGI application."""

try:
    from tools.app.server import create_app
    from tools.app.config import get_tools_config
except ImportError:
    from app.server import create_app
    from app.config import get_tools_config

app = create_app()

if __name__ == "__main__":
    import uvicorn

    cfg = get_tools_config()
    uvicorn.run(app, host="0.0.0.0", port=cfg.port)
