"""Regression on benchmarks/datasets/nlu_commands.yaml (nlu.md §4).

The labels describe what a person meant, so the rule parser does not get all
of them right (free wording, heavy STT errors); the test guards against the
accuracy dropping below the level measured in E2.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

import run_nlu  # noqa: E402

RULE_BASELINE = 85.0  # % full match; E2 (benchmarks/results/SUMMARY.md) measured more


@pytest.fixture(scope="module")
def items():
    return run_nlu.load_dataset()


def test_dataset_is_valid(items, registry) -> None:
    assert len(items) >= 200
    assert {i["lang"] for i in items} == {"uk", "en"}
    assert len({i["text"] for i in items}) == len(items), "duplicate phrases"
    for item in items:
        expect = item["expect"]
        assert item["cat"] in run_nlu.CATEGORY_UK, item
        if "error" in expect:
            assert expect["error"] in ("ambiguous", "reject"), item
            continue
        assert expect["kind"] in ("command", "query"), item
        for device_id in expect.get("devices", []):
            assert device_id in registry, item
        if expect["kind"] == "command":
            assert expect["devices"], item
            for device_id in expect["devices"]:
                assert registry.get(device_id).type.supports(expect["action"]), item


def test_rule_parser_accuracy(items, registry) -> None:
    from teleckinez.nlu.resolver import TargetResolver
    from teleckinez.nlu.rule_parser import RuleParser

    parser, resolver = RuleParser(registry), TargetResolver(registry)
    marks = [
        run_nlu.score(i["expect"], run_nlu.outcome_of(parser.parse(i["text"]), resolver, registry))
        for i in items
    ]
    accuracy = 100 * sum(m["full_ok"] for m in marks) / len(marks)
    assert accuracy >= RULE_BASELINE
