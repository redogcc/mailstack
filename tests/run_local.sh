#!/usr/bin/env bash
# Local, dependency-free checks: shell syntax, python compilation, unit tests,
# and template-token coverage. No server, no network.
set -Eeuo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

fail=0

echo "== shell syntax =="
for script in $(find . -name '*.sh' -not -path './.git/*' | sort) bin/mailctl install.sh uninstall.sh; do
  if bash -n "$script"; then
    echo "  ok  $script"
  else
    echo "  FAIL $script"; fail=1
  fi
done

echo
echo "== python compile =="
if python3 -m py_compile admin/mailadmin.py scripts/*.py tests/*.py; then
  echo "  ok  all python files"
else
  echo "  FAIL python compilation"; fail=1
fi

echo
echo "== unit tests =="
if python3 tests/test_units.py; then
  echo "  ok  unit tests"
else
  echo "  FAIL unit tests"; fail=1
fi

echo
echo "== template token coverage =="
if python3 tests/check_tokens.py; then
  : # script prints its own result
else
  fail=1
fi

echo
if [[ $fail -eq 0 ]]; then
  echo "ALL LOCAL CHECKS PASSED"
else
  echo "SOME CHECKS FAILED"
fi
exit $fail
