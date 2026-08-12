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
| 2 — setup | `setup-project` | **built** (GitHub) |
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

## Phase 2 walkthrough

`setup-project` projects the task graph onto an issue tracker: one issue per
task file, the task's `dir` as the label, its `phase` as the milestone.

### Credentials

A token is read from the first of these that has one — the process environment,
`<target-repo>/.env`, then `~/.ai-harness/.env`:

```
GITHUB_TOKEN=ghp_...
```

Both `.env` locations are gitignored (see `.env.example`). If none carries a
token and the GitHub CLI is logged in, `gh auth token` is used as a fallback, so
a machine that already ran `gh auth login` needs no `.env` at all. The token
needs the `repo` scope.

### Running it

```bash
harness setup-project existing --dry-run
```

`existing` targets the repository this checkout already points at, read from
`git remote origin`; `--repo owner/name` overrides it. The command prints a
complete plan — account, repository, labels and milestones to create, and every
issue it would open — and `--dry-run` stops there.

```bash
harness setup-project existing
```

Without `--dry-run` the same plan is printed and then you are asked to confirm.
Nothing reaches the tracker before you answer. `--yes` skips the prompt for
non-interactive runs.

To create the repository as well:

```bash
harness setup-project new --repo my-service --private
```

Both `--repo` and a visibility flag are required — nothing is inferred from the
directory name. `--owner <org>` creates under an organisation instead of your
own account. **`new` creates no folders in the target project**: the repository
starts empty, and target directories appear in phase 3 when the Developer agent
first implements in one (spec open question #9).

### Re-running

`setup-project` is meant to be re-run. A task already linked to an issue is
never re-created; its issue is compared against the current task file and
updated only where the two disagree. Labels someone added by hand are kept — the
harness adds, it does not prune. Each issue is recorded as a `task.issue_linked`
event *and* as `issue_ref` in the task's frontmatter, so a task file read on its
own still names its ticket.

Failure is partial by design: a rejected credential or an exhausted rate limit
would fail identically for every remaining task, so the run aborts on it; one
rejected issue does not stop the other thirty-nine. Either way, everything
already created is in the event log, so the next run resumes instead of
duplicating.

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
├── env.py            layered credential lookup for the tracker adapters
├── taskfile.py       reading task files back, re-validated on load
├── trackers/         issue-tracker adapters (base.py + github.py)
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
round-trip, dependency-graph validation, contracts, registry enforcement, and
the whole of phase 2 against a mocked tracker. It makes no API calls and no
network calls of any kind.
