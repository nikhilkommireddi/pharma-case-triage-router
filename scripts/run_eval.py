"""Run the eval set through the live pipeline (calls Claude) and score routing.

Usage:  python scripts/run_eval.py [--only EVAL-03,EVAL-07]

Scoring per case:
- destinations: the set of downstream destinations (excluding human_triage)
  must match exactly — a missed drug_safety route counts as a critical miss.
- status: auto_routed vs pending_review, unless expected is "any".
- clock: report category must start with the expected prefix, if given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app import db, pipeline  # noqa: E402
from app.extraction import ClaudeExtractor  # noqa: E402
from app.models import Destination, IntakeCase  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="Comma-separated case IDs")
    args = ap.parse_args()

    cases = json.loads((ROOT / "data" / "eval_cases.json").read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["case"]["case_id"] in wanted]

    conn = db.connect(":memory:")
    extractor = ClaudeExtractor()
    rows, critical = [], 0

    for item in cases:
        case = IntakeCase.model_validate(item["case"])
        exp = item["expected"]
        result = pipeline.triage(conn, case, extractor)

        got = sorted(r.destination.value for r in result.routes if r.destination != Destination.human_triage)
        want = sorted(exp["destinations"])
        dest_ok = got == want
        status_ok = exp["status"] == "any" or (
            result.status.value == exp["status"]
            or (exp["status"] == "pending_review" and result.status.value == "failed")
        )
        clock_cat = result.clock.category if result.clock else None
        clock_ok = exp["clock"] is None or (clock_cat or "").startswith(exp["clock"])
        if "drug_safety" in want and "drug_safety" not in got:
            critical += 1

        rows.append((case.case_id, dest_ok and status_ok and clock_ok, got, want,
                     result.status.value, clock_cat, result.confidence, result.review_reasons))
        mark = "PASS" if rows[-1][1] else "FAIL"
        print(f"[{mark}] {case.case_id} {exp['note']}")
        if not rows[-1][1]:
            print(f"        destinations got={got} want={want} | status={result.status.value} "
                  f"(want {exp['status']}) | clock={clock_cat!r} (want {exp['clock']!r})")
            for reason in result.review_reasons:
                print(f"        review: {reason}")

    passed = sum(1 for r in rows if r[1])
    print(f"\n{passed}/{len(rows)} passed; missed safety routes: {critical}")
    ok, _ = db.verify_audit_chain(conn)
    print(f"Audit chain intact: {ok}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
