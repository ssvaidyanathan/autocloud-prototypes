#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -d "${SCRIPT_DIR}/sample-pipeline-app" ]]; then
    SOURCE_DIR="${SCRIPT_DIR}/sample-pipeline-app"
else
    SOURCE_DIR="${SCRIPT_DIR}"
fi

TARGET_DIR="${1:-/tmp/gdi-sample-pipeline-app}"

echo "=================================================="
echo "Seeding Standalone Demo Git Repository"
echo "Source: ${SOURCE_DIR}"
echo "Target: ${TARGET_DIR}"
echo "=================================================="

# Create target directory
mkdir -p "${TARGET_DIR}"

# Copy all source files excluding caches, backups, and temporary artifacts
rsync -av \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.bak' \
    --exclude='*.orig' \
    --exclude='*.egg-info' \
    "${SOURCE_DIR}/" "${TARGET_DIR}/"

# Initialize standalone git repository
cd "${TARGET_DIR}"
if [[ ! -d ".git" ]]; then
    git init -b main
    echo "Initialized empty Git repository in ${TARGET_DIR}"
fi

# Set local git committer details if none exist
if ! git config user.name >/dev/null 2>&1; then
    git config user.name "Autocloud Deploy Investigator"
fi
if ! git config user.email >/dev/null 2>&1; then
    git config user.email "autocloud@example.com"
fi

git add .
if git diff --staged --quiet; then
    echo "No changes to commit. Repository is already clean."
else
    git commit -m "Initial commit: sample pipeline app pristine state"
    echo "Committed initial pristine demo app state."
fi

echo ""
echo "=================================================="
echo "SUCCESS: Standalone demo repository seeded at:"
echo "  ${TARGET_DIR}"
echo ""
echo "To publish this repository for Phase 5 live testing:"
echo "  cd ${TARGET_DIR}"
echo "  git remote add origin https://github.com/ssvaidyanathan/gdi-sample-pipeline-app.git"
echo "  git branch -M main"
echo "  git push -u origin main"
echo "=================================================="
