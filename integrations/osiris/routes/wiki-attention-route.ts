import { NextResponse } from 'next/server';

/**
 * OSIRIS — Wikipedia attention (Wikimedia pageviews API, free, no key).
 * What humanity is suddenly looking at: yesterday's most-viewed articles with a
 * spike factor vs the day before. A pure human-attention signal — pageview
 * spikes on a country, person, or event often precede or confirm world news.
 */
export const dynamic = 'force-dynamic';

const BOILERPLATE = /^(Main_Page|Special:|Wikipedia:|Portal:|File:|Template:|Help:|User:|Talk:|Category:|Draft:)/;
// evergreen high-traffic pages that are attention noise, not news
const EVERGREEN = /^(Cleopatra|Deaths_in_\d{4}|XXX|Pornhub|ChatGPT|Google|Facebook|YouTube|Bible|Sex|India|United_States)$/;

async function top(date: Date): Promise<Map<string, number>> {
  const y = date.getUTCFullYear();
  const m = String(date.getUTCMonth() + 1).padStart(2, '0');
  const d = String(date.getUTCDate()).padStart(2, '0');
  const url = `https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/${y}/${m}/${d}`;
  const res = await fetch(url, { signal: AbortSignal.timeout(10000), headers: { Accept: 'application/json' }, cache: 'no-store' });
  if (!res.ok) return new Map();
  const data = await res.json();
  const out = new Map<string, number>();
  for (const a of data?.items?.[0]?.articles || []) {
    if (!BOILERPLATE.test(a.article) && !EVERGREEN.test(a.article)) out.set(a.article, a.views);
  }
  return out;
}

// pageview dumps land with a lag — serve a 3h cache
let cache: { ts: number; body: Record<string, unknown> } | null = null;

export async function GET() {
  if (cache && Date.now() - cache.ts < 3 * 3600_000) return NextResponse.json(cache.body);
  try {
    const day = 86_400_000;
    const yesterday = new Date(Date.now() - day);
    const before = new Date(Date.now() - 2 * day);
    const [cur, prev] = await Promise.all([top(yesterday), top(before)]);

    const items = [...cur.entries()].slice(0, 40).map(([article, views]) => {
      const prior = prev.get(article);
      return {
        article,
        title: article.replace(/_/g, ' '),
        views,
        spike: prior ? Math.round((views / prior) * 10) / 10 : null,   // null = wasn't in yesterday's top at all
        new_entry: !prior,
        url: `https://en.wikipedia.org/wiki/${encodeURIComponent(article)}`,
      };
    });
    // surges first: new entries and big spikes over steady high-traffic
    items.sort((a, b) => (b.new_entry ? 9 : b.spike || 1) - (a.new_entry ? 9 : a.spike || 1));
    const body = { items: items.slice(0, 25), date: yesterday.toISOString().slice(0, 10), timestamp: new Date().toISOString() };
    if (items.length) cache = { ts: Date.now(), body };
    return NextResponse.json(body);
  } catch (e) {
    return NextResponse.json({ items: [], error: String(e) });
  }
}
