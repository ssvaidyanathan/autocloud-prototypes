#!/usr/bin/env bash
# Bad: sed --in-place is a GNU-only long option
sed --in-place 's/foo/bar/' file.txt
