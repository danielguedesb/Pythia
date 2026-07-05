import { NextResponse } from 'next/server';

/**
 * OSIRIS — 30-day flood outlook (Open-Meteo Flood API / Copernicus GloFAS, free, no key).
 * Forecast river discharge vs the recent past for the world's major basins —
 * a predicted-flood signal before the water arrives. risk = peak forecast
 * discharge / recent median (>=1.5 elevated, >=2 significant).
 */
export const dynamic = 'force-dynamic';

// gauge points chosen on the rivers themselves (GloFAS grid is ~5 km)
const BASINS: Array<{ name: string; lat: number; lng: number }> = [
  { name: 'Mississippi (Baton Rouge)', lat: 30.44, lng: -91.19 },
  { name: 'Missouri (Kansas City)', lat: 39.11, lng: -94.58 },
  { name: 'Rhine (Cologne)', lat: 50.94, lng: 6.96 },
  { name: 'Danube (Budapest)', lat: 47.49, lng: 19.05 },
  { name: 'Po (Ferrara)', lat: 44.88, lng: 11.6 },
  { name: 'Ganges (Patna)', lat: 25.62, lng: 85.17 },
  { name: 'Brahmaputra (Guwahati)', lat: 26.18, lng: 91.73 },
  { name: 'Indus (Sukkur)', lat: 27.7, lng: 68.85 },
  { name: 'Yangtze (Wuhan)', lat: 30.57, lng: 114.28 },
  { name: 'Yellow River (Zhengzhou)', lat: 34.9, lng: 113.6 },
  { name: 'Mekong (Phnom Penh)', lat: 11.57, lng: 104.92 },
  { name: 'Irrawaddy (Mandalay)', lat: 21.96, lng: 96.09 },
  { name: 'Nile (Khartoum)', lat: 15.6, lng: 32.53 },
  { name: 'Niger (Niamey)', lat: 13.52, lng: 2.11 },
  { name: 'Congo (Kinshasa)', lat: -4.3, lng: 15.3 },
  { name: 'Zambezi (Tete)', lat: -16.16, lng: 33.59 },
  { name: 'Amazon (Manaus)', lat: -3.13, lng: -60.02 },
  { name: 'Paraná (Rosario)', lat: -32.95, lng: -60.63 },
  { name: 'Magdalena (Barranquilla)', lat: 10.96, lng: -74.75 },
  { name: 'Euphrates (Nasiriyah)', lat: 31.05, lng: 46.26 },
  { name: 'Volga (Volgograd)', lat: 48.7, lng: 44.5 },
  { name: 'Elbe (Dresden)', lat: 51.05, lng: 13.74 },
];

// GloFAS updates daily — cache for 6 hours
let cache: { ts: number; body: Record<string, unknown> } | null = null;

export async function GET() {
  if (cache && Date.now() - cache.ts < 6 * 3600_000) return NextResponse.json(cache.body);
  try {
    const lats = BASINS.map(b => b.lat).join(',');
    const lngs = BASINS.map(b => b.lng).join(',');
    const url = `https://flood-api.open-meteo.com/v1/flood?latitude=${lats}&longitude=${lngs}&daily=river_discharge&forecast_days=31&past_days=31`;
    const res = await fetch(url, { signal: AbortSignal.timeout(20000), headers: { Accept: 'application/json' }, cache: 'no-store' });
    if (!res.ok) return NextResponse.json({ basins: [], error: `open-meteo ${res.status}` });
    const data = await res.json();
    const points = Array.isArray(data) ? data : [data];

    const basins: Array<Record<string, unknown>> = [];
    points.forEach((pt, i) => {
      const b = BASINS[i];
      const times: string[] = pt?.daily?.time || [];
      const q: Array<number | null> = pt?.daily?.river_discharge || [];
      if (!b || !times.length) return;
      const today = new Date().toISOString().slice(0, 10);
      const past = q.filter((v, j) => v != null && times[j] < today) as number[];
      const future = q.map((v, j) => ({ v, day: times[j] })).filter(x => x.v != null && x.day >= today) as Array<{ v: number; day: string }>;
      if (past.length < 10 || !future.length) return;
      const median = [...past].sort((a, c) => a - c)[Math.floor(past.length / 2)];
      const peak = future.reduce((best, x) => (x.v > best.v ? x : best), future[0]);
      if (median <= 0.01) return;                        // not on a resolvable river cell
      basins.push({
        name: b.name, lat: b.lat, lng: b.lng,
        median_past: Math.round(median), peak_forecast: Math.round(peak.v), peak_day: peak.day,
        risk: Math.round((peak.v / median) * 100) / 100,
      });
    });
    basins.sort((a, b) => (b.risk as number) - (a.risk as number));
    const body = { basins, timestamp: new Date().toISOString() };
    if (basins.length) cache = { ts: Date.now(), body };
    return NextResponse.json(body);
  } catch (e) {
    return NextResponse.json({ basins: [], error: String(e) });
  }
}
