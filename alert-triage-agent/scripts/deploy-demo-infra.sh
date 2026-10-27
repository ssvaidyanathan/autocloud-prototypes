#!/usr/bin/env bash
# ==============================================================================
# Compatibility entrypoint: the demo infrastructure part of the from-scratch deploy.
#
# Usage: scripts/deploy-demo-infra.sh [--yes]
#
# scripts/deploy-stage1.sh is the single deploy entrypoint. This script only runs its phases
# from "database" on (database, schema, applications, inventory-pin, triage, triage-iam,
# apigee, observability, verify), in the corrected order:
#   scripts/deploy-stage1.sh --from database [--yes]
# The earlier phases (preflight, bootstrap, foundation, secrets, images) must have run; the
# image refs come from the images phase (or exported INVENTORY_IMAGE, ORDERS_IMAGE,
# AGENT_IMAGE, BRIDGE_IMAGE, TOOLS_IMAGE). Required inputs: as for deploy-stage1.sh.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for arg in "$@"; do
    case "${arg}" in
        --yes|-y) ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "Unknown argument: ${arg} (use --yes)" >&2; exit 1 ;;
    esac
done

echo "deploy-demo-infra.sh: running scripts/deploy-stage1.sh --from database $*"
exec bash "${SCRIPT_DIR}/deploy-stage1.sh" --from database "$@"
