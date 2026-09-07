// Builds the weather.json document from the club's Grafana server.
// A line-for-line port of scripts/fetch_weather.py so the page gets the
// same shape from either source: same Flux queries, +80 deg vane offset,
// zone=="shield" sensors, LCRA Mansfield Dam buoy, 5-minute aggregation.

export const GRAFANA = "https://grafana.ageddon.com/api/ds/query";
const DATASOURCE = { uid: "cf0y3k7lgwa9sd" };
const USER_AGENT = "Mozilla/5.0 (compatible; AYC-weather-relay; +https://aycweather.com/help/)";

const MAIN_Q =
  'from(bucket:"default") |> range(start: -3h) ' +
  '|> filter(fn: (r) => r._measurement == "env.wind.speed" ' +
  'or r._measurement == "env.wind.speed.max" ' +
  'or r._measurement == "env.wind.speed.min" ' +
  'or r._measurement == "env.wind.direction" ' +
  'or ((r._measurement == "env.temperature" or r._measurement == "env.relative_humidity") and r.zone == "shield") ' +
  'or r._measurement == "env.count.boat" ' +
  'or r._measurement == "env.coverage.cloud" ' +
  'or r._measurement == "env.raingauge.event_acc") ' +
  '|> keep(columns: ["_time", "_value", "_measurement"])';

const WATER_Q =
  'from(bucket:"default") |> range(start: -12h) ' +
  '|> filter(fn: (r) => r["_measurement"] == "lcra_wtemp") ' +
  '|> filter(fn: (r) => r["_field"] == "value") ' +
  '|> filter(fn: (r) => r["location"] == "Mansfield Dam Floating Buoy Gage") ' +
  '|> keep(columns: ["_time", "_value", "_measurement"])';

// Python's round(): halves go to the even neighbour.
function roundHalfEven(x) {
  const f = Math.floor(x), d = x - f;
  if (d < 0.5) return f;
  if (d > 0.5) return f + 1;
  return f % 2 === 0 ? f : f + 1;
}
const round1 = (x) => Math.round(x * 10) / 10;

// One Flux query -> { measurement: [[epochSeconds, value], ...] } with all
// result tables for a measurement merged and time-sorted.
async function fluxMulti(query, from, fetchImpl) {
  const body = JSON.stringify({
    queries: [{ refId: "A", datasource: DATASOURCE, query, intervalMs: 60000, maxDataPoints: 20000 }],
    from,
    to: "now",
  });
  const r = await fetchImpl(GRAFANA, {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": USER_AGENT },
    body,
  });
  if (!r.ok) throw new Error("grafana responded " + r.status);
  const d = await r.json();
  const out = {};
  for (const frame of (d.results && d.results.A && d.results.A.frames) || []) {
    const name = (frame.schema && frame.schema.name) || "";
    const vals = (frame.data && frame.data.values) || [];
    if (vals.length < 2) continue;
    const pts = out[name] || (out[name] = []);
    for (let i = 0; i < vals[0].length; i++) {
      const t = vals[0][i], v = vals[1][i];
      if (t != null && v != null) pts.push([Math.floor(t / 1000), v]);
    }
  }
  for (const k in out) out[k].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return out;
}

const windowValues = (pts, seconds, now) => pts.filter((p) => p[0] >= now - seconds).map((p) => p[1]);

// Mean-aggregate raw points into N-minute windows, window-end labelled.
function agg(pts, minutes) {
  const w = minutes * 60, buckets = new Map();
  for (const [t, v] of pts) {
    const b = t - (t % w);
    if (!buckets.has(b)) buckets.set(b, []);
    buckets.get(b).push(v);
  }
  return [...buckets.keys()].sort((a, b) => a - b)
    .map((b) => { const vs = buckets.get(b); return [b + w, vs.reduce((s, v) => s + v, 0) / vs.length]; });
}

const tempRound = (v) => (v > 72 ? Math.ceil(v) : Math.floor(v));

/** Returns the weather document, or null when the station has sent no wind
 *  data in the last 30 minutes. */
export async function buildWeather(fetchImpl = fetch) {
  const [m, water] = await Promise.all([fluxMulti(MAIN_Q, "now-3h", fetchImpl), fluxMulti(WATER_Q, "now-12h", fetchImpl)]);
  const waterPts = water["lcra_wtemp"] || [];
  const g = (k) => m[k] || [];

  const now = Date.now() / 1000;
  const wind = windowValues(g("env.wind.speed"), 1800, now);
  const gusts = windowValues(g("env.wind.speed.max"), 1800, now);
  const lulls = windowValues(g("env.wind.speed.min"), 1800, now);
  const dirsRaw = windowValues(g("env.wind.direction"), 3600, now);
  const airC = windowValues(g("env.temperature"), 1800, now);
  const boats = g("env.count.boat");
  const cloud = g("env.coverage.cloud");
  const rainAcc = windowValues(g("env.raingauge.event_acc"), 3600, now);

  if (!wind.length) return null;

  const avg = wind.reduce((s, v) => s + v, 0) / wind.length;
  const gust = gusts.length ? Math.max(...gusts) : avg;
  const lull = lulls.length ? Math.min(...lulls) : avg;

  // Panel calibration: displayed bearing = (raw + 80) % 360
  const dirs = dirsRaw.map((v) => (v + 80) % 360);
  const direction = dirs.length ? dirs[dirs.length - 1] : null;

  // Circular concentration: R near 1 = steady direction, low R = shifty.
  let shifty = null;
  if (dirs.length >= 10) {
    let x = 0, y = 0;
    for (const d of dirs) { x += Math.cos((d * Math.PI) / 180); y += Math.sin((d * Math.PI) / 180); }
    shifty = Math.hypot(x / dirs.length, y / dirs.length) < 0.85;
  }

  const raining = rainAcc.length >= 2 && rainAcc[rainAcc.length - 1] - rainAcc[0] > 200; // accumulation is in um

  let verdict, color, tagline;
  if (avg >= 20 || gust >= 25) {
    verdict = "RISKY"; color = "red";
    tagline = "It's honking out there — check conditions carefully before launching.";
  } else if (avg >= 15 || gust >= 20) {
    verdict = "SPORTY"; color = "orange";
    tagline = "Plenty of wind — reef early and know your limits.";
  } else if (avg >= 5) {
    verdict = "GREAT SAILING"; color = "green";
    tagline = "Solid breeze on the lake — come sail!";
  } else {
    verdict = "LIGHT AIR"; color = "blue";
    tagline = "Drifter conditions right now — bring your patience (or a swim suit).";
  }
  if (raining) tagline += " Rain has been moving through recently.";

  const cardinals = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

  const wsAvg = agg(g("env.wind.speed"), 5);
  const wsLull = new Map(agg(g("env.wind.speed.min"), 5));
  const wsGust = new Map(agg(g("env.wind.speed.max"), 5));
  const windSeries = wsAvg.map(([t, a]) => [t, round1(a), round1(wsLull.has(t) ? wsLull.get(t) : a), round1(wsGust.has(t) ? wsGust.get(t) : a)]);

  const stamp = Math.floor(now);
  return {
    updated: stamp,
    checked: stamp,
    source: "relay",
    verdict, color, tagline,
    wind_kn: Math.floor(avg),
    gust_kn: Math.floor(gust),
    lull_kn: Math.floor(lull),
    dir_deg: direction != null ? roundHalfEven(direction) : null,
    dir_card: direction != null ? cardinals[roundHalfEven(direction / 45) % 8] : null,
    shifty,
    air_f: airC.length ? tempRound((airC[airC.length - 1] * 9) / 5 + 32) : null,
    water_f: waterPts.length ? tempRound(waterPts[waterPts.length - 1][1]) : null,
    boats: boats.length ? roundHalfEven(boats[boats.length - 1][1]) : null,
    cloud_pct: cloud.length ? roundHalfEven(cloud[cloud.length - 1][1] * 100) : null,
    raining,
    // 3h histories at 5-minute resolution for the custom charts.
    dir_series: agg(g("env.wind.direction"), 5).map(([t, v]) => [t, roundHalfEven((v + 80) % 360)]),
    wind_series: windSeries,
    temp_series: agg(g("env.temperature"), 5).map(([t, v]) => [t, round1((v * 9) / 5 + 32)]),
    hum_series: agg(g("env.relative_humidity"), 5).map(([t, v]) => [t, round1(v)]),
    // 12h water temp (F) from the LCRA Mansfield Dam buoy.
    water_series: agg(waterPts, 30).map(([t, v]) => [t, round1(v)]),
  };
}
