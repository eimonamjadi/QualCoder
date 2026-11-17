"""Lightweight retrieval (BM25) over the codebook, examples and past corrections.

Why BM25 instead of an embedding model: it runs anywhere with no downloads, no API
key and no GPU, and for short codebook entries and quotes it is a solid baseline.
The ``Retriever`` interface is small so an embedding retriever (for example Ollama
``nomic-embed-text`` or a sentence-transformers model) can be swapped in later.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

STOPWORDS = set(
    """a an and are as at be been but by for from had has have he her his i if in into is it
    its me my no not of on or our she so than that the their them then there these they this
    to too us was we were what when which who will with would you your just like really very
    um uh yeah okay ok kind sort""".split()
)
TOKEN_RE = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    out = []
    for tok in TOKEN_RE.findall(text.lower()):
        tok = tok.strip("'")
        if len(tok) < 2 or tok in STOPWORDS:
            continue
        # crude stemming, enough to match "feel/feeling/feels"
        for suffix in ("ing", "ed", "es", "s"):
            if len(tok) > len(suffix) + 3 and tok.endswith(suffix):
                tok = tok[: -len(suffix)]
                break
        out.append(tok)
    return out


@dataclass
class Item:
    ref: str                 # citation id shown to the model, e.g. "CODE:3" or "EX:12"
    kind: str                # "code" | "example"
    text: str                # what gets indexed
    payload: dict = field(default_factory=dict)


class BM25Retriever:
    def __init__(self, items: list[Item], k1: float = 1.5, b: float = 0.75):
        self.items = items
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(i.text)) for i in items]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if items else 0.0
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(items)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def score(self, query: str) -> list[tuple[float, Item]]:
        q = tokenize(query)
        scored = []
        for doc, length, item in zip(self.docs, self.lengths, self.items):
            s = 0.0
            for t in q:
                if t not in doc:
                    continue
                tf = doc[t]
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self.avg_len or 1))
                s += self.idf[t] * tf * (self.k1 + 1) / denom
            scored.append((s, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    def top(self, query: str, k: int, min_score: float = 0.0) -> list[Item]:
        return [item for s, item in self.score(query)[:k] if s > min_score]
