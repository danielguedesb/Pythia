from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from engine.config import Config
from engine.ledger import Ledger
from engine.models import AgentView, Prediction, WorldBrief, WorldEvent
from engine.oracle import Oracle
from engine.osiris_intake import (
    _gdacs_alert_level,
    _normalized_source_category,
    _stable_event_id,
)
from engine.swarm import _ask, deliberate
from engine.world_state import build_brief


class PredictionContractTests(unittest.TestCase):
    def test_event_ids_are_stable_within_day_and_roll_next_day(self) -> None:
        first = WorldEvent(
            title="Missile strike reported",
            category="conflict",
            source="gdelt",
            ts=1_720_000_000_000,
        )
        same = first.model_copy(update={"ts": first.ts + 3_600_000})
        next_day = first.model_copy(update={"ts": first.ts + 86_400_000})

        self.assertEqual(_stable_event_id(first), _stable_event_id(same))
        self.assertNotEqual(_stable_event_id(first), _stable_event_id(next_day))

    def test_brief_exposes_exact_ids_and_titles(self) -> None:
        event = WorldEvent(
            id="evt_0123456789abcdef",
            title="M5 — Test place",
            category="seismic",
            source="usgs",
        )
        brief = build_brief([event])

        self.assertIn("[evt_0123456789abcdef]", brief.text)
        self.assertEqual(brief.visible_event_ids, [event.id])
        self.assertEqual(brief.visible_event_titles[event.id], event.title)

    def test_brief_reserves_dynamic_domain_coverage_before_feed_depth(self) -> None:
        infrastructure = [
            WorldEvent(
                id=f"evt_{index:016x}",
                title=f"Static facility {index}",
                category="infrastructure",
                source="infra",
                salience=1.0,
            )
            for index in range(80)
        ]
        conflict = WorldEvent(
            id="evt_ffffffffffffffff",
            title="Verified conflict escalation",
            category="conflict",
            source="conflicts",
            salience=0.8,
        )
        health = WorldEvent(
            id="evt_eeeeeeeeeeeeeeee",
            title="WHO outbreak update",
            category="health",
            source="who",
            salience=0.8,
        )

        brief = build_brief(infrastructure + [conflict, health])

        self.assertLessEqual(len(brief.text), 6500)
        self.assertIn(conflict.id, brief.visible_event_ids)
        self.assertIn(health.id, brief.visible_event_ids)
        self.assertLess(brief.text.index("[CONFLICT]"), brief.text.index("[INFRASTRUCTURE]"))

    def test_structured_gdacs_alias_cannot_masquerade_as_geopolitical(self) -> None:
        raw = {
            "id": "gdacs-117",
            "url": "https://www.gdacs.org/report.aspx?eventtype=TC&id=1",
        }
        self.assertEqual(
            _normalized_source_category(
                raw,
                "gdelt",
                "geopolitical",
                "Red notification for tropical cyclone BAVI-26",
            ),
            ("gdacs", "hurricane"),
        )
        self.assertEqual(
            _normalized_source_category(
                {"id": "gdelt-117", "url": "https://example.org"},
                "gdelt",
                "geopolitical",
                "Cyclone mentioned in a diplomatic article",
            ),
            ("gdelt", "geopolitical"),
        )
        self.assertEqual(
            _gdacs_alert_level(raw, "Red notification for tropical cyclone BAVI-26"),
            "red",
        )
        self.assertEqual(
            _gdacs_alert_level(raw, "Green flood alert in Bangladesh"),
            "green",
        )
        self.assertEqual(
            _gdacs_alert_level(
                {"alert": "Orange"}, "A title with no severity adjective"
            ),
            "orange",
        )

    def test_config_clamps_generation_to_meridian_horizons(self) -> None:
        with patch.dict("os.environ", {"HORIZONS": "year,week,month,24h"}):
            self.assertEqual(Config().horizons, ["24h", "week"])

    def test_config_rejects_horizons_outside_meridian_contract(self) -> None:
        with (
            patch.dict("os.environ", {"HORIZONS": "month,year"}),
            self.assertRaisesRegex(ValueError, "HORIZONS"),
        ):
            Config()

    def test_prompt_requests_one_driver_and_only_admitted_horizons(self) -> None:
        brief = WorldBrief(
            event_count=1,
            text="[evt_0123456789abcdef] Observed signal",
        )
        with patch("engine.oracle.CONFIG.horizons", ["24h", "week"]):
            prompt = Oracle()._prompt(brief)

        self.assertIn("exactly ONE event id", prompt)
        self.assertIn('"horizon": <one of "24h", "week">', prompt)
        self.assertNotIn("1 to 3 event ids", prompt)
        self.assertNotIn('"month"', prompt)
        self.assertNotIn('"year"', prompt)

    def test_parse_requires_one_known_driver_and_caps_each_horizon(self) -> None:
        event_id = "evt_0123456789abcdef"
        second_event_id = "evt_1111111111111111"
        payload = [
            {
                "statement": "ambiguous lineage",
                "horizon": "24h",
                "probability": 70,
                "driver_event_ids": [event_id, second_event_id],
                "trajectory": "continuation",
            },
            {
                "statement": "second",
                "horizon": "24h",
                "probability": 80,
                "driver_event_ids": [event_id],
                "trajectory": "escalation",
            },
            {
                "statement": "long horizon",
                "horizon": "month",
                "probability": 90,
                "driver_event_ids": [event_id],
                "trajectory": "continuation",
            },
            {
                "statement": "fabricated lineage",
                "horizon": "week",
                "probability": 90,
                "driver_event_ids": ["evt_unknown"],
                "trajectory": "continuation",
            },
        ]
        with (
            patch("engine.oracle.CONFIG.horizons", ["24h", "week"]),
            patch("engine.oracle.CONFIG.predictions_per_horizon", 1),
        ):
            predictions = Oracle._parse(
                json.dumps(payload),
                "brief_1",
                driver_titles={
                    event_id: "Observed signal",
                    second_event_id: "Unrelated signal",
                },
            )

        self.assertEqual([prediction.statement for prediction in predictions], ["second"])
        self.assertEqual(predictions[0].contract_version, 2)
        self.assertEqual(predictions[0].driver_event_ids, [event_id])
        self.assertEqual(predictions[0].drivers, ["Observed signal"])

    def test_ledger_round_trip_preserves_v2_lineage_and_agent_note(self) -> None:
        prediction = Prediction(
            statement="Port disruption persists",
            horizon="week",
            probability=0.65,
            contract_version=2,
            drivers=["Port closure"],
            driver_event_ids=["evt_0123456789abcdef"],
            trajectory="continuation",
            agents=[
                AgentView(
                    name="Skeptic",
                    probability=0.55,
                    note="Base rate still supports persistence.",
                    model="local",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            Ledger(path).record_forecasts([prediction], None)
            restored = Ledger(path).open_recent()[0]

        self.assertEqual(restored["contract_version"], 2)
        self.assertEqual(restored["driver_event_ids"], prediction.driver_event_ids)
        self.assertEqual(restored["drivers"], prediction.drivers)
        self.assertEqual(restored["trajectory"], "continuation")
        self.assertEqual(restored["agents"][0]["note"], prediction.agents[0].note)

    def test_hydration_exposes_only_admitted_single_driver_v2_forecasts(self) -> None:
        from engine.pipeline import hydrate_from_ledger
        from engine.state import STATE

        event_id = "evt_0123456789abcdef"
        base = {
            "probability": 0.7,
            "contract_version": 2,
            "driver_event_ids": [event_id],
            "trajectory": "continuation",
            "ts": 1_720_000_000_000,
        }
        rows = [
            {**base, "id": "pred_valid", "statement": "valid", "horizon": "week"},
            {
                **base,
                "id": "pred_ambiguous",
                "statement": "ambiguous",
                "horizon": "24h",
                "driver_event_ids": [event_id, "evt_1111111111111111"],
            },
            {
                **base,
                "id": "pred_long",
                "statement": "long",
                "horizon": "month",
            },
            {
                **base,
                "id": "pred_legacy",
                "statement": "legacy",
                "horizon": "24h",
                "contract_version": 1,
            },
        ]
        previous = STATE.predictions
        try:
            STATE.predictions = []
            with (
                patch("engine.runtime.ledger.open_recent", return_value=rows),
                patch("engine.config.CONFIG.horizons", ["24h", "week"]),
            ):
                count = hydrate_from_ledger()

            self.assertEqual(count, 1)
            self.assertEqual([prediction.id for prediction in STATE.predictions], ["pred_valid"])
        finally:
            STATE.predictions = previous


class WhatIfContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_what_if_requests_and_exposes_only_admitted_horizons(self) -> None:
        response = {
            "narrative": "Conditional chain",
            "predictions": [
                {
                    "statement": "short consequence",
                    "horizon": "week",
                    "probability": 65,
                },
                {
                    "statement": "long consequence",
                    "horizon": "month",
                    "probability": 75,
                },
            ],
        }
        oracle = Oracle()
        complete = AsyncMock(return_value=json.dumps(response))
        with (
            patch.object(oracle, "_complete", complete),
            patch("engine.oracle.CONFIG.horizons", ["24h", "week"]),
        ):
            result = await oracle.what_if("A hypothetical event", None)

        prompt = complete.await_args.args[0][-1]["content"]
        self.assertIn('"horizon": "24h"|"week"', prompt)
        self.assertNotIn('"month"', prompt)
        self.assertEqual(
            [prediction["statement"] for prediction in result["predictions"]],
            ["short consequence"],
        )


class SwarmCompletenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_persona_reply_retries_only_missing_votes(self) -> None:
        class PartialOracle:
            model = "local"

            def __init__(self) -> None:
                self.calls = 0

            _extract_objects = staticmethod(Oracle._extract_objects)

            async def _complete(self, messages, max_tokens, model=None):
                self.calls += 1
                if self.calls == 1:
                    return '[{"i":0,"p":60,"note":"first"}]'
                return (
                    '[{"i":0,"p":61,"note":"second"},'
                    '{"i":1,"p":62,"note":"third"}]'
                )

        predictions = [
            Prediction(statement=f"p{index}", horizon="24h", probability=0.5)
            for index in range(3)
        ]
        oracle = PartialOracle()

        _, _, scored = await _ask(
            oracle, "Skeptic", "base rates", "brief", predictions
        )

        self.assertEqual(sorted(scored), [0, 1, 2])
        self.assertEqual(oracle.calls, 2)

    async def test_deliberation_batches_and_scores_every_prediction(self) -> None:
        class CompleteOracle:
            model = "local"
            _extract_objects = staticmethod(Oracle._extract_objects)

            async def _complete(self, messages, max_tokens, model=None):
                listing = messages[-1]["content"].split(
                    "=== CANDIDATE PREDICTIONS ===\n", 1
                )[1]
                count = len(
                    re.findall(r"^\d+\. ", listing, flags=re.MULTILINE)
                )
                return json.dumps(
                    [
                        {"i": index, "p": 60 + index % 3, "note": "grounded"}
                        for index in range(count)
                    ]
                )

        predictions = [
            Prediction(statement=f"p{index}", horizon="24h", probability=0.5)
            for index in range(18)
        ]
        with patch("engine.swarm._persona_weights", return_value={}):
            enriched = await deliberate(CompleteOracle(), None, predictions)

        self.assertEqual(len(enriched), 18)
        self.assertTrue(all(len(prediction.agents) == 4 for prediction in enriched))


if __name__ == "__main__":
    unittest.main()
