"""Guard against data contamination of locate v2's example asks.

An example ask that is also a labeled evaluation case inflates every reported gain — the model
sees the gold ask at training time and again at test time. The reviewer measured 121 exact and
96 near matches between asks/*.jsonl and the eval sets. This module is the gate they built.

An ask matches an eval case when, after a shared normalization (lowercase, drop punctuation and
apostrophes, collapse whitespace), either the strings are equal (exact) or the Jaccard overlap
of their word sets is at least 0.75 (near). The normalization and the measure are the ones the
reviewer's scripts use, so numbers here compare to theirs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

NEAR_THRESHOLD = 0.75


def norm(s: str) -> str:
    """Lowercase alphanumerics, apostrophes dropped, single spaces. Reviewer's exact rule."""
    assert isinstance(s, str), "norm wants a string"
    low = s.lower().replace("'", "")
    out = " ".join(re.findall(r"[a-z0-9]+", low))
    assert "'" not in out, "apostrophes must be gone"
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    assert isinstance(a, set) and isinstance(b, set), "sets please"
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def overlaps(train_asks: list[str], eval_asks: list[str],
             threshold: float = NEAR_THRESHOLD) -> list[tuple[str, str, float]]:
    """Pairs (train_ask, eval_ask, jaccard) where normalized equal or jaccard >= threshold.

    An exact match is reported with jaccard = 1.0. A near match needs at least two words in the
    eval ask (one-word asks have no information to overlap on; the reviewer's rule)."""
    assert 0.0 < threshold <= 1.0, f"threshold {threshold} out of range"
    assert isinstance(train_asks, list) and isinstance(eval_asks, list), "lists please"
    train = {norm(a): a for a in train_asks}
    train_sets = {t: set(t.split()) for t in train}
    hits: list[tuple[str, str, float]] = []
    for ea in eval_asks:
        en = norm(ea)
        if not en:
            continue
        if en in train:
            hits.append((train[en], ea, 1.0))
            continue
        es = set(en.split())
        if len(es) < 2:
            continue
        for t, ts in train_sets.items():
            j = _jaccard(es, ts)
            if j >= threshold:
                hits.append((train[t], ea, round(j, 2)))
    return hits


def _ask_strings(doc) -> list[str]:
    """Pull every ``ask`` out of a JSON document.

    Three shapes are read: ``{"cases": [{"ask": ...}, ...]}`` and lists / nested dicts that carry
    ``ask`` fields; and the ``labels_source`` shape ``{split: {ask_text: [source_ids]}}`` where
    asks are inner-dict keys. Any other string key is ignored."""
    out: list[str] = []
    if _is_labels_source(doc):
        for split in doc.values():
            for k in split:
                if isinstance(k, str):
                    out.append(k)
        return out
    _walk(doc, out, 0)
    return out


def _is_labels_source(doc) -> bool:
    """{split: {ask: [ids] | null}} — the retrieval labels_source.json shape (unlabeled asks are null)."""
    if not isinstance(doc, dict) or not doc:
        return False
    for v in doc.values():
        if not isinstance(v, dict) or not v:
            return False
        for inner in v.values():
            ok = inner is None or (isinstance(inner, list) and all(isinstance(x, str) for x in inner))
            if not ok:
                return False
    return True


_WALK_MAX = 100_000


def _walk(x, out: list[str], depth: int) -> None:
    assert depth < 32, "ask-tree too deep"
    assert len(out) < _WALK_MAX, "ask-tree too wide"
    if isinstance(x, dict):
        a = x.get("ask")
        if isinstance(a, str):
            out.append(a)
        for v in x.values():
            if isinstance(v, (dict, list)):
                _walk(v, out, depth + 1)
    elif isinstance(x, list):
        for v in x:  # a raw string in a list is a label (source id, class), not an ask
            if isinstance(v, (dict, list)):
                _walk(v, out, depth + 1)


def eval_asks_from(path: Path) -> list[str]:
    """Every ``ask`` string in one labeled file; raises on bad JSON."""
    assert path.exists(), f"missing eval file: {path}"
    assert path.is_file(), f"not a file: {path}"
    return _ask_strings(json.loads(path.read_text()))


def train_asks_from_library(asks_dir: Path) -> list[str]:
    """Every ask in asks/sources.jsonl + asks/routes.jsonl."""
    assert asks_dir.is_dir(), f"not a directory: {asks_dir}"
    out: list[str] = []
    for name in ("sources.jsonl", "routes.jsonl"):
        p = asks_dir / name
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            doc = json.loads(line)
            for a in doc.get("asks", []):
                if isinstance(a, str):
                    out.append(a)
    assert out, "no training asks found"
    return out
