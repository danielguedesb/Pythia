import { NextResponse } from 'next/server';

/**
 * OSIRIS — Manifold Markets crowd odds (free, no key).
 * Top open binary prediction markets by recent volume — a second crowd-forecast
 * anchor alongside Polymarket. (Metaculus now requires an API token, so it's out.)
 */
export const dynamic = 'force-dynamic';

const SPORTS = /world cup|super bowl|\bnfl\b|\bnba\b|\bnhl\b|\bmlb\b|premier league|la liga|champions league|\bvs\.?\b|\bufc\b|formula 1|grand prix|wimbledon|world series|playoffs|finals mvp|\bopen\b.*(tennis|golf)/i;

export async function GET() {
  try {
    const url = 'https://api.manifold.markets/v0/search-markets?sort=24-hour-vol&filter=open&contractType=BINARY&limit=100&term=';
    const res = await fetch(url, { signal: AbortSignal.timeout(10000), headers: { Accept: 'application/json' }, cache: 'no-store' });
    if (!res.ok) return NextResponse.json({ markets: [], error: `manifold ${res.status}` });
    const data = await res.json();

    const markets: Array<Record<string, unknown>> = [];
    for (const m of Array.isArray(data) ? data : []) {
      const q = String(m.question || '').trim();
      if (!q || SPORTS.test(q)) continue;
      if (typeof m.probability !== 'number') continue;
      markets.push({
        id: m.id,
        question: q,
        yes_prob: Math.round(m.probability * 100) / 100,
        volume: Math.round(m.volume24Hours || m.volume || 0),
        traders: m.uniqueBettorCount || 0,
        close_time: m.closeTime || null,
        url: m.url || '',
      });
    }
    return NextResponse.json({ markets: markets.slice(0, 25), total: markets.length, timestamp: new Date().toISOString() });
  } catch (e) {
    return NextResponse.json({ markets: [], error: String(e) });
  }
}
