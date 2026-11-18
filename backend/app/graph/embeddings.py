"""Embedders for the Tier 3 classifier. Both produce 1536-dim unit vectors (the pgvector column size).

* `HashingEmbedder` (default when LLM_API_KEY is unset): deterministic feature hashing of word unigrams and
  bigrams with sublinear term frequency, so CI and local runs need no external service.
* `OpenAIEmbedder`: any OpenAI-compatible /embeddings endpoint (LLM_BASE_URL, EMBEDDING_MODEL).
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol

DIM = 1536
WORD_RE = re.compile(r"[a-z][a-z0-9]+")
STOP = {"the", "and", "for", "with", "from", "this", "that", "our", "your", "are", "was", "were", "has", "have",
        "not", "but", "you", "can", "cannot", "after", "when", "into", "about", "please", "help", "there", "they",
        "its", "it's", "been", "being", "will", "would", "should", "could", "just", "all", "any", "some", "hi",
        "hello", "thanks", "thank", "team", "issue", "problem", "question", "working", "work", "works"}


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def words(text: str) -> list[str]:
    return [w for w in WORD_RE.findall(text.lower()) if w not in STOP]


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def to_pgvector(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


class HashingEmbedder:
    model = "hashing-v1-1536"

    def _one(self, text: str) -> list[float]:
        v = [0.0] * DIM
        ws = words(text)
        grams: dict[str, int] = {}
        for g in ws + [f"{a}_{b}" for a, b in zip(ws, ws[1:])]:
            grams[g] = grams.get(g, 0) + 1
        for g, tf in grams.items():
            h = hashlib.md5(g.encode()).digest()
            idx = int.from_bytes(h[:4], "big") % DIM
            sign = 1.0 if h[4] & 1 else -1.0
            weight = (1 + math.log(tf)) * (0.6 if "_" in g else 1.0)
            v[idx] += sign * weight
        return _normalize(v)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class OpenAIEmbedder:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key, self.base_url, self.model = api_key, base_url.rstrip("/"), model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60) as client:
            for i in range(0, len(texts), 256):
                r = await client.post(f"{self.base_url}/embeddings",
                                      headers={"Authorization": f"Bearer {self.api_key}"},
                                      json={"model": self.model, "input": texts[i:i + 256], "dimensions": DIM})
                r.raise_for_status()
                out += [_normalize(d["embedding"]) for d in sorted(r.json()["data"], key=lambda d: d["index"])]
        return out


def default_embedder(env: dict[str, str] | None = None) -> Embedder:
    env = os.environ if env is None else env
    if env.get("LLM_API_KEY"):
        return OpenAIEmbedder(env["LLM_API_KEY"], env.get("LLM_BASE_URL", "https://api.openai.com/v1"),
                              env.get("EMBEDDING_MODEL", "text-embedding-3-small"))
    return HashingEmbedder()
