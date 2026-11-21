"""Offline tests for the council service mode. No network access.

The config test writes the YAML config as JSON — valid YAML (JSON is a subset)
and parseable by both pyyaml and a plain json.loads stub.
"""

import asyncio
import json
import os
import random
import sys
import tempfile
from urllib.parse import urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backend import cli
from backend.council import (
    calculate_aggregate_rankings,
    parse_ranking_from_text,
    run_full_council,
    stage2_collect_rankings,
)
from backend.cli import _render_digest
from backend.config import CouncilConfig, load_config
from backend.providers import ModelEntry, _message_content


# --- _message_content (Mistral returns content as a list of typed parts) ------

def test_message_content_plain_string():
    text, reasoning = _message_content({"content": "hello", "reasoning_details": None})
    assert text == "hello" and reasoning is None


def test_message_content_mistral_reasoning_parts():
    # Live-observed shape from Mistral reasoning models (zai-glm-5-3-flash etc.)
    message = {
        "content": [
            {"type": "thinking", "closed": True,
             "thinking": [{"type": "text", "text": "let me think"}]},
            {"type": "text", "text": "OK"},
        ],
        "role": "assistant",
    }
    text, reasoning = _message_content(message)
    assert text == "OK"
    assert reasoning == ["let me think"]


def test_message_content_degenerate():
    assert _message_content({"content": None}) == ("", None)
    assert _message_content({}) == ("", None)
    text, _ = _message_content({"content": [{"type": "text", "text": "a"}, "junk", {"type": "text", "text": "b"}]})
    assert text == "ab"


# --- parse_ranking_from_text -------------------------------------------------

def test_parse_ranking_exact_format():
    text = (
        "Response A is good but misses Y.\nResponse B lacks depth.\n\n"
        "FINAL RANKING:\n1. Response B\n2. Response A\n3. Response C\n"
    )
    assert parse_ranking_from_text(text) == ["Response B", "Response A", "Response C"]


def test_parse_ranking_fallback_and_garbage():
    assert parse_ranking_from_text("FINAL RANKING:\nResponse A then Response B") == [
        "Response A", "Response B",
    ]
    assert parse_ranking_from_text("no ranking here at all") == []
    assert parse_ranking_from_text("") == []
    # degenerate: non-string input must not raise (regression: live crash)
    assert parse_ranking_from_text(["not", "a", "string"]) == []
    assert parse_ranking_from_text(None) == []


def test_parse_ranking_without_heading_is_no_vote():
    # prose mentions are discussion order, not a ranking (old fallback counted
    # them all, Response A twice here)
    text = "Response A is thorough. Response B misses X, unlike Response A. Response C is weakest."
    assert parse_ranking_from_text(text) == []


def test_parse_ranking_uses_last_heading():
    text = (
        "I will end with FINAL RANKING: as asked.\nResponse C is best.\n\n"
        "FINAL RANKING:\n1. Response C\n2. Response A\n3. Response B"
    )
    assert parse_ranking_from_text(text) == ["Response C", "Response A", "Response B"]


def test_parse_ranking_drops_duplicates():
    text = "FINAL RANKING:\n1. Response B\n2. Response A\n3. Response B"
    assert parse_ranking_from_text(text) == ["Response B", "Response A"]


# --- calculate_aggregate_rankings --------------------------------------------

def test_aggregate_rankings_ordering():
    stage2 = [
        {"model": "m1", "ranking": "", "parsed_ranking": ["Response A", "Response B"]},
        {"model": "m2", "ranking": "", "parsed_ranking": ["Response B", "Response A"]},
        {"model": "m3", "ranking": "", "parsed_ranking": ["Response A", "Response B"]},
    ]
    label_to_model = {"Response A": "a", "Response B": "b"}
    agg = calculate_aggregate_rankings(stage2, label_to_model)
    assert agg[0]["model"] == "a" and agg[0]["average_rank"] == round((1 + 2 + 1) / 3, 2)
    assert agg[1]["model"] == "b" and agg[1]["rankings_count"] == 3


def test_aggregate_rankings_empty():
    assert calculate_aggregate_rankings([], {}) == []


# --- load_config --------------------------------------------------------------

CONFIG_JSON = json.dumps({
    "providers": {
        "ollama": {"base_url": "http://llm-server:11434/v1", "timeout": 600},
        "mistral": {"base_url": "https://api.mistral.ai/v1",
                    "key_env": "TEST_COUNCIL_MISTRAL_KEY", "timeout": 120},
    },
    "councils": {
        "default": [
            {"provider": "ollama", "model": "qwen3.8:27b"},
            {"provider": "mistral", "model": "glm-5-3-flash"},
        ],
        "review": [
            {"provider": "ollama", "model": "qwen3.8:27b"},
        ],
    },
    "chairmen": {
        "default": {"provider": "mistral", "model": "mistral-large-4"},
        "review": {"provider": "mistral", "model": "glm-5-3"},
    },
})


def _write_config(tmpdir):
    path = os.path.join(tmpdir, "council.yaml")
    with open(path, "w") as f:
        f.write(CONFIG_JSON)
    return path


def test_load_config_entries_and_timeouts():
    os.environ["TEST_COUNCIL_MISTRAL_KEY"] = "test-key"
    try:
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(_write_config(d))
        assert set(cfg.councils) == {"default", "review"}
        ollama_entry = cfg.councils["default"][0]
        assert ollama_entry.name == "ollama/qwen3.8:27b"
        assert ollama_entry.base_url == "http://llm-server:11434/v1"
        assert ollama_entry.timeout == 600.0
        mistral_entry = cfg.councils["default"][1]
        assert mistral_entry.key == "test-key"
        assert cfg.chairmen["review"].name == "mistral/glm-5-3"
    finally:
        del os.environ["TEST_COUNCIL_MISTRAL_KEY"]


def test_load_config_missing_key_fails_fast():
    os.environ.pop("TEST_COUNCIL_MISTRAL_KEY", None)
    with tempfile.TemporaryDirectory() as d:
        try:
            load_config(_write_config(d))
        except ValueError as e:
            assert "TEST_COUNCIL_MISTRAL_KEY" in str(e)
        else:
            raise AssertionError("expected ValueError for missing key_env")


def test_load_config_no_councils_fails():
    os.environ["TEST_COUNCIL_MISTRAL_KEY"] = "x"
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "council.yaml")
            with open(path, "w") as f:
                f.write(json.dumps({"providers": {}, "councils": {}}))
            try:
                load_config(path)
            except ValueError as e:
                assert "no councils" in str(e)
            else:
                raise AssertionError("expected ValueError for empty councils")
    finally:
        del os.environ["TEST_COUNCIL_MISTRAL_KEY"]


REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


@pytest.mark.parametrize("name", ["council.yaml", "council.test.yaml"])
def test_shipped_configs_use_llm_server_for_ollama(name, monkeypatch):
    # qwen-lan never resolved, so the local seat silently failed every run
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    cfg = load_config(os.path.join(REPO, name))
    entries = [e for c in cfg.councils.values() for e in c] + list(cfg.chairmen.values())
    ollama = [e for e in entries if e.provider == "ollama"]
    assert ollama, f"{name} has no Ollama entry"
    for e in ollama:
        assert urlparse(e.base_url).hostname == "llm-server", e.base_url


# --- run_full_council with a fake registry (offline) ---------------------------

class FakeRegistry:
    """Stands in for ProviderRegistry; records which entries were queried."""

    def __init__(self, responses):
        self.responses = responses  # entry name -> content string or None
        self.calls = []

    async def query_parallel(self, entries, messages, timeout=None):
        self.calls.append([e.name for e in entries])
        return {
            e.name: (
                {"content": self.responses[e.name], "reasoning_details": None}
                if self.responses.get(e.name) is not None
                else None
            )
            for e in entries
        }

    async def query(self, entry, messages, timeout=None):
        self.calls.append([entry.name])
        content = self.responses.get(entry.name)
        return (
            {"content": content, "reasoning_details": None}
            if content is not None
            else None
        )


def _entries():
    return [
        ModelEntry(provider="ollama", model="a"),
        ModelEntry(provider="mistral", model="b"),
        ModelEntry(provider="mistral", model="c"),
    ], ModelEntry(provider="mistral", model="chair")


def test_run_full_council_stage2_only_queries_stage1_successes():
    council, chair = _entries()
    registry = FakeRegistry({
        "ollama/a": "answer A",
        "mistral/b": "FINAL RANKING:\n1. Response A\n2. Response B",
        "mistral/c": None,  # failed stage 1
        "mistral/chair": "synthesis",
    })
    stage1, stage2, stage3, meta = asyncio.run(
        run_full_council("task", registry, council, chair, mode="answer")
    )
    assert {r["model"] for r in stage1} == {"ollama/a", "mistral/b"}
    # stage 2 call must contain only the two stage-1 successes, not mistral/c
    stage2_call = registry.calls[1]
    assert set(stage2_call) == {"ollama/a", "mistral/b"}
    assert stage3["response"] == "synthesis" and not stage3.get("failed")
    # labels are shuffled: the top model is whichever one got "Response A"
    assert meta["aggregate_rankings"][0]["model"] == meta["label_to_model"]["Response A"]


def test_stage2_labels_are_shuffled():
    council, _ = _entries()
    stage1 = [{"model": e.name, "response": f"answer {e.model}"} for e in council]
    firsts = set()
    for seed in range(20):
        registry = FakeRegistry({})
        _, label_to_model = asyncio.run(stage2_collect_rankings(
            registry, council, "t", stage1, rng=random.Random(seed)
        ))
        assert sorted(label_to_model.values()) == sorted(e.name for e in council)
        firsts.add(label_to_model["Response A"])
    assert len(firsts) > 1, "the same seat was always Response A"


def test_run_full_council_chairman_failure_is_flagged():
    council, chair = _entries()
    registry = FakeRegistry({e.name: "answer" for e in council} | {"mistral/chair": None})
    stage1, _, stage3, _ = asyncio.run(run_full_council("task", registry, council, chair))
    assert len(stage1) == 3
    assert stage3["failed"] is True


def test_run_full_council_all_fail_degenerate():
    council, chair = _entries()
    registry = FakeRegistry({e.name: None for e in council} | {"mistral/chair": None})
    stage1, stage2, stage3, meta = asyncio.run(
        run_full_council("task", registry, council, chair)
    )
    assert stage1 == [] and stage2 == []
    assert "All models failed" in stage3["response"] and stage3["failed"] is True
    assert meta["aggregate_rankings"] == []


def test_run_full_council_review_mode_verdict():
    council, chair = _entries()
    registry = FakeRegistry({
        "ollama/a": "NO ISSUES",
        "mistral/b": "major | f.py:10 | bug | `x = 1`",
        "mistral/c": "NO ISSUES",
        "mistral/chair": "VERDICT: CHANGES NEEDED\nmajor | f.py:10 | bug | `x = 1`",
    })
    stage1, stage2, stage3, meta = asyncio.run(
        run_full_council("review this", registry, council, chair, mode="review")
    )
    assert stage3["response"].startswith("VERDICT: CHANGES NEEDED")
    assert len(stage1) == 3


# --- digest rendering ----------------------------------------------------------

def test_render_digest_review_and_failures():
    meta = {"aggregate_rankings": [
        {"model": "ollama/a", "average_rank": 1.0, "rankings_count": 2},
    ]}
    out = _render_digest(
        "review task", "review",
        [{"model": "ollama/a", "response": "NO ISSUES"}],
        [], {"model": "mistral/glm-5-3", "response": "VERDICT: APPROVE"},
        meta, failed=["mistral/c"],
    )
    assert "COUNCIL VERDICT (review)" in out
    assert "REVIEW CHAIRMAN VERDICT:" in out
    assert "VERDICT: APPROVE" in out
    assert "FAILURES: mistral/c" in out


def test_render_digest_empty_stage1():
    out = _render_digest(
        "t", "answer", [], [],
        {"model": "error", "response": "All models failed to respond. Please try again."},
        {"aggregate_rankings": []}, failed=["ollama/a"],
    )
    assert "All models failed" in out
    assert "Aggregate peer rankings" not in out


# --- CLI main (fake config + registry, no network) ------------------------------

def _run_cli(monkeypatch, capsys, responses, argv):
    council, chair = _entries()
    cfg = CouncilConfig(councils={"default": council}, chairmen={"default": chair})
    monkeypatch.setattr(cli, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(cli, "ProviderRegistry", lambda entries: FakeRegistry(responses))
    monkeypatch.setattr(sys, "argv", ["council", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code, capsys.readouterr().out


def test_cli_json_lists_failures(monkeypatch, capsys):
    code, out = _run_cli(monkeypatch, capsys, {
        "ollama/a": None, "mistral/b": "B", "mistral/c": "C", "mistral/chair": "synthesis",
    }, ["task", "--json"])
    assert code == 0
    assert json.loads(out)["failures"] == ["ollama/a"]


def test_cli_chairman_failure_exits_1(monkeypatch, capsys):
    code, out = _run_cli(monkeypatch, capsys, {
        "ollama/a": "A", "mistral/b": "B", "mistral/c": "C", "mistral/chair": None,
    }, ["task", "--json"])
    assert code == 1
    assert json.loads(out)["failures"] == ["mistral/chair (chairman)"]


@pytest.mark.parametrize("stdin_text", ["", "  \n"])
def test_cli_rejects_empty_task(monkeypatch, capsys, stdin_text):
    import io
    stdin = io.StringIO(stdin_text)
    stdin.isatty = lambda: False
    monkeypatch.setattr(sys, "stdin", stdin)

    def no_config(path=None):
        raise AssertionError("config loaded (and models queried) for an empty task")

    monkeypatch.setattr(cli, "load_config", no_config)
    monkeypatch.setattr(sys, "argv", ["council", "--json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert "empty" in str(exc.value.code)
