"""
NetMirror Stremio Addon — uses NetMirror TV API through a Novada residential proxy.

Architecture:
  - Addon (Render) → Novada proxy → tv.imgcdn.kim/newtv/* API
  - Stremio client → m3u8 URL directly (residential IP, no block)

The proxy is REQUIRED because tv.imgcdn.kim is Cloudflare-protected and blocks
datacenter IPs. The Novada proxy gives a residential IP that Cloudflare allows.

Stream URLs (m3u8) work from the user's home IP directly — the addon only needs
the proxy for API calls, not for stream playback.

Environment variables:
  PROXY_URL  — Novada proxy URL (required, e.g. http://user:pass@host:port)
  TMDB_API_KEY — TMDB API key (optional, has default)
  PORT — Server port (default 8000)
"""
import os, sys, json, re, base64, time, asyncio
from typing import Optional, List, Dict, Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse

# ============================================================
# Configuration
# ============================================================
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "439c478a771f35c05022f9feabcca01c")
TMDB_BASE = "https://api.themoviedb.org/3"
NM_API_BASE = "https://tv.imgcdn.kim/newtv"
NM_REFERER = "https://net52.cc"
NM_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
PROXY_URL = os.getenv("PROXY_URL", "")
REQUEST_TIMEOUT = 20.0

# ============================================================
# HTTP client with proxy
# ============================================================
_client: Optional[httpx.AsyncClient] = None

async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        kwargs = {
            "timeout": REQUEST_TIMEOUT,
            "follow_redirects": True,
            "headers": {"User-Agent": NM_UA, "Accept": "application/json, text/plain, */*"},
        }
        if PROXY_URL:
            kwargs["proxy"] = PROXY_URL
        _client = httpx.AsyncClient(**kwargs)
    return _client

# ============================================================
# TMDB lookup
# ============================================================
async def tmdb_title(tmdb_id: str, media_type: str) -> Optional[str]:
    # Strip tmdb: prefix if present
    if tmdb_id.startswith("tmdb:"):
        tmdb_id = tmdb_id[5:]
    typ = "tv" if media_type == "series" else "movie"
    url = f"{TMDB_BASE}/{typ}/{tmdb_id}?api_key={TMDB_API_KEY}"
    try:
        c = await get_client()
        r = await c.get(url)
        if r.status_code != 200: return None
        d = r.json()
        return d.get("name") if media_type == "series" else d.get("title")
    except Exception:
        return None

# ============================================================
# NetMirror TV API
# ============================================================
PLATFORMS = [
    ("Netflix",     "nf"),
    ("PrimeVideo",  "pv"),
    ("Hotstar",     "hs"),
]

def nm_headers(ott: str, extra: Optional[Dict] = None) -> Dict[str, str]:
    h = {
        "ott": ott,
        "User-Agent": NM_UA,
        "x-requested-with": "NetmirrorNewTV v1.0",
        "Accept": "application/json, text/plain, */*",
    }
    if extra: h.update(extra)
    return h

async def nm_search(ott: str, title: str) -> Optional[Dict]:
    url = f"{NM_API_BASE}/search.php?s={quote(title)}"
    try:
        c = await get_client()
        r = await c.get(url, headers=nm_headers(ott))
        if r.status_code != 200: return None
        return r.json()
    except Exception:
        return None

async def nm_post(ott: str, content_id: str) -> Optional[Dict]:
    url = f"{NM_API_BASE}/post.php?id={content_id}"
    try:
        c = await get_client()
        r = await c.get(url, headers=nm_headers(ott, {"Lastep": "", "Usertoken": ""}))
        if r.status_code != 200: return None
        return r.json()
    except Exception:
        return None

async def nm_episodes(ott: str, season_id: str, page: int = 1) -> Optional[Dict]:
    url = f"{NM_API_BASE}/episodes.php?id={season_id}&page={page}"
    try:
        c = await get_client()
        r = await c.get(url, headers=nm_headers(ott))
        if r.status_code != 200: return None
        return r.json()
    except Exception:
        return None

async def nm_player(ott: str, target_id: str) -> Optional[Dict]:
    url = f"{NM_API_BASE}/player.php?id={target_id}"
    try:
        c = await get_client()
        r = await c.get(url, headers=nm_headers(ott, {"Usertoken": ""}))
        if r.status_code != 200: return None
        return r.json()
    except Exception:
        return None

def _match_season(seasons: List[Dict], wanted: int) -> Optional[Dict]:
    if not seasons: return None
    for s in seasons:
        label = (s.get("s") or s.get("title") or "").lower()
        if re.match(rf"^season\s+{wanted}\b", label):
            return s
    return None

async def _find_episode_id(ott: str, season_id: str, wanted_ep: int) -> Optional[str]:
    page = 1
    while page < 30:
        data = await nm_episodes(ott, season_id, page)
        if not data or not data.get("episodes"): return None
        for e in data["episodes"]:
            if not e: continue
            ep_str = e.get("ep") or (e.get("epNum") or "").replace("E", "")
            try:
                if int(ep_str) == wanted_ep:
                    return e.get("id")
            except (ValueError, TypeError):
                continue
        if int(data.get("nextPageShow", 0)) != 1: break
        page += 1
    return None

# ============================================================
# Stream resolver
# ============================================================
async def resolve_movie_streams(tmdb_id: str) -> List[Dict[str, Any]]:
    title = await tmdb_title(tmdb_id, "movie")
    if not title: return []
    streams = []
    for name, ott in PLATFORMS:
        try:
            s = await nm_search(ott, title)
            if not s or not s.get("searchResult"): continue
            match = next((r for r in s["searchResult"] if r and r.get("t","").strip().lower() == title.lower()), None)
            if not match: match = s["searchResult"][0]
            content_id = match.get("id")
            if not content_id: continue
            post = await nm_post(ott, content_id)
            if not post: continue
            if post.get("type") == "t" or (post.get("episodes") and [e for e in post["episodes"] if e]):
                continue
            target_id = post.get("main_id") or content_id
            player = await nm_player(ott, target_id)
            if not player or not player.get("video_link"): continue
            streams.append(_build_stream(name, player, title, "movie"))
        except Exception as e:
            print(f"[NM] movie {name} error: {e}", file=sys.stderr)
    return streams

async def resolve_series_streams(tmdb_id: str, season: int, episode: int) -> List[Dict[str, Any]]:
    title = await tmdb_title(tmdb_id, "series")
    if not title: return []
    streams = []
    for name, ott in PLATFORMS:
        try:
            s = await nm_search(ott, title)
            if not s or not s.get("searchResult"): continue
            match = next((r for r in s["searchResult"] if r and r.get("t","").strip().lower() == title.lower()), None)
            if not match: match = s["searchResult"][0]
            content_id = match.get("id")
            if not content_id: continue
            post = await nm_post(ott, content_id)
            if not post: continue
            if post.get("type") != "t" and not (post.get("episodes") and [e for e in post["episodes"] if e]):
                continue
            seasons = post.get("season") or []
            # Check page-1 episodes
            target_ep_id = None
            selected_idx = next((i for i, s in enumerate(seasons) if s.get("selected")), -1)
            selected_season_num = selected_idx + 1 if selected_idx >= 0 else 1
            if selected_season_num == season and post.get("episodes"):
                for e in post["episodes"]:
                    if not e: continue
                    ep_str = e.get("ep") or ""
                    try:
                        if int(ep_str) == episode:
                            target_ep_id = e.get("id")
                            break
                    except: continue
            if not target_ep_id:
                season_item = _match_season(seasons, season)
                if not season_item or not season_item.get("id"): continue
                target_ep_id = await _find_episode_id(ott, season_item["id"], episode)
            if not target_ep_id: continue
            player = await nm_player(ott, target_ep_id)
            if not player or not player.get("video_link"): continue
            streams.append(_build_stream(name, player, title, "series", season, episode))
        except Exception as e:
            print(f"[NM] series {name} error: {e}", file=sys.stderr)
    return streams

def _build_stream(platform_name: str, player: Dict, title: str, media_type: str,
                  season: Optional[int] = None, episode: Optional[int] = None) -> Dict[str, Any]:
    video_link = player["video_link"]
    referer = player.get("referer") or NM_REFERER
    if media_type == "movie":
        stream_title = f"NetMirror | {platform_name} | {title}"
    else:
        ep_label = player.get("ep_title") or player.get("ep") or ""
        stream_title = f"NetMirror | {platform_name} | S{season}E{episode} · {ep_label}"
    return {
        "name": "NetMirror",
        "title": stream_title,
        "url": video_link,
        "quality": "Auto (1080p/720p/480p)",
        "behaviorHints": {
            "notWebReady": True,
            "proxyHeaders": {
                "request": {
                    "User-Agent": NM_UA,
                    "Referer": referer,
                    "Origin": "https://net52.cc",
                }
            }
        }
    }

# ============================================================
# FastAPI app
# ============================================================
app = FastAPI(title="NetMirror Stremio Addon")

@app.on_event("startup")
async def startup():
    c = await get_client()
    has_proxy = "with proxy" if PROXY_URL else "DIRECT (no proxy)"
    print(f"[NM] NetMirror addon started ({has_proxy})", file=sys.stderr)

def _build_manifest() -> Dict[str, Any]:
    return {
        "id": "com.netmirror.stremio",
        "version": "1.0.0",
        "name": "NetMirror",
        "description": "Movies & series from Netflix/PrimeVideo/Hotstar via NetMirror TV API. Multi-audio HLS with subtitles.",
        "logo": "https://tv.imgcdn.kim/newtv/img/nf_logo.png",
        "resources": ["stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "tmdb"],
        "catalogs": [],
        "behaviorHints": {"configurable": False},
    }

@app.get("/manifest.json")
async def manifest():
    return _build_manifest()

@app.get("/stream/movie/{id}.json")
async def stream_movie(id: str):
    return {"streams": await resolve_movie_streams(id)}

@app.get("/stream/series/{id}.json")
@app.get("/stream/series/{id}:{season}.json")
@app.get("/stream/series/{id}:{season}:{episode}.json")
async def stream_series(id: str, season: Optional[str] = None, episode: Optional[str] = None):
    if not season or not episode:
        return {"streams": []}
    # Handle tmdb:1396:1:1 format — FastAPI splits on ":"
    # id="tmdb", season="1396", episode="1" and there's a 4th part
    # Actually FastAPI path matching handles this differently. Let me check.
    # For tmdb:1396:1:1, id="tmdb:1396", season="1", episode="1" works
    # because the route pattern {id}:{season}:{episode} matches greedily on id
    return {"streams": await resolve_series_streams(id, int(season), int(episode))}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "proxy": "configured" if PROXY_URL else "none",
        "api_base": NM_API_BASE,
    }

@app.get("/")
async def root():
    return RedirectResponse(url="/configure")

@app.get("/configure")
async def configure_page():
    return HTMLResponse(CONFIGURE_HTML)

CONFIGURE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NetMirror — Stremio Addon</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0a0a0c;color:#e5e5e5;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:2rem}
.wrap{max-width:420px;width:100%;text-align:center}
.logo{width:80px;height:80px;margin:0 auto 1.5rem;border-radius:16px;overflow:hidden;background:#111}
.logo img{width:100%;height:100%;object-fit:cover}
h1{font-size:2rem;font-weight:700;margin-bottom:.5rem}
.sub{font-size:.9rem;opacity:.6;margin-bottom:2rem;line-height:1.5}
.url-box{width:100%;padding:1rem;background:#111;border:1px solid #222;border-radius:8px;font-family:monospace;font-size:.85rem;color:#888;word-break:break-all;text-align:center;margin-bottom:1rem;min-height:50px;display:flex;align-items:center;justify-content:center}
.copy-btn{display:block;width:100%;background:#e50914;color:#fff;border:none;padding:1rem;font-size:1rem;font-weight:600;border-radius:8px;cursor:pointer;transition:background .2s}
.copy-btn:hover{background:#b00710}
.copy-btn:active{transform:scale(.98)}
</style>
</head>
<body>
<div class="wrap">
<div class="logo"><img src="https://tv.imgcdn.kim/newtv/img/nf_logo.png" alt="NetMirror" onerror="this.style.display='none'"></div>
<h1>NetMirror</h1>
<p class="sub">Netflix / PrimeVideo / Hotstar<br>Movies & Series — All Qualities</p>
<div class="url-box" id="url">Loading...</div>
<button class="copy-btn" id="copy" onclick="copyUrl()">Copy Manifest URL</button>
</div>
<script>
var url=window.location.href.replace(/configure\\/?$/,'').replace(/\\/$/,'')+'/manifest.json';
document.getElementById('url').textContent=url;
function copyUrl(){
  navigator.clipboard.writeText(url);
  var b=document.getElementById('copy');
  b.textContent='Copied!';
  setTimeout(function(){b.textContent='Copy Manifest URL'},2000);
}
</script>
</body></html>"""

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
