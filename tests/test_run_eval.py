"""Tests for the eval harness's pure helpers (eval/run_eval.py, #209 A1c).

Only the token-free helpers are covered: contract parsing, cost summing, judge-
reply JSON extraction, and the fixture_recall collapse policy. run_review/judge
spend tokens and are exercised by a real operator-triggered run, not here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("run_eval", REPO_ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


def test_parse_dryrun_contract_pulls_only_dryrun_lines():
    stdout = (
        "some log line\n"
        "dryrun_payload=/scratch/.pr-review-payload.json\n"
        "dryrun_count=3\n"
        "[pr-review-agent] unrelated=noise\n"
    )
    contract = run_eval.parse_dryrun_contract(stdout)
    assert contract == {
        "dryrun_payload": "/scratch/.pr-review-payload.json",
        "dryrun_count": "3",
    }


def test_parse_dryrun_contract_keeps_paths_with_equals():
    contract = run_eval.parse_dryrun_contract("dryrun_payload=/a/b=c.json\n")
    assert contract["dryrun_payload"] == "/a/b=c.json"


def test_sum_cost_adds_sidecars(tmp_path):
    (tmp_path / ".pr-review-raw.txt.cost").write_text("1.25")
    (tmp_path / ".pr-review-raw-perf.txt.cost").write_text("0.75")
    (tmp_path / "not-a-cost.txt").write_text("99")
    assert run_eval.sum_cost(tmp_path) == 2.0


def test_sum_cost_tolerates_empty_or_garbage(tmp_path):
    (tmp_path / "a.cost").write_text("")
    (tmp_path / "b.cost").write_text("nan-ish garbage")
    (tmp_path / "c.cost").write_text("0.5")
    assert run_eval.sum_cost(tmp_path) == 0.5


def test_sum_cost_zero_when_no_sidecars(tmp_path):
    assert run_eval.sum_cost(tmp_path) == 0.0


def test_extract_json_object_from_fenced_reply():
    reply = 'Here is my verdict:\n```json\n{"caught": true, "matched_index": 1}\n```\n'
    assert run_eval.extract_json_object(reply) == {"caught": True, "matched_index": 1}


def test_extract_json_object_from_bare_reply():
    assert run_eval.extract_json_object('{"caught": false, "rationale": "x"}') == {
        "caught": False,
        "rationale": "x",
    }


def test_extract_json_object_raises_without_json():
    with pytest.raises(ValueError):
        run_eval.extract_json_object("no json here at all")


def test_fixture_recall_is_the_fraction_caught():
    verdicts = [
        {"caught": True},
        {"caught": False},
        {"caught": True},
        {"caught": True},
        {"caught": False},
    ]
    assert run_eval.fixture_recall(verdicts) == 0.6


def test_fixture_recall_all_and_none():
    assert run_eval.fixture_recall([{"caught": True}, {"caught": True}]) == 1.0
    assert run_eval.fixture_recall([{"caught": False}, {"caught": False}]) == 0.0


def test_fixture_recall_single_run():
    assert run_eval.fixture_recall([{"caught": True}]) == 1.0
    assert run_eval.fixture_recall([{"caught": False}]) == 0.0


def test_fixture_recall_treats_missing_caught_as_false():
    # A verdict from a failed review/judge may lack "caught"; count it as a miss.
    assert run_eval.fixture_recall([{"rationale": "review failed"}, {"caught": True}]) == 0.5
