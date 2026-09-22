"""Swappable embedding abstraction for the RAG pipeline.

Two providers ship today:

- ``DeterministicHashEmbedding``: a pure-Python hashing-trick bag-of-words
  embedding. No model download, no network, fully deterministic across runs
  -- this is what the test suite always uses, and it's a safe offline
  fallback for the app.
- ``OnnxMiniLMEmbedding``: wraps chromadb's bundled local ONNX build of
  all-MiniLM-L6-v2 (via ``chromadb.utils.embedding_functions``). This is the
  production/demo default: real semantic embeddings, runs fully locally
  (no external API calls) after a one-time model download, and is Windows
  compatible since it only needs onnxruntime, not a full torch install.

Both implement the same ``EmbeddingProvider`` protocol, so retrieval code
never depends on which one is in use, and a future provider (e.g. a hosted
embedding API) can be added without touching callers.
"""
from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from typing import List

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

# Common English function words, dropped so keyword/embedding similarity
# reflects shared content vocabulary rather than shared grammar -- without
# this, "what is the ... for a ..." alone was enough to give unrelated
# queries a nontrivial keyword-overlap score.
_STOPWORDS = frozenset(
    """
    the a an is are was were be been being to of in on at for with and
    or but if then so as by from that this these those it its what who
    whom which when where why how do does did can may might must shall
    should will would you your yours he she we they them his her their
    our my me him us all any not no nor
    """.split()
)


def tokenize(text: str) -> List[str]:
    """Lowercase alphanumeric content-word tokenization for embedding + keyword scoring.

    Tokens shorter than 3 characters and common English stopwords are
    dropped -- keeping them in was inflating both hashed-embedding and
    keyword-overlap similarity for essentially unrelated text.
    """
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


class EmbeddingProvider(ABC):
    """A swappable text -> vector embedding backend."""

    name: str
    dimension: int

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts, returning one fixed-length vector per text."""
        raise NotImplementedError


class DeterministicHashEmbedding(EmbeddingProvider):
    """Offline, dependency-free embedding for tests and as a safe fallback.

    Unigram + bigram hashing trick into a fixed-size vector, L2-normalized.
    Deterministic across processes and machines (uses md5, not Python's
    randomized ``hash()``), so retrieval rankings are reproducible in tests.
    """

    name = "deterministic-hash-v1"

    def __init__(self, dimension: int = 256) -> None:
        self.dimension = dimension

    def _hash_index(self, token: str) -> int:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % self.dimension

    def embed(self, texts: List[str]) -> List[List[float]]:
        vectors: List[List[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            tokens = tokenize(text)
            for token in tokens:
                vector[self._hash_index(token)] += 1.0
            for a, b in zip(tokens, tokens[1:]):
                vector[self._hash_index(f"{a}_{b}")] += 0.5
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors


class OnnxMiniLMEmbedding(EmbeddingProvider):
    """Local ONNX all-MiniLM-L6-v2 embedding (chromadb's bundled default model).

    Imports chromadb's embedding-function utilities lazily so importing this
    module never requires onnxruntime unless this provider is actually
    selected. The model is downloaded once and cached locally by chromadb;
    inference afterwards is fully local (no external API calls).
    """

    name = "onnx-minilm-l6-v2"
    dimension = 384

    def __init__(self) -> None:
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        self._fn = ONNXMiniLM_L6_V2()

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[float(x) for x in vector] for vector in self._fn(texts)]


_PROVIDER_ALIASES = {
    "deterministic": DeterministicHashEmbedding,
    "hash": DeterministicHashEmbedding,
    "test": DeterministicHashEmbedding,
    "onnx_minilm": OnnxMiniLMEmbedding,
    "onnx": OnnxMiniLMEmbedding,
    "local": OnnxMiniLMEmbedding,
}


def get_embedding_provider(provider: str, *, model_name: str | None = None) -> EmbeddingProvider:
    """Build an EmbeddingProvider by name (see ``_PROVIDER_ALIASES``).

    ``model_name`` is accepted for forward-compatibility (documented in
    .env.example as CAMPUSNEXUS_EMBEDDING_MODEL) but is not yet threaded
    through to either provider, both of which currently use a single fixed
    model/algorithm.
    """
    key = provider.strip().lower()
    try:
        cls = _PROVIDER_ALIASES[key]
    except KeyError as exc:
        raise ValueError(
            f"unknown embedding provider {provider!r}; expected one of {sorted(set(_PROVIDER_ALIASES))}"
        ) from exc
    return cls()
