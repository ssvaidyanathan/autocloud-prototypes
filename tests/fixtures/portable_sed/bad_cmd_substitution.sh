#!/usr/bin/env bash
# Bad: sed -i in command substitution $(...) and backticks
val=$(sed -i 's/foo/bar/' file.txt)
other=`sed -i 's/baz/qux/' file.txt`
