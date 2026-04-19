"""
Echo Eval Runner

Runs 20 synthetic test cases through the reply_agent + validator pipeline.
Each test auto-selects the FIRST option at every decision node (deterministic path).
Reports: pass rate, avg depth, hallucination count, forbidden-string hits.

Usage:
    cd echo/backend
    python ../evals/eval_runner.py

Output:
    Prints summary to stdout.
    Writes detailed results to evals/eval_report.json
"""

import asyncio
import json
import logging
import os
import sys
import time

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))

import reply_agent
import validator as val_module

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("eval_runner")

CASES_PATH = os.path.join(os.path.dirname(__file__), "test_cases.json")
REPORT_PATH = os.path.join(os.path.dirname(__file__), "eval_report.json")


def load_cases():
    with open(CASES_PATH) as f:
        return json.load(f)


async def run_case(case: dict) -> dict:
    path_taken = []
    depth = 0
    used_intent_confirm = False
    start = time.time()

    while True:
        if depth > 4:
            return {
                "id": case["id"],
                "category": case["category"],
                "passed": False,
                "failure_reason": "tree_too_deep",
                "depth": depth,
                "reply": None,
                "latency_s": round(time.time() - start, 2),
            }

        try:
            result = await reply_agent.get_next_node(
                message=case["message"],
                sender=case["sender"],
                history=case.get("conversation_history", []),
                path_taken=path_taken,
                memory_suggestions="",
                depth=depth,
            )
        except Exception as e:
            return {
                "id": case["id"],
                "category": case["category"],
                "passed": False,
                "failure_reason": f"claude_error: {e}",
                "depth": depth,
                "reply": None,
                "latency_s": round(time.time() - start, 2),
            }

        # Structural validation
        try:
            val_module.validate_node(result)
        except val_module.ValidationError as e:
            return {
                "id": case["id"],
                "category": case["category"],
                "passed": False,
                "failure_reason": f"validation_error: {e}",
                "depth": depth,
                "reply": None,
                "latency_s": round(time.time() - start, 2),
            }

        tool_type = result["type"]
        data = result["data"]

        if tool_type == "confirm_intent":
            used_intent_confirm = True

        if tool_type == "generate_final_reply":
            reply = data["reply_text"]
            expected = case["expected"]
            latency = round(time.time() - start, 2)

            # Check forbidden strings
            forbidden_hits = [
                f for f in expected.get("reply_should_not_contain", [])
                if f.lower() in reply.lower()
            ]

            # Check hallucination
            all_history = case.get("conversation_history", []) + [{"body": case["message"]}]
            hallucination_free = val_module.check_hallucination(reply, all_history)

            # Check depth bounds
            max_depth = expected.get("max_depth", 4)
            depth_ok = depth <= max_depth

            # Check intent confirm expectation
            expected_ic = expected.get("uses_intent_confirm", False)
            intent_confirm_ok = (used_intent_confirm == expected_ic) or (
                used_intent_confirm and not expected_ic
            )  # using intent confirm when not expected is acceptable (conservative)

            passed = (
                not forbidden_hits
                and hallucination_free
                and depth_ok
                and intent_confirm_ok
            )

            return {
                "id": case["id"],
                "category": case["category"],
                "passed": passed,
                "failure_reason": None if passed else {
                    "forbidden_hits": forbidden_hits,
                    "hallucination": not hallucination_free,
                    "depth_exceeded": not depth_ok,
                    "intent_confirm_mismatch": not intent_confirm_ok,
                },
                "depth": depth,
                "reply": reply,
                "used_intent_confirm": used_intent_confirm,
                "hallucination_free": hallucination_free,
                "forbidden_hits": forbidden_hits,
                "latency_s": latency,
            }

        # Decision node: auto-select first option
        selected_opt = data["options"][0]
        path_taken.append({
            "question": data["question"],
            "selected_label": selected_opt["label"],
            "selected_description": selected_opt.get("description", ""),
        })
        depth += 1


async def run_all():
    cases = load_cases()
    print(f"\nEcho Eval Runner — {len(cases)} test cases\n{'─'*60}")

    results = []
    for i, case in enumerate(cases):
        print(f"  [{i+1:02d}/{len(cases)}] {case['id']} ({case['category']})...", end=" ", flush=True)
        result = await run_case(case)
        status = "✓" if result["passed"] else "✗"
        print(f"{status}  depth={result['depth']}  {result['latency_s']}s")
        if not result["passed"]:
            print(f"         FAIL: {result['failure_reason']}")
        results.append(result)

    # Summary
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    avg_depth = sum(r["depth"] for r in results) / total
    hal_count = sum(1 for r in results if not r.get("hallucination_free", True))
    forbidden_count = sum(1 for r in results if r.get("forbidden_hits"))

    print(f"\n{'═'*60}")
    print(f"  PASS:          {passed}/{total} ({100*passed//total}%)")
    print(f"  Avg depth:     {avg_depth:.1f}")
    print(f"  Hallucinations:{hal_count}")
    print(f"  Forbidden hits:{forbidden_count}")
    print(f"{'═'*60}\n")

    report = {
        "timestamp": time.time(),
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 3),
        "avg_depth": round(avg_depth, 2),
        "hallucination_count": hal_count,
        "forbidden_hit_count": forbidden_count,
        "results": results,
    }

    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)

    print(f"  Detailed report written to evals/eval_report.json")
    return report


if __name__ == "__main__":
    asyncio.run(run_all())
