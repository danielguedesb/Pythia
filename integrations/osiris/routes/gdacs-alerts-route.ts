import { NextResponse } from 'next/server';

/**
 * OSIRIS — GDACS disaster alerts (UN Global Disaster Alert & Coordination System, free, no key).
 * Current worldwide disaster events with Red/Orange/Green severity, type, and coordinates —
 * the best-structured keyless disaster feed there is.
 */
export const dynamic = 'force-dynamic';

const TYPE_LABEL: Record<string, string> = {
  EQ: 'Earthquake', TC: 'Tropical Cyclone', FL: 'Flood', DR: 'Drought',
  VO: 'Volcano', WF: 'Wildfire', TS: 'Tsunami',
};

export async function GET() {
  try {
    const res = await fetch('https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP', {
      signal: AbortSignal.timeout(12000), headers: { Accept: 'application/json' }, cache: 'no-store',
    });
    if (!res.ok) return NextResponse.json({ events: [], error: `gdacs ${res.status}` });
    const data = await res.json();

    const events: Array<Record<string, unknown>> = [];
    for (const f of data?.features || []) {
      const p = f?.properties || {};
      if (!p.name) continue;
      // geometry is usually a Point, but some episodes ship Polygons — use the bbox center then
      let lat: number | null = null, lng: number | null = null;
      const g = f?.geometry;
      if (g?.type === 'Point' && typeof g.coordinates?.[0] === 'number') {
        lng = g.coordinates[0]; lat = g.coordinates[1];
      } else if (Array.isArray(f?.bbox) && f.bbox.length >= 4) {
        lng = (f.bbox[0] + f.bbox[2]) / 2; lat = (f.bbox[1] + f.bbox[3]) / 2;
      }
      if (lat == null || lng == null) continue;
      events.push({
        title: String(p.name),
        type: TYPE_LABEL[String(p.eventtype)] || String(p.eventtype || 'Event'),
        alert: String(p.alertlevel || 'Green'),        // Red | Orange | Green
        country: String(p.country || ''),
        current: String(p.iscurrent) === 'true',
        from: p.fromdate || '', to: p.todate || '',
        lat, lng,
        url: p.url?.report || p.url?.details || '',
      });
    }
    // most severe first; cap so the payload stays lean
    const rank: Record<string, number> = { Red: 0, Orange: 1, Green: 2 };
    events.sort((a, b) => (rank[a.alert as string] ?? 3) - (rank[b.alert as string] ?? 3));
    return NextResponse.json({ events: events.slice(0, 60), total: events.length, timestamp: new Date().toISOString() });
  } catch (e) {
    return NextResponse.json({ events: [], error: String(e) });
  }
}
