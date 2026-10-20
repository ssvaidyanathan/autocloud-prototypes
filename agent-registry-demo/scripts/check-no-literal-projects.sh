#!/usr/bin/env bash
set -euo pipefail

# Guard script to ensure no forbidden environment-specific identifiers or un-externalized Gemini model literals
# exist in repository files.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

echo "========================================================"
echo "Checking for forbidden literal identifiers in repo..."
echo "========================================================"

DEFAULT_FORBIDDEN="cloud-assist-fde-7"
SCAN_LITERALS="${FORBIDDEN_LITERALS:-$DEFAULT_FORBIDDEN}"

if [ -z "${SCAN_LITERALS}" ]; then
    echo "[NOTICE] FORBIDDEN_LITERALS is unset; skipping environment-specific identifier scan."
else
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        LITERAL_MATCHES=$(git grep -E "${SCAN_LITERALS}" -- ':!scripts/check-no-literal-projects.sh' || true)
    else
        LITERAL_MATCHES=$(grep -rnE "${SCAN_LITERALS}" . \
            --exclude="check-no-literal-projects.sh" \
            --exclude-dir=".git" \
            --exclude-dir=".venv" \
            --exclude-dir="__pycache__" \
            --exclude-dir=".pytest_cache" \
            --exclude-dir=".ruff_cache" \
            --exclude-dir=".mypy_cache" \
            --exclude-dir=".terraform" || true)
    fi

    if [ -n "${LITERAL_MATCHES}" ]; then
        echo "ERROR: Forbidden literal identifier found in files:" >&2
        echo "${LITERAL_MATCHES}" >&2
        echo "Sanitize with generic placeholders (e.g. 'example-project-id', 'your-project-id', 'xyz123') or make configurable." >&2
        exit 1
    fi
    echo "[PASS] Zero forbidden literal identifiers found."
fi

echo "========================================================"
echo "Checking for un-externalized Gemini model literals in code..."
echo "========================================================"

# Check Python source files, excluding config.py, test files, examples, and documentation.
# Model names must only appear in config.py, .env.example, tfvars examples, and documentation.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GEMINI_MATCHES=$(git grep -E "gemini-[0-9]" -- \
        ':!*config.py' \
        ':!*.env.example' \
        ':!*tfvars.example' \
        ':!*.tf' \
        ':!docs/*' \
        ':!README*' \
        ':!docker-compose.yml' \
        ':!*tests/*' \
        || true)
else
    GEMINI_MATCHES=$(grep -rnE "gemini-[0-9]" . \
        --exclude="*config.py" \
        --exclude="*.env.example" \
        --exclude="*tfvars.example" \
        --exclude="*.tf" \
        --exclude="README*" \
        --exclude="docker-compose.yml" \
        --exclude-dir="tests" \
        --exclude-dir="docs" \
        --exclude-dir=".git" \
        --exclude-dir=".venv" \
        --exclude-dir="__pycache__" \
        --exclude-dir=".pytest_cache" \
        --exclude-dir=".ruff_cache" \
        --exclude-dir=".mypy_cache" \
        --exclude-dir=".terraform" || true)
fi

if [ -n "${GEMINI_MATCHES}" ]; then
    echo "ERROR: Forbidden Gemini model literal found in code:" >&2
    echo "${GEMINI_MATCHES}" >&2
    echo "Model names must only appear as defaults in config.py or configuration examples." >&2
    exit 1
fi

echo "[PASS] Zero un-externalized Gemini model literals found."

echo "SUCCESS: All literal and model checks passed cleanly."
