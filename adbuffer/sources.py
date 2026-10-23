"""Viewer-added sources: web pages that list streams (a league's live page, a channel, any site).

Nothing here knows about particular sites. A source page is listed with yt-dlp when it
understands the page; otherwise its links that look like events or streams are listed, plus
the page itself if it embeds a stream. Playing a page that yt-dlp can't resolve falls back to
`find_streams`, which looks for an HLS/DASH URL in the page and its embedded frames.
"""

import html as htmllib
import json
import logging
import re
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

log = logging.getLogger("sources")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
MEDIA = re.compile(r"""https?:[^"'\s<>()]+?\.(?:m3u8|mpd)(?:\?[^"'\s<>()]*)?""", re.I)
EVENT_HINT = re.compile(r"\b(vs\.?|v\.?|@|live|stream|watch|game|match|fight|race|kick-?off|tip-?off)\b", re.I)
SKIP_LINK = re.compile(r"(login|signup|register|privacy|terms|contact|about|faq|dmca|cookie|mailto:|javascript:)", re.I)
CACHE_TTL = 60
MAX_ITEMS = 60


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.frames: list[str] = []
        self.media: list[str] = []
        self.title = ""
        self._a: list | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._a = [a["href"], ""]
        elif tag == "iframe" and a.get("src"):
            self.frames.append(a["src"])
        elif tag in ("video", "source") and a.get("src"):
            self.media.append(a["src"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "a" and self._a:
            self.links.append((self._a[0], " ".join(self._a[1].split())))
            self._a = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._a is not None:
            self._a[1] += data
        if self._in_title:
            self.title += data


def fetch(url: str, referer: str | None = None, timeout: float = 10) -> tuple[str, str]:
    headers = {"User-Agent": UA, "Accept": "text/html,*/*"}
    if referer:
        headers["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.geturl(), r.read(2_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")


def parse(url: str, text: str) -> _Page:
    p = _Page()
    try:
        p.feed(text)
    except Exception:  # broken markup: keep whatever was parsed
        pass
    unescaped = text.replace("\\/", "/")
    p.media += MEDIA.findall(unescaped)
    p.media = list(dict.fromkeys(urljoin(url, htmllib.unescape(m)) for m in p.media))
    p.frames = [urljoin(url, f) for f in p.frames]
    return p


def find_streams(url: str, depth: int = 2, referer: str | None = None) -> list[dict]:
    """HLS/DASH URLs in a page or the frames it embeds, as yt-dlp-style format dicts."""
    try:
        final, text = fetch(url, referer)
    except Exception as e:
        log.info("can't fetch %s: %s", url, e)
        return []
    page = parse(final, text)
    found = [{"url": m, "protocol": "m3u8" if ".m3u8" in m.lower() else "https",
              "http_headers": {"User-Agent": UA, "Referer": final}} for m in page.media
             if re.search(r"\.(m3u8|mpd|mp4)(\?|$)", m, re.I)]
    if found or depth <= 0:
        return found
    for frame in page.frames[:6]:
        if found := find_streams(frame, depth - 1, referer=final):
            return found
    return []


def _ytdlp_items(url: str) -> list[dict]:
    out = subprocess.run(["yt-dlp", "-q", "--no-warnings", "-J", "--flat-playlist", "--playlist-items", f"1:{MAX_ITEMS}", url],
                         capture_output=True, text=True, timeout=25)
    if out.returncode != 0:
        return []
    info = json.loads(out.stdout)
    entries = info.get("entries")
    if entries is None:  # a single video or stream
        return [{"url": url, "title": info.get("title") or url, "live": bool(info.get("is_live")),
                 "thumb": info.get("thumbnail") or ""}]
    items = []
    for e in entries:
        if e and (e.get("url") or e.get("webpage_url")):
            thumbs = e.get("thumbnails") or []
            items.append({"url": e.get("webpage_url") or e["url"], "title": e.get("title") or e["url"],
                          "live": e.get("live_status") == "is_live", "thumb": thumbs[-1]["url"] if thumbs else ""})
    return items


def _page_items(url: str) -> list[dict]:
    final, text = fetch(url)
    page = parse(final, text)
    host = urlparse(final).hostname or ""
    site = ".".join(host.split(".")[-2:])
    items, seen = [], set()
    if page.media or page.frames:
        items.append({"url": final, "title": f"Stream on this page: {page.title.strip() or host}", "live": True, "thumb": ""})
        seen.add(final)
    for href, label in page.links:
        link = urljoin(final, href).split("#")[0]
        lhost = urlparse(link).hostname or ""
        if link in seen or not link.startswith("http") or SKIP_LINK.search(link) or not lhost.endswith(site):
            continue
        if not (EVENT_HINT.search(label) or EVENT_HINT.search(urlparse(link).path.replace("-", " "))):
            continue
        seen.add(link)
        items.append({"url": link, "title": label or urlparse(link).path.strip("/").replace("-", " "), "live": True, "thumb": ""})
        if len(items) >= MAX_ITEMS:
            break
    return items


def list_source(src: dict) -> dict:
    """Listing for one source: {"source", "items", "error"}."""
    url = src["url"]
    try:
        if re.search(r"\.(m3u8|mpd)(\?|$)", url, re.I):  # a stream URL itself
            items = [{"url": url, "title": src.get("name") or url, "live": True, "thumb": ""}]
        else:
            items = _ytdlp_items(url) or _page_items(url)
        error = "" if items else "nothing playable found on this page"
    except Exception as e:
        items, error = [], str(e)[:200]
    for it in items:
        u = urlparse(url)
        it["channel"] = src.get("name") or (u.netloc + u.path).removeprefix("www.").rstrip("/")
        it["source"] = url
    return {"source": url, "items": items, "error": error}


class SourceCache:
    def __init__(self):
        self.cache: dict[str, tuple[float, dict]] = {}
        self.pool = ThreadPoolExecutor(max_workers=6)

    def forget(self, url: str):
        self.cache.pop(url, None)

    def listings(self, sources: list[dict], refresh: bool = False) -> list[dict]:
        now = time.time()
        stale = [s for s in sources if refresh or now - self.cache.get(s["url"], (0, None))[0] > CACHE_TTL]
        for src, res in zip(stale, self.pool.map(list_source, stale)):
            self.cache[src["url"]] = (time.time(), res)
        return [self.cache[s["url"]][1] for s in sources if s["url"] in self.cache]
