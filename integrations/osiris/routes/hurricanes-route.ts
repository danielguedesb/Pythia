import { NextResponse } from 'next/server';

/**
 * OSIRIS — active tropical cyclones + forecast cones (NOAA NHC, free, no key).
 * CurrentStorms.json lists every active Atlantic/Pacific system; each storm's
 * official forecast cone GeoJSON is fetched and merged in — literal drawn-on-the-map
 * futures. Off-season this returns an empty collection, cheaply.
 */
export const dynamic = 'force-dynamic';

const CLASS_LABEL: Record<string, string> = {
  TD: 'Tropical Depression', TS: 'Tropical Storm', HU: 'Hurricane',
  MH: 'Major Hurricane', STD: 'Subtropical Depression', STS: 'Subtropical Storm',
  PTC: 'Post-tropical Cyclone', PC: 'Potential Tropical Cyclone',
};

async function coneFor(id: string): Promise<any[]> {
  try {
    const url = `https://www.nhc.noaa.gov/storm_graphics/api/${id.toUpperCase()}_CONE_latest.geojson`;
    const res = await fetch(url, { signal: AbortSignal.timeout(10000), cache: 'no-store' });
    if (!res.ok) return [];
    const gj = await res.json();
    return (gj?.features || []).map((f: any) => ({
      ...f, properties: { ...(f.properties || {}), kind: 'cone', storm: id.toUpperCase() },
    }));
  } catch { return []; }
}

export async function GET() {
  try {
    const res = await fetch('https://www.nhc.noaa.gov/CurrentStorms.json',
      { signal: AbortSignal.timeout(10000), headers: { Accept: 'application/json' }, cache: 'no-store' });
    if (!res.ok) return NextResponse.json({ type: 'FeatureCollection', features: [], storms: [], error: `nhc ${res.status}` });
    const data = await res.json();
    const active = Array.isArray(data?.activeStorms) ? data.activeStorms : [];

    const storms: Array<Record<string, unknown>> = [];
    const features: any[] = [];
    await Promise.all(active.map(async (s: any) => {
      const id = String(s.id || s.binNumber || '').trim();
      const lat = s.latitudeNumeric, lng = s.longitudeNumeric;
      const cls = CLASS_LABEL[String(s.classification)] || String(s.classification || 'Storm');
      storms.push({
        id, name: s.name, classification: cls,
        winds_mph: s.intensity ? Math.round(Number(s.intensity) * 1.15078) : null,   // kt -> mph
        pressure_mb: s.pressure || null,
        movement: [s.movementDir, s.movementSpeed].filter(Boolean).join('° at ') || '',
        lat, lng,
      });
      if (lat != null && lng != null) {
        features.push({
          type: 'Feature', geometry: { type: 'Point', coordinates: [lng, lat] },
          properties: { kind: 'center', storm: id.toUpperCase(), name: s.name, classification: cls,
                        winds_kt: s.intensity || null, pressure_mb: s.pressure || null },
        });
      }
      if (id) features.push(...await coneFor(id));
    }));

    return NextResponse.json({ type: 'FeatureCollection', features, storms, timestamp: new Date().toISOString() });
  } catch (e) {
    return NextResponse.json({ type: 'FeatureCollection', features: [], storms: [], error: String(e) });
  }
}
