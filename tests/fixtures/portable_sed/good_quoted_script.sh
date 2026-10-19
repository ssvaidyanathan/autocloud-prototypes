#!/usr/bin/env bash
# Good: sed script or argument contains -i inside quotes (not in-place sed)
sed 's/-i/replacement/' file.txt
sed "s/-i/replacement/" file.txt
sed -e 's/-i/x/' file.txt
