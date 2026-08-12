#!/bin/sh
# Test gate. Emits real counts where the runner reports them, because
# "Tests executed: 184 / Passed: 184" is evidence and "tests pass" is a claim.
set -eu
. "$(dirname "$0")/_lib.sh"

STACK=$(detect_stack)

case "$STACK" in
    node)
        has_npm_script test || emit_skip "package.json declares no test script"
        run_capture "$(node_runner)" test
        ;;
    python)
        if command -v pytest >/dev/null 2>&1; then run_capture pytest -q
        else run_capture python -m unittest discover -q
        fi
        ;;
    go)     run_capture go test ./... ;;
    rust)   run_capture cargo test --locked ;;
    java)
        if [ -f pom.xml ]; then run_capture mvn -B -q test
        else run_capture ./gradlew --quiet test
        fi
        ;;
    *)      emit_skip "no recognised test runner in this repository" ;;
esac

# Counts are scraped best-effort; a runner that does not report them yields
# nulls rather than invented numbers.
PASSED=$(grep -oE '[0-9]+ passed' "$EVIDENCE_FILE" | tail -1 | grep -oE '[0-9]+' || true)
FAILED=$(grep -oE '[0-9]+ (failed|failures)' "$EVIDENCE_FILE" | tail -1 | grep -oE '[0-9]+' || true)
METRICS=$(printf '{"passed":%s,"failed":%s}' "${PASSED:-null}" "${FAILED:-null}")

emit_from_exit "tests passed ($STACK)" "tests failed ($STACK)" "$METRICS"
