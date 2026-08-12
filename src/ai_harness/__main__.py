"""Allow `python -m ai_harness` as an alternative to the `harness` script."""

from .cli import main

raise SystemExit(main())
