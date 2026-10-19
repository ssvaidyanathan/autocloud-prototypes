#!/usr/bin/env bash
# Bad: sed -i '' is not portable to GNU/Linux sed
sed -i '' 's/foo/bar/' file.txt
