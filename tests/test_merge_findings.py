"""Tests for daemon/merge_findings.py (ADR 0023, multi-lens union + dedup).

Covers: union across an arbitrary number of lens payloads, same-(path, line)
dedup via body-similarity clustering (a same-defect merge keeps the max scored
confidence; a distinct-defect pair both survive), the post-merge confidence
gate, and cap truncation (by severity then confidence) in place of a
single-payload hard-fail."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MERGE_PATH = Path(__file__).resolve().parent.parent / "daemon" / "merge_findings.py"
_spec = importlib.util.spec_from_file_location("merge_findings", MERGE_PATH)
assert _spec is not None and _spec.loader is not None
merge_findings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_findings)

ExtractError = merge_findings.ExtractError


def _wrap(payload: dict) -> str:
    return f"lens prose\n\n```json\n{json.dumps(payload)}\n```\n"


def _finding(**overrides) -> dict:
    f = {
        "path": "src/main.py",
        "line": 42,
        "severity": "important",
        "type": "bug",
        "body": "**Diverging state.** The row and rank disagree while browsing.",
    }
    f.update(overrides)
    return f


def _lens(*findings: dict, summary: str = "lens summary") -> str:
    return _wrap({"summary": summary, "comments": list(findings)})


@pytest.fixture(autouse=True)
def _default_threshold(monkeypatch):
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)  # gate default 80


def test_union_distinct_locations_keeps_all():
    a = _lens(_finding(line=10, confidence=90))
    b = _lens(_finding(line=20, path="src/other.py", confidence=90))
    merged = merge_findings.merge([a, b])
    locations = {(c.path, c.line) for c in merged.comments}
    assert locations == {("src/main.py", 10), ("src/other.py", 20)}


def test_dedup_same_location_collapses_to_one():
    a = _lens(_finding(line=10, confidence=90))
    b = _lens(_finding(line=10, confidence=85))
    merged = merge_findings.merge([a, b])
    assert len(merged.comments) == 1
    assert (merged.comments[0].path, merged.comments[0].line) == ("src/main.py", 10)


def test_overlap_keeps_max_confidence():
    # Two lenses flag the same location; the merged score is the higher one, so
    # overlap raises effective confidence rather than averaging it down.
    a = _lens(_finding(line=10, confidence=70))
    b = _lens(_finding(line=10, confidence=88))
    merged = merge_findings.merge([a, b])
    assert merged.comments[0].confidence == 88


def test_distinct_defects_at_same_line_both_survive():
    # Two lenses flag the SAME line but genuinely different bugs, worded
    # nothing alike: the dedup must not silently drop one just because they
    # share a (path, line) key.
    null_deref = _lens(
        _finding(
            line=10,
            confidence=90,
            body="**Null pointer dereference.** Crashes when the cache misses.",
        )
    )
    off_by_one = _lens(
        _finding(
            line=10,
            confidence=85,
            body="**Loop bound is off by one.** The last element is never processed.",
        )
    )
    merged = merge_findings.merge([null_deref, off_by_one])
    assert len(merged.comments) == 2
    assert {c.confidence for c in merged.comments} == {90, 85}


def test_paraphrased_same_defect_at_same_line_still_merges():
    # Two lenses independently describe the SAME defect at one line, worded
    # differently: similarity clustering must still recognize these as one
    # defect and merge them (this is the recall-boosting overlap case ADR 0023
    # depends on; a naive exact-body-match dedup would wrongly keep both).
    a = _lens(
        _finding(
            line=10,
            confidence=70,
            body="**Cache entries can be null.** A missing lookup crashes the handler.",
        )
    )
    b = _lens(
        _finding(
            line=10,
            confidence=88,
            body="**Null cache entries crash the handler.** A missing lookup is not handled.",
        )
    )
    merged = merge_findings.merge([a, b])
    assert len(merged.comments) == 1
    assert merged.comments[0].confidence == 88


def test_five_lens_union_keeps_max_across_all_of_them():
    # Production shape (ADR 0023): default + correctness + perf + security +
    # tests, five raw payloads. Two agree on the location; the merge is not
    # hardcoded to a pair, the max holds regardless of how many lenses ran.
    lenses = [
        _lens(_finding(line=10, confidence=60)),
        _lens(_finding(line=20, path="other.py", confidence=90)),  # distinct location
        _lens(_finding(line=10, confidence=91)),  # same location as the first, higher
        _lens(),  # a lens with nothing to flag
        _lens(_finding(line=10, confidence=55)),  # same location, lower still
    ]
    merged = merge_findings.merge(lenses)
    by_location = {(c.path, c.line): c.confidence for c in merged.comments}
    assert by_location == {("src/main.py", 10): 91, ("other.py", 20): 90}


def test_overlap_survives_gate_when_one_lens_is_confident():
    # The recall win: a location one lens scored below the gate (70) but another
    # scored above it (88) is kept, because the max clears the threshold.
    a = _lens(_finding(line=10, confidence=70))
    b = _lens(_finding(line=10, confidence=88))
    merged = merge_findings.merge([a, b])
    assert len(merged.comments) == 1
    assert merged.comments[0].confidence == 88


def test_lone_low_confidence_finding_is_gated():
    # A single lens, single low score, no overlap to lift it: the gate drops it.
    merged = merge_findings.merge([_lens(_finding(line=10, confidence=70))])
    assert merged.comments == []


def test_unscored_none_survives_merge_and_gate():
    # None means not-scored, never gated; a lone unscored finding is kept.
    merged = merge_findings.merge([_lens(_finding(line=10, confidence=None))])
    assert len(merged.comments) == 1
    assert merged.comments[0].confidence is None


def test_scored_and_unscored_at_same_location_keeps_the_score():
    # Mixed None + real score at one location: the real score represents the
    # location (None must not be read as a low number that hides the 88).
    a = _lens(_finding(line=10, confidence=None))
    b = _lens(_finding(line=10, confidence=88))
    merged = merge_findings.merge([a, b])
    assert len(merged.comments) == 1
    assert merged.comments[0].confidence == 88


def test_gate_runs_once_post_merge():
    a = _lens(_finding(line=10, confidence=90))
    b = _lens(_finding(line=20, confidence=40))  # distinct location, below gate
    merged = merge_findings.merge([a, b])
    assert [(c.line, c.confidence) for c in merged.comments] == [(10, 90)]


def test_cap_truncates_instead_of_failing_post_merge():
    # 11 distinct high-confidence locations across two lenses exceed MAX_FINDINGS
    # (10). Unlike extract_json.enforce_cap's single-payload hard-fail, a merged
    # multi-lens set truncates: every individual lens obeyed its own "at most 10"
    # instruction, so exceeding the cap post-merge is an expected byproduct of
    # union, not evidence of a misbehaving agent that should sink the review.
    left = [_finding(line=i, confidence=90) for i in range(1, 7)]
    right = [_finding(line=i, confidence=90) for i in range(7, 12)]
    merged = merge_findings.merge([_lens(*left), _lens(*right)])
    assert len(merged.comments) == 10


def test_merge_cap_follows_max_findings_env(monkeypatch):
    # #199: the post-merge truncation reads the same MAX_FINDINGS tunable as
    # extract_json's single-payload hard cap, so the two stay one knob.
    monkeypatch.setenv("MAX_FINDINGS", "3")
    findings = [_finding(line=i, confidence=90) for i in range(1, 6)]
    merged = merge_findings.merge([_lens(*findings)])
    assert len(merged.comments) == 3


def test_cap_truncation_emits_a_parseable_count(capsys):
    # review-pr.sh greps this exact line out of merge_findings.py's stderr and
    # passes the count on to apply_edits.py, which surfaces it in the posted
    # summary (mirrors ExtractError's `category=` line convention).
    left = [_finding(line=i, confidence=90) for i in range(1, 7)]
    right = [_finding(line=i, confidence=90) for i in range(7, 12)]
    merge_findings.merge([_lens(*left), _lens(*right)])
    assert "truncated_count=1\n" in capsys.readouterr().err


def test_cap_truncation_ranks_important_over_nit_over_pre_existing():
    # All 11 at distinct locations, same confidence, so severity is the only
    # differentiator: the single pre_existing finding is the one dropped.
    important = [_finding(line=i, confidence=90, severity="important") for i in range(1, 6)]
    nits = [_finding(line=i, confidence=90, severity="nit") for i in range(6, 11)]
    pre_existing = [_finding(line=11, confidence=90, severity="pre_existing")]
    merged = merge_findings.merge([_lens(*important, *nits, *pre_existing)])
    assert len(merged.comments) == 10
    assert all(c.severity != "pre_existing" for c in merged.comments)


def test_cap_truncation_ranks_by_confidence_within_same_severity():
    # 11 same-severity findings at distinct locations: the lowest-confidence
    # one is dropped, the 10 highest survive.
    findings = [_finding(line=i, confidence=100 - i) for i in range(1, 12)]
    merged = merge_findings.merge([_lens(*findings)])
    assert len(merged.comments) == 10
    assert (
        min(c.confidence for c in merged.comments) == 90
    )  # line=1..10 survive; line=11 (conf=89) dropped


def test_cap_truncation_ranks_unscored_after_scored_at_same_severity():
    # 10 scored + 1 unscored, all same severity, distinct locations: forced to
    # drop one, the unscored finding goes first since it never demonstrated a
    # score (it is still never *gated*, this is truncation-order only).
    scored = [_finding(line=i, confidence=90) for i in range(1, 11)]
    unscored = [_finding(line=11, confidence=None)]
    merged = merge_findings.merge([_lens(*scored, *unscored)])
    assert len(merged.comments) == 10
    assert all(c.confidence is not None for c in merged.comments)


def test_empty_raw_list_raises_empty_stdout():
    with pytest.raises(ExtractError) as exc:
        merge_findings.merge([])
    assert exc.value.category == "empty-stdout"


def test_one_lens_parse_failure_does_not_sink_the_others(capsys):
    # Dogfood-observed bug: one lens's malformed payload (schema-invalid
    # severity, missing fence, etc.) used to crash the whole merge, discarding
    # every other already-completed lens's valid findings. It must not.
    good = _lens(_finding(confidence=90))
    bad = "lens produced prose but no json fence at all\n"
    merged = merge_findings.merge([good, bad])
    assert len(merged.comments) == 1
    assert merged.comments[0].confidence == 90
    assert "merge-skip: lens 1 payload failed (no-fence)" in capsys.readouterr().err


def test_labels_name_the_failed_lens_in_the_skip_message(capsys):
    good = _lens(_finding(confidence=90))
    bad = "no fence here\n"
    merge_findings.merge([good, bad], labels=["default", "correctness"])
    assert "merge-skip: correctness payload failed" in capsys.readouterr().err


def test_all_lenses_failing_raises_a_distinct_category():
    bad_a = "no fence here either\n"
    bad_b = "still no fence\n"
    with pytest.raises(ExtractError) as exc:
        merge_findings.merge([bad_a, bad_b])
    assert exc.value.category == "all-lenses-failed"


def test_summary_is_first_nonempty_lens_summary():
    a = _lens(_finding(confidence=90), summary="found a real divergence bug")
    b = _lens(summary="nothing to flag")
    merged = merge_findings.merge([a, b])
    assert merged.summary == "found a real divergence bug"


# --- _label_from_path ---------------------------------------------------


def test_label_from_path_default_lens():
    assert merge_findings._label_from_path("/scratch/.pr-review-raw.txt") == "default"


def test_label_from_path_named_lens():
    assert (
        merge_findings._label_from_path("/scratch/.pr-review-raw-correctness.txt") == "correctness"
    )


def test_label_from_path_unrecognized_name_passes_through():
    assert merge_findings._label_from_path("/scratch/something-else.txt") == "something-else.txt"
