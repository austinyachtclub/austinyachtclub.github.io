#!/usr/bin/env python3
"""Fetch live conditions from the club's Grafana server into weather.json.

Run in a loop by .github/workflows/weather.yml. Everything comes from just
two Flux queries (Cloudflare slow-walks datacenter IPs, so request count is
the latency driver); windowing and 5-minute aggregation happen here in
Python. Calibrations mirror the Grafana dashboard panels: +80 deg wind vane
offset, the zone=="shield" temp/humidity sensors, and the LCRA Mansfield Dam
buoy for water temperature.
"""
import json
import math
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

GRAFANA = "https://grafana.ageddon.com/api/ds/query"
DATASOURCE = {"uid": "cf0y3k7lgwa9sd"}
USER_AGENT = "Mozilla/5.0 (compatible; AYC-weather-page; +https://github.com/austinyachtclub/austinyachtclub.github.io)"

MAIN_Q = (
    'from(bucket:"default") |> range(start: -3h) '
    '|> filter(fn: (r) => r._measurement == "env.wind.speed" '
    'or r._measurement == "env.wind.speed.max" '
    'or r._measurement == "env.wind.speed.min" '
    'or r._measurement == "env.wind.direction" '
    'or ((r._measurement == "env.temperature" or r._measurement == "env.relative_humidity") and r.zone == "shield") '
    'or r._measurement == "env.count.boat" '
    'or r._measurement == "env.coverage.cloud" '
    'or r._measurement == "env.raingauge.event_acc") '
    '|> keep(columns: ["_time", "_value", "_measurement"])')

WATER_Q = (
    'from(bucket:"default") |> range(start: -12h) '
    '|> filter(fn: (r) => r["_measurement"] == "lcra_wtemp") '
    '|> filter(fn: (r) => r["_field"] == "value") '
    '|> filter(fn: (r) => r["location"] == "Mansfield Dam Floating Buoy Gage") '
    '|> keep(columns: ["_time", "_value", "_measurement"])')


def flux_multi(query, frm):
    """Run one Flux query; return {measurement: [(epoch_s, value), ...]} with
    all result tables for a measurement merged and time-sorted."""
    body = json.dumps({
        "queries": [{
            "refId": "A",
            "datasource": DATASOURCE,
            "query": query,
            "intervalMs": 60000,
            "maxDataPoints": 20000,
        }],
        "from": frm,
        "to": "now",
    }).encode()
    req = urllib.request.Request(GRAFANA, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    out = {}
    for frame in d["results"]["A"].get("frames") or []:
        name = (frame.get("schema") or {}).get("name") or ""
        vals = frame.get("data", {}).get("values") or []
        if len(vals) < 2:
            continue
        pts = [(int(t / 1000), v) for t, v in zip(vals[0], vals[1])
               if t is not None and v is not None]
        out.setdefault(name, []).extend(pts)
    for name in out:
        out[name].sort()
    return out


def window_values(pts, seconds, now):
    return [v for t, v in pts if t >= now - seconds]


def agg(pts, minutes):
    """Mean-aggregate raw points into N-minute windows (window-end labeled,
    like Flux aggregateWindow), merging duplicate sensors."""
    buckets = {}
    for t, v in pts:
        b = t - t % (minutes * 60)
        buckets.setdefault(b, []).append(v)
    return [[b + minutes * 60, sum(vs) / len(vs)] for b, vs in sorted(buckets.items())]


def main(out_path):
    with ThreadPoolExecutor(max_workers=2) as ex:
        main_job = ex.submit(flux_multi, MAIN_Q, "now-3h")
        water_job = ex.submit(flux_multi, WATER_Q, "now-12h")
        m = main_job.result()
        water_pts = water_job.result().get("lcra_wtemp", [])

    now = time.time()
    wind = window_values(m.get("env.wind.speed", []), 1800, now)
    gusts = window_values(m.get("env.wind.speed.max", []), 1800, now)
    lulls = window_values(m.get("env.wind.speed.min", []), 1800, now)
    dirs_raw = window_values(m.get("env.wind.direction", []), 3600, now)
    air_c = window_values(m.get("env.temperature", []), 1800, now)
    boats = m.get("env.count.boat", [])
    cloud = m.get("env.coverage.cloud", [])
    rain_acc = window_values(m.get("env.raingauge.event_acc", []), 3600, now)

    if not wind:
        print("no wind data; leaving previous weather.json in place")
        return 1

    avg = sum(wind) / len(wind)
    gust = max(gusts) if gusts else avg
    lull = min(lulls) if lulls else avg

    # Panel calibration: displayed bearing = (raw + 80) % 360
    dirs = [(v + 80.0) % 360.0 for v in dirs_raw]
    direction = dirs[-1] if dirs else None

    # Circular concentration: R near 1 = steady direction, low R = shifty.
    shifty = None
    if len(dirs) >= 10:
        x = sum(math.cos(math.radians(d)) for d in dirs) / len(dirs)
        y = sum(math.sin(math.radians(d)) for d in dirs) / len(dirs)
        shifty = math.hypot(x, y) < 0.85

    raining = len(rain_acc) >= 2 and (rain_acc[-1] - rain_acc[0]) > 200  # accumulation is in um

    if avg >= 20 or gust >= 25:
        verdict, color = "RISKY", "red"
        tagline = "It's honking out there — check conditions carefully before launching."
    elif avg >= 15 or gust >= 20:
        verdict, color = "SPORTY", "orange"
        tagline = "Plenty of wind — reef early and know your limits."
    elif avg >= 5:
        verdict, color = "GREAT SAILING", "green"
        tagline = "Solid breeze on the lake — come sail!"
    else:
        verdict, color = "LIGHT AIR", "blue"
        tagline = "Drifter conditions right now — bring your patience (or a swim suit)."
    if raining:
        tagline += " Rain has been moving through recently."

    cardinals = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

    def temp_round(v):
        """Club preference: above 72F round up, below 72F round down."""
        return int(math.ceil(v)) if v > 72 else int(math.floor(v))

    ws_avg = agg(m.get("env.wind.speed", []), 5)
    ws_lull = {t: v for t, v in agg(m.get("env.wind.speed.min", []), 5)}
    ws_gust = {t: v for t, v in agg(m.get("env.wind.speed.max", []), 5)}
    wind_series = [[t, round(a, 1), round(ws_lull.get(t, a), 1), round(ws_gust.get(t, a), 1)]
                   for t, a in ws_avg]

    data = {
        "updated": int(now),
        "verdict": verdict,
        "color": color,
        "tagline": tagline,
        "wind_kn": int(math.floor(avg)),
        "gust_kn": int(math.floor(gust)),
        "lull_kn": int(math.floor(lull)),
        "dir_deg": round(direction) if direction is not None else None,
        "dir_card": cardinals[round(direction / 45.0) % 8] if direction is not None else None,
        "shifty": shifty,
        "air_f": temp_round(air_c[-1] * 9.0 / 5.0 + 32.0) if air_c else None,
        "water_f": temp_round(water_pts[-1][1]) if water_pts else None,
        "boats": round(boats[-1][1]) if boats else None,
        "cloud_pct": round(cloud[-1][1] * 100.0) if cloud else None,
        "raining": raining,
        # 3h histories at 5-minute resolution for the custom charts.
        "dir_series": [[t, round((v + 80.0) % 360.0)] for t, v in agg(m.get("env.wind.direction", []), 5)],
        "wind_series": wind_series,
        "temp_series": [[t, round(v * 9.0 / 5.0 + 32.0, 1)] for t, v in agg(m.get("env.temperature", []), 5)],
        "hum_series": [[t, round(v, 1)] for t, v in agg(m.get("env.relative_humidity", []), 5)],
        # 12h water temp (F) from the LCRA Mansfield Dam buoy.
        "water_series": [[t, round(v, 1)] for t, v in agg(water_pts, 30)],
    }
    with open(out_path, "w") as f:
        json.dump(data, f, indent=1)
    print(json.dumps(data, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "weather.json"))
