#!/bin/sh
# Build gate. "The repository must actually build" is the Developer contract's
# hard rule; this is what makes it enforceable.
set -eu
. "$(dirname "$0")/_lib.sh"

STACK=$(detect_stack)

case "$STACK" in
    node)
        has_npm_script build || emit_skip "package.json declares no build script"
        run_capture "$(node_runner)" run build
        ;;
    python)
        # Import-compile every module: the closest thing Python has to a build,
        # and it catches syntax errors a test run would only reach lazily.
        run_capture python -m compileall -q .
        ;;
    go)     run_capture go build ./... ;;
    rust)   run_capture cargo build --locked ;;
    java)
        if [ -f pom.xml ]; then run_capture mvn -B -q compile
        else run_capture ./gradlew --quiet compileJava
        fi
        ;;
    *)      emit_skip "no recognised build system in this repository" ;;
esac

emit_from_exit "build succeeded ($STACK)" "build failed ($STACK)"
