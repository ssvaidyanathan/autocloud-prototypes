#!/usr/bin/env bash
# Bad: sed -i behind command wrappers
xargs sed -i 's/foo/bar/'
sudo sed -i 's/foo/bar/' file.txt
env sed -i 's/foo/bar/' file.txt
command sed -i 's/foo/bar/' file.txt
exec sed -i 's/foo/bar/' file.txt
