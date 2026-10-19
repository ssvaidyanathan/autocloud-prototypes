#!/usr/bin/env bash
# Bad: uses sed "${sedi_args[@]}" but omitted the uname Darwin branch!
sedi_args=("-i")
sed "${sedi_args[@]}" 's/foo/bar/' file.txt
