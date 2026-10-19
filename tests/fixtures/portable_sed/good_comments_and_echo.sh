#!/usr/bin/env bash
# Good: comments, echo and printf mentioning sed -i must not be flagged
# sed -i 's/foo/bar/' file.txt
echo "Do not use sed -i in scripts"
printf "Use sed \${sedi_args[@]} instead of sed -i\n"
echo sed -i is forbidden
