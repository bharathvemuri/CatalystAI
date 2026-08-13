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
| 3 — execution | `run`, `run-ticket`, `approve` | **built** |
| 4 — documentation | `write-documentation` | **built** |
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

## Phase 3 walkthrough

Each ticket runs in its own git worktree and its own container, through a fixed
pipeline: **Architect → Developer → gates → {Security, QA, Performance} →
Reviewer → human approval gate**.

### What it needs

```bash
pip install -e ".[context]"     # adds tree-sitter for the context index
```

Docker must be installed: spec §10 requires project-level work — installs,
builds, tests, agent shell commands — to happen inside a container rather than
on your machine. `--no-container` waives that and writes an
`ticket.isolation_waived` event, so the audit trail says which kind of run
produced the evidence.

### Running

```bash
harness run --dry-run
```

Prints the plan: which tickets are dependency-ready, where their worktrees go,
and the pipeline they will follow. Then:

```bash
harness run-ticket T-001      # one ticket
harness run                   # every dependency-ready ticket
```

Both ask before starting. The pipeline stops at the human gate — nothing merges,
closes an issue, or opens a PR on its own.

### Gates are scripts, not opinions

`core/gates/{build,test,lint,security-scan}.sh` run real commands and emit a
JSON result validated against `gate-result.schema.json`. Evidence lands in
`.harness/reports/<id>/evidence/`. A gate that *skipped* is not a gate that
passed — whether a skip is tolerated is set in `core/gates/thresholds.yaml`,
alongside the coverage and performance regression limits and the
Developer→Reviewer retry cap.

The Reviewer reads those results rather than re-deriving them, and its verdict
is checked against them: an `APPROVE` issued while the build is failing or a
HIGH security finding is open is downgraded to `BLOCK` and recorded as
overridden.

### Approving

```bash
harness approve T-001
```

Assembles `.harness/reports/T-001/approval.md` from the real Security, QA and
Reviewer reports plus the gate results, prints it, and asks. It refuses — with
no override flag — if a report is missing, a gate failed, or the Reviewer did
not approve. On approval it offers to push the ticket branch and open a **draft**
pull request that closes the linked issue and embeds the evidence.

### Agent contracts

The eight contracts live in `core/agents/` as portable markdown, each following
the Standard Agent Contract (Mission / Inputs / Responsibilities /
Non-Responsibilities / Required Tools / Required Output / Blocking Conditions /
Success / Failure). Override any of them per project in
`.harness/overrides/agents/`.

Write boundaries are enforced, not requested: the Architect, Security and
Reviewer agents are never handed write tools, QA may write only tests,
Documentation only docs, and a write outside an agent's scope is rejected and
recorded as a contract violation.

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
├── agents.py         agent contracts, their tools, and boundary enforcement
├── pipeline.py       the per-ticket state machine
├── gates.py          deterministic gates + verdict
├── thresholds.py     gate limits, resolved through the three tiers
├── worktrees.py      one isolated git worktree per ticket
├── containers.py     one devcontainer per worktree
├── execution.py      Executor seam: container vs host
├── context_index.py  tree-sitter symbol/dependency index
├── commands/         one module per CLI command
└── core/             shipped defaults: contracts, prompts, model registry,
                      agent contracts, gate scripts, Dockerfile
```

In a target repository:

```
.harness/
├── events/           append-only log, one segment per writer
├── state.json        DERIVED — regenerated, do not edit
├── revised-spec.md   append-only Q&A audit trail
├── open-questions.md the current round, awaiting your answers
├── overrides/        per-project prompt/contract/registry/agent overrides
├── tasks/<dir>/      T-XXX.md
├── worktrees/<id>/   one isolated checkout per active ticket
├── index/            DERIVED — the context index cache
├── architecture/     ADR-NNN.md (numbers allocated, never self-assigned)
└── reports/<id>/     agent reports, gate-results.json, evidence/, approval.md
```

Cross-project state (institutional-memory lessons) lives in `~/.ai-harness/`.

## Phase 4 walkthrough

Phase 3 already runs a Documentation agent per ticket, which keeps docs from
drifting as each ticket lands. Phase 4 is the pass that reconciles those
ticket-scoped edits into one coherent set and guarantees the top-level documents
exist.

```bash
harness write-documentation --dry-run
```

The plan names the base ref it will document, every ticket that has shipped,
every ticket withheld because it has not, and which of `README.md`,
`ARCHITECTURE.md` and `CONTRIBUTING.md` are currently missing.

**Only tickets a human approved or that are done are documented.** Everything
else is withheld from the agent entirely — the Documentation contract's failure
conditions include documenting behaviour that was not implemented, so the
material simply is not offered.

```bash
harness write-documentation
```

Runs in its own worktree on `harness/docs`, container-isolated like any ticket,
and ends by offering a draft pull request you confirm. `--base REF` documents
something other than the current HEAD; `--no-pr` stops after committing.

Two things are checked mechanically afterwards, because an agent's account of
its own work is the last thing that should be taken on trust:

- The three required documents are looked for **on disk**, not read out of the
  report.
- What the agent says it touched is compared against what `git` says changed.
  A file changed without being declared, or declared without being changed, is
  recorded as a contract violation and the command exits non-zero.

One check happens before the pass instead. A ticket's work lives on its own
branch until its pull request merges, so if the base does not contain a shipped
ticket's commits, that ticket's code is not visible and the plan says so up
front — rather than letting it surface later as documentation describing code
that is not there.

## Customising

Anything under `core/` can be overridden without forking. Drop a file at the
same relative path in `.harness/overrides/` (per project) or `~/.ai-harness/`
(all your projects) and it wins over the shipped default. Most useful targets:

- `prompts/review-specs.md` — how strict the reviewer is about what counts as a gap
- `prompts/chunk-specs.md` — how work is sliced
- `model-registry.yaml` — which models and effort levels agents may use
- `agents/<name>.md` — an agent's contract; `agents/_preamble.md` for rules
  shared by all eight; `agents/documentation-project.md` for the phase 4 pass
- `gates/*.sh` — how build, test, lint and security scanning are actually run
- `gates/thresholds.yaml` — coverage and performance limits, retry cap, which
  gates may be skipped

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests -q
```

The suite covers the deterministic layer only — event log, state fold, Q&A
round-trip, dependency-graph validation, contracts, registry enforcement, phase
2 against a mocked tracker, phase 3's contracts, write boundaries, gates,
worktrees, context index and approval gate, and phase 4's shipped-ticket
filtering, merge detection and self-verification. It makes no API calls and no
network calls of any kind; the gate scripts and git worktrees are exercised for
real against temporary directories.
