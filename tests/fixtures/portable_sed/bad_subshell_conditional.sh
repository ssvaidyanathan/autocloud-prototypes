#!/usr/bin/env bash
# Bad: sed -i inside subshells and conditionals
(sed -i 's/foo/bar/' file.txt)
if sed -i 's/foo/bar/' file.txt; then echo ok; fi
true && sed -i 's/foo/bar/' file.txt
! sed -i 's/foo/bar/' file.txt
