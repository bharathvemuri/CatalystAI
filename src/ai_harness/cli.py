"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from . import HARNESS_VERSION
from .commands import (approve, chunk_specs, init, review_specs, run,
                       setup_project, status, write_documentation)
from .commands._common import CommandError

EPILOG = """\
phase 1 (implemented):
  init            attach the harness to a target repository
  review-specs    find every gap and ask about it; never assumes
  chunk-specs     split the approved spec into dependency-ordered tasks

phase 2 (implemented):
  setup-project   create tracker issues, labels and milestones from the tasks

phase 3 (implemented):
  run             drive every dependency-ready ticket
  run-ticket      drive one ticket through the pipeline
  approve         the human gate: read the evidence, then approve

phase 4 (implemented):
  write-documentation   document what shipped, across the whole project

any time:
  status / log    read-only views over the event log

phase 5 (production) is not built yet.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="AI engineering harness — agents decide, tools provide evidence, "
                    "gates enforce deterministic outcomes.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ai-harness {HARNESS_VERSION}")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    init.add_parser(subparsers)
    review_specs.add_parser(subparsers)
    chunk_specs.add_parser(subparsers)
    setup_project.add_parser(subparsers)
    run.add_parser(subparsers)
    approve.add_parser(subparsers)
    write_documentation.add_parser(subparsers)
    status.add_parser(subparsers)
    return parser


def _make_output_encoding_safe() -> None:
    """Never let a legacy console codepage turn a report into a traceback.

    Windows consoles still default to cp1252 in places, where an em dash raises
    UnicodeEncodeError mid-print. Keeping the console's own encoding but
    replacing unmappable characters degrades to '?' instead of crashing.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):  # not a reconfigurable text stream
            pass


def main(argv: list[str] | None = None) -> int:
    _make_output_encoding_safe()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    try:
        return args.func(args) or 0
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
