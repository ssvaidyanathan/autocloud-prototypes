#!/usr/bin/env bash
# Bad: bare sed -i is not portable to macOS BSD sed
sed -i 's/foo/bar/' file.txt
