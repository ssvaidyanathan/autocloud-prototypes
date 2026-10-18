import os
import subprocess
import sys
from pathlib import Path


def test_container_import_without_agent_top_level_module() -> None:
    """Verify that `import app.main` loads cleanly without requiring an `agent.` top-level module,
    matching the container environment where app/ is placed at /app/app with PYTHONPATH=/app.
    """
    agent_dir = Path(__file__).resolve().parent.parent

    # Emulate container environment: PYTHONPATH=/app, CWD=/app, no parent repo in sys.path
    code = (
        "import sys\n"
        "import app.main\n"
        "assert 'agent' not in sys.modules, 'agent module should not be imported'\n"
        "assert app.main.app is not None, 'FastAPI app instance should exist'\n"
        "print('CONTAINER_IMPORT_OK')\n"
    )

    env = {
        "PYTHONPATH": str(agent_dir),
        "PATH": os.environ.get("PATH", ""),
        "APP_ENV": "test",
        "ENVIRONMENT": "test",
    }
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(agent_dir),
    )

    msg = f"Subprocess import failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
    assert res.returncode == 0, msg
    assert "CONTAINER_IMPORT_OK" in res.stdout


def test_runtime_dependencies_import() -> None:
    """Verify all container runtime dependencies can be imported cleanly."""
    import cryptography
    import google.adk
    import jwt
    from agent.app.github.auth import GitHubAppAuth
    from agent.app.skills_loader import SkillsLoader

    assert jwt is not None
    assert cryptography is not None
    assert google.adk is not None
    assert GitHubAppAuth is not None
    assert SkillsLoader is not None

