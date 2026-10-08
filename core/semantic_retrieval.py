import os
import math
import re
import threading
from collections import Counter
from typing import Sequence, List, Dict, Any, Optional

import numpy as np

def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]+", (text or "").lower()) if len(t) > 2]

def _tfidf_vectors(texts: Sequence[str]) -> list[dict[str, float]]:
    tokenized = [_tokenize(text) for text in texts]
    n = len(tokenized)
    if not n:
        return []
    df: Counter[str] = Counter()
    for tokens in tokenized:
        df.update(set(tokens))
    vectors: list[dict[str, float]] = []
    for tokens in tokenized:
        tf = Counter(tokens)
        vec = {}
        for term, count in tf.items():
            idf = math.log((1 + n) / (1 + df[term])) + 1.0
            vec[term] = (count / max(len(tokens), 1)) * idf
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm:
            vec = {k: v / norm for k, v in vec.items()}
        vectors.append(vec)
    return vectors


_SEMANTIC_CANONICAL = {
    "slow": "latency", "slower": "latency", "latency": "latency",
    "throughput": "throughput", "volume": "throughput", "high-volume": "throughput",
    "verification": "verification", "verify": "verification", "verifier": "verification",
    "increases": "increase", "increased": "increase", "increase": "increase",
    "becomes": "increase", "grows": "increase", "scales": "scale", "scalability": "scale",
    "cannot": "limit", "limited": "limit", "limitation": "limit", "bottleneck": "limit",
    "unacceptable": "limit", "impractical": "limit", "acceptable": "limit", "too": "limit",
}
_SEMANTIC_STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "and", "or", "but", "with", "for", "to", "in", "of", "on", "when", "that", "this", "be", "only", "from"}

def _canonicalize_text(text: str) -> str:
    tokens = _tokenize(text)
    canonical = []
    for token in tokens:
        if token in _SEMANTIC_STOPWORDS:
            continue
        canonical.append(_SEMANTIC_CANONICAL.get(token, token))
    return " ".join(canonical)

def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(term, 0.0) for term, value in left.items())

_EMBEDDING_MODEL = None
_EMBEDDING_LOCK = threading.RLock()
_EMBEDDING_CACHE: Dict[str, Any] = {}

def get_embedding_model():
    global _EMBEDDING_MODEL
    with _EMBEDDING_LOCK:
        if _EMBEDDING_MODEL is None:
            enabled = os.environ.get("RIF_USE_TRANSFORMER_EMBEDDINGS", "false").strip().lower() in {"1", "true", "yes", "on"}
            if not enabled:
                _EMBEDDING_MODEL = "UNAVAILABLE"
            else:
                model_name = os.environ.get("RIF_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
                try:
                    from sentence_transformers import SentenceTransformer
                    _EMBEDDING_MODEL = SentenceTransformer(model_name)
                except Exception:
                    _EMBEDDING_MODEL = "UNAVAILABLE"
        return _EMBEDDING_MODEL

def _hashed_fallback_embedding(text: str, dimensions: int = 384) -> np.ndarray:
    """NON-SEMANTIC hash-based embedding. For offline/test use ONLY.
    
    WARNING: This does NOT capture semantic similarity.
    Results from this fallback MUST be excluded from semantic-quality metrics.
    """
    vector = np.zeros(dimensions, dtype=np.float32)
    tokens = _tokenize(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = __import__("hashlib").sha256(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "little") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[idx] += sign
    norm = float(np.linalg.norm(vector))
    if norm:
        vector /= norm
    return vector


def _get_embedding(text: str, content_hash: Optional[str] = None) -> tuple[np.ndarray, str]:
    if content_hash and content_hash in _EMBEDDING_CACHE:
        return _EMBEDDING_CACHE[content_hash]

    with _EMBEDDING_LOCK:
        if content_hash and content_hash in _EMBEDDING_CACHE:
            return _EMBEDDING_CACHE[content_hash]
        model = get_embedding_model()
        if model == "UNAVAILABLE":
            emb, method = _hashed_fallback_embedding(text), "hashed_fallback"
        else:
            emb, method = model.encode(text, normalize_embeddings=True, show_progress_bar=False), "transformer"
        if content_hash:
            _EMBEDDING_CACHE[content_hash] = (emb, method)
        return emb, method

def semantic_scores(query: str, documents: Sequence[str]) -> list[float]:
    if not documents:
        return []
    
    model = get_embedding_model()
    if model != "UNAVAILABLE":
        try:
            from sentence_transformers import util
            q_emb, _ = _get_embedding(query)
            docs_emb = model.encode(list(documents), normalize_embeddings=True, show_progress_bar=False)
            scores = util.cos_sim(q_emb, docs_emb).tolist()[0]
            return [max(0.0, min(1.0, float(s))) for s in scores]
        except Exception:
            pass

    # Fallback
    vectors = _tfidf_vectors([query, *documents])
    query_vec = vectors[0] if vectors else {}
    return [max(0.0, min(1.0, _cosine(query_vec, vector))) for vector in vectors[1:]]

def rerank(query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
    scores = semantic_scores(query, documents)
    return sorted(enumerate(scores), key=lambda item: item[1], reverse=True)

def compute_cluster_score(text1: str, hash1: str, type1: str, domain1: str,
                          text2: str, hash2: str, type2: str, domain2: str) -> float:
    # Type similarity
    type_sim = 1.0 if type1 == type2 else 0.0
    
    # Domain similarity
    domain_sim = 1.0 if domain1 == domain2 else 0.0
    
    if get_embedding_model() == "UNAVAILABLE":
        # Dependency-free semantic proxy: canonicalize common research-language
        # synonyms before TF-IDF scoring, while retaining type/domain signals.
        canonical_texts = [_canonicalize_text(text1), _canonicalize_text(text2)]
        vecs = _tfidf_vectors(canonical_texts)
        semantic_sim = _cosine(vecs[0], vecs[1]) if len(vecs) == 2 else 0.0
        canonical_words1 = set(canonical_texts[0].split())
        canonical_words2 = set(canonical_texts[1].split())
        canonical_lexical = len(canonical_words1 & canonical_words2) / max(1, len(canonical_words1 | canonical_words2))
        return semantic_sim * 0.65 + canonical_lexical * 0.20 + type_sim * 0.075 + domain_sim * 0.075

    # Get embeddings with caching
    emb1, _ = _get_embedding(text1, hash1)
    emb2, _ = _get_embedding(text2, hash2)
    
    semantic_sim = 0.0
    if emb1 is not None and emb2 is not None:
        try:
            semantic_sim = float(np.dot(np.asarray(emb1), np.asarray(emb2)))
            semantic_sim = max(0.0, min(1.0, semantic_sim))
        except Exception:
            semantic_sim = 0.0
            
    # Lexical overlap
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    lexical = len(words1.intersection(words2)) / max(1, len(words1.union(words2)))

    if semantic_sim > 0:
        return semantic_sim * 0.5 + lexical * 0.2 + type_sim * 0.15 + domain_sim * 0.15

    vecs = _tfidf_vectors([text1, text2])
    if len(vecs) == 2:
        tf_idf_sim = _cosine(vecs[0], vecs[1])
        return tf_idf_sim * 0.5 + lexical * 0.2 + type_sim * 0.15 + domain_sim * 0.15
    return lexical * 0.5 + type_sim * 0.25 + domain_sim * 0.25
