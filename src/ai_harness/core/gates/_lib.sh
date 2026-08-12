#!/bin/sh
# Shared gate helpers.
#
# A gate is a script, not a prompt. It runs a real command, captures the real
# output, and emits one JSON object on stdout describing what happened. Nothing
# here asks a model whether something passed.
#
# Contract: stdout is exactly one JSON object matching contracts/gate-result.schema.json.
# Everything else goes to the evidence file or stderr, so a chatty build tool can
# never corrupt the machine-readable result.

set -eu

: "${HARNESS_REPORT_DIR:?HARNESS_REPORT_DIR must be set to .harness/reports/<id>}"
: "${HARNESS_GATE:?HARNESS_GATE must be set}"

mkdir -p "$HARNESS_REPORT_DIR/evidence"
EVIDENCE_FILE="$HARNESS_REPORT_DIR/evidence/$HARNESS_GATE.log"
: > "$EVIDENCE_FILE"

# JSON string escaping in POSIX sh: backslash, quote, then control characters.
json_escape() {
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' \
        -e 's/\r/\\r/g' -e 's/\t/\\t/g' \
        | awk 'BEGIN { ORS="" } { if (NR > 1) print "\\n"; print }'
}

# detect_stack -> node | python | go | rust | java | unknown
detect_stack() {
    if [ -f package.json ]; then echo node
    elif [ -f pyproject.toml ] || [ -f requirements.txt ] || [ -f setup.py ]; then echo python
    elif [ -f go.mod ]; then echo go
    elif [ -f Cargo.toml ]; then echo rust
    elif [ -f pom.xml ] || [ -f build.gradle ] || [ -f build.gradle.kts ]; then echo java
    else echo unknown
    fi
}

node_runner() {
    if [ -f pnpm-lock.yaml ]; then echo pnpm
    elif [ -f yarn.lock ]; then echo yarn
    else echo npm
    fi
}

# has_npm_script <name> — true when package.json declares that script.
has_npm_script() {
    [ -f package.json ] || return 1
    node -e "process.exit(require('./package.json').scripts?.['$1'] ? 0 : 1)" 2>/dev/null
}

# run_capture <command...> — run it, tee to the evidence file, remember the code.
# Never aborts the script: a failing command is a gate result, not a crash.
RUN_EXIT=0
RUN_COMMAND=""
run_capture() {
    RUN_COMMAND="$*"
    {
        echo "\$ $RUN_COMMAND"
        echo "--- started $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"
    } >> "$EVIDENCE_FILE"
    set +e
    "$@" >> "$EVIDENCE_FILE" 2>&1
    RUN_EXIT=$?
    set -e
    echo "--- exit $RUN_EXIT ---" >> "$EVIDENCE_FILE"
    return 0
}

# emit <status> <summary> [metrics_json]
# status: pass | fail | skip. skip means "not applicable here", and a skipped
# gate is reported as skipped rather than quietly counted as a pass.
emit() {
    _status="$1"
    _summary=$(printf '%s' "$2" | json_escape)
    _metrics="${3:-{\}}"
    _command=$(printf '%s' "$RUN_COMMAND" | json_escape)
    _tail=$(tail -c 4000 "$EVIDENCE_FILE" 2>/dev/null | json_escape)

    printf '{"gate":"%s","status":"%s","command":"%s","exit_code":%d,' \
        "$HARNESS_GATE" "$_status" "$_command" "$RUN_EXIT"
    printf '"summary":"%s","evidence_file":"%s","evidence_tail":"%s","metrics":%s}\n' \
        "$_summary" "$EVIDENCE_FILE" "$_tail" "$_metrics"
}

# emit_skip <reason> — no runner for this stack, or nothing to do.
emit_skip() {
    RUN_EXIT=0
    emit skip "$1"
    exit 0
}

# emit_from_exit <summary-pass> <summary-fail> [metrics]
emit_from_exit() {
    if [ "$RUN_EXIT" -eq 0 ]; then
        emit pass "$1" "${3:-{\}}"
    else
        emit fail "$2" "${3:-{\}}"
    fi
}
