#!/usr/bin/env python3
"""tiktok-dl - tải toàn bộ video của một profile TikTok.

Danh sách bài đăng lấy bằng yt-dlp, metadata từng bài đọc từ trang TikTok
(__UNIVERSAL_DATA_FOR_REHYDRATION__), file tải bằng aria2c rồi mux bằng ffmpeg
(gắn metadata, ảnh bìa, phụ đề; bài ảnh được dựng thành video slideshow).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

__version__ = "2.0.0"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
SCRIPT_DIR = Path(__file__).resolve().parent
REHYDRATION_RE = re.compile(
    r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)

# Ngôn ngữ thông báo: "en" (mặc định) hoặc "vi". Đặt bằng --lang hoặc biến môi trường TIKTOK_DL_LANG.
LANG = "en"


def t(en: str, vi: str) -> str:
    """Chọn câu thông báo theo ngôn ngữ đang dùng."""
    return vi if LANG == "vi" else en


def detect_lang(argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1].lower()
        if arg == "--lang" and i + 1 < len(argv):
            return argv[i + 1].lower()
    env = os.environ.get("TIKTOK_DL_LANG", "").lower()
    if env in ("en", "vi"):
        return env
    try:
        import locale
        name = (locale.getlocale()[0] or "").lower()
    except Exception:
        name = ""
    return "vi" if name.startswith(("vi", "vietnam")) else "en"


STATUS_MESSAGES = {
    10101: ("TikTok server error", "lỗi máy chủ TikTok"),
    10202: ("account does not exist", "tài khoản không tồn tại"),
    10204: ("post does not exist or was deleted", "bài đăng không tồn tại hoặc đã bị xoá"),
    10216: ("post is private", "bài đăng đang để riêng tư"),
    10221: ("account is banned", "tài khoản đã bị cấm"),
    10222: ("account is private", "tài khoản đang để riêng tư"),
    10223: ("account is restricted", "tài khoản bị hạn chế"),
}
FATAL_PROFILE_CODES = {10202, 10221, 10222, 10223}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Console output
# --------------------------------------------------------------------------- #

_print_lock = threading.RLock()
_status_line = ""   # dòng trạng thái luôn nằm dưới cùng (thanh tiến độ)


def _width() -> int:
    return max(40, shutil.get_terminal_size((100, 20)).columns - 1)


def _clear_status() -> None:
    if _status_line:
        sys.stdout.write("\r" + " " * _width() + "\r")


def _draw_status() -> None:
    if _status_line:
        w = _width()
        sys.stdout.write("\r" + _status_line[:w].ljust(w))
        sys.stdout.flush()


def log(msg: str = "", err: bool = False) -> None:
    """In một dòng log phía trên thanh tiến độ, rồi vẽ lại thanh tiến độ."""
    with _print_lock:
        _clear_status()
        sys.stdout.flush()
        print(msg, file=sys.stderr if err else sys.stdout, flush=True)
        _draw_status()


def status(msg: str) -> None:
    """Đặt dòng trạng thái dưới cùng (chuỗi rỗng = xoá)."""
    global _status_line
    with _print_lock:
        _clear_status()
        _status_line = msg
        _draw_status()


def fmt_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0 or seconds > 99 * 3600:
        return "--:--"
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


class ProgressBar:
    """Thanh tiến độ tổng cho cả profile, vẽ lại 4 lần/giây.

    % và ETA tính theo số bài đã xong (không nhảy), tốc độ là trung bình trượt 5 giây.
    """

    WIDTH = 28

    def __init__(self, total: int):
        self.total = max(1, total)
        self.ok = self.failed = 0
        self.base_bytes = 0
        self.active: list = []
        self.samples: list[tuple[float, int]] = []
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self.render()
        status("")

    def post_done(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self.ok += 1
            else:
                self.failed += 1

    def downloading(self, tasks) -> None:
        with self._lock:
            self.active = tasks

    def downloaded(self) -> None:
        with self._lock:
            self.base_bytes += sum(d.stat().st_size for _, d, _ in self.active if d.exists())
            self.active = []

    def _bytes(self) -> int:
        with self._lock:
            active = list(self.active)
            base = self.base_bytes
        total = base
        for _, d, _ in active:
            try:
                total += d.stat().st_size
            except OSError:
                pass
        return total

    def _loop(self) -> None:
        while not self._stop.wait(0.25):
            self.render()

    def render(self) -> None:
        now = time.monotonic()
        got = self._bytes()
        self.samples = [s for s in self.samples if now - s[0] <= 5] + [(now, got)]
        t0, b0 = self.samples[0]
        speed = (got - b0) / (now - t0) if now - t0 >= 0.5 else 0.0
        finished = self.ok + self.failed
        frac = min(1.0, finished / self.total)
        filled = int(frac * self.WIDTH)
        bar = "█" * filled + "░" * (self.WIDTH - filled)
        elapsed = now - self.started
        eta = elapsed / finished * (self.total - finished) if finished else None
        digits = len(str(self.total))
        line = (f"  {bar} {frac * 100:5.1f}%  {finished:>{digits}}/{self.total} {t('posts', 'bài')}"
                f"  {fmt_bytes(got):>8}  {fmt_bytes(speed) + '/s':>10}  ETA {fmt_eta(eta):>7}")
        if self.failed:
            line += f"  ✗ {self.failed}"
        status(line)


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def fmt_int(v) -> str:
    try:
        return f"{int(v):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "?"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class ScrapeError(Exception):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


def find_tool(name: str) -> Optional[str]:
    """Ưu tiên file chạy nằm cạnh tiktok-dl.py (vd aria2c.exe, ffmpeg.exe), sau đó tới PATH."""
    for cand in (SCRIPT_DIR / f"{name}.exe", SCRIPT_DIR / name):
        if cand.is_file():
            return str(cand)
    return shutil.which(name)


def find_ffmpeg() -> Optional[str]:
    path = find_tool("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


SHORT_LINK_RE = re.compile(r"^(?:https?://)?(?:(?:vt|vm)\.tiktok\.com/|(?:www\.)?tiktok\.com/t/)\w+", re.I)


def resolve_short_link(text: str) -> str:
    """Đổi link rút gọn (vt.tiktok.com/..., vm.tiktok.com/..., tiktok.com/t/...) thành link đầy đủ."""
    if not SHORT_LINK_RE.match(text):
        return text
    url = text if text.lower().startswith("http") else f"https://{text}"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, allow_redirects=False, timeout=20)
        for _ in range(5):
            loc = r.headers.get("Location")
            if not loc:
                break
            if re.search(r"tiktok\.com/@[^/?#]+", loc):
                return loc
            r = requests.get(loc, headers={"User-Agent": UA}, allow_redirects=False, timeout=20)
    except requests.RequestException as e:
        raise ScrapeError(t(f"could not open short link {text} ({e})", f"không mở được link rút gọn {text} ({e})"))
    raise ScrapeError(t(f"short link does not lead to a TikTok post or profile: {text}",
                        f"link rút gọn không dẫn tới bài đăng hay profile TikTok: {text}"))


def parse_target(text: str) -> tuple[str, Optional[str]]:
    """'user', '@user', link profile hoặc link một video -> (username, video_id | None)."""
    raw = resolve_short_link(text.strip())
    m = re.search(r"tiktok\.com/@([^/?#\s]+)(?:/(?:video|photo)/(\d+))?", raw, re.I)
    name, vid = (m.group(1), m.group(2)) if m else (raw.lstrip("@"), None)
    if not re.fullmatch(r"[\w.]{1,64}", name):
        raise ScrapeError(t(f'not a valid TikTok username: "{text}"', f'không nhận ra username TikTok: "{text}"'))
    return name, vid


def sanitize(text: str, limit: int = 60) -> str:
    text = re.sub(r"[\r\n\t]+", " ", text or "")
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f#]', "", text)
    text = re.sub(r"\s+", " ", text).strip()[:limit]
    return text.rstrip(". ")


def to_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def created_at(item: dict) -> datetime:
    return datetime.fromtimestamp(to_int(item.get("createTime")) or 0, tz=timezone.utc)


def base_name(item: dict) -> str:
    desc = sanitize(item.get("desc", ""))
    return f"{created_at(item):%Y-%m-%d}_{item['id']}" + (f"_{desc}" if desc else "")


def uniq(seq) -> list:
    return list(dict.fromkeys(x for x in seq if x))


# --------------------------------------------------------------------------- #
# TikTok web client
# --------------------------------------------------------------------------- #

class TikTok:
    def __init__(self, session_id: Optional[str] = None, wait: float = 0.3, proxy: Optional[str] = None):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        })
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        # Dùng phiên đăng nhập (cookie sessionid) của chính bạn.
        if session_id:
            for name in ("sessionid", "sessionid_ss", "sid_tt"):
                self.s.cookies.set(name, session_id, domain=".tiktok.com")
        self.wait = wait
        self._lock = threading.Lock()
        self._last = 0.0

    def _throttle(self) -> None:
        with self._lock:
            delay = self._last + self.wait - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()

    def _scope(self, url: str, scope: str) -> dict:
        last = t("no response", "không có phản hồi")
        for attempt in range(4):
            self._throttle()
            try:
                r = self.s.get(url, timeout=25)
                m = REHYDRATION_RE.search(r.text)
                if m:
                    data = json.loads(m.group(1)).get("__DEFAULT_SCOPE__", {}).get(scope)
                    if data is not None:
                        return data
                last = t(f"HTTP {r.status_code}, page has no data", f"HTTP {r.status_code}, trang không có dữ liệu")
            except (requests.RequestException, ValueError) as e:
                last = str(e)
            time.sleep(1 + 2 * attempt)
        raise ScrapeError(last)

    @staticmethod
    def _check(data: dict) -> None:
        code = to_int(data.get("statusCode")) or 0
        if code:
            known = STATUS_MESSAGES.get(code)
            raw = data.get("statusMsg") or ""
            msg = t(*known) if known else t(f"TikTok returned statusCode {code} {raw}", f"TikTok trả statusCode {code} {raw}")
            raise ScrapeError(msg.strip(), code)

    def user(self, username: str) -> dict:
        data = self._scope(f"https://www.tiktok.com/@{username}", "webapp.user-detail")
        self._check(data)
        return data["userInfo"]

    def item(self, username: str, video_id: str) -> dict:
        data = self._scope(f"https://www.tiktok.com/@{username}/video/{video_id}", "webapp.video-detail")
        self._check(data)
        return data["itemInfo"]["itemStruct"]

    def whoami(self) -> Optional[dict]:
        """Tài khoản đang đăng nhập theo cookie hiện tại, hoặc None nếu chưa đăng nhập.

        Khi cookie sessionid hợp lệ, trang TikTok nhúng thông tin người dùng vào webapp.app-context.
        """
        data = self._scope("https://www.tiktok.com/explore", "webapp.app-context")
        user = data.get("user") or {}
        return user if user.get("uniqueId") else None

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.s.cookies)


def list_posts(username: str, limit: Optional[int], cookiefile: Optional[str]) -> list[dict]:
    """Danh sách toàn bộ bài đăng của profile, mới nhất trước (yt-dlp TikTokUser)."""
    import yt_dlp

    class Quiet:
        def debug(self, msg):
            m = re.search(r"Downloading page (\d+)", msg)
            if m:
                status(t(f"  Getting post list... page {m.group(1)}", f"  Đang lấy danh sách bài đăng... trang {m.group(1)}"))
        info = debug

        def warning(self, msg):
            pass

        def error(self, msg):
            log(f"  {msg}", err=True)

    opts = {"extract_flat": "in_playlist", "skip_download": True, "logger": Quiet(), "no_warnings": True}
    if limit:
        opts["playlistend"] = limit
    if cookiefile:
        opts["cookiefile"] = cookiefile
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.tiktok.com/@{username}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ScrapeError(t(f"yt-dlp could not get the post list ({e}). Try: pip install -U yt-dlp",
                             f"yt-dlp không lấy được danh sách bài đăng ({e}). Thử: pip install -U yt-dlp"))
    return [e for e in (info or {}).get("entries") or [] if e and e.get("id")]


def write_cookie_file(session_id: str, folder: Path) -> str:
    path = folder / "session-cookies.txt"
    exp = int(time.time()) + 30 * 86400
    rows = [f".tiktok.com\tTRUE\t/\tTRUE\t{exp}\t{n}\t{session_id}" for n in ("sessionid", "sessionid_ss", "sid_tt")]
    path.write_text("# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #

def flatten(item: dict) -> dict:
    author = item.get("author") or {}
    music = item.get("music") or {}
    video = item.get("video") or {}
    stats = {**(item.get("stats") or {}), **(item.get("statsV2") or {})}
    extra = item.get("textExtra") or []
    images = (item.get("imagePost") or {}).get("images") or []
    subs = video.get("subtitleInfos") or []
    username = author.get("uniqueId", "")
    return {
        "id": item.get("id"),
        "url": f"https://www.tiktok.com/@{username}/video/{item.get('id')}",
        "type": "slide" if images else "video",
        "created": created_at(item).isoformat(),
        "username": username,
        "nickname": author.get("nickname"),
        "author_id": author.get("id"),
        "author_verified": author.get("verified"),
        "description": item.get("desc"),
        "hashtags": " ".join(f"#{t['hashtagName']}" for t in extra if t.get("hashtagName")),
        "mentions": " ".join(f"@{t['userUniqueId']}" for t in extra if t.get("userUniqueId")),
        "plays": to_int(stats.get("playCount")),
        "likes": to_int(stats.get("diggCount")),
        "comments": to_int(stats.get("commentCount")),
        "shares": to_int(stats.get("shareCount")),
        "saves": to_int(stats.get("collectCount")),
        "reposts": to_int(stats.get("repostCount")),
        "duration": to_int(video.get("duration")),
        "width": to_int(video.get("width")),
        "height": to_int(video.get("height")),
        "definition": video.get("definition") or video.get("ratio"),
        "codec": video.get("codecType"),
        "bitrate": to_int(video.get("bitrate")),
        "file_size": to_int(video.get("size")),
        "image_count": len(images) or None,
        "subtitles": " ".join(s.get("LanguageCodeName", "") for s in subs) or None,
        "music_id": music.get("id"),
        "music_title": music.get("title"),
        "music_author": music.get("authorName"),
        "music_original": music.get("original"),
        "music_duration": to_int(music.get("duration")),
        "location_created": item.get("locationCreated"),
        "text_language": item.get("textLanguage"),
        "is_ad": item.get("isAd", False),
        "is_ai_generated": item.get("IsAigc"),
        "ai_description": item.get("AIGCDescription") or None,
        "category_type": item.get("CategoryType"),
        "diversification_labels": " | ".join(item.get("diversificationLabels") or []) or None,
        "suggested_words": " | ".join(item.get("suggestedWords") or []) or None,
        "sticker_texts": " | ".join(t for s in item.get("stickersOnItem") or [] for t in s.get("stickerText") or []) or None,
        "effects": " | ".join(e.get("name", "") for e in item.get("effectStickers") or []) or None,
        "duet_enabled": item.get("duetEnabled", False),
        "stitch_enabled": item.get("stitchEnabled", False),
        "share_enabled": item.get("shareEnabled"),
        "comment_status": item.get("itemCommentStatus"),
        "private_item": item.get("privateItem"),
        "pinned": item.get("isPinnedItem"),
        "schedule_time": item.get("scheduleTime"),
        "warn_info": json.dumps(item["warnInfo"], ensure_ascii=False) if item.get("warnInfo") else None,
    }


def mux_tags(item: dict) -> dict:
    """Metadata ghi vào file MP4 (Explorer / VLC / MediaInfo đọc được)."""
    f = flatten(item)
    when = created_at(item)
    desc = item.get("desc") or ""
    title = desc.splitlines()[0][:120] if desc.strip() else f"TikTok {item['id']}"
    comment = [
        f["url"],
        f"Views {fmt_int(f['plays'])} · Likes {fmt_int(f['likes'])} · Comments {fmt_int(f['comments'])}"
        f" · Shares {fmt_int(f['shares'])} · Saves {fmt_int(f['saves'])}",
    ]
    if f["music_title"]:
        comment.append(f"Music: {f['music_title']}" + (f" - {f['music_author']}" if f["music_author"] else ""))
    if f["hashtags"]:
        comment.append(f"Hashtags: {f['hashtags']}")
    return {
        "title": title,
        "artist": f"{f['nickname']} (@{f['username']})" if f["nickname"] else f"@{f['username']}",
        "album_artist": f["nickname"] or f["username"],
        "album": f"TikTok @{f['username']}",
        "date": f"{when:%Y-%m-%d}",
        "creation_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": desc,
        "synopsis": desc,
        "comment": "\n".join(comment),
        "genre": "TikTok",
        "episode_id": str(item["id"]),
        "copyright": f"© {f['nickname'] or f['username']}",
    }


# --------------------------------------------------------------------------- #
# Progress tracking (SQLite)
# --------------------------------------------------------------------------- #

class Progress:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, status TEXT, "
                        "file TEXT, error TEXT, updated TEXT)")
        self.db.commit()

    def done(self, post_id: str, folder: Path) -> bool:
        row = self.db.execute("SELECT status, file FROM posts WHERE id = ?", (post_id,)).fetchone()
        return bool(row and row[0] == "done" and row[1] and (folder / row[1]).exists())

    def mark(self, post_id: str, state: str, file: Optional[str] = None, error: Optional[str] = None) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO posts VALUES (?, ?, ?, ?, ?)",
                            (post_id, state, file, error, datetime.now().isoformat(timespec="seconds")))
            self.db.commit()


# --------------------------------------------------------------------------- #
# Download plan
# --------------------------------------------------------------------------- #

class Post:
    def __init__(self, item: dict, work: Path, opts):
        self.item = item
        self.id = str(item["id"])
        self.base = base_name(item)
        self.work = work / self.id
        self.tasks: list[tuple[list[str], Path, int]] = []   # (mirrors, dest, expected size)
        self.hidden: Optional[str] = None                    # lý do nếu TikTok không trả link tải
        video = item.get("video") or {}
        images = (item.get("imagePost") or {}).get("images") or []
        self.is_slide = bool(images)
        username = (item.get("author") or {}).get("uniqueId") or "_"
        self.url = f"https://www.tiktok.com/@{username}/{'photo' if images else 'video'}/{self.id}"

        if self.is_slide:
            self.images = [self.work / f"img_{i:02d}.jpg" for i in range(1, len(images) + 1)]
            for img, dest in zip(images, self.images):
                self.tasks.append((uniq((img.get("imageURL") or {}).get("urlList") or []), dest, 0))
            play = (item.get("music") or {}).get("playUrl")
            self.audio = self.work / "audio.mp3" if play else None
            if play:
                self.tasks.append(([play], self.audio, 0))
            return

        # Các link này KHÔNG phải mirror của cùng một file: www.tiktok.com/aweme/v1/play trả HTML,
        # downloadAddr là bản có watermark. aria2c chỉ dùng một link mỗi lượt, lỗi thì thử link sau.
        struct = video.get("PlayAddrStruct") or {}
        mirrors = uniq([video.get("playAddr"), *(struct.get("UrlList") or []), video.get("downloadAddr")])
        mirrors = [u for u in mirrors if "/aweme/v1/play" not in u]
        if not mirrors:
            # TikTok cố ý không trả link video cho người chưa đăng nhập, hay gặp ở bài quảng cáo / TikTok Shop.
            if item.get("isAd") or item.get("isECVideo"):
                self.hidden = t("ad / TikTok Shop video: TikTok does not provide a download link on the web "
                                "(even when logged in)",
                                "video quảng cáo / TikTok Shop: TikTok không cung cấp link tải qua web "
                                "(kể cả khi đã đăng nhập)")
            else:
                self.hidden = t("TikTok returned no download link for this video (it may be restricted), "
                                "try again with --session-id",
                                "TikTok không trả link tải cho video này (có thể bị giới hạn), thử lại với --session-id")
            return
        self.video = self.work / "video.mp4"
        self.tasks.append((mirrors, self.video, to_int(struct.get("DataSize")) or to_int(video.get("size")) or 0))

        cover = video.get("originCover") or video.get("cover")
        self.cover = self.work / "cover.img" if cover and opts.cover else None
        if self.cover:
            self.tasks.append(([cover], self.cover, 0))

        self.subs: list[tuple[Path, str]] = []
        seen = set()
        for sub in video.get("subtitleInfos") or [] if opts.subs != "none" else []:
            lang = (sub.get("LanguageCodeName") or "und").split("-")[0].lower()
            if not sub.get("Url") or lang in seen:
                continue
            if opts.subs == "original" and sub.get("Source") == "MT":
                continue
            seen.add(lang)
            dest = self.work / f"sub_{len(self.subs)}.vtt"
            self.subs.append((dest, lang))
            self.tasks.append(([sub["Url"]], dest, 0))


def is_valid_file(dest: Path) -> bool:
    """Loại các file 'tải xong' nhưng thực ra là trang HTML/JSON báo lỗi."""
    try:
        if not dest.is_file() or dest.stat().st_size < 64 or Path(f"{dest}.aria2").exists():
            return False
        with open(dest, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    if dest.suffix == ".vtt":
        return head.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT")
    if head.lstrip()[:1] in (b"<", b"{"):
        return False
    if dest.suffix == ".mp4":
        return head[4:8] == b"ftyp"
    return True


def download_aria2(aria2: str, tasks, cookie: str, jobs: int, conns: int, work: Path, attempt: int) -> None:
    lines = []
    for mirrors, dest, _ in tasks:
        if attempt >= len(mirrors) or is_valid_file(dest):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        for leftover in (dest, Path(f"{dest}.aria2")):
            leftover.unlink(missing_ok=True)
        lines += [mirrors[attempt], f"  dir={dest.parent}", f"  out={dest.name}"]
    if not lines:
        return
    inp = work / "aria2-input.txt"
    inp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        aria2, f"--input-file={inp}", f"--max-concurrent-downloads={jobs}",
        f"--max-connection-per-server={conns}", f"--split={conns}", "--min-split-size=1M",
        "--continue=true", "--allow-overwrite=true", "--auto-file-renaming=false",
        "--file-allocation=none", "--max-tries=5", "--retry-wait=2", "--timeout=30",
        "--connect-timeout=15", "--quiet=true", f"--user-agent={UA}",
        "--referer=https://www.tiktok.com/", f"--header=Cookie: {cookie}", "--header=Accept: */*",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        raise


def download_requests(session: requests.Session, tasks, jobs: int) -> None:
    def one(task):
        mirrors, dest, _ = task
        if is_valid_file(dest):
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        for url in mirrors:
            for _ in range(3):
                try:
                    with session.get(url, stream=True, timeout=30) as r:
                        r.raise_for_status()
                        with open(dest, "wb") as fh:
                            for chunk in r.iter_content(1 << 16):
                                fh.write(chunk)
                    if is_valid_file(dest):
                        return
                    break  # tải được nhưng là trang lỗi: sang link khác
                except requests.RequestException:
                    time.sleep(1)
        dest.unlink(missing_ok=True)

    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(one, tasks))


# --------------------------------------------------------------------------- #
# ffmpeg mux
# --------------------------------------------------------------------------- #

def run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return p.returncode == 0, p.stderr.decode("utf-8", "replace").strip()[-400:]


def tag_args(tags: dict) -> list[str]:
    out = []
    for k, v in tags.items():
        if v:
            out += ["-metadata", f"{k}={v}"]
    return out


def mux_video(ffmpeg: str, post: Post, out: Path) -> str:
    tags = tag_args(mux_tags(post.item))
    subs = [(p, lang) for p, lang in post.subs if p.exists() and p.stat().st_size]
    cover = post.cover if post.cover and post.cover.exists() and post.cover.stat().st_size else None

    def build(with_cover: bool, with_subs: bool) -> list[str]:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(post.video)]
        maps = ["-map", "0:v:0", "-map", "0:a?"]
        n = 1
        use_subs = subs if with_subs else []
        for p, _ in use_subs:
            cmd += ["-i", str(p)]
            maps += ["-map", f"{n}:0"]
            n += 1
        if with_cover and cover:
            cmd += ["-i", str(cover)]
            maps += ["-map", f"{n}:0"]
        cmd += maps + ["-c", "copy", "-c:s", "mov_text"]
        if with_cover and cover:
            cmd += ["-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic"]
        for i, (_, lang) in enumerate(use_subs):
            cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
        return cmd + tags + ["-movflags", "+faststart", str(out)]

    # Nếu ảnh bìa (có thể là HEIC/WebP lạ) hoặc phụ đề làm ffmpeg lỗi thì bỏ dần từng thứ.
    notes = []
    for with_cover, with_subs in ((True, True), (False, True), (False, False)):
        ok, err = run_ffmpeg(build(with_cover, with_subs))
        if ok:
            return ", ".join(notes)
        notes.append(t("cover skipped", "bỏ ảnh bìa") if with_cover and cover else t("subtitles skipped", "bỏ phụ đề"))
    raise ScrapeError(t(f"ffmpeg error: {err}", f"ffmpeg lỗi: {err}"))


def mux_slideshow(ffmpeg: str, post: Post, out: Path, seconds: float) -> None:
    images = [p for p in post.images if p.exists() and p.stat().st_size]
    lst = post.work / "slides.txt"
    body = "".join(f"file '{p.as_posix()}'\nduration {seconds}\n" for p in images)
    lst.write_text(body + f"file '{images[-1].as_posix()}'\n", encoding="utf-8")
    vf = ("scale=1080:1920:force_original_aspect_ratio=decrease:out_range=tv,"
          "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=yuv420p")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst)]
    audio = post.audio if post.audio and post.audio.exists() and post.audio.stat().st_size else None
    if audio:
        cmd += ["-stream_loop", "-1", "-i", str(audio)]
    cmd += ["-map", "0:v"] + (["-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-shortest"] if audio else [])
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-t", str(seconds * len(images))]
    cmd += tag_args(mux_tags(post.item)) + ["-movflags", "+faststart", str(out)]
    ok, err = run_ffmpeg(cmd)
    if not ok:
        raise ScrapeError(t(f"ffmpeg could not build the slideshow: {err}", f"ffmpeg không dựng được slideshow: {err}"))


def finalize(post: Post, out_dir: Path, ffmpeg: Optional[str], opts) -> tuple[str, int, str]:
    """Mux/move files of one post into out_dir. Returns (main file name, total bytes, note)."""
    when = created_at(post.item).timestamp()
    produced: list[Path] = []
    note = ""

    if post.is_slide:
        images = [p for p in post.images if p.exists() and p.stat().st_size]
        if not images:
            raise ScrapeError(t("no image could be downloaded", "không tải được ảnh nào"))
        if len(images) < len(post.images):
            missing = len(post.images) - len(images)
            note = t(f"{missing} image(s) missing", f"thiếu {missing} ảnh")
        main = None
        if ffmpeg and opts.slideshow:
            tmp = post.work / "slideshow.mp4"
            mux_slideshow(ffmpeg, post, tmp, opts.slide_seconds)
            main = out_dir / f"{post.base}.mp4"
            os.replace(tmp, main)
            produced.append(main)
        if opts.keep_images or not main:
            for i, p in enumerate(images, 1):
                dest = out_dir / f"{post.base}_{i:02d}.jpg"
                shutil.copyfile(p, dest)
                produced.append(dest)
            if post.audio and post.audio.exists():
                dest = out_dir / f"{post.base}.mp3"
                shutil.copyfile(post.audio, dest)
                produced.append(dest)
        main = main or produced[0]
    else:
        if not post.video.exists() or post.video.stat().st_size < 1024:
            raise ScrapeError(t("video could not be downloaded", "không tải được video"))
        main = out_dir / f"{post.base}.mp4"
        if ffmpeg:
            tmp = post.work / "muxed.mp4"
            note = mux_video(ffmpeg, post, tmp)
            os.replace(tmp, main)
        else:
            shutil.copyfile(post.video, main)
            note = t("no ffmpeg, metadata not added", "không có ffmpeg, chưa gắn metadata")
        produced.append(main)

    for p in produced:
        os.utime(p, (when, when))
    if not opts.keep_parts:
        shutil.rmtree(post.work, ignore_errors=True)
    return main.name, sum(p.stat().st_size for p in produced), note


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #

def save_profile(tt: TikTok, username: str, out_dir: Path) -> Optional[dict]:
    try:
        info = tt.user(username)
    except ScrapeError as e:
        if e.code in FATAL_PROFILE_CODES:
            raise ScrapeError(f"@{username}: {e}", e.code)
        log(t(f"  ! Could not read profile info ({e}), continuing.",
              f"  ! Không đọc được thông tin profile ({e}), vẫn tiếp tục."))
        return None
    (out_dir / "profile.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    avatar = (info.get("user") or {}).get("avatarLarger")
    if avatar:
        try:
            r = tt.s.get(avatar, timeout=20)
            if r.ok and r.content:
                (out_dir / "avatar.jpg").write_bytes(r.content)
        except requests.RequestException:
            pass
    return info


def write_csv(out_dir: Path) -> int:
    rows = []
    for f in sorted((out_dir / "metadata").glob("*.json")):
        try:
            rows.append(json.loads(f.read_text(encoding="utf-8"))["metadata"])
        except (ValueError, KeyError):
            continue
    rows.sort(key=lambda r: r.get("created") or "", reverse=True)
    if rows:
        with open(out_dir / "videos.csv", "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    return len(rows)


def scrape_profile(target: str, opts, aria2: Optional[str], ffmpeg: Optional[str]) -> int:
    username, single = parse_target(target)
    out_dir = Path(opts.output) / username
    (out_dir / "metadata").mkdir(parents=True, exist_ok=True)
    work = out_dir / ".parts"
    work.mkdir(exist_ok=True)
    tt = TikTok(opts.session_id, opts.wait, opts.proxy)

    log(f"\n=== @{username} ===")
    info = save_profile(tt, username, out_dir)
    if info:
        u, st = info.get("user") or {}, info.get("stats") or {}
        log(f"  {u.get('nickname', username)}{' ✓' if u.get('verified') else ''} · "
            f"{fmt_int(st.get('followerCount'))} {t('followers', 'follower')} · "
            f"{fmt_int(st.get('heartCount'))} {t('likes', 'tim')} · "
            f"{fmt_int(st.get('videoCount'))} {t('posts', 'bài đăng')}")

    if single:
        entries = [{"id": single}]
    else:
        cookiefile = write_cookie_file(opts.session_id, work) if opts.session_id else None
        try:
            entries = list_posts(username, opts.limit, cookiefile)
        finally:
            status("")
        log(t(f"  Found {len(entries)} posts.", f"  Tìm thấy {len(entries)} bài đăng."))

    progress = Progress(out_dir / ".progress.db")
    if opts.metadata_only:
        pending = [e for e in entries if not (out_dir / "metadata" / f"{e['id']}.json").exists()]
    else:
        pending = [e for e in entries if not progress.done(str(e["id"]), out_dir)]
    if len(pending) < len(entries):
        skipped = len(entries) - len(pending)
        log(t(f"  Skipping {skipped} posts already downloaded.", f"  Bỏ qua {skipped} bài đã có."))

    n = len(pending)
    if not n:
        log(t("  Nothing left to download.", "  Không còn bài nào cần tải."))
        write_csv(out_dir)
        return 0
    log(t(f"  Processing {n} post{'s' if n != 1 else ''} → {out_dir.resolve()}\n", f"  Bắt đầu xử lý {n} bài → {out_dir.resolve()}\n"))
    started = time.monotonic()
    tally = {"ok": 0, "failed": 0, "bytes": 0, "dl_bytes": 0, "dl_secs": 0.0}
    tally_lock = threading.Lock()
    failures: list[tuple[str, str]] = []   # (link bài, lý do)
    bar = ProgressBar(n)

    def post_url(pid: str) -> str:
        return f"https://www.tiktok.com/@{username}/video/{pid}"

    def succeed(line: str, size: int = 0) -> None:
        with tally_lock:
            tally["ok"] += 1
            tally["bytes"] += size
        bar.post_done(True)
        log(f"  ✓ {line}")

    def fail(url: str, reason: str) -> None:
        with tally_lock:
            tally["failed"] += 1
            failures.append((url, reason))
        bar.post_done(False)
        log(f"  ✗ {url}\n      └ {reason}")

    def fetch(entry):
        try:
            return entry, tt.item(username, str(entry["id"])), None
        except ScrapeError as e:
            return entry, None, str(e)

    def fetch_batch(batch):
        with ThreadPoolExecutor(opts.workers) as pool:
            return list(pool.map(fetch, batch))

    def finish(post: Post) -> None:
        try:
            name, size, note = finalize(post, out_dir, ffmpeg, opts)
        except (ScrapeError, OSError) as e:
            progress.mark(post.id, "error", error=str(e))
            fail(post.url, str(e))
            return
        progress.mark(post.id, "done", file=name)
        kind = "[slideshow] " if post.is_slide else ""
        succeed(f"{kind}{name}  {fmt_bytes(size)}{f'  ({note})' if note else ''}", size)

    # Chạy gối đầu 3 bước: trong lúc aria2c tải lô hiện tại thì lô sau đang được đọc metadata,
    # còn các lô đã tải xong được ffmpeg mux ở nền. Mỗi lần chỉ đọc trước 1 lô vì link tải
    # của TikTok hết hạn sau vài giờ.
    batches = [pending[i:i + opts.batch] for i in range(0, n, opts.batch)]
    meta_pool = ThreadPoolExecutor(1)
    mux_pool = ThreadPoolExecutor(opts.mux_jobs)
    mux_futures = []
    with bar:
        try:
            upcoming = meta_pool.submit(fetch_batch, batches[0])
            for bi, batch in enumerate(batches):
                results = upcoming.result()
                cookie = tt.cookie_header()
                upcoming = meta_pool.submit(fetch_batch, batches[bi + 1]) if bi + 1 < len(batches) else None

                posts: list[Post] = []
                for entry, item, err in results:
                    pid = str(entry["id"])
                    if err:
                        progress.mark(pid, "error", error=err)
                        fail(post_url(pid), t(f"could not read the post page: {err}", f"không đọc được trang bài đăng: {err}"))
                        continue
                    meta_path = out_dir / "metadata" / f"{pid}.json"
                    meta_path.write_text(json.dumps({"metadata": flatten(item), "raw": item},
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
                    if opts.metadata_only:
                        progress.mark(pid, "metadata")
                        succeed(f"metadata {pid}")
                        continue
                    post = Post(item, work, opts)
                    if post.hidden:
                        progress.mark(pid, "error", error=post.hidden)
                        fail(post.url, post.hidden)
                        continue
                    posts.append(post)
                if not posts:
                    continue

                tasks = [t for p in posts for t in p.tasks]
                bar.downloading(tasks)
                t0 = time.monotonic()
                try:
                    if aria2:
                        for attempt in range(max((len(t[0]) for t in tasks), default=0)):
                            download_aria2(aria2, tasks, cookie, opts.jobs, opts.connections, work, attempt)
                        for _, dest, _ in tasks:
                            if not is_valid_file(dest):
                                dest.unlink(missing_ok=True)
                    else:
                        download_requests(tt.s, tasks, opts.jobs)
                finally:
                    bar.downloaded()
                with tally_lock:
                    tally["dl_secs"] += time.monotonic() - t0
                    tally["dl_bytes"] += sum(d.stat().st_size for _, d, _ in tasks if d.exists())

                mux_futures += [mux_pool.submit(finish, post) for post in posts]
            for fut in mux_futures:
                fut.result()
        finally:
            meta_pool.shutdown(wait=False, cancel_futures=True)
            mux_pool.shutdown(wait=True)

    ok, failed, total_bytes = tally["ok"], tally["failed"], tally["bytes"]
    rows = write_csv(out_dir)
    if not opts.keep_parts and work.exists() and not any(p.is_dir() for p in work.iterdir()):
        shutil.rmtree(work, ignore_errors=True)

    failed_file = out_dir / "failed.txt"
    if failures:
        lines = [t(f"# @{username} - {len(failures)} posts could not be downloaded - {datetime.now():%Y-%m-%d %H:%M}",
                   f"# @{username} - {len(failures)} bài không tải được - {datetime.now():%Y-%m-%d %H:%M}"),
                 t("# Run the same command again to retry. Open each link to check the post yourself.",
                   "# Chạy lại đúng lệnh cũ để thử lại. Mở từng link để tự kiểm tra bài đó."), ""]
        lines += [f"{url}\n    {reason}" for url, reason in failures]
        failed_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        failed_file.unlink(missing_ok=True)

    secs = max(1.0, time.monotonic() - started)
    speed = f" · aria2c {fmt_bytes(tally['dl_bytes'] / tally['dl_secs'])}/s" if tally["dl_secs"] > 0 else ""
    log(t(f"\n  ── Done @{username} ──", f"\n  ── Xong @{username} ──"))
    log(f"  ✓ {ok} {t('done', 'thành công')}   ✗ {failed} {t('failed', 'lỗi')}"
        + (f"   · {fmt_bytes(total_bytes)} {t('in', 'trong')} {fmt_eta(secs)}{speed}" if total_bytes else ""))
    log(t(f"  Folder: {out_dir.resolve()}  (videos.csv: {rows} post{'s' if rows != 1 else ''})",
          f"  Thư mục: {out_dir.resolve()}  (videos.csv: {rows} bài)"))
    if failures:
        log(t(f"  Failed posts with links: {failed_file.resolve()}",
              f"  Danh sách bài lỗi kèm link: {failed_file.resolve()}"))
        log(t("  Run the same command again to retry failed posts (finished posts are skipped).",
              "  Chạy lại đúng lệnh cũ để thử lại các bài lỗi (bài đã xong sẽ được bỏ qua)."))
    return failed


def main() -> None:
    global LANG
    LANG = detect_lang(sys.argv[1:])
    if LANG not in ("en", "vi"):
        LANG = "en"

    p = argparse.ArgumentParser(
        prog="tiktok-dl",
        description=t("Download every video from a TikTok profile (aria2c + ffmpeg, with full metadata).",
                      "Tải toàn bộ video của profile TikTok (aria2c + ffmpeg, kèm đầy đủ metadata)."),
        epilog=t("Examples:", "Ví dụ:")
               + "\n  python tiktok-dl.py @tiktok\n  python tiktok-dl.py tiktok -n 20 -j 16\n"
               "  python tiktok-dl.py https://www.tiktok.com/@tiktok/video/7704834703446117662\n"
               "  python tiktok-dl.py https://vt.tiktok.com/XXXXXXXXX/\n"
               "  python tiktok-dl.py @tiktok --lang vi\n\n"
               + t("Put aria2c.exe and ffmpeg.exe next to tiktok-dl.py (or in PATH).",
                   "Đặt aria2c.exe và ffmpeg.exe cạnh tiktok-dl.py (hoặc trong PATH)."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("targets", nargs="*", metavar="profile",
                   help=t("username, @username, profile link, video link or short link (several allowed)",
                          "username, @username, link profile, link video hoặc link rút gọn (có thể nhập nhiều)"))
    p.add_argument("-o", "--output", default="downloads",
                   help=t("root folder to save into (default: downloads)", "thư mục gốc để lưu (mặc định: downloads)"))
    p.add_argument("-n", "--limit", type=int, help=t("only the N newest posts", "chỉ lấy N bài mới nhất"))
    p.add_argument("-j", "--jobs", type=int, default=16,
                   help=t("files aria2c downloads at once (default: 16)", "số file aria2c tải cùng lúc (mặc định: 16)"))
    p.add_argument("-x", "--connections", type=int, default=16,
                   help=t("aria2c connections per file, max 16 (default: 16)",
                          "số kết nối aria2c cho mỗi file, tối đa 16 (mặc định: 16)"))
    p.add_argument("-w", "--workers", type=int, default=8,
                   help=t("threads reading metadata (default: 8)", "số luồng đọc metadata (mặc định: 8)"))
    p.add_argument("--wait", type=float, default=0.1,
                   help=t("seconds between TikTok page requests (default: 0.1)",
                          "giãn cách giữa các request trang TikTok, giây (mặc định: 0.1)"))
    p.add_argument("--batch", type=int, default=50,
                   help=t("posts per read-download-mux batch (default: 50)", "số bài mỗi lô đọc-tải-mux (mặc định: 50)"))
    p.add_argument("--mux-jobs", type=int, default=max(1, min(4, (os.cpu_count() or 2) // 2)),
                   help=t("files ffmpeg processes at once", "số file ffmpeg xử lý cùng lúc"))
    p.add_argument("--metadata-only", action="store_true",
                   help=t("only save metadata (JSON + CSV), no files", "chỉ lấy metadata (JSON + CSV), không tải file"))
    p.add_argument("--subs", choices=("original", "all", "none"), default="original",
                   help=t("subtitles to embed: original = original/auto captions, all = also machine translations "
                          "(default: original)",
                          "phụ đề gắn vào video: original = phụ đề gốc/tự động, all = cả bản dịch máy "
                          "(mặc định: original)"))
    p.add_argument("--no-cover", dest="cover", action="store_false",
                   help=t("do not embed the cover image", "không gắn ảnh bìa vào MP4"))
    p.add_argument("--no-slideshow", dest="slideshow", action="store_false",
                   help=t("photo posts: save images + music only, no slideshow video",
                          "bài ảnh: không dựng video slideshow, chỉ lưu ảnh + nhạc"))
    p.add_argument("--keep-images", action="store_true",
                   help=t("photo posts: also save each JPG + MP3 next to the slideshow",
                          "bài ảnh: lưu thêm từng ảnh JPG + MP3 cạnh slideshow"))
    p.add_argument("--slide-seconds", type=float, default=3.0,
                   help=t("seconds per image in slideshows (default: 3)",
                          "số giây mỗi ảnh trong slideshow (mặc định: 3)"))
    p.add_argument("--keep-parts", action="store_true",
                   help=t("keep temporary files in .parts/", "giữ lại file tạm trong .parts/"))
    p.add_argument("--session-id",
                   help=t('your TikTok "sessionid" cookie (age-restricted posts, private accounts you follow)',
                          'cookie "sessionid" tài khoản TikTok của bạn (bài giới hạn tuổi, '
                          'tài khoản riêng tư bạn theo dõi)'))
    p.add_argument("--check-session", action="store_true",
                   help=t("only check whether --session-id is logged in, then exit",
                          "chỉ kiểm tra --session-id có đăng nhập được không rồi thoát"))
    p.add_argument("--proxy", help=t("proxy for TikTok page requests, e.g. http://127.0.0.1:8080",
                                     "proxy cho request trang TikTok, vd http://127.0.0.1:8080"))
    p.add_argument("--lang", choices=("en", "vi"), default=LANG,
                   help=t("message language: en = English, vi = Vietnamese (default: auto)",
                          "ngôn ngữ thông báo: en = tiếng Anh, vi = tiếng Việt (mặc định: tự nhận)"))
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    opts = p.parse_args()
    LANG = opts.lang
    opts.jobs = max(1, min(32, opts.jobs))
    opts.connections = max(1, min(16, opts.connections))
    opts.workers = max(1, min(16, opts.workers))
    opts.batch = max(1, opts.batch)
    if opts.check_session and not opts.session_id:
        p.error(t("--check-session needs --session-id <sessionid>",
                  "--check-session cần đi kèm --session-id <sessionid>"))
    if not opts.targets and not opts.check_session:
        p.error(t("enter at least one profile (username, @username or link)",
                  "cần nhập ít nhất một profile (username, @username hoặc link)"))

    if opts.session_id:
        try:
            me = TikTok(opts.session_id, 0, opts.proxy).whoami()
        except ScrapeError as e:
            log(t(f"Error: could not check the session ({e})", f"Lỗi: không kiểm tra được session ({e})"), err=True)
            sys.exit(1)
        if not me:
            log(t("✗ Session ID is invalid or expired (TikTok treats you as logged out).\n"
                  "  Copy a new sessionid cookie (see README, 'Get your session ID').",
                  "✗ Session ID không hợp lệ hoặc đã hết hạn (TikTok coi như chưa đăng nhập).\n"
                  "  Hãy lấy lại cookie sessionid mới (xem README, mục lấy session ID)."), err=True)
            sys.exit(1)
        nick = f" ({me['nickName']})" if me.get("nickName") else ""
        log(t(f"✓ Session OK: logged in as @{me['uniqueId']}{nick}",
              f"✓ Session hợp lệ: đang đăng nhập là @{me['uniqueId']}{nick}"))
        if opts.check_session:
            sys.exit(0)

    aria2 = find_tool("aria2c")
    ffmpeg = find_ffmpeg()
    if not opts.metadata_only:
        log(t("Downloader: ", "Bộ tải: ") + (aria2 or t("requests (aria2c not found, slower)",
                                                         "requests (không tìm thấy aria2c, sẽ chậm hơn)")))
        log("ffmpeg: " + (ffmpeg or t("not found (no metadata in videos, no slideshows for photo posts)",
                                      "không có (video sẽ không được gắn metadata, bài ảnh không dựng slideshow)")))

    failed = 0
    try:
        for target in opts.targets:
            try:
                failed += scrape_profile(target, opts, aria2, ffmpeg)
            except ScrapeError as e:
                failed += 1
                log(t(f"Error: {e}", f"Lỗi: {e}"), err=True)
    except KeyboardInterrupt:
        log(t("\nStopped. Run the same command again to continue.",
              "\nĐã dừng. Chạy lại đúng lệnh cũ để tải tiếp."), err=True)
        sys.exit(130)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
