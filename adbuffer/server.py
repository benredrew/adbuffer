"""HTTP front end: the app UI, its JSON API, and the cleaned HLS stream."""

import asyncio
import logging
import os
import shutil
import subprocess
import tomllib
from urllib.parse import quote_plus

from aiohttp import web

from .library import Library, lookup_title, search
from .reports import KINDS, Reporter
from .sources import SourceCache
from .session import Sessions

log = logging.getLogger("server")

STATIC = os.path.join(os.path.dirname(__file__), "static")
THEME = os.path.expanduser("~/.local/state/omarchy/current/theme/colors.toml")
THEME_DEFAULTS = {
    "background": "#121212", "dark_background": "#0d0d0d", "lighter_background": "#1e1e1e",
    "foreground": "#bebebe", "light_foreground": "#8a8a8d", "dark_foreground": "#555555",
    "accent": "#e68e0d", "selection": "#2a2a2a", "muted": "#333333", "red": "#d35f5f",
}


def theme_mtime() -> float:
    try:
        return os.stat(THEME).st_mtime
    except OSError:
        return 0.0


def theme_css() -> str:
    colors = dict(THEME_DEFAULTS)
    try:
        with open(THEME, "rb") as f:
            colors.update({k: v for k, v in tomllib.load(f).items() if isinstance(v, str)})
    except (OSError, tomllib.TOMLDecodeError):
        pass
    body = "\n".join(f"  --{k.replace('_', '-')}: {v};" for k, v in colors.items())
    return f":root {{\n{body}\n  color-scheme: {colors.get('mode', 'dark')};\n}}\n"


def normalize_url(text: str) -> str | None:
    """Return a URL if the omnibox text looks like one, else None (= search)."""
    t = text.strip()
    if t.startswith(("http://", "https://")) or os.path.exists(t):
        return t
    if " " not in t and "." in t.split("/")[0] and not t.startswith("."):
        return "https://" + t
    return None


def open_in_browser(url: str):
    """Hand a page to the desktop's default browser (Brave on this system)."""
    cmd = ["omarchy-launch-browser", url] if shutil.which("omarchy-launch-browser") else ["xdg-open", url]
    subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_app(sessions: Sessions, library: Library, reporter: Reporter) -> web.Application:
    source_cache = SourceCache()

    async def index(_):
        # versioned asset URLs: a plain reload doesn't revalidate scripts and stylesheets
        with open(os.path.join(STATIC, "index.html")) as f:
            html = f.read()
        for name in ("app.js", "app.css", "hls.min.js"):
            v = int(os.stat(os.path.join(STATIC, name)).st_mtime)
            html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def theme(_):
        return web.Response(text=theme_css(), content_type="text/css", headers={"Cache-Control": "no-cache"})

    async def status(request):
        s = sessions.current
        around = request.query.get("around")
        c = sessions.classifier
        return web.json_response({
            "session": s.status(float(around) if around else None) if s else None,
            "theme": theme_mtime(),
            "detector": {"mode": c.name, "model": getattr(c, "model", None), "last": c.last},
            "reports": reporter.summary(),
        })

    async def get_library(_):
        return web.json_response(library.snapshot())

    async def start(url: str, title: str):
        s = await asyncio.to_thread(sessions.start, url, title)
        library.visit(url, title or url)
        if not title:
            async def fill_title():
                if t := await asyncio.to_thread(lookup_title, url):
                    s.title = t
                    library.set_title(url, t)
            asyncio.create_task(fill_title())
        return s

    async def watch(request):
        body = await request.json()
        url = normalize_url(body.get("url", ""))
        if not url:
            raise web.HTTPBadRequest(text="not a URL")
        s = await start(url, body.get("title", ""))
        return web.json_response({"id": s.id})

    async def watch_get(request):
        """GET /watch?url=... for the Brave passthrough and bookmarklets."""
        url = normalize_url(request.query.get("url", ""))
        if url:
            await start(url, request.query.get("title", ""))
        raise web.HTTPFound("/#player" if url else "/")

    async def stop(_):
        await asyncio.to_thread(sessions.stop)
        return web.json_response({})

    async def do_search(request):
        q = request.query.get("q", "").strip()
        if not q:
            return web.json_response([])
        try:
            return web.json_response(await asyncio.to_thread(search, q))
        except Exception as e:
            log.warning("search failed: %s", e)
            raise web.HTTPBadGateway(text=str(e))

    async def bookmark(request):
        body = await request.json()
        return web.json_response({"bookmarked": library.toggle_bookmark(body["url"], body.get("title", ""))})

    async def remove(request):
        body = await request.json()
        if body.get("kind") not in ("history", "bookmarks", "sources"):
            raise web.HTTPBadRequest()
        library.remove(body["kind"], body["url"])
        return web.json_response({})

    async def open_url(request):
        body = await request.json()
        text = body.get("url", "").strip()
        url = normalize_url(text) or f"https://search.brave.com/search?q={quote_plus(text)}"
        open_in_browser(url)
        return web.json_response({"opened": url})

    async def report(request):
        body = await request.json()
        s = sessions.current
        if not s:
            raise web.HTTPConflict(text="nothing playing")
        if body.get("kind") not in KINDS:
            raise web.HTTPBadRequest(text="unknown kind")
        job = await asyncio.to_thread(reporter.file, s, float(body["stream_time"]), body["kind"], body.get("note", "")[:500])
        return web.json_response({"id": job["id"]})

    async def add_source(request):
        body = await request.json()
        url = normalize_url(body.get("url", ""))
        if not url or os.path.exists(url):
            raise web.HTTPBadRequest(text="not a URL")
        library.add_source(url, body.get("name", "").strip())
        return web.json_response({"url": url})

    async def source_items(request):
        sources = library.snapshot()["sources"]
        listings = await asyncio.to_thread(source_cache.listings, sources, request.query.get("refresh") == "1")
        return web.json_response(listings)

    async def hls(request):
        s = sessions.current
        sid, name = request.match_info["sid"], request.match_info["name"]
        if not s or s.id != sid or "/" in name:
            raise web.HTTPNotFound()
        path = os.path.join(s.out_dir, name)
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        m3u8 = name.endswith(".m3u8")
        return web.FileResponse(path, headers={
            "Content-Type": "application/vnd.apple.mpegurl" if m3u8 else "video/mp2t",
            **({"Cache-Control": "no-cache"} if m3u8 else {}),
        })

    @web.middleware
    async def revalidate_static(request, handler):
        # the UI changes often; make Brave revalidate instead of running a stale copy
        resp = await handler(request)
        if request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    app = web.Application(middlewares=[revalidate_static])
    app.router.add_get("/", index)
    app.router.add_get("/theme.css", theme)
    app.router.add_get("/watch", watch_get)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/library", get_library)
    app.router.add_get("/api/search", do_search)
    app.router.add_post("/api/watch", watch)
    app.router.add_post("/api/stop", stop)
    app.router.add_post("/api/bookmark", bookmark)
    app.router.add_post("/api/remove", remove)
    app.router.add_post("/api/open", open_url)
    app.router.add_post("/api/report", report)
    app.router.add_post("/api/sources", add_source)
    app.router.add_get("/api/source-items", source_items)
    app.router.add_get("/hls/{sid}/{name}", hls)
    app.router.add_static("/static", STATIC)
    return app
