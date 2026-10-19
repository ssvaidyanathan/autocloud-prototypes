#!/usr/bin/env bash
# Good: portable sed with wrappers, substitutions, conditionals, and line continuations
sedi_args=("-i")
if [[ "$(uname)" == "Darwin" ]]; then
  sedi_args=("-i" "")
fi

val=$(sed "${sedi_args[@]}" 's/foo/bar/' file.txt)
other=`sed "${sedi_args[@]}" 's/baz/qux/' file.txt`
(sed "${sedi_args[@]}" 's/foo/bar/' file.txt)
if sed "${sedi_args[@]}" 's/foo/bar/' file.txt; then echo ok; fi
true && sed "${sedi_args[@]}" 's/foo/bar/' file.txt
! sed "${sedi_args[@]}" 's/foo/bar/' file.txt
xargs sed "${sedi_args[@]}" 's/foo/bar/'
sudo sed "${sedi_args[@]}" 's/foo/bar/' file.txt
env sed "${sedi_args[@]}" 's/foo/bar/' file.txt
command sed "${sedi_args[@]}" 's/foo/bar/' file.txt
exec sed "${sedi_args[@]}" 's/foo/bar/' file.txt
gsed "${sedi_args[@]}" 's/foo/bar/' file.txt
/usr/bin/sed "${sedi_args[@]}" 's/foo/bar/' file.txt
sed \
  "${sedi_args[@]}" \
  's/foo/bar/' file.txt
