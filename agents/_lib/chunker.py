"""
agents/_lib/chunker.py — semantic-boundary file chunker.

Splits large source files at function/class boundaries (not arbitrary line
splits) so chunked source still parses as a coherent unit. Ported from
agentrepo/ms-agent-harness/agent_harness/context/chunker.py — agentrepo's
settings-loader dependency is replaced with constants here, since chunking
thresholds rarely change and adding a YAML round-trip just for them isn't
worth it. If we ever need per-language overrides, lift the constants into
agents/config/chunking.yaml then.

Triggers chunking when EITHER threshold is exceeded:
  - lines > MAX_LINES (default 3000)
  - chars > MAX_CHARS (default 150_000)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Constants ported from agentrepo/.../config/settings.yaml `chunking:` block.
MAX_LINES = 3000
MAX_CHARS = 150_000
OVERLAP_LINES = 50
TARGET_CHUNK_LINES_DEFAULT = 500


@dataclass
class Chunk:
    """A semantic chunk of source code."""
    index: int
    start_line: int
    end_line: int
    content: str
    context_summary: str = ""

    def __str__(self) -> str:  # f-string friendly: f"{chunk}" yields content
        return self.content


_LANG_BY_SUFFIX = {
    ".py": "python",
    ".js": "node",
    ".mjs": "node",
    ".ts": "node",
    ".java": "java",
    ".cs": "csharp",
}


# Function/class boundary regexes per language. These are intentionally
# permissive — false positives just mean a slightly smaller chunk; false
# negatives drop you to the fixed-size fallback.
BOUNDARY_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r'^(?:class\s+\w|(?:async\s+)?def\s+\w)', re.MULTILINE),
    "node":   re.compile(r'^(?:(?:async\s+)?function\s+\w|class\s+\w|(?:const|let)\s+\w+\s*=\s*(?:async\s+)?(?:\(|function))', re.MULTILINE),
    "java":   re.compile(r'^\s*(?:public|private|protected|static|\s)*(?:class|interface|(?:[\w<>\[\]]+\s+\w+\s*\())', re.MULTILINE),
    "csharp": re.compile(r'^\s*(?:public|private|protected|static|async|\s)*(?:class|interface|(?:[\w<>\[\]]+\s+\w+\s*\())', re.MULTILINE),
}


def _coerce_text(src: str | Path) -> tuple[str, str | None]:
    """Accept raw content or Path; return (content, inferred_language)."""
    if isinstance(src, Path):
        return src.read_text(encoding="utf-8", errors="replace"), _LANG_BY_SUFFIX.get(src.suffix)
    return src, None


def needs_chunking(content: str | Path) -> bool:
    """True iff the file exceeds the line OR char threshold."""
    text, _ = _coerce_text(content)
    lines = text.count("\n") + 1
    return lines > MAX_LINES or len(text) > MAX_CHARS


def chunk_file(
    content: str | Path,
    language: str | None = None,
    target_chunk_lines: int = TARGET_CHUNK_LINES_DEFAULT,
) -> list[Chunk]:
    """Split a large file into semantic chunks at function/class boundaries.

    Strategy:
      1. Find all boundary line numbers via the language regex.
      2. Walk forward, closing a chunk once it has accumulated >= target_chunk_lines.
      3. Prepend `OVERLAP_LINES` from the previous chunk for context continuity.
      4. If no boundaries are found, fall back to fixed-size chunks.

    For files that don't need chunking (under both thresholds), returns a
    single Chunk covering the whole file — callers don't need to special-case.
    """
    text, inferred_lang = _coerce_text(content)
    language = language or inferred_lang or "python"
    if not needs_chunking(text):
        return [Chunk(index=0, start_line=1, end_line=text.count("\n") + 1, content=text)]

    lines = text.split("\n")
    total_lines = len(lines)
    pattern = BOUNDARY_PATTERNS.get(language, BOUNDARY_PATTERNS["python"])
    boundaries = [i for i, line in enumerate(lines) if pattern.match(line)]

    if not boundaries:
        return _fixed_size_chunks(lines, target_chunk_lines, OVERLAP_LINES)

    chunks: list[Chunk] = []
    chunk_start = 0
    for i, boundary in enumerate(boundaries):
        lines_so_far = boundary - chunk_start
        if lines_so_far >= target_chunk_lines or i == len(boundaries) - 1:
            chunk_end = boundaries[i + 1] if i + 1 < len(boundaries) else total_lines
            overlap_start = max(0, chunk_start - OVERLAP_LINES)
            chunks.append(Chunk(
                index=len(chunks),
                start_line=chunk_start + 1,
                end_line=chunk_end,
                content="\n".join(lines[overlap_start:chunk_end]),
            ))
            chunk_start = boundary

    if chunk_start < total_lines:
        if not chunks or chunks[-1].end_line < total_lines:
            overlap_start = max(0, chunk_start - OVERLAP_LINES)
            chunks.append(Chunk(
                index=len(chunks),
                start_line=chunk_start + 1,
                end_line=total_lines,
                content="\n".join(lines[overlap_start:]),
            ))

    return chunks


def _fixed_size_chunks(lines: list[str], target: int, overlap: int) -> list[Chunk]:
    """Fallback: split at fixed line intervals with overlap."""
    chunks: list[Chunk] = []
    total = len(lines)
    start = 0
    while start < total:
        end = min(start + target, total)
        overlap_start = max(0, start - overlap)
        chunks.append(Chunk(
            index=len(chunks),
            start_line=start + 1,
            end_line=end,
            content="\n".join(lines[overlap_start:end]),
        ))
        start = end
    return chunks
