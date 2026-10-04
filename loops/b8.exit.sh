#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:?set BASE_REF to merged full b8 spec SHA}"
[[ "$BASE" =~ ^[0-9a-f]{40}$ ]] || { echo '[b8] FAIL: full BASE_REF required'; exit 1; }
git merge-base --is-ancestor "$BASE" HEAD
python3 loops/b8.verify.py "$BASE"
