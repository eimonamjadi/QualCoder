"""Turn uploaded files into plain text, then into passages (units of coding)."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

SPEAKER_RE = re.compile(r"^\s*[A-Z][\w .'\-]{0,40}:\s")  # "Interviewer: ", "P03: ", "Dr. Lee: "
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
MAX_PASSAGE_CHARS = 1200


@dataclass
class Passage:
    text: str
    start: int
    end: int


def extract_text(filename: str, data: bytes) -> str:
    """Plain text, Markdown, .docx and .pdf. Other formats are rejected."""
    name = filename.lower()
    if name.endswith((".txt", ".md", ".text")):
        return data.decode("utf-8", errors="replace")
    if name.endswith(".docx"):
        try:
            import docx  # python-docx
        except ImportError as e:  # pragma: no cover
            raise ValueError("Install python-docx to upload .docx files") from e
        document = docx.Document(io.BytesIO(data))
        return "\n\n".join(p.text for p in document.paragraphs)
    if name.endswith(".pdf"):
        try:
            import pypdf
        except ImportError as e:  # pragma: no cover
            raise ValueError("Install pypdf to upload .pdf files") from e
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    raise ValueError("Unsupported file type. Use .txt, .md, .docx or .pdf")


def segment(text: str, max_chars: int = MAX_PASSAGE_CHARS) -> list[Passage]:
    """Split into passages with character offsets into the original text.

    Rules, in order:
    1. Blank lines separate passages (paragraphs).
    2. Inside a block, a line that starts with a speaker label ("P03: ...") starts a
       new passage, so each speaker turn in a transcript is its own unit.
    3. Anything longer than ``max_chars`` is split on sentence boundaries.
    """
    units: list[tuple[int, int]] = []  # (start, end) offsets
    pos = 0
    for block in re.split(r"(\n\s*\n)", text):
        if not block.strip() or re.fullmatch(r"\n\s*\n", block):
            pos += len(block)
            continue
        line_start = pos
        cur_start: int | None = None
        cur_end = pos
        for line in block.splitlines(keepends=True):
            stripped = line.strip()
            if stripped:
                lead = len(line) - len(line.lstrip())
                s = line_start + lead
                e = line_start + len(line.rstrip())
                if cur_start is not None and SPEAKER_RE.match(line):
                    units.append((cur_start, cur_end))
                    cur_start = None
                if cur_start is None:
                    cur_start = s
                cur_end = e
            line_start += len(line)
        if cur_start is not None:
            units.append((cur_start, cur_end))
        pos += len(block)

    passages: list[Passage] = []
    for start, end in units:
        chunk = text[start:end]
        if len(chunk) <= max_chars:
            passages.append(Passage(chunk, start, end))
            continue
        # sentence-split long units, packing sentences up to max_chars
        piece_start = start
        cursor = start
        for sentence_end in [m.start() for m in SENTENCE_RE.finditer(chunk)] + [len(chunk)]:
            abs_end = start + sentence_end
            if abs_end - piece_start > max_chars and cursor > piece_start:
                passages.append(Passage(text[piece_start:cursor].strip(), piece_start, cursor))
                piece_start = cursor
                while piece_start < end and text[piece_start].isspace():
                    piece_start += 1
            cursor = abs_end
        if cursor > piece_start:
            passages.append(Passage(text[piece_start:cursor].strip(), piece_start, cursor))
    return [p for p in passages if p.text]
