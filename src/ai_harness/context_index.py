"""Symbol and dependency index (spec section 9).

The requirement is an effect, not a product: agents should query a compact index
— "what calls this", "what imports that", "where is this defined" — instead of
reading whole files into context to find out. Resolved question #1 chose
tree-sitter for it: language-agnostic, offline, and no external service to
depend on or pay for.

The index is a cache, never a source of truth. It is keyed by file content hash
so a stale entry is impossible rather than merely unlikely, and only changed
files are re-parsed. Deleting ``.harness/index/`` loses nothing but time.

When tree-sitter is not installed the index refuses to build rather than falling
back to a regex approximation. A degraded index that silently answers "nothing
calls this" is worse than no index, because an agent cannot tell the difference.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .paths import Project

INDEX_VERSION = "0.1"

NO_TREE_SITTER = (
    "the context index needs tree-sitter, which is not installed.\n"
    "  pip install 'ai-harness[context]'\n"
    "  or:  pip install tree-sitter tree-sitter-language-pack\n"
    "Spec section 9 has agents query an index rather than read whole files; a "
    "regex approximation is not offered, because an agent cannot tell an empty "
    "answer from a wrong one."
)

# Per-language node types. Keeping this as data rather than per-language code is
# what makes adding a language a three-line edit instead of a new module.
LANGUAGES: dict[str, dict[str, Any]] = {
    "python": {
        "extensions": [".py"],
        "definitions": {"function_definition": "function", "class_definition": "class"},
        "imports": ["import_statement", "import_from_statement"],
        "calls": ["call"],
        "call_field": "function",
    },
    "javascript": {
        "extensions": [".js", ".jsx", ".mjs", ".cjs"],
        "definitions": {"function_declaration": "function", "class_declaration": "class",
                        "method_definition": "method"},
        "imports": ["import_statement"],
        "calls": ["call_expression"],
        "call_field": "function",
    },
    "typescript": {
        "extensions": [".ts"],
        "definitions": {"function_declaration": "function", "class_declaration": "class",
                        "method_definition": "method", "interface_declaration": "interface"},
        "imports": ["import_statement"],
        "calls": ["call_expression"],
        "call_field": "function",
    },
    "tsx": {
        "extensions": [".tsx"],
        "definitions": {"function_declaration": "function", "class_declaration": "class",
                        "method_definition": "method"},
        "imports": ["import_statement"],
        "calls": ["call_expression"],
        "call_field": "function",
    },
    "go": {
        "extensions": [".go"],
        "definitions": {"function_declaration": "function", "method_declaration": "method",
                        "type_declaration": "type"},
        "imports": ["import_declaration"],
        "calls": ["call_expression"],
        "call_field": "function",
    },
    "rust": {
        "extensions": [".rs"],
        "definitions": {"function_item": "function", "struct_item": "struct",
                        "impl_item": "impl", "trait_item": "trait"},
        "imports": ["use_declaration"],
        "calls": ["call_expression"],
        "call_field": "function",
    },
    "java": {
        "extensions": [".java"],
        "definitions": {"class_declaration": "class", "method_declaration": "method",
                        "interface_declaration": "interface"},
        "imports": ["import_declaration"],
        "calls": ["method_invocation"],
        "call_field": "name",
    },
}

SKIP_DIRS = {".git", ".harness", "node_modules", "__pycache__", "venv", ".venv",
             "dist", "build", "target", ".mypy_cache", ".pytest_cache", "vendor"}

MAX_FILE_BYTES = 2_000_000


class ContextIndexUnavailable(RuntimeError):
    """tree-sitter is not installed."""


def available() -> bool:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_language_pack  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class Symbol:
    name: str
    kind: str
    line: int


@dataclass
class FileIndex:
    path: str
    language: str
    digest: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["symbols"] = [asdict(s) for s in self.symbols]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileIndex":
        return cls(path=data["path"], language=data["language"], digest=data["digest"],
                   symbols=[Symbol(**s) for s in data.get("symbols", [])],
                   imports=list(data.get("imports", [])),
                   calls=list(data.get("calls", [])))


def language_for(path: Path) -> str | None:
    suffix = path.suffix.lower()
    for name, config in LANGUAGES.items():
        if suffix in config["extensions"]:
            return name
    return None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _node_name(node, source: bytes) -> str | None:
    named = node.child_by_field_name("name")
    if named is not None:
        return source[named.start_byte:named.end_byte].decode("utf-8", "replace")
    return None


def _call_target(node, source: bytes, call_field: str) -> str | None:
    """The bare name being called, with any receiver stripped.

    ``a.b.c()`` indexes as ``c``: the index answers "what calls this symbol",
    and resolving which ``c`` would require type inference the index does not do
    and does not claim to.
    """
    target = node.child_by_field_name(call_field)
    if target is None:
        return None
    text = source[target.start_byte:target.end_byte].decode("utf-8", "replace")
    text = text.split("(")[0].strip()
    for separator in (".", "::", "->"):
        if separator in text:
            text = text.rsplit(separator, 1)[-1]
    return text or None


def _walk(node) -> Iterator[Any]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def parse_file(path: Path, relative: str, language: str, source: bytes) -> FileIndex:
    from tree_sitter_language_pack import get_parser

    config = LANGUAGES[language]
    entry = FileIndex(path=relative, language=language, digest=_digest(source))

    tree = get_parser(language).parse(source)
    definitions = config["definitions"]
    import_types = set(config["imports"])
    call_types = set(config["calls"])
    call_field = config["call_field"]

    seen_calls: set[str] = set()
    for node in _walk(tree.root_node):
        kind = definitions.get(node.type)
        if kind:
            name = _node_name(node, source)
            if name:
                entry.symbols.append(
                    Symbol(name=name, kind=kind, line=node.start_point[0] + 1))
        elif node.type in import_types:
            text = source[node.start_byte:node.end_byte].decode("utf-8", "replace")
            entry.imports.append(" ".join(text.split())[:200])
        elif node.type in call_types:
            target = _call_target(node, source, call_field)
            if target and target not in seen_calls:
                seen_calls.add(target)
                entry.calls.append(target)

    return entry


class ContextIndex:
    """A built index over one tree. Queries are pure lookups over ``files``."""

    def __init__(self, root: Path, files: dict[str, FileIndex] | None = None):
        self.root = Path(root)
        self.files: dict[str, FileIndex] = files or {}

    # ------------------------------------------------------------- building

    @classmethod
    def index_path(cls, project: Project) -> Path:
        return project.harness / "index" / "index.json"

    @classmethod
    def load(cls, project: Project, root: Path | None = None) -> "ContextIndex":
        path = cls.index_path(project)
        target = root or project.root
        if not path.is_file():
            return cls(target)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return cls(target)
        if payload.get("version") != INDEX_VERSION:
            return cls(target)
        return cls(target, {p: FileIndex.from_dict(d)
                            for p, d in payload.get("files", {}).items()})

    def save(self, project: Project) -> Path:
        path = self.index_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_VERSION,
            "root": str(self.root),
            "files": {p: entry.to_dict() for p, entry in self.files.items()},
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(path)
        return path

    def source_files(self) -> list[Path]:
        found: list[Path] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if language_for(path) is None:
                continue
            found.append(path)
        return sorted(found)

    def build(self, *, force: bool = False) -> dict[str, int]:
        """(Re)build. Only files whose content hash changed are re-parsed."""
        if not available():
            raise ContextIndexUnavailable(NO_TREE_SITTER)

        stats = {"indexed": 0, "reused": 0, "skipped": 0, "removed": 0}
        current: dict[str, FileIndex] = {}

        for path in self.source_files():
            relative = path.relative_to(self.root).as_posix()
            try:
                source = path.read_bytes()
            except OSError:
                stats["skipped"] += 1
                continue
            if len(source) > MAX_FILE_BYTES:
                stats["skipped"] += 1
                continue

            digest = _digest(source)
            previous = self.files.get(relative)
            if previous is not None and previous.digest == digest and not force:
                current[relative] = previous
                stats["reused"] += 1
                continue

            language = language_for(path)
            try:
                current[relative] = parse_file(path, relative, language, source)
                stats["indexed"] += 1
            except Exception:
                # A file tree-sitter cannot parse is skipped and counted, never
                # silently dropped: the count is how a caller notices.
                stats["skipped"] += 1

        stats["removed"] = len(set(self.files) - set(current))
        self.files = current
        return stats

    # -------------------------------------------------------------- queries

    def definitions(self, name: str) -> list[tuple[str, Symbol]]:
        """Where a symbol is defined."""
        return [(entry.path, symbol)
                for entry in self.files.values()
                for symbol in entry.symbols
                if symbol.name == name]

    def callers(self, name: str) -> list[str]:
        """Files containing a call to ``name``."""
        return sorted(entry.path for entry in self.files.values() if name in entry.calls)

    def importers(self, needle: str) -> list[str]:
        """Files whose import statements mention ``needle``."""
        return sorted(entry.path for entry in self.files.values()
                      if any(needle in statement for statement in entry.imports))

    def outline(self, relative_path: str) -> list[Symbol]:
        entry = self.files.get(relative_path)
        return list(entry.symbols) if entry else []

    def summary(self) -> dict[str, Any]:
        by_language: dict[str, int] = {}
        symbols = 0
        for entry in self.files.values():
            by_language[entry.language] = by_language.get(entry.language, 0) + 1
            symbols += len(entry.symbols)
        return {"files": len(self.files), "symbols": symbols, "languages": by_language}

    def as_prompt_block(self, paths: list[str] | None = None, limit: int = 400) -> str:
        """A compact outline for an agent's context.

        This is the whole point of the index: an agent gets structure without
        the harness pasting entire files into a prompt.
        """
        selected = [self.files[p] for p in (paths or sorted(self.files)) if p in self.files]
        lines: list[str] = []
        for entry in selected:
            if len(lines) >= limit:
                lines.append(f"... {len(selected)} files indexed; outline truncated")
                break
            lines.append(f"{entry.path}  [{entry.language}]")
            for symbol in entry.symbols[:40]:
                lines.append(f"  {symbol.line:>5}  {symbol.kind:<9} {symbol.name}")
        return "\n".join(lines) if lines else "(index is empty)"
