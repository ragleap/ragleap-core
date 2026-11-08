"""A deterministic fake embedder for tests: hashes words into a small vector. Words in CONCEPTS share one
direction, so "car" and "automobile" are close and a keyword search cannot find one with the other."""
import hashlib
import re

DIM = 32
MODEL = "fake-1"
CONCEPTS = {"car": "vehicle", "automobile": "vehicle", "truck": "vehicle", "van": "vehicle",
            "doctor": "medic", "physician": "medic"}


def embed_text(text, dim=DIM):
    v = [0.0] * dim
    for w in re.findall(r"\w+", text.lower()):
        h = hashlib.sha256(CONCEPTS.get(w, w).encode()).digest()
        v[h[0] % dim] += 1.0 if h[1] % 2 == 0 else -1.0
    if not any(v):
        v[0] = 1.0
    return v


class FakeEmbedder:
    """Records every batch it is asked to embed in `calls` (a list of lists of texts)."""

    def __init__(self, dim=DIM):
        self.dim = dim
        self.calls = []

    def __call__(self, texts):
        self.calls.append(list(texts))
        return [embed_text(t, self.dim) for t in texts]

    @property
    def texts(self):
        return [t for batch in self.calls for t in batch]


def words(text):
    """Token estimator for tests: one token per word."""
    return len(text.split())
