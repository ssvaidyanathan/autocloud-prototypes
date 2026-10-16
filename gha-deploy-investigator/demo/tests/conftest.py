import importlib.util
import sys
from pathlib import Path
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

DEMO_APP_PATH = Path(__file__).resolve().parent.parent / "sample-pipeline-app"


def load_demo_app() -> FastAPI:
    """Dynamically load sample-pipeline-app under isolated module namespace."""
    app_dir = DEMO_APP_PATH / "app"
    cfg_spec = importlib.util.spec_from_file_location(
        "sample_pipeline_app.config", app_dir / "config.py"
    )
    if cfg_spec is None or cfg_spec.loader is None:
        raise RuntimeError("Failed to locate sample_pipeline_app.config")
    cfg_mod = importlib.util.module_from_spec(cfg_spec)
    sys.modules["sample_pipeline_app.config"] = cfg_mod
    cfg_spec.loader.exec_module(cfg_mod)

    main_spec = importlib.util.spec_from_file_location(
        "sample_pipeline_app.main", app_dir / "main.py"
    )
    if main_spec is None or main_spec.loader is None:
        raise RuntimeError("Failed to locate sample_pipeline_app.main")
    main_mod = importlib.util.module_from_spec(main_spec)
    sys.modules["sample_pipeline_app.main"] = main_mod
    main_spec.loader.exec_module(main_mod)

    return main_mod.app


@pytest.fixture
def demo_client() -> Generator[TestClient, None, None]:
    """TestClient fixture for sample-pipeline-app."""
    app = load_demo_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def demo_app_path() -> Path:
    """Path to sample-pipeline-app directory."""
    return DEMO_APP_PATH
