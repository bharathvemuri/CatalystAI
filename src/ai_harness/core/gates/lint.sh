#!/bin/sh
# Lint gate.
set -eu
. "$(dirname "$0")/_lib.sh"

STACK=$(detect_stack)

case "$STACK" in
    node)
        has_npm_script lint || emit_skip "package.json declares no lint script"
        run_capture "$(node_runner)" run lint
        ;;
    python)
        if command -v ruff >/dev/null 2>&1; then run_capture ruff check .
        elif command -v flake8 >/dev/null 2>&1; then run_capture flake8 .
        elif command -v pyflakes >/dev/null 2>&1; then run_capture pyflakes .
        else emit_skip "no python linter available (ruff, flake8, pyflakes)"
        fi
        ;;
    go)     run_capture go vet ./... ;;
    rust)   run_capture cargo clippy -- -D warnings ;;
    java)
        if [ -f pom.xml ]; then run_capture mvn -B -q checkstyle:check
        else emit_skip "no gradle lint task configured"
        fi
        ;;
    *)      emit_skip "no recognised linter for this repository" ;;
esac

emit_from_exit "lint clean ($STACK)" "lint reported problems ($STACK)"
