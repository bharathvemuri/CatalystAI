"""Append-only event log — the harness's source of truth.

Spec section 2 made ``state.json`` the single writer-of-record. That does not
survive section 11's parallel worktrees: N processes mutating one JSON file
corrupt it. So the log is the truth and ``state.json`` is a *derived, read-only
view* (see ``state.py``).

Concurrency is handled by construction rather than by locking: the log is
partitioned into per-stream segment files under ``.harness/events/``. The
orchestrator writes ``main.jsonl``; each ticket's worktree writes only its own
``T-001.jsonl``. No two processes ever append to the same file, so no lock is
needed and a crashed worker can never leave a half-written shared file behind.

Ordering across streams is by timestamp; within a stream it is file order, which
is what actually matters for causality (a ticket's own events are totally
ordered, and cross-ticket ordering is only ever coarse).
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

LOG_VERSION = "0.1"

_SAFE_STREAM = re.compile(r"^[A-Za-z0-9._-]+$")

MAIN_STREAM = "main"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_event_id() -> str:
    return "ev_" + secrets.token_hex(8)


@dataclass(frozen=True)
class Event:
    id: str
    ts: str
    stream: str
    type: str
    payload: dict[str, Any]
    seq: int = 0  # position within its own segment; not persisted

    def to_json(self) -> str:
        return json.dumps(
            {"v": LOG_VERSION, "id": self.id, "ts": self.ts,
             "stream": self.stream, "type": self.type, "payload": self.payload},
            ensure_ascii=False,
        )


class EventLog:
    def __init__(self, events_dir: Path):
        self.dir = Path(events_dir)

    def _segment(self, stream: str) -> Path:
        if not _SAFE_STREAM.match(stream):
            raise ValueError(f"unsafe stream name: {stream!r}")
        return self.dir / f"{stream}.jsonl"

    def append(self, type: str, payload: dict[str, Any] | None = None,
               stream: str = MAIN_STREAM) -> Event:
        event = Event(id=new_event_id(), ts=utcnow(), stream=stream,
                      type=type, payload=payload or {})
        path = self._segment(stream)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = event.to_json() + "\n"
        # O_APPEND: every write lands at the current end of file, so a second
        # process appending to the *same* stream still cannot interleave lines.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return event

    def read_stream(self, stream: str) -> list[Event]:
        path = self._segment(stream)
        if not path.exists():
            return []
        out: list[Event] = []
        with path.open("r", encoding="utf-8") as fh:
            for seq, raw in enumerate(fh):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    # A torn final line means a writer died mid-append. Every
                    # earlier line is still valid, so stop rather than discard.
                    break
                out.append(Event(id=rec["id"], ts=rec["ts"], stream=rec["stream"],
                                 type=rec["type"], payload=rec.get("payload", {}), seq=seq))
        return out

    def streams(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.jsonl"))

    def read_all(self) -> list[Event]:
        events: list[Event] = []
        for stream in self.streams():
            events.extend(self.read_stream(stream))
        events.sort(key=lambda e: (e.ts, e.stream, e.seq))
        return events

    def __iter__(self) -> Iterator[Event]:
        return iter(self.read_all())
