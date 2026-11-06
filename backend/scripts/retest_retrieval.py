"""Read-only, version-checked retrieval benchmark; no ingest/build/chat calls."""

from __future__ import annotations
import argparse
import hashlib
import json
import math
import random
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

METRICS = ("recall", "precision", "hit_rate", "mrr", "ndcg", "complete_evidence")


def score_hits(ids: list[str], relevant: list[str], k: int) -> dict[str, float]:
    """Macro evidence metrics: unreturned labels remain in recall denominator."""
    if k < 1 or not relevant or len(ids) != len(set(ids)):
        raise ValueError("Invalid K, empty labels or duplicate response IDs")
    relevant = set(relevant)
    ranks = [r for r, cid in enumerate(ids[:k], 1) if cid in relevant]
    ideal = sum(1 / math.log2(r + 1) for r in range(1, min(k, len(relevant)) + 1))
    return {
        "recall": len(ranks) / len(relevant),
        "precision": len(ranks) / k,
        "hit_rate": float(bool(ranks)),
        "mrr": 1 / min(ranks) if ranks else 0.0,
        "ndcg": sum(1 / math.log2(r + 1) for r in ranks) / ideal,
        "complete_evidence": float(relevant <= set(ids[:k])),
    }


def percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    p = (len(values) - 1) * q
    low, high = math.floor(p), math.ceil(p)
    return values[low] + (values[high] - values[low]) * (p - low)


def api(base: str, route: str, payload: dict | None = None):
    body = (
        json.dumps(payload, ensure_ascii=False).encode()
        if payload is not None
        else None
    )
    request = Request(
        base + route, data=body, headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=40) as response:
        return json.load(response)


def read_suite(base: str, directory: Path, name: str) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    corpus = json.loads((directory / "corpus.json").read_text())
    cases = json.loads((directory / "dataset.json").read_text())
    doc_ids = manifest.get("document_ids") or [manifest["document_id"]]
    live = []
    scopes = set()
    for did in doc_ids:
        scopes.add(api(base, "/documents/" + did)["knowledge_base"])
        live.extend(api(base, "/documents/" + did + "/chunks"))
    expected = {row.get("chunk_id") or row["id"]: row["content"] for row in corpus}
    current = {row["id"]: row["content"] for row in live}
    if (
        len(scopes) != 1
        or current != expected
        or any(not row["enabled"] for row in live)
    ):
        raise ValueError(name + ": corpus changed; review labels before testing")
    if any(
        not case["relevant_chunk_ids"]
        or not set(case["relevant_chunk_ids"]) <= current.keys()
        for case in cases
    ):
        raise ValueError(name + ": invalid evidence labels")
    kb = scopes.pop()
    scope_docs = [d for d in api(base, "/documents") if d["knowledge_base"] == kb]
    states = []
    offset = 0
    while True:
        page = api(base, f"/graph/states?offset={offset}&limit=100")["items"]
        states.extend(s for s in page if s["document_id"] in doc_ids)
        if len(page) < 100:
            break
        offset += 100
    return {
        "name": name,
        "knowledge_base": kb,
        "document_ids": doc_ids,
        "cases": cases,
        "labelled_chunk_count": len(current),
        "question_count": len(cases),
        "scope_document_count": len(scope_docs),
        "scope_declared_chunk_count": sum(d["chunk_count"] for d in scope_docs),
        "content_sha256": hashlib.sha256(
            json.dumps(sorted(current.items()), ensure_ascii=False).encode()
        ).hexdigest(),
        "graph_states": states,
        "indexed_graph_documents": sum(
            s["published_version"] is not None
            and s["desired_version"] == s["published_version"]
            for s in states
        ),
    }


def summarize(rows: list[dict]) -> dict:
    result = {m: statistics.mean(r[m] for r in rows) for m in METRICS}
    result.update(
        requests=len(rows),
        errors=sum("error" in r for r in rows),
        p50_ms=percentile([r["latency_ms"] for r in rows], 0.5),
        p95_ms=percentile([r["latency_ms"] for r in rows], 0.95),
        graph_status_counts=dict(Counter(r.get("graph_status", "error") for r in rows)),
        postgres_fallback_requests=sum(
            "postgres_fallback" in r.get("retrieval_sources", []) for r in rows
        ),
    )
    result["rounds"] = [
        {
            "round": n,
            **{
                m: statistics.mean(r[m] for r in rows if r["round"] == n)
                for m in METRICS
            },
        }
        for n in sorted({r["round"] for r in rows})
    ]
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3, choices=range(1, 6))
    args = parser.parse_args()
    suites = [
        read_suite(args.base, args.original, "original_60"),
        read_suite(args.base, args.graph, "graph_20_synthetic"),
    ]
    args.output.mkdir(parents=True, exist_ok=False)

    def save(name, value):
        (args.output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save("fixed_suites.json", suites)
    manifest = {
        "started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "rounds": args.rounds,
        "ks": [5, 10],
        "modes": ["hybrid", "graph_hybrid"],
        "health_before": api(args.base.removesuffix("/api/v1"), "/health"),
        "note": "Read-only retrieval; no builds, indexing, ingestion or chat. Assistant-authored non-exhaustive labels; FAQ remains searchable. Shuffled sequential requests after warm-up; not a load test. Calls cloud embedding service.",
    }
    save("manifest.json", manifest)
    warmups = []
    for suite in suites:
        for mode in ["hybrid", "graph_hybrid"]:
            response = api(
                args.base,
                "/retrieval/search",
                {
                    "query": suite["cases"][0]["question"],
                    "top_k": 5,
                    "retrieval_mode": mode,
                    "knowledge_base": suite["knowledge_base"],
                },
            )
            warmups.append(
                {
                    "suite": suite["name"],
                    "mode": mode,
                    "graph_status": response["graph_status"],
                }
            )
    save("warmups.json", warmups)
    rows = []
    with (args.output / "raw_results.jsonl").open("x", encoding="utf-8") as raw:
        for n in range(1, args.rounds + 1):
            schedule = [
                (s, c, m, k)
                for s in suites
                for c in s["cases"]
                for m in ["hybrid", "graph_hybrid"]
                for k in [5, 10]
            ]
            random.Random(20261102 + n).shuffle(schedule)
            for index, (suite, case, mode, k) in enumerate(schedule, 1):
                started = time.perf_counter()
                row = {
                    "suite": suite["name"],
                    "id": case["id"],
                    "round": n,
                    "mode": mode,
                    "k": k,
                }
                try:
                    response = api(
                        args.base,
                        "/retrieval/search",
                        {
                            "query": case["question"],
                            "top_k": k,
                            "retrieval_mode": mode,
                            "knowledge_base": suite["knowledge_base"],
                        },
                    )
                    row.update(
                        score_hits(
                            [h["chunk_id"] for h in response["items"]],
                            case["relevant_chunk_ids"],
                            k,
                        )
                    )
                    row.update(
                        graph_status=response["graph_status"],
                        degradation_reason=response["degradation_reason"],
                        retrieval_sources=sorted(
                            {
                                v
                                for h in response["items"]
                                for v in h["retrieval_sources"]
                            }
                        ),
                    )
                except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as exc:
                    response = None
                    row.update(error=type(exc).__name__, **dict.fromkeys(METRICS, 0.0))
                row["latency_ms"] = (time.perf_counter() - started) * 1000
                rows.append(row)
                raw.write(
                    json.dumps(
                        {"metrics": row, "response": response}, ensure_ascii=False
                    )
                    + "\n"
                )
                raw.flush()
                if index % 80 == 0:
                    print(f"round={n} completed={index}/{len(schedule)}", flush=True)
            save("partial_metrics.json", rows)
    for suite, directory in zip(suites, [args.original, args.graph]):
        after = read_suite(args.base, directory, suite["name"])
        if (
            after["content_sha256"] != suite["content_sha256"]
            or after["graph_states"] != suite["graph_states"]
        ):
            save(
                "invalidated.json",
                {"reason": "Concurrent corpus or graph publication change"},
            )
            raise RuntimeError("Benchmark invalidated by concurrent changes")
    summary = {
        s["name"]: {
            m: {
                str(k): summarize(
                    [
                        r
                        for r in rows
                        if r["suite"] == s["name"] and r["mode"] == m and r["k"] == k
                    ]
                )
                for k in [5, 10]
            }
            for m in ["hybrid", "graph_hybrid"]
        }
        for s in suites
    }
    save("summary.json", summary)
    manifest.update(
        finished_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        measured_requests=len(rows),
        health_after=api(args.base.removesuffix("/api/v1"), "/health"),
    )
    save("manifest.json", manifest)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
