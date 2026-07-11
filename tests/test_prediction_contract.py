from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.ledger import Ledger
from engine.models import AgentView, Prediction, WorldEvent
from engine.oracle import Oracle
from engine.osiris_intake import _stable_event_id
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

    def test_parse_requires_known_lineage_and_caps_each_horizon(self) -> None:
        event_id = "evt_0123456789abcdef"
        payload = [
            {
                "statement": "first",
                "horizon": "24h",
                "probability": 70,
                "driver_event_ids": [event_id, "evt_unknown"],
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
                driver_titles={event_id: "Observed signal"},
            )

        self.assertEqual([prediction.statement for prediction in predictions], ["first"])
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
