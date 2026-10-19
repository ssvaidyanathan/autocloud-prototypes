#!/usr/bin/env bash
set -euo pipefail

# Read-only sed operations should never be flagged
sed -n 's/pattern/replacement/p' input.txt
sed '$d' input.txt
echo "test" | sed 's/test/result/'
sed -e 's/a/b/' -e 's/c/d/' input.txt
sed -E 's/([0-9]+)/\1/g' input.txt
