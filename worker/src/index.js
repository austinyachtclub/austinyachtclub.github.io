// AYC weather relay: a Cloudflare Worker that asks the club's Grafana for
// the current conditions and hands aycweather.com the same weather.json the
// GitHub updater publishes, but live and with CORS headers. Results are
// cached for a minute so a busy race evening is still one Grafana query
// per minute.
import { buildWeather } from "./weather.js";

const TTL_MS = 60 * 1000;
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Max-Age": "86400",
};
const CACHE_KEY = new Request("https://ayc-weather-relay.invalid/weather.json");

// Per-isolate memo; the Cache API below shares across isolates when available.
let memo = { at: 0, body: null };

function json(body, status, extra) {
  return new Response(body, {
    status,
    headers: { ...CORS, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "public, max-age=30", ...extra },
  });
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (request.method !== "GET") return json('{"error":"method not allowed"}', 405);
    const path = new URL(request.url).pathname;
    if (path !== "/" && path !== "/weather.json") return json('{"error":"not found"}', 404);

    const now = Date.now();
    if (memo.body && now - memo.at < TTL_MS) return json(memo.body, 200, { "X-Relay-Cache": "memo" });

    let cache = null;
    try { cache = caches.default; } catch (e) { /* not available here */ }
    if (cache) {
      const hit = await cache.match(CACHE_KEY).catch(() => null);
      if (hit) {
        const body = await hit.text();
        memo = { at: now, body };
        return json(body, 200, { "X-Relay-Cache": "edge" });
      }
    }

    let data;
    try {
      data = await buildWeather(fetch);
    } catch (e) {
      return json(JSON.stringify({ error: "grafana unreachable: " + (e && e.message) }), 502, { "Cache-Control": "no-store" });
    }
    if (!data) {
      // Station quiet: say so and let the page fall back to the GitHub feed,
      // which carries the last readings plus a heartbeat.
      return json(JSON.stringify({ error: "no wind data in the last 30 minutes" }), 503, { "Cache-Control": "no-store" });
    }

    const body = JSON.stringify(data);
    memo = { at: now, body };
    if (cache) {
      ctx.waitUntil(
        cache.put(CACHE_KEY, new Response(body, { headers: { "Content-Type": "application/json", "Cache-Control": "public, max-age=60" } })).catch(() => {})
      );
    }
    return json(body, 200, { "X-Relay-Cache": "miss" });
  },
};
