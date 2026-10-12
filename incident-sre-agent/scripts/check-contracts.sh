#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BRIDGE_CONTRACTS="${REPO_ROOT}/bridge/app/contracts"
AGENT_CONTRACTS="${REPO_ROOT}/agent/app/contracts"

if [ ! -d "${BRIDGE_CONTRACTS}" ]; then
    echo "ERROR: Bridge contracts directory not found: ${BRIDGE_CONTRACTS}" >&2
    exit 1
fi

if [ ! -d "${AGENT_CONTRACTS}" ]; then
    echo "ERROR: Agent contracts directory not found: ${AGENT_CONTRACTS}" >&2
    exit 1
fi

echo "Checking contract sync between bridge and agent..."
if diff -r -u -x '__pycache__' -x '*.pyc' "${BRIDGE_CONTRACTS}" "${AGENT_CONTRACTS}"; then
    echo "SUCCESS: Contracts are byte-identical."
    exit 0
else
    echo "ERROR: Contracts in bridge/app/contracts and agent/app/contracts have diverged!" >&2
    exit 1
fi
