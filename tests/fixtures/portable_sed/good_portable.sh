#!/usr/bin/env bash
set -euo pipefail

# Determine sed in-place arguments for portability (macOS vs Linux)
sedi_args=("-i")
if [[ "$(uname)" == "Darwin" ]]; then
  sedi_args=("-i" "") # For macOS, sed -i requires an extension argument. "" means no backup.
fi

sed "${sedi_args[@]}" 's/foo/bar/' file.txt
sed "${sedi_args[@]}" "s/hello/world/" file.txt
