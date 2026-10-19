#!/usr/bin/env bash
# Bad: sed -i.bak leaves leftover backup files
sed -i.bak 's/foo/bar/' file.txt
