# NetMirror Stremio Addon

A Stremio addon that provides streams from NetMirror (Netflix / PrimeVideo / Hotstar content) via the NetMirror TV API.

## How it works

- **Addon (server)** → FloppyData proxy → `tv.imgcdn.kim/newtv/*` API
- **Stremio client (your home IP)** → `tv.imgcdn.kim` m3u8 directly with `Referer: https://net52.cc`

The proxy is mandatory because `tv.imgcdn.kim` is behind Cloudflare and silently 403s datacenter IPs. Stremio on your home network fetches the m3u8 directly (residential IPs are not blocked).

## Features

- ✅ Movies & TV series
- ✅ Netflix / PrimeVideo / Hotstar platforms (tried in parallel)
- ✅ Multi-audio HLS streams (English, Hindi, Tamil, Telugu, etc.)
- ✅ Subtitles included
- ✅ Automatic proxy rotation across 895 FloppyData proxies
- ✅ Chrome TLS fingerprint (via curl_cffi) to bypass JA3 fingerprinting
- ✅ Auto-retry with different proxies on failure

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /manifest.json` | Stremio manifest |
| `GET /stream/movie/{tmdb_id}.json` | Movie streams |
| `GET /stream/series/{tmdb_id}:{season}:{episode}.json` | Series streams |
| `GET /configure` | Configuration page |
| `GET /health` | Health check + proxy pool status |

## Local development

```bash
cd nm-final
pip install -r requirements.txt

# Set proxy pool (comma-separated http://user:pass@host:port)
export PROXY_URL="$(cat proxies.txt)"

python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

## Deployment (Render)

1. Push this directory to a GitHub repo
2. Create a new Web Service on Render, connect the repo
3. Render will detect `render.yaml` and use the Dockerfile
4. The Dockerfile auto-loads `proxies.txt` into `FLOPPY_PROXIES` env var

## Environment variables

| Var | Default | Description |
|---|---|---|
| `TMDB_API_KEY` | `439c478a771f35c05022f9feabcca01c` | TMDB API key for title lookup |
| `PROXY_URL` | (empty) | Comma-separated proxy URLs (overrides proxies.txt) |
| `FLOPPY_PROXIES` | (from proxies.txt) | Fallback proxy list |
| `PORT` | `8000` | Server port |

## Tested working titles

| Title | Type | Result |
|---|---|---|
| Inception (TMDB 27205) | Movie | Netflix + PrimeVideo + Hotstar |
| Avatar (TMDB 19995) | Movie | PrimeVideo + Hotstar |
| Breaking Bad S1E1 (TMDB 1396) | Series | Netflix ("Pilot") |
| Money Heist S1E1 (TMDB 71446) | Series | Netflix ("Episode 1") |
| Stranger Things S1E1 (TMDB 66732) | Series | Netflix ("Chapter One: The Vanishing of Will Byers") |

## Architecture

```
┌─────────────┐      ┌──────────────────┐      ┌─────────────────┐
│   Stremio   │─────▶│  Addon (FastAPI) │─────▶│ FloppyData proxy│
│  (home IP)  │      │  curl_cffi +     │      │ (residential)   │
│             │      │  session pool    │      └────────┬────────┘
│             │◀─────│  proxy rotation  │               │
│             │      └──────────────────┘               ▼
│             │                                ┌─────────────────┐
│             │                                │ tv.imgcdn.kim   │
│             │         (plays m3u8            │ /newtv/ API     │
│             │          directly with         │ (Cloudflare)    │
└─────────────┘          Referer header)       └─────────────────┘
```

## Files

- `main.py` — FastAPI addon (single file, ~700 lines)
- `proxies.txt` — 895 FloppyData proxies (residential + datacenter)
- `requirements.txt` — fastapi, uvicorn, curl_cffi
- `Dockerfile` — Python 3.11-slim based
- `render.yaml` — Render deployment config
