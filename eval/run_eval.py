#!/usr/bin/env python3
"""Recall/cost eval harness for the review pipeline (#209 A1c).

Runs the current review config over eval/fixtures.jsonl and reports, per config,
how many known bugs it catches (recall) and what it costs. "Measure, do not
guess": the point is to compare configs (current 5-Opus vs a cheaper CR-style
pipeline) before a cost-cutting change ships, and to refuse a config that
regresses recall (#185 is why the lenses exist).

Not hermetic and not for CI: each fixture drives a real `review-pr.sh --dry-run
--at-sha` review plus an Opus judge, so a full run spends tokens. Trigger it
manually. The pure helpers (contract parsing, cost summing, aggregation) are
unit-tested in tests/test_run_eval.py without spending anything.

Usage:
    python3 eval/run_eval.py [--fixtures eval/fixtures.jsonl] [--repeats N]
                             [--filter <id-substring>] [--out results.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_PR = REPO_ROOT / "daemon" / "review-pr.sh"
DEFAULT_FIXTURES = REPO_ROOT / "eval" / "fixtures.jsonl"

JUDGE_MODEL = "opus"


# --- pure helpers (unit-tested, no tokens) -----------------------------------


def parse_dryrun_contract(stdout: str) -> dict[str, str]:
    """Pull the `dryrun_*=<value>` lines review-pr.sh --dry-run prints on stdout."""
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        if line.startswith("dryrun_") and "=" in line:
            key, value = line.split("=", 1)
            out[key] = value
    return out


def sum_cost(scratch_dir: Path) -> float:
    """Sum the per-agent `.cost` sidecars review-pr.sh leaves in the scratch."""
    total = 0.0
    for cost_file in Path(scratch_dir).glob("*.cost"):
        text = cost_file.read_text().strip()
        try:
            total += float(text or 0)
        except ValueError:
            continue
    return round(total, 4)


def extract_json_object(text: str) -> dict:
    """Best-effort pull of the first JSON object from a model reply, tolerating
    ```json fences and surrounding prose."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = brace.group(0) if brace else None
    if candidate is None:
        raise ValueError(f"no JSON object in judge reply: {text[:200]!r}")
    return json.loads(candidate)


def fixture_recall(verdicts: list[dict]) -> float:
    """Collapse the per-repeat judge verdicts for ONE fixture into a recall score
    in [0, 1]: the fraction of runs that caught the fixture's known bug.

    Each verdict is a dict with a boolean "caught". With --repeats > 1 the same
    config is run several times because LLM output is stochastic, so "caught once"
    and "caught every time" are different claims. Fraction reports the reliability
    rate (3 of 5 -> 0.6) rather than collapsing to a single pass/fail: it neither
    hides flakiness (any-caught) nor over-punishes it (all-caught), which is the
    honest answer to "caught once != always catches". It also matches how the
    references report recall at the corpus level (issues caught / total) and how
    the summary reads a fixture as caught at recall >= 0.5. Assume verdicts is
    non-empty.
    """
    caught = sum(1 for verdict in verdicts if verdict.get("caught"))
    return caught / len(verdicts)


# --- token-spending steps (run by the operator) ------------------------------


def run_review(fixture: dict) -> dict:
    """Run review-pr.sh --dry-run --at-sha for one fixture and collect its
    findings and cost from the preserved scratch. Spends tokens."""
    proc = subprocess.run(
        ["bash", str(REVIEW_PR), "--at-sha", fixture["at_sha"], fixture["pr_url"]],
        capture_output=True,
        text=True,
    )
    contract = parse_dryrun_contract(proc.stdout)
    payload_path = contract.get("dryrun_payload")
    findings: list[dict] = []
    cost = 0.0
    scratch: Path | None = None
    if payload_path and os.path.exists(payload_path):
        scratch = Path(payload_path).parent
        payload = json.loads(Path(payload_path).read_text())
        findings = payload.get("comments", [])
        cost = sum_cost(scratch)
    return {
        "ok": proc.returncode == 0 and payload_path is not None,
        "findings": findings,
        "cost": cost,
        "count": int(contract.get("dryrun_count", 0)),
        "scratch": str(scratch) if scratch else None,
        "rc": proc.returncode,
        "stderr_tail": proc.stderr[-800:],
    }


def judge(fixture: dict, findings: list[dict]) -> dict:
    """Ask the judge model whether any finding surfaces the fixture's known bug.
    Recall only: precision/false-positive judgment needs per-finding ground truth
    the corpus does not carry (deferred). Spends tokens."""
    bug = fixture["bug"]
    findings_view = [
        {"path": f.get("path"), "line": f.get("line"), "body": f.get("body")} for f in findings
    ]
    prompt = (
        "You are grading a code review for recall against one known bug.\n\n"
        f"KNOWN BUG (must be caught):\n  file: {bug['path']}\n  defect: {bug['summary']}\n\n"
        "The review produced these findings:\n"
        f"{json.dumps(findings_view, indent=2)}\n\n"
        "Did any finding describe THIS SAME defect (same root cause and roughly "
        "the same location)? A finding merely in the same file about a different "
        "issue does NOT count. Reply with ONLY a JSON object:\n"
        '{"caught": true|false, "matched_index": <int or null>, "rationale": "<one line>"}'
    )
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", JUDGE_MODEL],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {
            "caught": False,
            "matched_index": None,
            "rationale": f"judge failed rc={proc.returncode}",
        }
    try:
        verdict = extract_json_object(proc.stdout)
    except ValueError as exc:
        return {
            "caught": False,
            "matched_index": None,
            "rationale": f"unparseable judge reply: {exc}",
        }
    return {
        "caught": bool(verdict.get("caught")),
        "matched_index": verdict.get("matched_index"),
        "rationale": str(verdict.get("rationale", "")),
    }


# --- driver ------------------------------------------------------------------


def load_fixtures(path: Path, id_filter: str | None) -> list[dict]:
    fixtures = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        fixture = json.loads(line)
        if id_filter and id_filter not in fixture["id"]:
            continue
        fixtures.append(fixture)
    return fixtures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    ap.add_argument("--repeats", type=int, default=1, help="runs per fixture (stochasticity)")
    ap.add_argument(
        "--filter", dest="id_filter", default=None, help="only fixtures whose id contains this"
    )
    ap.add_argument("--out", type=Path, default=None, help="write full results JSON here")
    args = ap.parse_args(argv)

    fixtures = load_fixtures(args.fixtures, args.id_filter)
    if not fixtures:
        print("no fixtures matched", file=sys.stderr)
        return 1

    results = []
    total_cost = 0.0
    for fixture in fixtures:
        verdicts = []
        costs = []
        for run_i in range(args.repeats):
            review = run_review(fixture)
            costs.append(review["cost"])
            total_cost += review["cost"]
            if not review["ok"]:
                verdicts.append({"caught": False, "rationale": f"review failed rc={review['rc']}"})
            else:
                verdicts.append(judge(fixture, review["findings"]))
            if review["scratch"] and os.path.isdir(review["scratch"]):
                shutil.rmtree(review["scratch"], ignore_errors=True)
            print(
                f"  {fixture['id']} run{run_i + 1}/{args.repeats}: "
                f"caught={verdicts[-1]['caught']} cost=${review['cost']}",
                file=sys.stderr,
            )
        recall = fixture_recall(verdicts)
        avg_cost = round(sum(costs) / len(costs), 4)
        results.append(
            {"id": fixture["id"], "recall": recall, "avg_cost": avg_cost, "verdicts": verdicts}
        )

    caught = sum(1 for r in results if r["recall"] >= 0.5)
    mean_recall = round(sum(r["recall"] for r in results) / len(results), 3)
    avg_cost_per_pr = round(total_cost / (len(fixtures) * args.repeats), 3)
    print("\n=== eval summary ===")
    print(f"fixtures: {len(fixtures)} | repeats: {args.repeats}")
    print(f"mean recall: {mean_recall} ({caught}/{len(results)} fixtures at recall>=0.5)")
    print(f"avg cost/review: ${avg_cost_per_pr} | total: ${round(total_cost, 3)}")
    for r in results:
        print(f"  {r['recall']:.2f}  ${r['avg_cost']:<7} {r['id']}")

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "summary": {"mean_recall": mean_recall, "avg_cost_per_pr": avg_cost_per_pr},
                    "results": results,
                },
                indent=2,
            )
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
