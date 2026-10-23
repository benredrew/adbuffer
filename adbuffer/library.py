"""History, bookmarks and YouTube search for the app UI."""

import json
import os
import subprocess
import threading
import time

DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "adbuffer")
HISTORY_LIMIT = 200


class Library:
    def __init__(self, path: str = os.path.join(DATA_DIR, "library.json")):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}
        self.data.setdefault("history", [])
        self.data.setdefault("bookmarks", [])
        self.data.setdefault("sources", [])

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def visit(self, url: str, title: str):
        with self.lock:
            h = [e for e in self.data["history"] if e["url"] != url]
            h.insert(0, {"url": url, "title": title or url, "ts": time.time()})
            self.data["history"] = h[:HISTORY_LIMIT]
            self._save()

    def set_title(self, url: str, title: str):
        with self.lock:
            for e in self.data["history"] + self.data["bookmarks"]:
                if e["url"] == url and e["title"] == url:
                    e["title"] = title
            self._save()

    def toggle_bookmark(self, url: str, title: str) -> bool:
        with self.lock:
            b = self.data["bookmarks"]
            if any(e["url"] == url for e in b):
                self.data["bookmarks"] = [e for e in b if e["url"] != url]
                added = False
            else:
                b.insert(0, {"url": url, "title": title or url, "ts": time.time()})
                added = True
            self._save()
            return added

    def add_source(self, url: str, name: str):
        with self.lock:
            self.data["sources"] = [s for s in self.data["sources"] if s["url"] != url]
            self.data["sources"].append({"url": url, "name": name, "ts": time.time()})
            self._save()

    def remove(self, kind: str, url: str):
        with self.lock:
            self.data[kind] = [e for e in self.data[kind] if e["url"] != url]
            self._save()


def _ytdlp_json(args: list[str], timeout: float = 30) -> list[dict]:
    out = subprocess.run(["yt-dlp", "-q", "--no-warnings", "-J", *args],
                         capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "yt-dlp failed")
    data = json.loads(out.stdout)
    return data.get("entries") or [data]


def search(query: str, limit: int = 20) -> list[dict]:
    """YouTube search via yt-dlp (no API key needed)."""
    results = []
    for e in _ytdlp_json(["--flat-playlist", f"ytsearch{limit}:{query}"]):
        if not e or e.get("ie_key") not in (None, "Youtube"):
            continue
        thumbs = e.get("thumbnails") or []
        results.append({
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
            "title": e.get("title") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration": e.get("duration"),
            "live": e.get("live_status") == "is_live",
            "views": e.get("view_count"),
            "thumb": thumbs[-1]["url"] if thumbs else "",
        })
    return results


def lookup_title(url: str) -> str:
    try:
        out = subprocess.run(["yt-dlp", "-q", "--no-warnings", "--print", "title", "--playlist-items", "1", url],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    except (subprocess.TimeoutExpired, IndexError):
        return ""
