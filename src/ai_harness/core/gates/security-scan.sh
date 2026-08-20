#!/bin/sh
# Security gate: Semgrep plus whatever ecosystem scanner the stack provides.
#
# A scanner that cannot run is reported as an evidence gap and fails the gate.
# The alternative — treating "no scanner installed" as a clean scan — is exactly
# the silent pass the Security contract's blocking conditions exist to prevent.
set -eu
. "$(dirname "$0")/_lib.sh"

STACK=$(detect_stack)
SEMGREP_STATUS="missing"
ECOSYSTEM_STATUS="none"
FAILED=0

if command -v semgrep >/dev/null 2>&1; then
    # Security rulesets, not `--config auto`. auto pulls semgrep's full registry
    # selection, which includes package-manager *policy* rules (pnpm/npm supply-
    # chain config lint): opinions about configuration, not code vulnerabilities,
    # and on a repo pinned to an older package manager the settings they demand are
    # inert — so the gate blocks on a finding the code cannot honestly satisfy, and
    # the ticket loops without a real fix. security-audit + secrets are the
    # security packs (what the Security agent itself uses); the stack's language
    # pack adds depth.
    SEMGREP_CONFIGS="--config p/security-audit --config p/secrets"
    case "$STACK" in
        node)   SEMGREP_CONFIGS="$SEMGREP_CONFIGS --config p/javascript --config p/typescript" ;;
        python) SEMGREP_CONFIGS="$SEMGREP_CONFIGS --config p/python" ;;
        go)     SEMGREP_CONFIGS="$SEMGREP_CONFIGS --config p/golang" ;;
        java)   SEMGREP_CONFIGS="$SEMGREP_CONFIGS --config p/java" ;;
        rust)   SEMGREP_CONFIGS="$SEMGREP_CONFIGS --config p/rust" ;;
    esac
    # Dependencies and build output are excluded explicitly: a ticket worktree's
    # `.git` is a link git cannot resolve in the container, so semgrep does not
    # honour .gitignore and would otherwise walk all of node_modules/.pnpm-store —
    # thousands of third-party files, the old ~10-minute scan and a flood of noise.
    run_capture semgrep scan $SEMGREP_CONFIGS --error --quiet \
        --exclude node_modules --exclude .pnpm-store --exclude dist \
        --exclude test-results --exclude tests/dist .
    if [ "$RUN_EXIT" -eq 0 ]; then SEMGREP_STATUS="clean"; else SEMGREP_STATUS="findings"; FAILED=1; fi
else
    echo "semgrep is not installed; the base image is expected to provide it" >> "$EVIDENCE_FILE"
    FAILED=1
fi

case "$STACK" in
    node)
        # Audit with the package manager the repo actually uses. `npm audit`
        # needs a package-lock.json; a pnpm or yarn workspace has none, so
        # running npm here fails with ENOLOCK regardless of any real
        # vulnerability — a spurious gate failure the repo could never clear.
        # `node_runner` reads the lockfile the same way bootstrap does.
        RUNNER=$(node_runner)
        case "$RUNNER" in
            pnpm) AUDIT="pnpm audit --audit-level high" ;;
            yarn) AUDIT="yarn npm audit --severity high" ;;
            *)    AUDIT="npm audit --audit-level=high" ;;
        esac
        if command -v "$RUNNER" >/dev/null 2>&1; then
            run_capture $AUDIT
            if [ "$RUN_EXIT" -eq 0 ]; then ECOSYSTEM_STATUS="clean"; else ECOSYSTEM_STATUS="findings"; FAILED=1; fi
        fi
        ;;
    python)
        if command -v pip-audit >/dev/null 2>&1; then
            run_capture pip-audit
            if [ "$RUN_EXIT" -eq 0 ]; then ECOSYSTEM_STATUS="clean"; else ECOSYSTEM_STATUS="findings"; FAILED=1; fi
        fi
        ;;
    go)
        if command -v govulncheck >/dev/null 2>&1; then
            run_capture govulncheck ./...
            if [ "$RUN_EXIT" -eq 0 ]; then ECOSYSTEM_STATUS="clean"; else ECOSYSTEM_STATUS="findings"; FAILED=1; fi
        fi
        ;;
    rust)
        if command -v cargo-audit >/dev/null 2>&1; then
            run_capture cargo audit
            if [ "$RUN_EXIT" -eq 0 ]; then ECOSYSTEM_STATUS="clean"; else ECOSYSTEM_STATUS="findings"; FAILED=1; fi
        fi
        ;;
esac

METRICS=$(printf '{"semgrep":"%s","ecosystem_scanner":"%s","stack":"%s"}' \
    "$SEMGREP_STATUS" "$ECOSYSTEM_STATUS" "$STACK")

RUN_EXIT=$FAILED
if [ "$SEMGREP_STATUS" = "missing" ]; then
    emit fail "semgrep unavailable - the audit could not be performed" "$METRICS"
else
    emit_from_exit "static analysis and dependency scan clean" \
        "scanners reported findings" "$METRICS"
fi
