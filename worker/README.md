# AYC weather relay (Cloudflare Worker)

A tiny Worker that queries the club's Grafana server the same way
`scripts/fetch_weather.py` does and returns `weather.json` live, with CORS
headers, cached for one minute. The page normally reads the GitHub
`weather-data` feed and asks this relay only when that feed is unreachable
or more than five minutes behind, so it costs nothing on a normal day.

## Deploy (about five minutes, free tier)

1. Create a free account at https://dash.cloudflare.com/sign-up if you don't
   have one. No domain is needed; the Worker gets a `*.workers.dev` URL.
2. From this folder (current Wrangler wants Node 22+; on Node 20 use
   `npx wrangler@3` in place of `npx wrangler` below):

   ```bash
   cd worker
   npx wrangler login
   npx wrangler deploy
   ```

   The last line prints the URL, something like
   `https://ayc-weather.<your-subdomain>.workers.dev`.
3. Check it:

   ```bash
   curl -s https://ayc-weather.<your-subdomain>.workers.dev/ | head -c 300
   ```

4. Put that URL in `index.html` as `RELAY_URL` (near the top of the script)
   and push. That's it.

## Responses

- `200` with the weather document (same fields as the GitHub feed, plus
  `"source": "relay"`), cached 60 s at the edge and 30 s in the browser.
- `503 {"error": "no wind data in the last 30 minutes"}` when the station is
  quiet; the page then falls back to the GitHub feed, which keeps the last
  readings and a heartbeat.
- `502` when Grafana can't be reached.

## Local run

```bash
cd worker
npx wrangler dev
```

Then open http://localhost:8787/.

## Free-tier budget

Workers free plan: 100,000 requests/day. The relay is only called while the
GitHub feed is behind, and the 60 s cache means Grafana itself sees at most
one query a minute even then.
