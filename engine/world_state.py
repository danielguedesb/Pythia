"""Assemble all Osiris feeds into one prose 'world brief' for the oracle."""
from __future__ import annotations

from collections import defaultdict

from .models import WorldBrief, WorldEvent

_MAX_BRIEF_CHARS = 6500
_MAX_EVENTS_PER_DOMAIN = 4
_DYNAMIC_DOMAIN_ORDER = (
    "conflict", "hurricane", "disaster", "flood-outlook", "health", "cyber",
    "outage", "unrest", "food", "censorship", "geopolitical", "weatherAlerts",
    "severeStorms", "wildfire", "seismic", "futures", "markets", "market-odds",
    "news", "attention", "infrastructure", "economy", "displacement",
    "space-weather",
)
_DOMAIN_RANK = {category: index for index, category in enumerate(_DYNAMIC_DOMAIN_ORDER)}


def _domain_key(category: str) -> tuple[int, str]:
    return (_DOMAIN_RANK.get(category, len(_DOMAIN_RANK)), category)


def _render(selected: dict[str, list[WorldEvent]]) -> tuple[str, list[str]]:
    lines: list[str] = []
    top: list[str] = []
    for category in sorted(selected, key=_domain_key):
        lines.append(f"\n[{category.upper()}] ({len(selected[category])} shown)")
        for event in selected[category]:
            location = (
                f"  @{event.lat:.1f},{event.lng:.1f}"
                if event.lat is not None
                else ""
            )
            summary = f" — {event.summary[:70]}" if event.summary else ""
            lines.append(
                f"  • [{event.id}] {event.title[:140]}{summary}{location}"
            )
            top.append(event.title)
    return "\n".join(lines).strip(), top


def build_brief(events: list[WorldEvent]) -> WorldBrief:
    by_cat: dict[str, list[WorldEvent]] = defaultdict(list)
    for e in events:
        by_cat[e.category].append(e)

    domains = {c: len(v) for c, v in by_cat.items()}
    ranked = {
        category: sorted(rows, key=lambda event: (-event.salience, event.id))
        for category, rows in by_cat.items()
    }
    # Reserve one slot for every domain before adding depth. This prevents a
    # populous static/noisy feed from truncating dynamic domains out of sight.
    selected = {category: [rows[0]] for category, rows in ranked.items() if rows}
    candidates = sorted(
        (
            (category, event)
            for category, rows in ranked.items()
            for event in rows[1:_MAX_EVENTS_PER_DOMAIN]
        ),
        key=lambda item: (
            _domain_key(item[0]),
            -item[1].salience,
            item[1].id,
        ),
    )
    for category, event in candidates:
        selected[category].append(event)
        candidate_text, _ = _render(selected)
        if len(candidate_text) > _MAX_BRIEF_CHARS:
            selected[category].pop()

    text, top = _render(selected)
    visible_events = [event for rows in selected.values() for event in rows]
    visible_event_ids = [event.id for event in visible_events]
    return WorldBrief(
        event_count=len(events),
        domains=domains,
        text=text,
        top_events=top[:24],
        visible_event_ids=visible_event_ids,
        visible_event_titles={
            event.id: event.title for event in visible_events
        },
    )
