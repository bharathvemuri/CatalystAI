"""`harness review-specs` — phase 1, spec review.

Re-runnable indefinitely. Exits only when (a) the review has zero open questions
and the user explicitly approves, or (b) the user forces closure with
``--finalize``, which is logged as a forced closure rather than treated as
silent agreement.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import loaders, qa_round, runner as runners
from ..events import Event, utcnow
from ..llm import LLMError
from ..qa_round import Answer, Question
from ..paths import Project
from ..registry import Registry
from ..runner import Runner, RunnerUnavailable
from ..state import fold
from ._common import (CommandError, confirm, die, info, open_log, read_config,
                      rel, sync_state, warn, write_config)

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "question": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "source": {"type": "string"},
                    "severity": {"type": "string", "enum": list(qa_round.SEVERITIES)},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "question", "why_it_matters", "source",
                             "severity", "options"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assessment", "questions"],
    "additionalProperties": False,
}


def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "review-specs",
        help="review source documents and ask about every gap (phase 1)",
        description="Reads your specification documents, identifies everything it "
                    "will not guess at, and asks you. Never fills a gap with an "
                    "assumption. Re-run until clean, then --approve.",
    )
    p.add_argument("--docs", metavar="DIR",
                   help="folder of source documents (remembered after the first run)")
    p.add_argument("--interactive", action="store_true",
                   help="answer questions at the terminal instead of in a file")
    p.add_argument("--approve", action="store_true",
                   help="lock the spec; only allowed when zero questions are open")
    p.add_argument("--finalize", action="store_true",
                   help="force closure with questions still open (logged as forced)")
    p.add_argument("--model", metavar="ID",
                   help="override the registry default for this run")
    p.add_argument("--max-rounds", type=int, default=10, metavar="N",
                   help="safety cap on interactive rounds (default: 10)")
    runners.add_argument(p)
    p.set_defaults(func=run)


# ---------------------------------------------------------------- log reading

def _spec_events(events: list[Event]) -> list[Event]:
    return [e for e in events if e.type.startswith("spec.")]


def _pending_round(events: list[Event]) -> dict | None:
    """The last opened round that has had no answers recorded against it."""
    for event in reversed(_spec_events(events)):
        if event.type == "spec.round_opened":
            return event.payload
        if event.type in ("spec.answers_recorded", "spec.finalized"):
            return None
    return None


def _last_round_number(events: list[Event]) -> int:
    rounds = [e.payload.get("round", 0) for e in events if e.type == "spec.round_opened"]
    return max(rounds) if rounds else 0


def _review_is_clean(events: list[Event]) -> bool:
    for event in reversed(_spec_events(events)):
        if event.type == "spec.review_clean":
            return True
        if event.type in ("spec.round_opened", "spec.answers_recorded"):
            return False
    return False


# ------------------------------------------------------------------ documents

def _docs_dir(project: Project, args: argparse.Namespace) -> Path:
    config = read_config(project)
    if args.docs:
        candidate = Path(args.docs).expanduser()
        candidate = candidate if candidate.is_absolute() else (project.root / candidate)
        if not candidate.is_dir():
            die(f"--docs {candidate} is not a directory")
        config["docs_dir"] = rel(candidate.resolve(), project.root)
        write_config(project, config)
        return candidate.resolve()

    remembered = config.get("docs_dir")
    if not remembered:
        die("no source documents configured.\n"
            "Run: harness review-specs --docs <folder-with-your-spec-documents>")
    resolved = (project.root / remembered).resolve()
    if not resolved.is_dir():
        die(f"configured docs folder {remembered} no longer exists at {resolved}")
    return resolved


def _load_docs(project: Project, directory: Path) -> str:
    docs, skipped = loaders.load_dir(directory)
    if not docs:
        die(f"no readable documents in {directory}\n"
            f"supported formats: {', '.join(loaders.supported_extensions())}")
    for path in skipped:
        warn(f"skipped {rel(path, project.root)} — no loader for {path.suffix}")
    info(f"Loaded {len(docs)} document(s) from {rel(directory, project.root)}")
    return loaders.as_prompt_block(docs, directory)


# --------------------------------------------------------------------- review

def _build_runner(project: Project, args: argparse.Namespace) -> Runner:
    registry = Registry.load(project)
    model = registry.validate(args.model) if args.model else registry.default_for("architect")
    try:
        backend = runners.select(getattr(args, "runner", None))
    except RunnerUnavailable as exc:
        die(str(exc))
    return runners.build(backend, model=model,
                         effort=registry.effort_for("architect"), registry=registry)


def _run_review(project: Project, llm: Runner, docs_block: str) -> tuple[list[Question], str]:
    system = project.resolve("prompts/review-specs.md").read_text(encoding="utf-8")

    accumulated = ""
    if project.revised_spec.exists():
        accumulated = project.revised_spec.read_text(encoding="utf-8")

    parts = ["<source_documents>", docs_block, "</source_documents>"]
    if accumulated.strip():
        parts += [
            "",
            "<answered_questions>",
            "These have already been asked and answered by the human. They are now "
            "part of the specification — do not ask them again, and treat the "
            "answers as authoritative where they conflict with the source documents.",
            "",
            accumulated,
            "</answered_questions>",
        ]
    parts += ["", "Review the specification and return your questions."]

    detail = f", effort={llm.effort}" if llm.effort else ""
    info(f"Reviewing with {llm.model} via {llm.backend}{detail}…")
    result = llm.structured(system=system, user="\n".join(parts), schema=REVIEW_SCHEMA)
    questions = [Question.from_model(q) for q in result.get("questions", [])]
    return questions, result.get("assessment", "")


# -------------------------------------------------------------------- answers

def _ingest_answers(project: Project, log, pending: dict, *, allow_empty: bool) -> list[Answer]:
    round_no = pending.get("round", 0)
    if not project.open_questions.exists():
        die(f"round {round_no} is open but {rel(project.open_questions, project.root)} "
            "is missing. Restore it, or run with --finalize to force closure.")

    text = project.open_questions.read_text(encoding="utf-8")
    file_round = qa_round.round_number(text)
    if file_round is not None and file_round != round_no:
        die(f"{rel(project.open_questions, project.root)} is from round {file_round}, "
            f"but round {round_no} is open. The file was replaced — restore it or --finalize.")

    answers = qa_round.parse(text)
    answered = [a for a in answers if a.answered]

    if not answered and not allow_empty:
        die(f"no answers found in {rel(project.open_questions, project.root)}.\n"
            f"Fill in the {len(answers)} answer block(s) and re-run, or use "
            "--finalize to close the review with them still open.")

    qa_round.append_to_revised_spec(project.revised_spec, round_no, utcnow(), answered)
    log.append("spec.answers_recorded", {
        "round": round_no,
        "answers": [{"id": a.id, "question": a.question, "answer": a.answer}
                    for a in answered],
        "deferred": [a.id for a in answers if not a.answered],
    })

    deferred = len(answers) - len(answered)
    info(f"Recorded {len(answered)} answer(s) from round {round_no}"
         + (f"; {deferred} left open" if deferred else ""))
    return answers


def _ask_interactively(questions: list[Question], round_no: int) -> list[Answer]:
    info("")
    info(f"── Round {round_no}: {len(questions)} question(s). "
         "Press Enter on an empty line to defer one; Ctrl-C to stop. ──")
    answers: list[Answer] = []
    for i, q in enumerate(questions, start=1):
        info("")
        info(f"[{i}/{len(questions)}] ({q.severity}) {q.question}")
        info(f"      why: {q.why_it_matters}")
        if q.source:
            info(f"    where: {q.source}")
        for opt in q.options:
            info(f"      e.g. {opt}")
        try:
            text = input("    > ").strip()
        except (EOFError, KeyboardInterrupt):
            info("")
            info("Stopped. Unanswered questions stay open.")
            break
        answers.append(Answer(id=q.id, question=q.question, answer=text))
    return answers


def _open_round(project: Project, log, questions: list[Question], assessment: str,
                round_no: int) -> None:
    project.open_questions.write_text(
        qa_round.render(questions, round_no, assessment), encoding="utf-8")
    log.append("spec.round_opened", {
        "round": round_no,
        "questions": [q.to_dict() for q in questions],
        "assessment": assessment,
    })


# ------------------------------------------------------------------ finalising

def _finalize(project: Project, log, *, forced: bool, open_questions: int) -> int:
    log.append("spec.finalized", {
        "forced": forced,
        "open_questions": open_questions,
        "ts": utcnow(),
    })
    state = sync_state(project)
    info("")
    if forced:
        info(f"Spec closed by force with {open_questions} question(s) still open.")
        info("This is recorded in the event log as a forced closure, not agreement.")
    else:
        info("Spec approved. Zero open questions.")
    info(f"phase: {state['phase']}   revision: {state['spec']['revision']}")
    info("")
    info("Next: harness chunk-specs")
    return 0


# ------------------------------------------------------------------------ main

def run(args: argparse.Namespace) -> int:
    from ._common import require_project
    project = require_project()
    log = open_log(project)
    events = log.read_all()
    state = fold(log)

    if state["spec"]["status"] == "approved":
        die("the spec is already approved. The Q&A log is append-only; to reopen "
            "the review, start a new round deliberately by adding documents and "
            "re-running with --docs.")

    pending = _pending_round(events)

    # --approve: an explicit, separate act. Never automatic.
    if args.approve:
        if pending is not None:
            die(f"round {pending.get('round')} is still open. Answer it (or use "
                "--finalize) before approving.")
        if not _review_is_clean(events):
            die("cannot approve: the review has not yet come back with zero "
                "questions. Run `harness review-specs` again, or use --finalize "
                "to force closure.")
        return _finalize(project, log, forced=False, open_questions=0)

    # Ingest whatever is waiting before doing anything else.
    if pending is not None:
        answers = _ingest_answers(project, log, pending, allow_empty=args.finalize)
        still_open = sum(1 for a in answers if not a.answered)
        if args.finalize:
            sync_state(project)
            return _finalize(project, log, forced=True, open_questions=still_open)
        sync_state(project)
    elif args.finalize:
        return _finalize(project, log, forced=True,
                         open_questions=state["spec"]["open_questions"])

    docs_dir = _docs_dir(project, args)
    docs_block = _load_docs(project, docs_dir)
    llm = _build_runner(project, args)

    round_no = _last_round_number(log.read_all())
    rounds_run = 0

    while True:
        round_no += 1
        rounds_run += 1
        try:
            questions, assessment = _run_review(project, llm, docs_block)
        except LLMError as exc:
            raise CommandError(str(exc)) from exc

        if not questions:
            log.append("spec.review_clean", {"round": round_no, "assessment": assessment})
            sync_state(project)
            info("")
            info("No open questions.")
            if assessment:
                info("")
                info(assessment)
            info("")
            info("Nothing is approved automatically. To lock the spec, run:")
            info("  harness review-specs --approve")
            return 0

        if not args.interactive:
            _open_round(project, log, questions, assessment, round_no)
            state = sync_state(project)
            info("")
            info(f"{len(questions)} open question(s) written to "
                 f"{rel(project.open_questions, project.root)}")
            if assessment:
                info("")
                info(assessment)
            info("")
            info("Answer them in that file, then re-run `harness review-specs`.")
            return 0

        # Interactive: ask now, record now, loop.
        _open_round(project, log, questions, assessment, round_no)
        answers = _ask_interactively(questions, round_no)
        answered = [a for a in answers if a.answered]
        qa_round.append_to_revised_spec(project.revised_spec, round_no, utcnow(), answered)
        log.append("spec.answers_recorded", {
            "round": round_no,
            "answers": [{"id": a.id, "question": a.question, "answer": a.answer}
                        for a in answered],
            "deferred": [q.id for q in questions
                         if q.id not in {a.id for a in answered}],
        })
        sync_state(project)
        info("")
        info(f"Recorded {len(answered)} of {len(questions)} answer(s).")

        if not answered:
            info("Nothing answered — stopping so the same round is not regenerated.")
            return 0
        if rounds_run >= args.max_rounds:
            info(f"Reached --max-rounds ({args.max_rounds}). Re-run to continue.")
            return 0
        if not confirm("Run another review round?"):
            return 0
