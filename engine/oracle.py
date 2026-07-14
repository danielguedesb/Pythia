"""The Oracle — feeds a world snapshot to the local LLM and gets back predictions.

Uses MiroFish's configured model (local Ollama by default), OpenAI chat format.
No Zep, no cloud, no cost.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from .config import CONFIG
from .httpclient import CLIENT
from .models import Prediction, WorldBrief

log = logging.getLogger("pythia.oracle")
StageCB = Optional[Callable[[str, str], Awaitable[None]]]

SYSTEM = (
    "You are PYTHIA, a forecasting oracle. You watch a live snapshot of world activity "
    "(conflicts, disasters, seismic events, geopolitics, news) and predict concrete future "
    "events. Be specific, plausible, and grounded in the snapshot. Output strictly JSON."
)

_HORIZON_LABEL = {"24h": "the next 24 hours", "week": "the next week"}


def _norm_horizon(h: str) -> str:
    h = (h or "").lower()
    if "24" in h or "day" in h or "tomorrow" in h or "hour" in h:
        return "24h"
    if "week" in h:
        return "week"
    if "month" in h:
        return "month"
    if "year" in h:
        return "year"
    return ""


class Oracle:
    def __init__(self) -> None:
        self.base = CONFIG.llm_base_url.rstrip("/")
        self.key = CONFIG.llm_api_key
        self.model = CONFIG.llm_model

    async def health(self) -> bool:
        try:
            r = await CLIENT.get(f"{self.base}/models",
                                 headers={"Authorization": f"Bearer {self.key}"}, timeout=5)
            return r.status_code < 500
        except Exception:  # noqa: BLE001 — health is a status dot; never raise
            return False

    async def list_models(self) -> list[str]:
        """Scan the LLM backend (Ollama) for installed models."""
        try:
            r = await CLIENT.get(f"{self.base}/models",
                                 headers={"Authorization": f"Bearer {self.key}"}, timeout=8)
            r.raise_for_status()
            data = r.json().get("data", [])
            names = sorted({m.get("id", "") for m in data if m.get("id")})
            return [n for n in names if n and "embed" not in n.lower()]
        except Exception:  # noqa: BLE001
            return []

    def _prompt(self, brief: WorldBrief, horizon: str) -> str:
        if horizon not in CONFIG.horizons:
            raise ValueError(f"unsupported prediction horizon: {horizon}")
        span = _HORIZON_LABEL.get(horizon, horizon)
        today = datetime.now(timezone.utc).strftime("%A, %Y-%m-%d")
        return (
            f"=== TODAY IS {today} (UTC) — every prediction is about the future "
            f"RELATIVE TO THIS DATE; never reference earlier years as upcoming ===\n"
            f"=== LIVE WORLD SNAPSHOT ({brief.event_count} signals) ===\n{brief.text}\n\n"
            f"Note: any [MARKET-ODDS] signals are real-money crowd probabilities from Polymarket — "
            f"treat them as strong anchors; you may sharpen or disagree with them, but stay calibrated.\n"
            f"Any [FUTURES] signals are forward-looking prices: a curve in backwardation means the market "
            f"is paying a premium for delivery now (physical tightness / supply stress); a VIX jump means "
            f"equity markets are pricing near-term turmoil. Read them as the market's own forecast.\n"
            f"Prefer dynamic, corroborated changes over static status entries, repetitive headlines, "
            f"or novelty market questions. Treat raw news as unverified unless another independent "
            f"signal supports it. A market question resolving beyond a horizon cannot drive that horizon.\n"
            f"Give {CONFIG.predictions_per_horizon} concrete predictions for exactly ONE horizon: "
            f'"{horizon}" ({span}).\n'
            f"Each prediction MUST cite exactly ONE event id copied from the bracketed ids "
            f"in the snapshot. trajectory describes whether the predicted event is an escalation, "
            f"continuation, or resolution of that one cited signal; use other only when none applies. "
            f"Do not combine independent signals in one prediction.\n"
            f"Return ONLY a JSON array. Each element exactly:\n"
            f'{{"statement": "<specific predicted event>", "horizon": "{horizon}", '
            f'"probability": <integer 0-100>, "reasoning": "<one sentence grounded in the snapshot>", '
            f'"driver_event_ids": ["<one exact bracketed event id>"], '
            f'"trajectory": "escalation"|"continuation"|"resolution"|"other", '
            f'"location": "<the place this is about, e.g. Strait of Hormuz>", '
            f'"lat": <approx latitude or null>, "lng": <approx longitude or null>}}\n'
            f"JSON array only — no markdown, no commentary."
        )

    async def predict(self, brief: WorldBrief, on_stage: StageCB = None) -> list[Prediction]:
        driver_titles = {
            event_id: brief.visible_event_titles[event_id]
            for event_id in brief.visible_event_ids
            if event_id in brief.visible_event_titles
        }
        horizons = list(CONFIG.horizons)
        if on_stage:
            await on_stage("thinking", f"asking {self.model} for {', '.join(horizons)}")
        responses = await asyncio.gather(*(
            self._chat(self._prompt(brief, horizon)) for horizon in horizons
        ))
        preds: list[Prediction] = []
        for horizon, text in zip(horizons, responses):
            horizon_preds = [
                prediction
                for prediction in self._parse(
                    text,
                    brief.id,
                    driver_titles=driver_titles,
                )
                if prediction.horizon == horizon
            ]
            if not horizon_preds:
                log.warning("oracle: no %s predictions admitted", horizon)
            preds.extend(horizon_preds)
        log.info("oracle produced %d predictions", len(preds))
        return preds

    async def _chat(self, user: str) -> str:
        return await self._complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], 4096)

    async def _complete(self, messages: list[dict], max_tokens: int = 900, model: str | None = None) -> str:
        body = {"model": model or self.model, "messages": messages, "temperature": CONFIG.temperature, "max_tokens": max_tokens}
        r = await CLIENT.post(f"{self.base}/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {self.key}"},
                              timeout=CONFIG.request_timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def chat(self, question: str, brief, predictions, history=None) -> str:
        """Answer a free-form question grounded in EVERY live source + current predictions."""
        parts = []
        if brief:
            parts.append(f"=== LIVE WORLD DATA — {brief.event_count} signals across {len(brief.domains)} domains ===\n{brief.text}")
        if predictions:
            parts.append("=== YOUR CURRENT PREDICTIONS ===\n" + "\n".join(
                f"- [{p.horizon}] {int(p.probability * 100)}% {p.statement}" + (f" — {p.reasoning}" if p.reasoning else "")
                for p in predictions[:24]))
        context = "\n\n".join(parts) or "(no live data loaded yet — tell the user to run a forecast)"
        sys = ("You are PYTHIA, an oracle watching the world through live global feeds (news, conflict, "
               "weather/disasters, seismic, cyber, infrastructure, and Polymarket crowd odds). Answer the "
               "user's question using the live data below and sound reasoning. Be specific and concise, cite "
               "concrete signals, and give probabilities when it helps. If the data doesn't cover something, say so.")
        messages: list[dict] = [{"role": "system", "content": sys}]
        for h in (history or [])[-6:]:
            role = "assistant" if h.get("role") == "assistant" else "user"
            messages.append({"role": role, "content": str(h.get("content", ""))[:2000]})
        messages.append({"role": "user", "content": f"{context}\n\n— USER QUESTION —\n{question}"})
        return await self._complete(messages, 800)

    async def judge(self, forecast: dict, evidence: list[str], current_brief: str) -> tuple[str, str]:
        """Grade one expired forecast against what actually happened.
        Returns (verdict yes|no|unclear, one-sentence evidence)."""
        import time as _t
        made = _t.strftime("%Y-%m-%d", _t.gmtime(forecast["ts"] / 1000))
        due = _t.strftime("%Y-%m-%d", _t.gmtime(forecast["resolve_after"] / 1000))
        lines = "\n".join(f"- {t}" for t in evidence) or "(no archived signals for this window)"
        prompt = (
            f'FORECAST (made {made}, horizon "{forecast["horizon"]}", window closed {due}):\n'
            f'"{forecast["statement"]}"'
            + (f' — location: {forecast["location"]}' if forecast.get("location") else "") + "\n\n"
            f"WORLD SIGNALS ARCHIVED DURING THE WINDOW:\n{lines}\n\n"
            f"CURRENT WORLD SNAPSHOT (aftermath evidence):\n{current_brief[:2500]}\n\n"
            "Did the forecast come true within its window? Judge strictly from the evidence above.\n"
            'Return ONLY JSON: {"verdict": "yes" | "no" | "unclear", '
            '"evidence": "<one sentence citing the deciding signal>"}\n'
            '"yes" only if the evidence clearly shows it happened; "no" if the window closed and the '
            "evidence shows it did not (or an event that big would surely appear above and does not); "
            '"unclear" only if the evidence genuinely cannot decide.'
        )
        sys = "You are a strict, impartial resolution judge for a forecasting system. Output strictly JSON."
        text = await self._complete([{"role": "system", "content": sys},
                                     {"role": "user", "content": prompt}], 220)
        for chunk in self._extract_objects(text):
            try:
                d = json.loads(chunk)
            except (ValueError, TypeError):
                continue
            v = str(d.get("verdict", "")).lower().strip()
            if v in ("yes", "no", "unclear"):
                return v, str(d.get("evidence", ""))[:400]
        return "unclear", ""

    @staticmethod
    def _extract_objects(text: str) -> list[str]:
        """Pull every balanced top-level {...} object out of arbitrary model output.

        Robust to ```fences```, multiple JSON arrays, trailing prose, etc.
        """
        objs = Oracle._scan_balanced(text)
        if not objs:
            bracket = text.find("[")
            if bracket != -1:
                objs = Oracle._scan_balanced(text[bracket + 1:])
        return objs

    @staticmethod
    def _scan_balanced(text: str) -> list[str]:
        objs: list[str] = []
        depth, start, in_str, esc = 0, None, False, False
        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    objs.append(text[start:i + 1])
                    start = None
        return objs

    @staticmethod
    def _clean_pred(
        it: dict,
        brief_id: str,
        *,
        driver_titles: dict[str, str] | None = None,
    ) -> Prediction | None:
        """Normalize one raw prediction dict from model output (or None if unusable)."""
        if not isinstance(it, dict) or not it.get("statement"):
            return None
        p = it.get("probability", 50)
        try:
            p = float(p)
        except (TypeError, ValueError):
            p = 50.0
        p = max(0.0, min(1.0, p / 100.0 if p > 1 else p))

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        lat, lng = _num(it.get("lat")), _num(it.get("lng"))
        if lat is not None and not (-90 <= lat <= 90):
            lat = None
        if lng is not None and not (-180 <= lng <= 180):
            lng = None
        raw_driver_ids = it.get("driver_event_ids")
        driver_event_ids: list[str] = []
        if driver_titles is not None:
            if not isinstance(raw_driver_ids, list) or len(raw_driver_ids) != 1:
                return None
            event_id = str(raw_driver_ids[0]).strip()
            if not event_id or event_id not in driver_titles:
                return None
            driver_event_ids.append(event_id)
        elif isinstance(raw_driver_ids, list):
            for raw_id in raw_driver_ids:
                event_id = str(raw_id).strip()
                if not event_id or event_id in driver_event_ids:
                    continue
                driver_event_ids.append(event_id)
                if len(driver_event_ids) >= 1:
                    break
        trajectory = str(it.get("trajectory") or "other").strip().lower()
        if trajectory not in {"escalation", "continuation", "resolution", "other"}:
            trajectory = "other"
        return Prediction(
            statement=str(it["statement"]).strip()[:300],
            horizon=_norm_horizon(str(it.get("horizon", "week"))),
            probability=round(p, 2),
            contract_version=2 if driver_titles is not None else 1,
            reasoning=str(it.get("reasoning", "")).strip()[:400],
            drivers=[driver_titles[event_id] for event_id in driver_event_ids]
            if driver_titles is not None
            else [],
            driver_event_ids=driver_event_ids,
            trajectory=trajectory,
            location=str(it.get("location", "")).strip()[:80],
            lat=lat, lng=lng,
            brief_id=brief_id,
        )

    @classmethod
    def _parse(
        cls,
        text: str,
        brief_id: str,
        *,
        driver_titles: dict[str, str] | None = None,
    ) -> list[Prediction]:
        preds: list[Prediction] = []
        per_horizon: dict[str, int] = {}
        seen: set[tuple[str, str]] = set()
        for chunk in cls._extract_objects(text):
            try:
                it = json.loads(chunk)
            except (ValueError, TypeError):
                continue
            entries = (
                it["predictions"]
                if isinstance(it, dict) and isinstance(it.get("predictions"), list)
                else [it]
            )
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                pred = cls._clean_pred(
                    entry,
                    brief_id,
                    driver_titles=driver_titles,
                )
                if pred is None or pred.horizon not in CONFIG.horizons:
                    continue
                identity = (pred.horizon, pred.statement.casefold())
                if identity in seen:
                    continue
                if per_horizon.get(pred.horizon, 0) >= max(
                    0, CONFIG.predictions_per_horizon
                ):
                    continue
                seen.add(identity)
                per_horizon[pred.horizon] = per_horizon.get(pred.horizon, 0) + 1
                preds.append(pred)
        if not preds:
            log.warning("oracle: no predictions parsed from: %s", text[:200])
        return preds

    async def what_if(self, scenario: str, brief) -> dict:
        """Counterfactual mode: inject a hypothetical event into the live world and
        forecast the knock-on effects. Ephemeral — nothing is stored or ledgered."""
        base = (brief.text if brief else "(no live world data loaded)")[:4000]
        horizons = "|".join(f'"{horizon}"' for horizon in CONFIG.horizons)
        prompt = (
            f"=== LIVE WORLD SNAPSHOT ===\n{base}\n\n"
            f"=== HYPOTHETICAL EVENT (assume it just happened) ===\n{scenario.strip()[:400]}\n\n"
            "Reason through the knock-on consequences, grounded in the real snapshot above.\n"
            "Return ONLY JSON:\n"
            '{"narrative": "<3-4 sentences tracing the chain of consequences>", "predictions": ['
            f'{{"statement": "<concrete knock-on event>", "horizon": {horizons}, '
            '"probability": <integer 0-100, conditional on the hypothetical>, '
            '"reasoning": "<one sentence>", "location": "<place>", "lat": <or null>, "lng": <or null>}'
            ", ... 4 to 6 predictions]}\nJSON only — no markdown, no commentary."
        )
        text = await self._complete([{"role": "system", "content": SYSTEM},
                                     {"role": "user", "content": prompt}], 1200)
        narrative, preds = "", []
        for chunk in self._extract_objects(text):
            try:
                d = json.loads(chunk)
            except (ValueError, TypeError):
                continue
            if isinstance(d, dict) and isinstance(d.get("predictions"), list):
                narrative = str(d.get("narrative", "")).strip()[:900]
                preds = [
                    prediction
                    for prediction in (
                        self._clean_pred(item, "") for item in d["predictions"]
                    )
                    if prediction and prediction.horizon in CONFIG.horizons
                ]
                break
        if not preds:   # model skipped the wrapper and emitted bare prediction objects
            preds = self._parse(text, "")
        return {"scenario": scenario.strip()[:400], "narrative": narrative,
                "predictions": [p.model_dump() for p in preds]}
