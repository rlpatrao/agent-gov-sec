"""Progressive context compressor.

Keeps recent chunks at full detail; compresses older history to save tokens.
Strategy: last N chunks full, older chunks compressed to ~30% via extractive
summarization (function/class/import signatures — no LLM call needed).

Ported from agentrepo context/compressor.py with no logic changes.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CompressedContext:
    full_chunks: list[str]
    compressed_history: str
    total_original_tokens: int
    total_compressed_tokens: int


def compress_history(
    chunk_contents: list[str],
    keep_full: int = 3,
    compression_ratio: float = 0.3,
) -> CompressedContext:
    """Keep the last `keep_full` chunks at full resolution; compress the rest."""
    if len(chunk_contents) <= keep_full:
        tokens = _estimate_tokens(chunk_contents)
        return CompressedContext(
            full_chunks=chunk_contents,
            compressed_history="",
            total_original_tokens=tokens,
            total_compressed_tokens=tokens,
        )

    old_chunks = chunk_contents[:-keep_full]
    recent_chunks = chunk_contents[-keep_full:]
    compressed = _extractive_compress(old_chunks, compression_ratio)
    return CompressedContext(
        full_chunks=recent_chunks,
        compressed_history=compressed,
        total_original_tokens=_estimate_tokens(chunk_contents),
        total_compressed_tokens=_estimate_tokens(recent_chunks) + _estimate_tokens([compressed]),
    )


def _extractive_compress(chunks: list[str], ratio: float) -> str:
    compressed_parts = []
    for i, chunk in enumerate(chunks):
        lines = chunk.split("\n")
        target_lines = max(5, int(len(lines) * ratio))

        key_lines = []
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in [
                "def ", "async def ", "class ", "import ", "from ",
                "function ", "const ", "let ", "export ",
                "public ", "private ", "protected ",
                "#", "//", "/*", "/**", '"""', "'''",
            ]):
                key_lines.append(line)

        remaining = target_lines - len(key_lines)
        if remaining > 0:
            step = max(1, len(lines) // remaining)
            for j in range(0, len(lines), step):
                if lines[j].strip() and lines[j] not in key_lines:
                    key_lines.append(lines[j])
                    if len(key_lines) >= target_lines:
                        break

        compressed_parts.append(
            f"--- Chunk {i + 1} (compressed) ---\n" + "\n".join(key_lines[:target_lines])
        )
    return "\n\n".join(compressed_parts)


def _estimate_tokens(texts: list[str]) -> int:
    return int(sum(len(t) for t in texts) / 3.0)
