"""The open-questions round file: rendering and parsing.

Batch mode is the default because it is resumable and git-diffable — the review
can span days, survive a crashed process, and be answered by someone other than
whoever started it. ``--interactive`` exists for burning through a round in one
sitting.

The file is a contract in both directions, so parsing is deliberately anchored
to HTML comment markers rather than to prose: the user can reformat the human
parts freely without breaking the round-trip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROUND_MARKER = re.compile(r"<!--\s*ai-harness:round=(\d+)\s*-->")
QID_MARKER = re.compile(r"<!--\s*ai-harness:qid=([A-Za-z0-9_.-]+)\s*-->")
ANSWER_MARKER = "<!-- write your answer below this line -->"
SEVERITIES = ("blocking", "important", "minor")


@dataclass
class Question:
    id: str
    question: str
    why_it_matters: str
    source: str
    severity: str
    options: list[str] = field(default_factory=list)

    @classmethod
    def from_model(cls, raw: dict[str, Any]) -> "Question":
        return cls(
            id=raw["id"],
            question=raw["question"].strip(),
            why_it_matters=raw["why_it_matters"].strip(),
            source=raw.get("source", "").strip(),
            severity=raw.get("severity", "important"),
            options=[o.strip() for o in raw.get("options", []) if o.strip()],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "question": self.question,
            "why_it_matters": self.why_it_matters, "source": self.source,
            "severity": self.severity, "options": self.options,
        }


@dataclass
class Answer:
    id: str
    question: str
    answer: str

    @property
    def answered(self) -> bool:
        return bool(self.answer.strip())


def render(questions: list[Question], round_no: int, assessment: str = "") -> str:
    order = {s: i for i, s in enumerate(SEVERITIES)}
    ordered = sorted(questions, key=lambda q: order.get(q.severity, len(SEVERITIES)))

    out: list[str] = [
        f"# Open questions — round {round_no}",
        "",
        f"<!-- ai-harness:round={round_no} -->",
        "",
        f"The spec review found {len(ordered)} thing(s) it will not guess at.",
        "",
        "Write your answers under each **Answer:** marker, save the file, then re-run",
        "`harness review-specs`. Anything you leave blank stays open and will be",
        "asked again next round — nothing is ever filled in with an assumption.",
        "",
        "To close the review while questions are still open, run",
        "`harness review-specs --finalize`. That is logged as a forced closure.",
        "",
    ]
    if assessment.strip():
        out += ["> **Reviewer's read of the spec so far:**", ""]
        out += [f"> {line}" for line in assessment.strip().splitlines()]
        out += [""]
    out += ["---", ""]

    for i, q in enumerate(ordered, start=1):
        out += [
            f"## Q{i} · {q.severity}",
            "",
            f"<!-- ai-harness:qid={q.id} -->",
            "",
            f"**Question:** {q.question}",
            "",
            f"**Why it matters:** {q.why_it_matters}",
        ]
        if q.source:
            out += ["", f"**Where:** {q.source}"]
        if q.options:
            out += ["", "**Some possible answers** (suggestions only — write anything):", ""]
            out += [f"- {opt}" for opt in q.options]
        out += ["", "**Answer:**", "", ANSWER_MARKER, "", "", "---", ""]

    return "\n".join(out)


def round_number(text: str) -> int | None:
    match = ROUND_MARKER.search(text)
    return int(match.group(1)) if match else None


def parse(text: str) -> list[Answer]:
    """Extract answers from a filled-in round file.

    Each question's answer is the text between its answer marker and the next
    horizontal rule (or end of file).
    """
    answers: list[Answer] = []
    matches = list(QID_MARKER.finditer(text))

    for i, match in enumerate(matches):
        qid = match.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[match.end():end]

        question = ""
        q_match = re.search(r"\*\*Question:\*\*\s*(.+)", block)
        if q_match:
            question = q_match.group(1).strip()

        marker_at = block.find(ANSWER_MARKER)
        if marker_at == -1:
            answers.append(Answer(id=qid, question=question, answer=""))
            continue

        tail = block[marker_at + len(ANSWER_MARKER):]
        # Stop at the horizontal rule that closes the question block.
        rule = re.search(r"^\s*---\s*$", tail, flags=re.MULTILINE)
        if rule:
            tail = tail[:rule.start()]
        answers.append(Answer(id=qid, question=question, answer=tail.strip()))

    return answers


def append_to_revised_spec(path: Path, round_no: int, timestamp: str,
                           answers: list[Answer]) -> None:
    """Append-only audit log of every answered question (spec section 3)."""
    answered = [a for a in answers if a.answered]
    if not answered:
        return

    lines = ["", f"## Round {round_no} — {timestamp}", ""]
    for a in answered:
        lines += [
            f"### {a.question or a.id}",
            "",
            f"<!-- ai-harness:qid={a.id} -->",
            "",
            a.answer,
            "",
        ]

    header_needed = not path.exists()
    with path.open("a", encoding="utf-8") as fh:
        if header_needed:
            fh.write(
                "# Revised specification\n\n"
                "Append-only Q&A log produced by `harness review-specs`. Every entry is a\n"
                "question the review refused to answer with an assumption, plus the answer\n"
                "given. Nothing here is ever rewritten — this is the audit trail.\n"
            )
        fh.write("\n".join(lines) + "\n")
