"""Offline tests for the council service mode. No network access.

The config test writes the YAML config as JSON — valid YAML (JSON is a subset)
and parseable by both pyyaml and a plain json.loads stub.
"""

import asyncio
import json
import os
import random
import sys
from collections import defaultdict
import tempfile
from urllib.parse import urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import httpx

from backend import cli
from backend.council import (
    PLAN_ANGLES,
    calculate_aggregate_rankings,
    parse_dropped,
    parse_issues,
    parse_ranking_from_text,
    parse_votes,
    run_full_council,
    stage2_collect_rankings,
)
from backend.cli import _render_digest
from backend.config import CouncilConfig, check_keys, load_config
from backend.providers import ModelEntry, ProviderRegistry, _message_content


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


def test_missing_key_fails_only_for_the_entries_used():
    os.environ.pop("TEST_COUNCIL_MISTRAL_KEY", None)
    with tempfile.TemporaryDirectory() as d:
        cfg = load_config(_write_config(d))  # loading no longer needs every key
    # the Ollama-only review council runs without the Mistral key
    check_keys(cfg.councils["review"])
    with pytest.raises(ValueError, match="TEST_COUNCIL_MISTRAL_KEY"):
        check_keys(cfg.councils["default"] + [cfg.chairmen["default"]])


def test_key_is_never_read_from_the_yaml(tmp_path):
    path = tmp_path / "council.yaml"
    path.write_text(json.dumps({
        "providers": {"custom": {"base_url": "http://x/v1", "key": "sk-from-yaml"}},
        "councils": {"c": [{"provider": "custom", "model": "m"}]},
    }))
    assert load_config(str(path)).councils["c"][0].key == ""


def test_duplicate_seat_is_rejected(tmp_path):
    path = tmp_path / "council.yaml"
    path.write_text(json.dumps({
        "providers": {"ollama": {"base_url": "http://x/v1"}},
        "councils": {"c": [{"provider": "ollama", "model": "m"}, {"provider": "ollama", "model": "m"}]},
    }))
    with pytest.raises(ValueError, match="more than once"):
        load_config(str(path))


@pytest.mark.parametrize("content, message", [
    ("", "expected a mapping"),
    ("councils: [unclosed", "not valid YAML"),
    (json.dumps({"providers": {"o": {"base_url": "http://x"}},
                 "councils": {"c": [{"provider": "o", "model": "m"}]},
                 "implementers": [{"provider": "o"}]}), "implementers entry needs"),
    (json.dumps({"councils": {"c": [{"model": "m"}]}}), "needs provider and model"),
])
def test_bad_config_gives_a_one_line_cli_error(tmp_path, monkeypatch, content, message):
    path = tmp_path / "council.yaml"
    path.write_text(content)
    monkeypatch.setattr(sys, "argv", ["council", "task", "--config", str(path)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert str(exc.value.code).startswith("error: ") and message in str(exc.value.code)


def test_missing_config_gives_a_one_line_cli_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["council", "task", "--config", str(tmp_path / "nope.yaml")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert str(exc.value.code).startswith("error: council config not found")


def test_entry_timeout_is_not_overridden_by_run_timeout(tmp_path, monkeypatch):
    path = tmp_path / "council.yaml"
    path.write_text(json.dumps({
        "providers": {"ollama": {"base_url": "http://x/v1", "timeout": 600}},
        "councils": {"c": [{"provider": "ollama", "model": "capped", "timeout": 300},
                           {"provider": "ollama", "model": "default"}]},
    }))
    capped, default = load_config(str(path)).councils["c"]
    assert capped.timeout_pinned and not default.timeout_pinned
    seen = {}

    async def fake_retry(self, entry, messages, timeout):
        seen[entry.model] = timeout
        return {"content": "ok"}

    monkeypatch.setattr(ProviderRegistry, "_query_with_retry", fake_retry)
    registry = ProviderRegistry([capped, default])
    for e in (capped, default):
        asyncio.run(registry.query(e, [], timeout=900))
    assert seen == {"capped": 300, "default": 900}


def test_anthropic_base_url_default_and_override(tmp_path):
    path = tmp_path / "council.yaml"
    path.write_text(json.dumps({
        "providers": {"anthropic": {}, "proxy": {"base_url": "http://proxy"}},
        "councils": {"c": [
            {"provider": "anthropic", "model": "m"},
            {"provider": "anthropic", "model": "n", "base_url": "http://proxy"},
        ]},
    }))
    a, b = load_config(str(path)).councils["c"]
    assert a.base_url == "https://api.anthropic.com" and b.base_url == "http://proxy"


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
    """Stands in for ProviderRegistry; records which entries were queried.

    responses maps entry name -> content string, None (failure), or a list of
    those, consumed one per call (stage 1, stage 2, ...).
    """

    def __init__(self, responses):
        self.responses = {k: (list(v) if isinstance(v, list) else v) for k, v in responses.items()}
        self.calls = []
        self.messages = defaultdict(list)  # entry name -> messages of each call, in order

    def _answer(self, name, messages):
        self.messages[name].append(messages)
        content = self.responses.get(name)
        if isinstance(content, list):
            content = content.pop(0) if content else None
        return {"content": content, "reasoning_details": None} if content is not None else None

    async def query_parallel(self, entries, messages, timeout=None):
        self.calls.append([e.name for e in entries])
        return {e.name: self._answer(e.name, messages) for e in entries}

    async def query(self, entry, messages, timeout=None):
        self.calls.append([entry.name])
        return self._answer(entry.name, messages)


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
        _, label_to_model, _ = asyncio.run(stage2_collect_rankings(
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


# --- review mode: structured issues and per-issue votes --------------------------

REVIEW_A = """CHECKED: cli exit paths, config
major | BUG | cli.py:100 | exit 0 when the chairman fails | `sys.exit(0 if stage1 else 1)` | stub the chairman to fail, check $?
minor | MISSING | cli.py | empty task is sent | criterion: "reject empty input" | printf '' | council --json"""
REVIEW_B = "CHECKED: providers\n- nit | BUG | council.py:19 | unused import | `Any` | none"


def test_parse_issues_fields_and_kinds():
    issues = parse_issues(REVIEW_A, "A")
    assert [i["id"] for i in issues] == ["A1", "A2"]
    assert issues[0]["severity"] == "major" and issues[0]["kind"] == "BUG"
    assert issues[0]["location"] == "cli.py:100"
    assert issues[1]["kind"] == "MISSING"
    assert issues[1]["evidence"] == 'criterion: "reject empty input"'
    assert parse_issues(REVIEW_B, "B")[0]["severity"] == "nit"  # bulleted line
    assert parse_issues("CHECKED: everything\nNO ISSUES", "C") == []


def test_parse_issues_markdown_table_rows():
    text = ("CHECKED: x\n| SEVERITY | KIND | LOCATION | description | evidence | check |\n"
            "|---|---|---|---|---|---|\n| major | BUG | cli.py:1 | d | `e` | c |")
    [issue] = parse_issues(text, "A")
    assert (issue["kind"], issue["location"], issue["check"]) == ("BUG", "cli.py:1", "c")


def test_parse_issues_pipe_inside_backticks():
    [issue] = parse_issues(
        "major | BUG | cli.py:9 | pipe in evidence | `printf '' | council` | `a | b` run", "A")
    assert issue["evidence"] == "`printf '' | council`"
    assert issue["check"] == "`a | b` run"


def test_parse_dropped_line_without_reason_stays_one_entry():
    assert parse_dropped("DROPPED:\nA2 | FALSE POSITIVE\nB1 | DUPLICATE of A1 | same") == [
        {"id": "A2", "verdict": "FALSE POSITIVE", "reason": ""},
        {"id": "B1", "verdict": "DUPLICATE OF A1", "reason": "same"},
    ]


def test_review_empty_or_unformatted_reviewer_counts_as_failed(monkeypatch, capsys):
    council, chair = _entries()
    code, out = _run_cli(monkeypatch, capsys, {
        "ollama/a": "", "mistral/b": "looks fine to me", "mistral/c": "CHECKED: all\nNO ISSUES",
        "mistral/chair": "VERDICT: APPROVE",
    }, ["diff", "--mode", "review", "--json"])
    assert json.loads(out)["failures"] == ["ollama/a", "mistral/b"]


def test_review_skips_stage2_when_nothing_to_vote_on():
    council, chair = _entries()
    registry = FakeRegistry({e.name: "CHECKED: all\nNO ISSUES" for e in council}
                            | {"mistral/chair": "VERDICT: APPROVE"})
    _, stage2, _, meta = asyncio.run(run_full_council("r", registry, council, chair, mode="review"))
    assert stage2 == [] and meta["issues"] == []
    assert len(registry.calls) == 2  # stage 1 + chairman, no stage 2
    # a single reviewer: its votes on its own issues would be ignored anyway
    registry = FakeRegistry({"ollama/a": REVIEW_A, "mistral/b": None, "mistral/c": None,
                             "mistral/chair": "VERDICT: CHANGES NEEDED"})
    _, stage2, _, meta = asyncio.run(run_full_council("r", registry, council, chair, mode="review"))
    assert stage2 == [] and len(meta["issues"]) == 2
    assert all(i["votes"]["REAL"] == 0 for i in meta["issues"])


def test_parse_votes():
    text = "A1 | REAL | yes\n- **B1**: FALSE POSITIVE | no\nA2 | duplicate of A1 | same"
    assert parse_votes(text) == {"A1": "REAL", "B1": "FALSE POSITIVE", "A2": "DUPLICATE"}


def test_review_mode_votes_exclude_self_and_reach_the_chairman():
    council, chair = _entries()
    council = council[:2]  # ollama/a, mistral/b
    # stage 2 votes are written by id; ids depend on the shuffled labels, so
    # each voter votes REAL on every id it could see
    votes = "A1 | REAL | x\nA2 | REAL | x\nB1 | REAL | x\nB2 | REAL | x"
    new = "NEW | major | MISSING | cli.py | failures not in JSON | criterion | run with a failing seat"
    registry = FakeRegistry({
        "ollama/a": [REVIEW_A, votes + "\n" + new],
        "mistral/b": [REVIEW_B, votes],
        "mistral/chair": "VERDICT: CHANGES NEEDED",
    })
    stage1, stage2, stage3, meta = asyncio.run(run_full_council(
        "review", registry, council, chair, mode="review", rng=random.Random(1)
    ))
    issues = meta["issues"]
    by_reviewer = defaultdict(list)
    for i in issues:
        by_reviewer[i["reviewer"]].append(i)
    # a's two issues get only b's vote, b's issue only a's vote
    assert all(i["votes"]["REAL"] == 1 for i in issues if "votes" in i)
    assert len(by_reviewer["ollama/a"]) == 3  # 2 stage-1 issues + 1 NEW
    assert [i["id"] for i in issues if "votes" not in i] == ["N1"]
    assert meta["aggregate_rankings"] == []  # no peer ranking in review mode
    chair_prompt = registry.messages["mistral/chair"][-1][0]["content"]
    assert "[votes: REAL 1, FALSE POSITIVE 0, DUPLICATE 0]" in chair_prompt
    assert "[NEW, unvoted]" in chair_prompt
    stage1_system = registry.messages["mistral/b"][0][0]["content"]
    assert "MISSING" in stage1_system and "CHECKED:" in stage1_system
    # reviewers must not list issues they have already rejected (they inflate stage 2)
    assert "leave it out" in stage1_system
    stage2_prompt = registry.messages["mistral/b"][1][0]["content"]  # lists the ids
    assert "For EVERY issue id above" in stage2_prompt


def test_parse_dropped_section():
    synthesis = (
        "VERDICT: CHANGES NEEDED\n\nmajor | BUG | cli.py:1 | x | `y` | none\n\n"
        "DROPPED:\nA2 | FALSE POSITIVE | the guard exists at cli.py:30\n"
        "- B1 | DUPLICATE of A1 | same exit-code issue\n\nUNCHECKED: packaging"
    )
    assert parse_dropped(synthesis) == [
        {"id": "A2", "verdict": "FALSE POSITIVE", "reason": "the guard exists at cli.py:30"},
        {"id": "B1", "verdict": "DUPLICATE OF A1", "reason": "same exit-code issue"},
    ]
    assert parse_dropped("VERDICT: APPROVE") == []


def test_review_chairman_must_account_for_dropped_issues():
    council, chair = _entries()
    registry = FakeRegistry({
        "ollama/a": [REVIEW_A, "B1 | REAL | x"],
        "mistral/b": [REVIEW_B, "A1 | REAL | x"],
        "mistral/c": None,
        "mistral/chair": "VERDICT: CHANGES NEEDED\nDROPPED:\nA2 | NOT CONFIRMED | no evidence",
    })
    _, _, _, meta = asyncio.run(run_full_council("r", registry, council, chair, mode="review"))
    assert "DROPPED:" in registry.messages["mistral/chair"][-1][0]["content"]
    assert [d["id"] for d in meta["dropped"]] == ["A2"]


def test_cloudflare_520_is_retried(monkeypatch):
    statuses = [520, 200]

    def handler(request):
        status = statuses.pop(0)
        body = {"choices": [{"message": {"content": "ok"}}]} if status == 200 else {}
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    entry = ModelEntry(provider="mistral", model="m", base_url="https://api.mistral.ai/v1", key="k")
    result = asyncio.run(ProviderRegistry([entry]).query(entry, [{"role": "user", "content": "q"}]))
    assert result["content"] == "ok" and statuses == []


# --- plan mode: random angles ------------------------------------------------------

def test_plan_mode_assigns_distinct_random_angles():
    council, chair = _entries()
    seen = set()
    for seed in range(10):
        registry = FakeRegistry({e.name: ["proposal", "FINAL RANKING:\n1. Response A"] for e in council}
                                | {"mistral/chair": "plan"})
        stage1, _, _, meta = asyncio.run(run_full_council(
            "t", registry, council, chair, mode="plan", rng=random.Random(seed)
        ))
        roles = meta["roles"]
        assert sorted(roles.values()) == sorted(PLAN_ANGLES)
        assert {r["model"]: r["role"] for r in stage1} == roles
        seen.add(roles["ollama/a"])
    assert len(seen) > 1, "the same seat always got the same angle"


def test_plan_mode_angle_reaches_the_seat_and_can_be_turned_off():
    council, chair = _entries()
    registry = FakeRegistry({e.name: "p" for e in council} | {"mistral/chair": "plan"})
    _, _, _, meta = asyncio.run(run_full_council("t", registry, council, chair, mode="plan"))
    angle = meta["roles"]["mistral/b"]
    stage1_system = registry.messages["mistral/b"][0][0]
    assert stage1_system["role"] == "system"
    assert f"Your assigned angle: {angle}." in stage1_system["content"]
    registry2 = FakeRegistry({e.name: "p" for e in council} | {"mistral/chair": "plan"})
    _, _, _, meta2 = asyncio.run(run_full_council(
        "t", registry2, council, chair, mode="plan", angles=False
    ))
    assert meta2["roles"] == {}
    assert "assigned angle" not in registry2.messages["mistral/b"][0][0]["content"]


def test_timings_recorded():
    council, chair = _entries()
    registry = FakeRegistry({e.name: "x" for e in council} | {"mistral/chair": "s"})
    _, _, _, meta = asyncio.run(run_full_council("t", registry, council, chair))
    assert set(meta["timings"]) == {"stage1_s", "stage2_s", "stage3_s"}


# --- transport ------------------------------------------------------------------------

def test_keyless_provider_sends_no_authorization_and_calls_are_timed(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    entry = ModelEntry(provider="ollama", model="m", base_url="http://llm-server:11434/v1")
    registry = ProviderRegistry([entry])
    assert asyncio.run(registry.query(entry, [{"role": "user", "content": "q"}]))["content"] == "hi"
    assert seen["auth"] is None
    assert registry.calls[0]["model"] == "ollama/m" and registry.calls[0]["ok"] is True


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
        "ollama/a": "CHECKED: f.py\nNO ISSUES",
        "mistral/b": "CHECKED: f.py\nmajor | BUG | f.py:10 | bug | `x = 1` | none",
        "mistral/c": "CHECKED: f.py\nNO ISSUES",
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

def _run_cli(monkeypatch, capsys, responses, argv, implementers=()):
    council, chair = _entries()
    cfg = CouncilConfig(councils={"default": council}, chairmen={"default": chair},
                        implementers=set(implementers))
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


def test_cli_review_refuses_the_implementer(monkeypatch, capsys):
    code, _ = _run_cli(monkeypatch, capsys, {}, ["diff", "--mode", "review"],
                       implementers=["mistral/chair"])
    assert "implementers" in str(code) and "chairman" in str(code)


def test_cli_implementer_may_chair_outside_review(monkeypatch, capsys):
    code, out = _run_cli(monkeypatch, capsys, {
        "ollama/a": "A", "mistral/b": "B", "mistral/c": "C", "mistral/chair": "s",
    }, ["task", "--json"], implementers=["mistral/chair"])
    assert code == 0 and "timings" in json.loads(out)
