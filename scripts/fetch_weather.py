#!/usr/bin/env python3
"""Fetch live conditions from the club's Grafana server into weather.json.

Run by .github/workflows/weather.yml every 15 minutes. The queries mirror
the Grafana dashboard panels (including the +80 deg wind vane calibration,
the zone=="shield" air temp sensor, and the LCRA Mansfield Dam buoy for
water temperature) so the numbers match what the embedded panels display.
"""
import json
import math
import sys
import time
import urllib.request

GRAFANA = "https://grafana.ageddon.com/api/ds/query"
DATASOURCE = {"uid": "cf0y3k7lgwa9sd"}
USER_AGENT = "Mozilla/5.0 (compatible; AYC-weather-page; +https://github.com/austinyachtclub/austinyachtclub.github.io)"


def flux(query, frm="now-1h"):
    """Run one Flux query, return the list of values (may be empty)."""
    body = json.dumps({
        "queries": [{
            "refId": "A",
            "datasource": DATASOURCE,
            "query": query,
            "intervalMs": 60000,
            "maxDataPoints": 2000,
        }],
        "from": frm,
        "to": "now",
    }).encode()
    req = urllib.request.Request(GRAFANA, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    frames = d["results"]["A"].get("frames") or []
    if not frames:
        return []
    vals = frames[0]["data"]["values"]
    numbers = vals[1] if len(vals) > 1 else []
    return [v for v in numbers if v is not None]


def series(name, window, extra=""):
    q = ('from(bucket:"default") |> range(start: -%s) '
         '|> filter(fn: (r) => r._measurement == "%s"%s) '
         '|> keep(columns: ["_time", "_value"])' % (window, name, extra))
    return flux(q, frm="now-" + window)


def timed_series(name, window, every, extra=""):
    """Return [(epoch_seconds, value), ...] aggregated to `every` windows."""
    q = ('from(bucket:"default") |> range(start: -%s) '
         '|> filter(fn: (r) => r._measurement == "%s"%s) '
         '|> group() '
         '|> aggregateWindow(every: %s, fn: mean, createEmpty: false) '
         '|> keep(columns: ["_time", "_value"]) '
         '|> sort(columns: ["_time"])' % (window, name, extra, every))
    return timed_flux(q, "now-" + window)


def timed_flux(q, frm):
    body = json.dumps({
        "queries": [{
            "refId": "A",
            "datasource": DATASOURCE,
            "query": q,
            "intervalMs": 60000,
            "maxDataPoints": 2000,
        }],
        "from": frm,
        "to": "now",
    }).encode()
    req = urllib.request.Request(GRAFANA, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    frames = d["results"]["A"].get("frames") or []
    if not frames:
        return []
    vals = frames[0]["data"]["values"]
    if len(vals) < 2:
        return []
    return [(int(t / 1000), v) for t, v in zip(vals[0], vals[1]) if t is not None and v is not None]


def build_wind_series():
    """Merge avg/lull/gust into [t, avg, lull, gust] rows keyed by window."""
    avg = dict(timed_series("env.wind.speed", "3h", "5m"))
    lull = dict(timed_series("env.wind.speed.min", "3h", "5m"))
    gust = dict(timed_series("env.wind.speed.max", "3h", "5m"))
    rows = []
    for t in sorted(avg):
        a = avg[t]
        rows.append([t, round(a, 1), round(lull.get(t, a), 1), round(gust.get(t, a), 1)])
    return rows


def main(out_path):
    wind = series("env.wind.speed", "30m")
    gusts = series("env.wind.speed.max", "30m")
    lulls = series("env.wind.speed.min", "30m")
    dirs_raw = series("env.wind.direction", "1h")
    air_c = series("env.temperature", "30m", ' and r.zone == "shield"')
    water = flux(
        'from(bucket:"default") |> range(start: -12h) '
        '|> filter(fn: (r) => r["_measurement"] == "lcra_wtemp") '
        '|> filter(fn: (r) => r["_field"] == "value") '
        '|> filter(fn: (r) => r["location"] == "Mansfield Dam Floating Buoy Gage") '
        '|> keep(columns: ["_time", "_value"])', frm="now-12h")
    boats = series("env.count.boat", "3h")
    cloud = series("env.coverage.cloud", "3h")
    rain_acc = series("env.raingauge.event_acc", "1h")

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
        r_concentration = math.hypot(x, y)
        shifty = r_concentration < 0.85

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

    data = {
        "updated": int(time.time()),
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
        "water_f": temp_round(water[-1]) if water else None,
        "boats": round(boats[-1]) if boats else None,
        "cloud_pct": round(cloud[-1] * 100.0) if cloud else None,
        "raining": raining,
        # 3h direction history at 5-minute resolution, calibrated like the panels,
        # for the custom N/E/S/W chart on the page.
        "dir_series": [[t, round((v + 80.0) % 360.0)] for t, v in timed_series("env.wind.direction", "3h", "5m")],
        # 3h wind history [t, avg, lull, gust] for the custom speed chart.
        "wind_series": build_wind_series(),
        # 3h air temp (F) and humidity (%) for the custom instrument charts.
        "temp_series": [[t, round(v * 9.0 / 5.0 + 32.0, 1)]
                        for t, v in timed_series("env.temperature", "3h", "5m", ' and r.zone == "shield"')],
        "hum_series": [[t, round(v, 1)]
                       for t, v in timed_series("env.relative_humidity", "3h", "5m", ' and r.zone == "shield"')],
        # 12h water temp (F) from the LCRA Mansfield Dam buoy for the custom chart.
        "water_series": [[t, round(v, 1)] for t, v in timed_flux(
            'from(bucket:"default") |> range(start: -12h) '
            '|> filter(fn: (r) => r["_measurement"] == "lcra_wtemp") '
            '|> filter(fn: (r) => r["_field"] == "value") '
            '|> filter(fn: (r) => r["location"] == "Mansfield Dam Floating Buoy Gage") '
            '|> group() '
            '|> aggregateWindow(every: 30m, fn: mean, createEmpty: false) '
            '|> keep(columns: ["_time", "_value"]) '
            '|> sort(columns: ["_time"])', "now-12h")],
    }
    with open(out_path, "w") as f:
        json.dump(data, f, indent=1)
    print(json.dumps(data, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "weather.json"))
