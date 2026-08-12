# AI Engineering Harness

> Agents decide. Tools provide evidence. Gates enforce deterministic outcomes.
> Workflows orchestrate everything.
>
> No phase output is trusted because an LLM said so — it is trusted because a
> validator, a build, a test run, or an explicit human approval confirmed it.

A standalone CLI that drives a specification from raw documents through review,
chunking, implementation, and documentation, making its own model calls and
recording every step in an auditable log.

Architecture: [`harness-architecture.md`](harness-architecture.md).
Deviations from it: [`SPEC-AMENDMENTS.md`](SPEC-AMENDMENTS.md).

## Status

| Phase | Command | State |
|---|---|---|
| 1 — spec review | `review-specs` | **built** |
| 1 — chunking | `chunk-specs` | **built** |
| 2 — setup | `setup-project` | not started |
| 3 — execution | `run-architect`, `run-agent` | not started |
| 4 — documentation | `write-documentation` | not started |
| 5 — production | TBD | not scoped |

## Install

```bash
pip install -e .
```

That exposes a `harness` command. If your Python `Scripts/` directory is not on
`PATH`, `python -m ai_harness` is equivalent.

The harness makes its own API calls, so it needs its own credential:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

`ant auth login` works too — the SDK resolves either.

## Phase 1 walkthrough

```bash
cd /path/to/your/target/repo
harness init --docs ./specs
```

`init` is non-destructive: it creates `.harness/` and touches nothing else in
your source tree.

```bash
harness review-specs
```

Reads every document under `./specs`, finds everything it will not guess at, and
writes the questions to `.harness/open-questions.md`. **It never fills a gap
with an assumption.** Answer what you can in that file, then re-run. Blank
answers stay open and get asked again — they are never taken as agreement.

Repeat until the review comes back clean, then lock it:

```bash
harness review-specs --approve
```

Approval is always a separate, explicit act; `--approve` is refused unless the
last review actually returned zero questions. To close a review with questions
still open, `--finalize` records a *forced closure* in the log — the distinction
between "agreed" and "overridden" survives in the audit trail.

```bash
harness chunk-specs
```

Splits the approved spec into `.harness/tasks/<dir>/T-XXX.md`, each with YAML
frontmatter validated against `task.schema.json`, and each with explicit
`depends_on` edges. The dependency graph is checked for cycles, self-edges, and
dangling references before anything reaches disk — those edges are what let
phase 3 run tickets in parallel safely, so they are validated rather than
trusted. `--dry-run` prints the plan without writing.

```bash
harness status     # derived state: phase, spec status, task readiness
harness log        # the append-only event log
```

### Other flags

- `review-specs --interactive` — answer at the terminal instead of in a file.
- `chunk-specs --force` — replace existing task files.
- `--model <id>` — override the registry default for one run. Only ids in the
  registry are accepted; a free-text model name is refused.

## Layout

```
src/ai_harness/
├── cli.py            entry point
├── events.py         append-only log — the source of truth
├── state.py          derived state (a fold over the log)
├── contracts.py      JSON Schema validation
├── llm.py            the only module that knows about a model provider
├── loaders.py        pluggable document loaders (.md/.txt in v1)
├── qa_round.py       the open-questions file format
├── registry.py       the closed set of models an agent may use
├── paths.py          three-tier content resolution
├── commands/         one module per CLI command
└── core/             shipped defaults (contracts, prompts, model registry)
```

In a target repository:

```
.harness/
├── events/           append-only log, one segment per writer
├── state.json        DERIVED — regenerated, do not edit
├── revised-spec.md   append-only Q&A audit trail
├── open-questions.md the current round, awaiting your answers
├── overrides/        per-project prompt/contract/registry overrides
├── tasks/<dir>/      T-XXX.md
├── architecture/
└── reports/
```

Cross-project state (institutional-memory lessons) lives in `~/.ai-harness/`.

## Customising

Anything under `core/` can be overridden without forking. Drop a file at the
same relative path in `.harness/overrides/` (per project) or `~/.ai-harness/`
(all your projects) and it wins over the shipped default. Most useful targets:

- `prompts/review-specs.md` — how strict the reviewer is about what counts as a gap
- `prompts/chunk-specs.md` — how work is sliced
- `model-registry.yaml` — which models and effort levels agents may use

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests -q
```

The suite covers the deterministic layer only — event log, state fold, Q&A
round-trip, dependency-graph validation, contracts, and registry enforcement. It
makes no API calls.
