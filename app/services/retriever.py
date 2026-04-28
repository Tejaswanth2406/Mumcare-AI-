"""
Production-grade RAG retrieval layer.

Architecture:
  - Products loaded once at startup, validated against a strict schema,
    and cached in-process (no re-reads per request)
  - Scored with a multi-signal ranking function:
      • Intent-category match          (+3.0)
      • Tag exact match                (+2.0 per hit)
      • Tag token overlap              (+1.5 per hit)
      • Product name token overlap     (+1.5 per hit)
      • Description word overlap       (+1.0 per word hit)
      • Partial / stem overlap         (+0.5 per hit)
      • Boost for in-stock products    (+1.0)
      • Recency boost (newer products) (+0.0–0.5 proportional)
  - Hard minimum-score threshold guards against garbage results
  - Input constraints enforced before any catalogue work is done
  - Deterministic, no external dependencies — fast and debuggable
  - Structured logs for every retrieval (scores, timing, cache state)
  - Thread-safe lazy initialisation with double-checked locking

Upgrade path to full vector-DB RAG:
  Replace `retrieve_products` body with a call to your embedding store
  (e.g. Pinecone, Weaviate, pgvector).  Everything above and below this
  layer is untouched.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from app.core.logger import get_logger

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_PRODUCTS_PATH: Final[Path] = (
    Path(__file__).parent.parent / "data" / "products.json"
)

# Scoring weights — centralised so they can be tuned / A-B tested easily.
_W_INTENT_MATCH: Final[float] = 3.0
_W_TAG_EXACT: Final[float] = 2.0
_W_TAG_TOKEN: Final[float] = 1.5
_W_NAME_TOKEN: Final[float] = 1.5
_W_DESC_WORD: Final[float] = 1.0
_W_STEM: Final[float] = 0.5
_W_IN_STOCK: Final[float] = 1.0
_W_RECENCY_MAX: Final[float] = 0.5

# Only return products whose score clears this bar.
_MIN_SCORE_THRESHOLD: Final[float] = 0.5

# Guard against runaway top_k values.
_MAX_TOP_K: Final[int] = 50
_MIN_STEM_LEN: Final[int] = 5

# Required keys every product document must contain.
_REQUIRED_PRODUCT_KEYS: Final[frozenset[str]] = frozenset(
    {"product_id", "product_name", "description", "category", "tags"}
)

# Pre-compiled tokeniser — reused across all calls.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\b[a-z]{2,}\b")

# ─────────────────────────────────────────────────────────────────────────────
# Structured result type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """
    Immutable wrapper around a retrieved product with its relevance score.

    Attributes:
        product:  Raw product dict from the catalogue.
        score:    Relevance score (higher = more relevant).
        rank:     1-based position in the result list.
    """

    product: dict[str, Any]
    score: float
    rank: int

    @property
    def product_id(self) -> str:
        return str(self.product.get("product_id", ""))

    @property
    def product_name(self) -> str:
        return str(self.product.get("product_name", ""))


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue loader — thread-safe lazy singleton
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _CatalogueState:
    products: list[dict[str, Any]] = field(default_factory=list)
    loaded: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_catalogue = _CatalogueState()


def _validate_product(product: Any, index: int) -> None:
    """
    Raise ``ValueError`` if *product* is missing required keys or has wrong types.

    Args:
        product:  Candidate product object from JSON.
        index:    Position in the source array (for error messages).

    Raises:
        ValueError: On schema violation.
    """
    if not isinstance(product, dict):
        raise ValueError(
            f"Product at index {index} must be a JSON object, "
            f"got {type(product).__name__}."
        )
    # Accept legacy/alternate key `id` and normalise to `product_id` for internal use
    if "product_id" not in product and "id" in product:
        product["product_id"] = product["id"]

    missing = _REQUIRED_PRODUCT_KEYS - product.keys()
    if missing:
        raise ValueError(
            f"Product at index {index} (id={product.get('product_id', '?')!r}) "
            f"is missing required keys: {', '.join(sorted(missing))}."
        )
    if not isinstance(product["tags"], list):
        raise ValueError(
            f"Product {product['product_id']!r}: 'tags' must be a list, "
            f"got {type(product['tags']).__name__}."
        )


def _load_products() -> list[dict[str, Any]]:
    """
    Load, validate, and return the product catalogue.

    Thread-safe double-checked locking ensures the file is read exactly
    once per process lifetime even under concurrent first requests.

    Raises:
        FileNotFoundError:  If the catalogue file is absent.
        ValueError:         If any product fails schema validation.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    if _catalogue.loaded:
        return _catalogue.products

    with _catalogue.lock:
        if _catalogue.loaded:           # second check inside the lock
            return _catalogue.products

        if not _PRODUCTS_PATH.exists():
            raise FileNotFoundError(
                f"Product catalogue not found at {_PRODUCTS_PATH}. "
                "Ensure the data file is present before starting the service."
            )

        raw = _PRODUCTS_PATH.read_text(encoding="utf-8")
        products: list[Any] = json.loads(raw)

        if not isinstance(products, list):
            raise ValueError(
                f"Product catalogue must be a JSON array, "
                f"got {type(products).__name__}."
            )

        for i, p in enumerate(products):
            _validate_product(p, i)

        _catalogue.products = products
        _catalogue.loaded = True

    log.info(
        "product_catalogue_loaded",
        path=str(_PRODUCTS_PATH),
        count=len(_catalogue.products),
    )
    return _catalogue.products


def invalidate_catalogue_cache() -> None:
    """
    Force the next retrieval call to reload the catalogue from disk.

    Use after a hot-reload of ``products.json`` in staging / admin flows.
    Thread-safe.
    """
    with _catalogue.lock:
        _catalogue.products = []
        _catalogue.loaded = False
    _tokenise.cache_clear()
    log.info("product_catalogue_cache_invalidated")


# ─────────────────────────────────────────────────────────────────────────────
# Tokenisation (cached)
# ─────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=2048)
def _tokenise(text: str) -> frozenset[str]:
    """
    Lower-case word tokeniser — strips punctuation, returns a frozenset.

    The result is cached so repeated calls with the same product
    description (which never change) are O(1) after the first call.

    Args:
        text: Any string to tokenise.

    Returns:
        Frozenset of lowercase alphabetic tokens of length >= 2.
    """
    return frozenset(_TOKEN_RE.findall(text.lower()))


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────


def _recency_boost(product: dict[str, Any], newest_year: int) -> float:
    """
    Return a proportional boost in ``[0.0, _W_RECENCY_MAX]`` based on
    the product's ``launch_year`` relative to the newest year in the catalogue.

    Products without a ``launch_year`` receive 0.0.
    """
    launch_year = product.get("launch_year")
    if not isinstance(launch_year, int) or newest_year == 0:
        return 0.0
    # Clamp to non-negative; older products get smaller boost
    age = max(0, newest_year - launch_year)
    # Decay: full boost at age 0, halved every 3 years
    return _W_RECENCY_MAX * (0.5 ** (age / 3.0))


def _score_product(
    product: dict[str, Any],
    query_tokens: frozenset[str],
    intent: str,
    newest_year: int,
) -> float:
    """
    Compute a multi-signal relevance score for *product* against *query_tokens*.

    Signals (see module docstring for weights):
      1. Intent / category match
      2. Tag exact match
      3. Tag token overlap
      4. Product name token overlap
      5. Description word overlap
      6. Partial / stem overlap
      7. In-stock boost
      8. Recency boost

    Args:
        product:      A validated product dict from the catalogue.
        query_tokens: Pre-computed frozenset of query tokens.
        intent:       Detected intent category.
        newest_year:  Highest ``launch_year`` in the full catalogue.

    Returns:
        Float relevance score >= 0.0.
    """
    score: float = 0.0
    query_joined = " ".join(query_tokens)

    # 1. Intent / category match
    if intent and intent.lower() == product.get("category", "").lower():
        score += _W_INTENT_MATCH

    # 2 & 3. Tag matching
    for tag in product.get("tags", []):
        normalised_tag = tag.lower().replace("_", " ")
        if normalised_tag in query_joined:
            score += _W_TAG_EXACT
        elif _tokenise(tag) & query_tokens:
            score += _W_TAG_TOKEN

    # 4. Product name token overlap
    name_tokens = _tokenise(product.get("product_name", ""))
    score += len(name_tokens & query_tokens) * _W_NAME_TOKEN

    # 5. Description word overlap
    desc_tokens = _tokenise(product.get("description", ""))
    score += len(desc_tokens & query_tokens) * _W_DESC_WORD

    # 6. Partial / stem match — "breastfeed" ~ "breastfeeding"
    for q_tok in query_tokens:
        if len(q_tok) < _MIN_STEM_LEN:
            continue
        q_prefix = q_tok[:_MIN_STEM_LEN]
        for d_tok in desc_tokens:
            if len(d_tok) >= _MIN_STEM_LEN and (
                d_tok.startswith(q_prefix) or q_tok.startswith(d_tok[:_MIN_STEM_LEN])
            ):
                score += _W_STEM
                break  # count once per query token to avoid runaway scores

    # 7. In-stock boost
    if product.get("in_stock") is True:
        score += _W_IN_STOCK

    # 8. Recency boost
    score += _recency_boost(product, newest_year)

    return score


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def retrieve_products(
    query: str,
    intent: str,
    top_k: int | None = None,
) -> list[RetrievalResult]:
    """
    Retrieve the top-k most relevant products for the given query.

    Input constraints are enforced before any catalogue work is done so
    callers receive a clear error rather than a silent empty list.

    Args:
        query:   Raw (pre-sanitized) user query string.
        intent:  Detected intent category (e.g. ``"postpartum_care"``).
        top_k:   Maximum products to return.  Defaults to
                 ``settings.retrieval_top_k``.  Clamped to
                 ``[1, _MAX_TOP_K]``.

    Returns:
        Ordered list of :class:`RetrievalResult` (highest score first).
        Empty list when no product clears ``_MIN_SCORE_THRESHOLD``.

    Raises:
        ValueError:         On invalid *query* or *top_k* arguments.
        FileNotFoundError:  If the catalogue file is missing at startup.
    """
    from app.core.config import get_settings  # local import avoids circular

    # ── Input validation ────────────────────────────────────────────────────
    if not isinstance(query, str) or not query.strip():
        raise ValueError("'query' must be a non-empty string.")

    if top_k is None:
        top_k = get_settings().retrieval_top_k

    if not isinstance(top_k, int) or top_k < 1:
        raise ValueError(f"'top_k' must be a positive integer, got {top_k!r}.")

    top_k = min(top_k, _MAX_TOP_K)  # hard ceiling — no accidental full-scan returns

    # ── Load catalogue ──────────────────────────────────────────────────────
    t_start = time.perf_counter()
    products = _load_products()

    if not products:
        log.warning("retrieve_products_called_with_empty_catalogue")
        return []

    # ── Pre-compute reusable values ─────────────────────────────────────────
    query_tokens: frozenset[str] = _tokenise(query.strip())

    # Determine newest launch_year once — used by recency boost
    newest_year: int = max(
        (p.get("launch_year", 0) for p in products if isinstance(p.get("launch_year"), int)),
        default=0,
    )

    # ── Score all products ──────────────────────────────────────────────────
    scored: list[tuple[float, dict[str, Any]]] = []
    for product in products:
        s = _score_product(product, query_tokens, intent, newest_year)
        if s >= _MIN_SCORE_THRESHOLD:
            scored.append((s, product))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_scored = scored[:top_k]

    results: list[RetrievalResult] = [
        RetrievalResult(product=p, score=round(s, 4), rank=i + 1)
        for i, (s, p) in enumerate(top_scored)
    ]

    elapsed_ms = (time.perf_counter() - t_start) * 1000

    log.info(
        "products_retrieved",
        query_preview=query[:80],
        intent=intent,
        top_k=top_k,
        catalogue_size=len(products),
        candidates_above_threshold=len(scored),
        returned=len(results),
        elapsed_ms=round(elapsed_ms, 2),
        top_scores=[r.score for r in results[:5]],
        top_ids=[r.product_id for r in results[:5]],
    )

    if not results:
        log.warning(
            "no_products_above_threshold",
            query_preview=query[:80],
            intent=intent,
            min_score_threshold=_MIN_SCORE_THRESHOLD,
        )

    return results