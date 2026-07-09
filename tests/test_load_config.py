"""Tests for daemon/load_config.py."""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

import pytest

LOAD_PATH = Path(__file__).resolve().parent.parent / "daemon" / "load_config.py"
_spec = importlib.util.spec_from_file_location("load_config", LOAD_PATH)
assert _spec is not None and _spec.loader is not None
load_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load_config)

ConfigError = load_config.ConfigError


def _write_env(root: Path, body: str) -> None:
    (root / ".env").write_text(textwrap.dedent(body).lstrip())


def _write_yaml(root: Path, body: str) -> None:
    (root / ".pr-review.yaml").write_text(textwrap.dedent(body).lstrip())


def test_minimum_required_fields(tmp_path: Path):
    _write_env(
        tmp_path,
        """
        REPOS=alice/foo
        GITHUB_USER=alice
        """,
    )
    cfg = load_config.load(tmp_path)
    assert cfg.repos == ["alice/foo"]
    assert cfg.github_user == "alice"
    # Defaults kick in for everything else.
    assert cfg.poll_interval_seconds == 300
    assert cfg.review_own_prs is True
    assert cfg.opt_out_label == "no-ai-review"
    # No parsed-but-unread keys (#199): the schema carries only what the
    # daemon consumes, so an operator can't configure a silent no-op.
    assert not hasattr(cfg, "slack_webhook_url")
    assert not hasattr(cfg, "review")


def test_env_overrides_and_quoted_values(tmp_path: Path):
    _write_env(
        tmp_path,
        """
        # Comment line skipped
        REPOS="alice/foo bob/bar"
        GITHUB_USER='alice'
        POLL_INTERVAL_SECONDS=60
        REVIEW_OWN_PRS=false
        OPT_OUT_LABEL=skip-me
        """,
    )
    cfg = load_config.load(tmp_path)
    assert cfg.repos == ["alice/foo", "bob/bar"]
    assert cfg.github_user == "alice"
    assert cfg.poll_interval_seconds == 60
    assert cfg.review_own_prs is False
    assert cfg.opt_out_label == "skip-me"


def test_missing_env_fails_with_config_not_found(tmp_path: Path):
    with pytest.raises(ConfigError) as exc:
        load_config.load(tmp_path)
    assert exc.value.category == "config-not-found"
    assert ".env" in str(exc.value)


def test_stray_pr_review_yaml_warns_and_is_ignored(tmp_path: Path, capsys):
    # #199 removed the never-read .pr-review.yaml layer. A leftover file from
    # an earlier clone must not fail the load, but silence would recreate the
    # original defect (an operator edits it and nothing happens), so the load
    # says out loud that the file does nothing.
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=alice\n")
    _write_yaml(tmp_path, "max_findings: 5\n")
    cfg = load_config.load(tmp_path)
    assert cfg.repos == ["alice/foo"]
    assert ".pr-review.yaml" in capsys.readouterr().err


def test_missing_yaml_loads_silently(tmp_path: Path, capsys):
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=alice\n")
    load_config.load(tmp_path)
    assert capsys.readouterr().err == ""


def test_empty_repos_rejected(tmp_path: Path):
    _write_env(tmp_path, "REPOS=\nGITHUB_USER=alice\n")
    with pytest.raises(ConfigError) as exc:
        load_config.load(tmp_path)
    assert exc.value.category == "config-invalid"
    assert "REPOS" in str(exc.value)


def test_malformed_repo_entry_rejected(tmp_path: Path):
    _write_env(tmp_path, "REPOS=not-a-repo\nGITHUB_USER=alice\n")
    with pytest.raises(ConfigError) as exc:
        load_config.load(tmp_path)
    assert exc.value.category == "config-invalid"


def test_missing_github_user_rejected(tmp_path: Path):
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=\n")
    with pytest.raises(ConfigError) as exc:
        load_config.load(tmp_path)
    assert exc.value.category == "config-invalid"


def test_bad_bool_rejected(tmp_path: Path):
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=alice\nREVIEW_OWN_PRS=maybe\n")
    with pytest.raises(ConfigError) as exc:
        load_config.load(tmp_path)
    assert exc.value.category == "config-invalid"


def test_bad_interval_rejected(tmp_path: Path):
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=alice\nPOLL_INTERVAL_SECONDS=not-a-number\n")
    with pytest.raises(ConfigError) as exc:
        load_config.load(tmp_path)
    assert exc.value.category == "config-invalid"


def test_zero_interval_rejected(tmp_path: Path):
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=alice\nPOLL_INTERVAL_SECONDS=0\n")
    with pytest.raises(ConfigError) as exc:
        load_config.load(tmp_path)
    assert exc.value.category == "config-invalid"


def test_malformed_env_line_rejected(tmp_path: Path):
    _write_env(tmp_path, "this is not key=value\nREPOS=alice/foo\nGITHUB_USER=alice\n")
    with pytest.raises(ConfigError) as exc:
        load_config.load(tmp_path)
    assert exc.value.category == "config-parse-error"


def test_json_output_is_serialisable(tmp_path: Path):
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=alice\n")
    cfg = load_config.load(tmp_path)
    # Round-trip — daemon parses this with jq, so it must be valid JSON.
    parsed = json.loads(cfg.model_dump_json())
    assert parsed["repos"] == ["alice/foo"]
    # poll.sh reads max_parallel from this JSON to size its dispatch semaphore.
    assert parsed["max_parallel"] == 1


def test_max_parallel_defaults_to_serial(tmp_path: Path):
    # Default 1 keeps dispatch serial, so enabling parallelism is opt-in.
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=alice\n")
    assert load_config.load(tmp_path).max_parallel == 1


def test_max_parallel_env_override(tmp_path: Path):
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=alice\nMAX_PARALLEL=3\n")
    assert load_config.load(tmp_path).max_parallel == 3


def test_max_parallel_rejects_below_one(tmp_path: Path):
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=alice\nMAX_PARALLEL=0\n")
    with pytest.raises(ConfigError) as exc:
        load_config.load(tmp_path)
    assert exc.value.category == "config-invalid"


def test_max_parallel_rejects_far_above_ceiling(tmp_path: Path):
    # 1000 concurrent `claude -p` is a typo, not intent. Asserts the ceiling
    # rejects an absurd value without pinning the exact cap the validator picks.
    _write_env(tmp_path, "REPOS=alice/foo\nGITHUB_USER=alice\nMAX_PARALLEL=1000\n")
    with pytest.raises(ConfigError) as exc:
        load_config.load(tmp_path)
    assert exc.value.category == "config-invalid"
