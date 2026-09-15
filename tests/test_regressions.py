from __future__ import annotations

import yaml

from app.config_loader import ROOT
from app.prefilter import deterministic_prefilter
from app.rules import apply_rules
from tests.conftest import viable_listing


def test_regression_fixtures(config: dict) -> None:
    fixtures = yaml.safe_load(
        (ROOT / "tests/fixtures/regressions.yaml").read_text(encoding="utf-8")
    )
    for fixture in fixtures:
        prefilter = deterministic_prefilter(viable_listing(raw_text=fixture["text"]), config)
        decision = apply_rules(prefilter.facts, config, prefilter)
        assert decision.decision == fixture["expected"], fixture["id"]
