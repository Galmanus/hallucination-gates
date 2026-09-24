#!/usr/bin/env bash
# Run every gate's offline selftest. Exit non-zero if any gate fails.
# No dependencies beyond the Python standard library.
set -u

modules=(grounding selfcheck corroborate claim repro)
fail=0

for m in "${modules[@]}"; do
  echo "=== ${m}.py --selftest ==="
  if python3 "${m}.py" --selftest; then
    :
  else
    echo "FAILED: ${m}.py"
    fail=1
  fi
  echo
done

if [ "${fail}" -ne 0 ]; then
  echo "one or more gate selftests FAILED"
  exit 1
fi
echo "all gate selftests passed"
