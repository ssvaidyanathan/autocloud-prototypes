#!/usr/bin/env bash
# Bad: gsed and explicit paths ending in sed
gsed -i 's/foo/bar/' file.txt
/usr/bin/sed -i 's/foo/bar/' file.txt
./sed -i 's/foo/bar/' file.txt
