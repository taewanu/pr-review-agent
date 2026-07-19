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

A full run can spend real money, so it self-guards: --max-cost stops once
cumulative spend reaches a ceiling, an interrupt (Ctrl-C, or `pkill -f run_eval`)
tears down the whole review process group instead of orphaning it, and
--check-judge validates the judge on synthetic cases before a real run.

Usage:
    python3 eval/run_eval.py [--fixtures eval/fixtures.jsonl] [--repeats N]
                             [--filter <id-substring>] [--out results.json]
                             [--max-cost <dollars>] [--check-judge]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_PR = REPO_ROOT / "daemon" / "review-pr.sh"
DEFAULT_FIXTURES = REPO_ROOT / "eval" / "fixtures.jsonl"

JUDGE_MODEL = "opus"
# Rough observed per-review cost, shown in the startup plan so the operator sees
# the exposure before committing. Update if model or pricing shifts.
OBSERVED_REVIEW_COST = "$3-7"


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


def sum_tokens(scratch_dir: Path) -> int:
    """Sum the per-agent `.tokens` sidecars (total tokens across all review calls).

    Tokens, not dollars, are the rate-limit an operator hits (#209), so this is
    the primary cost metric: dollars vary by model price, tokens are what the
    subscription caps. Same summing shape as sum_cost, over the sibling sidecars.
    """
    total = 0
    for tokens_file in Path(scratch_dir).glob("*.tokens"):
        text = tokens_file.read_text().strip()
        try:
            total += int(text or 0)
        except ValueError:
            continue
    return total


def should_stop_for_cost(total_cost: float, max_cost: float | None) -> bool:
    """True when a cost cap is set and cumulative spend has reached it. A soft cap:
    checked after each review completes and pays its full cost, so a run can
    overshoot the ceiling by up to one review."""
    return max_cost is not None and total_cost >= max_cost


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


def fixture_recall(verdicts: list[dict]) -> float | None:
    """Collapse the per-repeat judge verdicts for ONE fixture into a recall score
    in [0, 1]: the fraction of runs that caught the fixture's known bug.

    Each verdict is a dict with a boolean "caught". With --repeats > 1 the same
    config is run several times because LLM output is stochastic, so "caught once"
    and "caught every time" are different claims. Fraction reports the reliability
    rate (3 of 5 -> 0.6) rather than collapsing to a single pass/fail: it neither
    hides flakiness (any-caught) nor over-punishes it (all-caught), which is the
    honest answer to "caught once != always catches". It also matches how the
    references report recall at the corpus level (issues caught / total) and how
    the summary reads a fixture as caught at recall >= 0.5.

    Runs whose review never completed (timeout/crash, marked "error") are dropped
    from both numerator and denominator: a review that failed to run is not the
    config missing the bug, and scoring a $0 600s-timeout as a miss deflates
    recall. Returns None when every run errored, so the fixture is still reported
    but left out of the corpus mean rather than counted as a 0.
    """
    valid = [verdict for verdict in verdicts if not verdict.get("error")]
    if not valid:
        return None
    caught = sum(1 for verdict in valid if verdict.get("caught"))
    return caught / len(valid)


def precision_fp(verdicts: list[dict]) -> float | None:
    """Mean posted-finding count for ONE precision fixture (a clean/cosmetic PR).

    Every finding on a PR with nothing substantive to flag is a false-positive
    proxy, so lower is better and 0 is ideal. This is a proxy, not true precision:
    it needs no per-finding labels (the whole PR is the negative), but it can only
    be trusted on genuinely cosmetic PRs where a real finding is implausible.
    Errored runs are dropped like in fixture_recall; None if every run errored.
    """
    valid = [verdict for verdict in verdicts if not verdict.get("error")]
    if not valid:
        return None
    return round(sum(verdict["findings_count"] for verdict in valid) / len(valid), 3)


# --- token-spending steps (run by the operator) ------------------------------

# The in-flight review-pr.sh child, tracked so an interrupt tears down its whole
# process group instead of orphaning it. Each review runs in its own session, so
# the review script, its lens subshells, and their `claude -p` calls share one
# group os.killpg reaps in full; an interrupt that killed only the runner would
# leave those children burning tokens. SIGKILL (pkill -9) bypasses the handler
# below and still orphans the tree; recover it by killing the token-burning
# children directly, whose argv carries neither the runner nor the script name:
#   pkill -f 'claude -p /review'; pkill -f 'claude -p /edit'; pkill -f 'review-pr.sh --at-sha'
_TERM_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM})
_active_child: subprocess.Popen | None = None

# Wall-clock ceiling per review, above review-pr.sh's own REVIEW_AGENT_TIMEOUT.
# The in-review timeout is a perl alarm that does not reliably kill claude (a
# fanout run's stuck subagent hung the tree for an hour), so the harness enforces
# its own hard ceiling and kills the process group. A normal review is 6-9 min;
# 20 min leaves headroom for a heavy PR while cutting a genuine hang.
_REVIEW_TIMEOUT_SECONDS = int(os.environ.get("EVAL_REVIEW_TIMEOUT", "1200"))


def _terminate_active_child() -> None:
    child = _active_child
    if child is None or child.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(child.pid), signal.SIGTERM)


def _install_signal_handlers() -> None:
    def handler(signum, frame):
        _terminate_active_child()
        raise SystemExit(130)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def run_review(fixture: dict) -> dict:
    """Run one review-pr.sh --dry-run for a fixture and collect its findings and
    cost from the preserved scratch. Spends tokens. Runs in its own process group
    so an interrupt kills the whole review tree, not just the runner (see
    _terminate_active_child). The interrupt signals are blocked across the
    spawn-and-track step so a term landing between creating the child and
    recording it cannot orphan it; preexec_fn unblocks them in the child so the
    review process itself stays killable.

    A recall fixture carries an at_sha and is reviewed pinned to that pre-fix
    commit (base...at_sha compare). A precision fixture carries no at_sha and is
    reviewed at the PR's current HEAD via plain --dry-run (gh pr diff), which
    works on a merged PR where base...merge_sha would be an empty compare."""
    if fixture.get("at_sha"):
        cmd = ["bash", str(REVIEW_PR), "--at-sha", fixture["at_sha"], fixture["pr_url"]]
    else:
        cmd = ["bash", str(REVIEW_PR), "--dry-run", fixture["pr_url"]]
    global _active_child
    signal.pthread_sigmask(signal.SIG_BLOCK, _TERM_SIGNALS)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_UNBLOCK, _TERM_SIGNALS),
        )
        _active_child = proc
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _TERM_SIGNALS)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=_REVIEW_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # A review-pr.sh whose own REVIEW_AGENT_TIMEOUT failed to reap its child —
        # a fanout run holds one claude -p that spawns five subagents, and the
        # perl-alarm timeout does not always kill claude, so a stuck subagent can
        # hang the whole tree for an hour. Kill the process group and record it as
        # an errored run (excluded from recall, like any other failed review)
        # rather than letting the eval stall indefinitely.
        timed_out = True
        _terminate_active_child()
        stdout, stderr = proc.communicate()
    finally:
        if proc.poll() is None:
            _terminate_active_child()
        _active_child = None
    if timed_out:
        return {
            "ok": False,
            "findings": [],
            "cost": 0.0,
            "tokens": 0,
            "count": 0,
            "scratch": None,
            "rc": -1,  # ERR, distinct from a real review's rc; -1 == wall-clock kill
            "stderr_tail": f"review exceeded {_REVIEW_TIMEOUT_SECONDS}s wall-clock; killed",
        }
    contract = parse_dryrun_contract(stdout)
    payload_path = contract.get("dryrun_payload")
    findings: list[dict] = []
    cost = 0.0
    tokens = 0
    scratch: Path | None = None
    if payload_path and os.path.exists(payload_path):
        scratch = Path(payload_path).parent
        payload = json.loads(Path(payload_path).read_text())
        findings = payload.get("comments", [])
        cost = sum_cost(scratch)
        tokens = sum_tokens(scratch)
    return {
        "ok": proc.returncode == 0 and payload_path is not None,
        "findings": findings,
        "cost": cost,
        "tokens": tokens,
        "count": int(contract.get("dryrun_count", 0)),
        "scratch": str(scratch) if scratch else None,
        "rc": proc.returncode,
        "stderr_tail": stderr[-800:],
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


def check_judge() -> bool:
    """Sanity-check the judge before trusting a run: an obviously-matching finding
    must score caught, an obviously-unrelated one must not. Guards against a judge
    that systematically says False (which would read as a recall collapse that is
    really a judging bug). Spends 2 judge calls; returns True if both are correct."""
    fixture = {
        "bug": {
            "path": "example.py",
            "summary": "divides by item count with no empty-list guard, raising ZeroDivisionError",
        }
    }
    matching = [
        {
            "path": "example.py",
            "line": 7,
            "body": "Divides by len(items) unguarded, so empty input raises ZeroDivisionError.",
        }
    ]
    unrelated = [
        {
            "path": "other.py",
            "line": 3,
            "body": "Consider renaming this variable to be more descriptive.",
        }
    ]
    pos = judge(fixture, matching)["caught"]
    neg = judge(fixture, unrelated)["caught"]
    ok = pos and not neg
    print(
        f"judge check: matching->caught={pos} (want True), "
        f"unrelated->caught={neg} (want False) => {'PASS' if ok else 'FAIL'}",
        file=sys.stderr,
    )
    return ok


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
    ap.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help=(
            "soft cap: stop before the next review once cumulative spend reaches "
            "this $ (overshoots by up to one review)"
        ),
    )
    ap.add_argument(
        "--check-judge",
        action="store_true",
        help="run 2 synthetic judge cases and exit (validate the judge before a real run)",
    )
    args = ap.parse_args(argv)

    _install_signal_handlers()

    if args.check_judge:
        return 0 if check_judge() else 1

    fixtures = load_fixtures(args.fixtures, args.id_filter)
    if not fixtures:
        print("no fixtures matched", file=sys.stderr)
        return 1

    n_runs = len(fixtures) * args.repeats
    cap = (
        f"${args.max_cost}"
        if args.max_cost is not None
        else "none (pass --max-cost to bound spend)"
    )
    print(
        f"plan: {len(fixtures)} fixture(s) x {args.repeats} = {n_runs} review(s); "
        f"observed ~{OBSERVED_REVIEW_COST} each; cost cap: {cap}",
        file=sys.stderr,
    )

    results = []
    total_cost = 0.0
    total_tokens = 0
    runs_done = 0
    runs_errored = 0
    stopped_early = False
    for fixture in fixtures:
        # A fixture with no known "bug" is a precision fixture: a clean/cosmetic
        # PR reviewed at HEAD, where any posted finding is a false-positive proxy.
        is_precision = "bug" not in fixture
        verdicts = []
        costs = []
        tokens_list = []
        for run_i in range(args.repeats):
            review = run_review(fixture)
            costs.append(review["cost"])
            tokens_list.append(review["tokens"])
            total_cost += review["cost"]
            total_tokens += review["tokens"]
            runs_done += 1
            if not review["ok"]:
                runs_errored += 1
                verdicts.append({"error": True, "rationale": f"review failed rc={review['rc']}"})
                label = "ERR"
            elif is_precision:
                verdicts.append({"findings_count": review["count"]})
                label = f"findings={review['count']}"
            else:
                verdicts.append(judge(fixture, review["findings"]))
                label = f"caught={verdicts[-1]['caught']}"
            if review["scratch"] and os.path.isdir(review["scratch"]):
                shutil.rmtree(review["scratch"], ignore_errors=True)
            print(
                f"  {fixture['id']} run{run_i + 1}/{args.repeats}: "
                f"{label} tokens={review['tokens']} cost=${review['cost']} "
                f"(total {total_tokens} tok / ${round(total_cost, 2)})",
                file=sys.stderr,
            )
            if should_stop_for_cost(total_cost, args.max_cost):
                print(
                    f"cost cap ${args.max_cost} reached at ${round(total_cost, 2)}; "
                    f"stopping after {runs_done} run(s)",
                    file=sys.stderr,
                )
                stopped_early = True
                break
        if verdicts:
            avg_cost = round(sum(costs) / len(costs), 4)
            avg_tokens = round(sum(tokens_list) / len(tokens_list))
            if is_precision:
                results.append(
                    {
                        "id": fixture["id"],
                        "mode": "precision",
                        "false_positives": precision_fp(verdicts),
                        "avg_cost": avg_cost,
                        "avg_tokens": avg_tokens,
                        "verdicts": verdicts,
                    }
                )
            else:
                results.append(
                    {
                        "id": fixture["id"],
                        "mode": "recall",
                        "recall": fixture_recall(verdicts),
                        "avg_cost": avg_cost,
                        "avg_tokens": avg_tokens,
                        "verdicts": verdicts,
                    }
                )
        if stopped_early:
            break

    if not results:
        print("no fixtures completed", file=sys.stderr)
        return 1

    recall_results = [r for r in results if r["mode"] == "recall"]
    precision_results = [r for r in results if r["mode"] == "precision"]
    scored = [r for r in recall_results if r["recall"] is not None]
    caught = sum(1 for r in scored if r["recall"] >= 0.5)
    mean_recall = round(sum(r["recall"] for r in scored) / len(scored), 3) if scored else None
    fp_scored = [r for r in precision_results if r["false_positives"] is not None]
    mean_fp = (
        round(sum(r["false_positives"] for r in fp_scored) / len(fp_scored), 3)
        if fp_scored
        else None
    )
    avg_cost_per_pr = round(total_cost / max(1, runs_done), 3)
    avg_tokens_per_pr = round(total_tokens / max(1, runs_done))

    print("\n=== eval summary ===")
    banner = f"fixtures completed: {len(results)}/{len(fixtures)} | runs: {runs_done}"
    if runs_errored:
        banner += f" ({runs_errored} errored, excluded)"
    if stopped_early:
        banner += " | STOPPED at cost cap (partial)"
    print(banner)
    if recall_results:
        print(f"mean recall: {mean_recall} ({caught}/{len(scored)} scored fixtures at recall>=0.5)")
        for r in recall_results:
            rec = "ERR " if r["recall"] is None else f"{r['recall']:.2f}"
            print(f"  recall    {rec}  {r['avg_tokens']:>8} tok  ${r['avg_cost']:<7} {r['id']}")
    if precision_results:
        print(f"mean false-positives/PR: {mean_fp} (lower is better; clean PRs, 0 ideal)")
        for r in precision_results:
            fp = "ERR " if r["false_positives"] is None else f"{r['false_positives']:.2f}"
            print(f"  precision {fp}  {r['avg_tokens']:>8} tok  ${r['avg_cost']:<7} {r['id']}")
    # Tokens are the rate-limit metric (#209); dollars are secondary (model-priced).
    print(f"avg tokens/review: {avg_tokens_per_pr} | total: {total_tokens}")
    print(f"avg cost/review: ${avg_cost_per_pr} | total: ${round(total_cost, 3)}")

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "summary": {
                        "mean_recall": mean_recall,
                        "mean_false_positives": mean_fp,
                        "avg_tokens_per_pr": avg_tokens_per_pr,
                        "avg_cost_per_pr": avg_cost_per_pr,
                    },
                    "results": results,
                },
                indent=2,
            )
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
