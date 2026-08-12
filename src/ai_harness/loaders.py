"""Source-document loaders.

v1 ships ``.md`` and ``.txt`` only. ``.pdf`` and ``.html`` (spec section 3) drop
in by registering another loader here — the rest of the pipeline sees only
``LoadedDoc`` and never learns what format it came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_BYTES = 2_000_000  # a single source doc larger than this is almost certainly a mistake


@dataclass(frozen=True)
class LoadedDoc:
    path: Path
    text: str

    @property
    def name(self) -> str:
        return self.path.name


class UnsupportedFormat(Exception):
    pass


def _load_text(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError(f"{path.name} is {len(data)} bytes; over the {MAX_BYTES} limit")
    return data.decode("utf-8", errors="replace")


LOADERS: dict[str, Callable[[Path], str]] = {
    ".md": _load_text,
    ".markdown": _load_text,
    ".txt": _load_text,
}


def register(extension: str, loader: Callable[[Path], str]) -> None:
    LOADERS[extension.lower()] = loader


def supported_extensions() -> list[str]:
    return sorted(LOADERS)


def load_file(path: Path) -> LoadedDoc:
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise UnsupportedFormat(
            f"{path.name}: no loader for {path.suffix!r} "
            f"(supported: {', '.join(supported_extensions())})"
        )
    return LoadedDoc(path=path, text=loader(path))


def load_dir(directory: Path) -> tuple[list[LoadedDoc], list[Path]]:
    """Load every supported doc under ``directory``.

    Returns (loaded, skipped). Skipped files are reported to the user rather
    than silently dropped — a spec review that quietly ignored half its inputs
    would be worse than one that refused to run.
    """
    if not directory.is_dir():
        raise NotADirectoryError(f"{directory} is not a directory")

    loaded: list[LoadedDoc] = []
    skipped: list[Path] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            loaded.append(load_file(path))
        except UnsupportedFormat:
            skipped.append(path)
    return loaded, skipped


def as_prompt_block(docs: list[LoadedDoc], root: Path) -> str:
    """Render loaded docs into a single delimited block for the model."""
    parts: list[str] = []
    for doc in docs:
        try:
            rel = doc.path.relative_to(root)
        except ValueError:
            rel = doc.path
        parts.append(f'<document path="{rel}">\n{doc.text}\n</document>')
    return "\n\n".join(parts)
