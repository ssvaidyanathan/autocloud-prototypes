import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# Load check-no-demo-log-hints.py module dynamically
SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "check-no-demo-log-hints.py"
)
spec = importlib.util.spec_from_file_location("check_no_demo_log_hints", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
guard_module: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard_module)


def test_shop_app_passes_ast_guard() -> None:
    """Verify demo/shop-api/shop_app.py has zero demo log hints."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    shop_app_path = repo_root / "demo" / "shop-api" / "shop_app.py"
    assert shop_app_path.exists(), f"File not found: {shop_app_path}"

    violations = guard_module.check_file_ast(shop_app_path)
    assert violations == [], f"Found forbidden demo log hints: {violations}"


@pytest.mark.parametrize(
    "hint,log_snippet",
    [
        (
            "scenario",
            'logger.warning("Scenario 2 (Latency Spike): Injecting 5s delay")',
        ),
        (
            "scenario",
            'logger.error("Scenario 3 (Bad Release): Missing PAYMENT_GATEWAY_KEY")',
        ),
        (
            "scenario",
            'logger.error("Scenario 5 (Dependency Failure): 403 Forbidden")',
        ),
        ("inject", 'logger.warning("FAULT INJECTED: %s", details)'),
        ("inject", 'logger.warning("Injecting artificial delay")'),
        (
            "leak",
            (
                'logger.warning("Scenario 4 (Memory Leak): Allocated 10MB chunk. '
                'Total leaked memory: %.2fMB")'
            ),
        ),
        ("leak", 'logger.error("Detected memory leak in worker pool")'),
        (
            "omitting",
            (
                'logger.warning("Scenario 5 (Dependency Failure): '
                'Omitting IAM invoker token for inventory-svc")'
            ),
        ),
        ("omitting", 'logger.warning("Omitting credentials for request")'),
    ],
)
def test_ast_check_detects_forbidden_hints_in_strings(hint: str, log_snippet: str) -> None:
    """Verify AST check catches forbidden substrings in string literals."""
    code = f"""
import logging
logger = logging.getLogger("test")
def foo():
    {log_snippet}
"""
    violations = guard_module.check_source_ast(code)
    assert len(violations) >= 1
    found_hints = [v[1].lower() for v in violations]
    assert any(hint in msg for msg in found_hints)


def test_ast_check_detects_fstrings_and_variables() -> None:
    """Verify AST check catches f-strings and variable references containing hints."""
    code_fstring = """
import logging
logger = logging.getLogger("test")
delay = 5.0
logger.info(f"Injecting {delay}s delay")
"""
    violations_fstring = guard_module.check_source_ast(code_fstring)
    assert len(violations_fstring) >= 1
    assert any("inject" in v[1].lower() for v in violations_fstring)

    code_var = """
import logging
logger = logging.getLogger("test")
leak_amount = "10MB"
logger.info("Current total: %s", leak_amount)
"""
    violations_var = guard_module.check_source_ast(code_var)
    assert len(violations_var) >= 1
    assert any("leak" in v[1].lower() for v in violations_var)


def test_clean_production_logs_pass() -> None:
    """Verify realistic operational telemetry logs produce zero violations."""
    code = """
import logging
logger = logging.getLogger("shop-api")

def handle_request():
    logger.info("Admin configuration updated")
    logger.info("Admin configuration reset")
    logger.error("Missing required environment variable: PAYMENT_GATEWAY_KEY")
    logger.error("Cannot process checkout; missing PAYMENT_GATEWAY_KEY")
    logger.error("Downstream dependency inventory-svc returned HTTP 403 Forbidden")
    logger.warning("inventory-svc returned HTTP %d: %s", 500, "error")
    logger.warning("Could not reach inventory-svc at %s: %s", "http://svc", "timeout")
    logger.error("%s\\n%s", "Connection failed", "traceback here")
    logger.info("Checkout successful: order_id=%s, total=$%.2f", "ord-123", 49.99)
"""
    violations = guard_module.check_source_ast(code)
    assert violations == []


def test_cli_execution_clean(tmp_path: Path) -> None:
    """Verify running the guard script via CLI succeeds on clean code and fails on violation."""
    # 1. Clean code
    clean_file = tmp_path / "clean_app.py"
    clean_file.write_text(
        'import logging\nlogger = logging.getLogger("t")\nlogger.info("Operational event")\n',
        encoding="utf-8",
    )
    result_clean = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(clean_file)],
        capture_output=True,
        text=True,
    )
    assert result_clean.returncode == 0
    assert "Zero demo log hints found" in result_clean.stdout

    # 2. Dirty code
    dirty_file = tmp_path / "dirty_app.py"
    dirty_code = (
        "import logging\n"
        'logger = logging.getLogger("t")\n'
        'logger.warning("Scenario 2: Injecting delay")\n'
    )
    dirty_file.write_text(dirty_code, encoding="utf-8")
    result_dirty = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(dirty_file)],
        capture_output=True,
        text=True,
    )
    assert result_dirty.returncode == 1
    assert "Forbidden demo log hints found" in result_dirty.stderr
