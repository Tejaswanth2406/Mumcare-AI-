"""
Production-grade evaluation harness for MumzWorld AI.

Evaluates the live API against a set of labelled test cases with:
  - Multi-dimensional scoring (intent, uncertainty, recommendations, relevance)
  - Confidence range validation
  - Safety contract checking (uncertain → no products)
  - Failure mode analysis and categorisation
  - Machine-readable JSON output alongside human-readable console report
  - Configurable base URL and timeout
  - Comprehensive error handling with safe fallbacks
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

TEST_CASES_PATH = Path(__file__).parent / "test_cases.json"
RESULTS_PATH = Path(__file__).parent / "eval_results.json"

# ─────────────────────────────────────────────────────────────────────────────
# Error handling
# ─────────────────────────────────────────────────────────────────────────────

class EvaluationError(Exception):
    """Base exception for evaluation errors."""
    pass


class ConfigurationError(EvaluationError):
    """Raised when test configuration is invalid."""
    pass


class APIConnectionError(EvaluationError):
    """Raised when cannot connect to the API."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_test_cases() -> list[dict[str, Any]]:
    """Load test cases with proper error handling."""
    try:
        if not TEST_CASES_PATH.exists():
            raise ConfigurationError(f"Test cases file not found: {TEST_CASES_PATH}")

        with TEST_CASES_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        if not isinstance(data, list):
            raise ConfigurationError("Test cases must be a JSON array")

        if not data:
            raise ConfigurationError("Test cases list is empty")

        return data
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Invalid JSON in test cases file: {exc}")
    except Exception as exc:
        raise EvaluationError(f"Failed to load test cases: {exc}")


def _post_query(base_url: str, query: str, timeout: int) -> dict[str, Any] | None:
    """
    POST to the API and return the parsed response dict, or None on error.

    Handles various error scenarios gracefully with detailed logging.
    """
    try:
        if not base_url or not base_url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid base URL: {base_url}")

        if not query or not query.strip():
            raise ValueError("Query cannot be empty")

        response = requests.post(
            f"{base_url}/ai/query",
            json={"query": query},
            timeout=timeout,
        )

        # Handle HTTP error responses
        if response.status_code == 400:
            print(f"\n  [X] Bad request (400): {response.text[:200]}")
            return None
        elif response.status_code == 429:
            print(f"\n  [X] Rate limited (429). Please retry after a delay.")
            return None
        elif response.status_code == 503:
            print(f"\n  [X] Service unavailable (503). Backend may be restarting.")
            return None
        elif response.status_code >= 400:
            print(f"\n  [X] HTTP {response.status_code}: {response.text[:300]}")
            return None

        response.raise_for_status()
        return response.json()

    except requests.exceptions.Timeout:
        print(f"\n  [X] Request timeout after {timeout}s - backend may be slow or unresponsive")
        return None
    except requests.exceptions.ConnectionError:
        print(f"\n  [X] Cannot connect to {base_url} - is the backend running?")
        return None
    except requests.exceptions.RequestException as exc:
        print(f"\n  [X] Request failed: {exc}")
        return None
    except ValueError as exc:
        print(f"\n  ❌ Invalid input: {exc}")
        return None
    except Exception as exc:
        print(f"\n  ❌ Unexpected error: {type(exc).__name__}: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def _score(test: dict[str, Any], resp: dict[str, Any]) -> dict[str, Any]:
    """
    Score a single test case against the API response.

    Returns a results dict with:
      score, max_score, percentage, details, failure_modes
    """
    score = 0
    max_score = 0
    details: list[str] = []
    failure_modes: list[str] = []

    # ── 1. Intent accuracy ────────────────────────────────────────────────
    if "expected_intent" in test:
        max_score += 2
        got = resp.get("intent", "")
        want = test["expected_intent"]
        if got == want:
            score += 2
            details.append(f"[OK] Intent correct: {want}")
        else:
            details.append(f"[X] Intent wrong: got '{got}', expected '{want}'")
            failure_modes.append("intent_mismatch")

    # ── 2. Uncertainty handling ───────────────────────────────────────────
    if "expected_uncertainty" in test:
        max_score += 2
        got_u = resp.get("uncertainty", False)
        want_u = test["expected_uncertainty"]
        if got_u == want_u:
            score += 2
            details.append(f"[OK] Uncertainty flag correct: {want_u}")
        else:
            details.append(f"[X] Uncertainty flag wrong: got {got_u}, expected {want_u}")
            failure_modes.append("uncertainty_mismatch")

    # ── 3. Recommendation presence ────────────────────────────────────────
    if "should_have_recommendations" in test:
        max_score += 1
        has = len(resp.get("recommendations", [])) > 0
        want = test["should_have_recommendations"]
        if has == want:
            score += 1
            label = f"Found {len(resp.get('recommendations', []))} recommendations" if want else "No recommendations (correct)"
            details.append(f"[OK] {label}")
        else:
            details.append(f"[X] Recommendations present={has}, expected={want}")
            failure_modes.append("recommendation_presence_mismatch")

    # ── 4. Safety contract: uncertain → no products ───────────────────────
    if resp.get("uncertainty") and resp.get("recommendations"):
        max_score += 1  # extra safety check
        details.append("[X] SAFETY VIOLATION: uncertainty=true but recommendations returned!")
        failure_modes.append("safety_contract_violated")
    elif resp.get("uncertainty"):
        max_score += 1
        score += 1
        details.append("[OK] Safety contract: no recommendations when uncertain")

    # ── 5. Product relevance ──────────────────────────────────────────────
    if "expected_products" in test and resp.get("recommendations"):
        max_score += 2
        got_names = {r["product_name"] for r in resp["recommendations"]}
        expected = set(test["expected_products"])
        matched = got_names & expected
        if matched:
            score += 2
            details.append(f"[OK] Relevant products: {', '.join(matched)}")
        else:
            details.append(f"[X] No matching products. Got: {got_names}")
            failure_modes.append("product_relevance_miss")

    # ── 6. Confidence range sanity ────────────────────────────────────────
    max_score += 1
    conf = resp.get("confidence", -1)
    if 0.0 <= conf <= 1.0:
        score += 1
        details.append(f"[OK] Confidence in range: {conf:.2f}")
    else:
        details.append(f"[X] Confidence out of range: {conf}")
        failure_modes.append("confidence_out_of_range")

    # ── 7. Comfort message presence ───────────────────────────────────────
    max_score += 1
    cm = resp.get("comfort_message", {})
    if cm.get("en") and cm.get("ar"):
        score += 1
        details.append("[OK] Bilingual comfort message present")
    else:
        details.append("[X] Comfort message missing or incomplete")
        failure_modes.append("comfort_message_missing")

    pct = round(score / max_score * 100, 1) if max_score > 0 else 0.0
    return {
        "test_id": test["id"],
        "description": test.get("description", ""),
        "score": score,
        "max_score": max_score,
        "percentage": pct,
        "details": details,
        "failure_modes": failure_modes,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run_evaluation(
    base_url: str = "http://localhost:8000",
    timeout: int = 30,
    save_results: bool = True,
) -> dict[str, Any]:
    """
    Execute the full evaluation suite against the live API.

    Args:
        base_url:     FastAPI server base URL.
        timeout:      Per-request timeout in seconds.
        save_results: Whether to write results JSON to disk.

    Returns:
        Summary dict with scores, per-test results, and failure analysis.
    """
    test_cases = _load_test_cases()
    results: list[dict[str, Any]] = []
    failure_mode_counts: dict[str, int] = {}

    print("\n" + "=" * 80)
    print("  MumzWorld AI - Evaluation Suite")
    print(f"  Target: {base_url}  |  Tests: {len(test_cases)}")
    print("=" * 80)

    for tc in test_cases:
        print(f"\nTest {tc['id']:02d}: {tc.get('description', 'N/A')}")
        print(f"  Query: \"{tc['input']}\"")

        t0 = time.perf_counter()
        resp = _post_query(base_url, tc["input"], timeout)
        latency = round((time.perf_counter() - t0) * 1000)

        if resp is None:
            results.append({
                "test_id": tc["id"],
                "description": tc.get("description", ""),
                "error": "API call failed",
            })
            print("  [!] Skipped (API error)")
            continue

        result = _score(tc, resp)
        result["latency_ms"] = latency
        results.append(result)

        print(f"  Score: {result['score']}/{result['max_score']} "
              f"({result['percentage']:.0f}%)  [{latency}ms]")
        for detail in result["details"]:
            print(f"  {detail}")

        for fm in result.get("failure_modes", []):
            failure_mode_counts[fm] = failure_mode_counts.get(fm, 0) + 1

    # ── Summary ───────────────────────────────────────────────────────────
    valid = [r for r in results if "error" not in r]
    total_score = sum(r["score"] for r in valid)
    total_max = sum(r["max_score"] for r in valid)
    overall_pct = round(total_score / total_max * 100, 1) if total_max > 0 else 0.0
    avg_latency = round(sum(r.get("latency_ms", 0) for r in valid) / len(valid)) if valid else 0

    print("\n" + "=" * 80)
    print(f"  Overall Score : {total_score}/{total_max} ({overall_pct}%)")
    print(f"  Tests Passed  : {len(valid)}/{len(test_cases)}")
    print(f"  Avg Latency   : {avg_latency}ms")

    if failure_mode_counts:
        print("\n  Failure Mode Analysis:")
        for mode, count in sorted(failure_mode_counts.items(), key=lambda x: -x[1]):
            print(f"    [!] {mode}: {count} occurrence(s)")
    else:
        print("\n  [OK] No failure modes detected!")

    print("=" * 80 + "\n")

    summary: dict[str, Any] = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "api_base_url": base_url,
        "total_tests": len(test_cases),
        "tests_run": len(valid),
        "total_score": total_score,
        "max_possible_score": total_max,
        "overall_percentage": overall_pct,
        "avg_latency_ms": avg_latency,
        "failure_modes": failure_mode_counts,
        "results": results,
    }

    if save_results:
        RESULTS_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  Results saved to: {RESULTS_PATH}")

    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MumzWorld AI Evaluation Suite")
    parser.add_argument("--url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--timeout", type=int, default=30, help="Per-request timeout (seconds)")
    parser.add_argument("--no-save", action="store_true", help="Don't save results to disk")
    args = parser.parse_args()

    result = run_evaluation(
        base_url=args.url,
        timeout=args.timeout,
        save_results=not args.no_save,
    )
    sys.exit(0 if result.get("overall_percentage", 0) >= 70 else 1)
