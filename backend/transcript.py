"""Transcript + metadata fetching.

Transcript: youtube-transcript-api first, yt-dlp captions as fallback.
Metadata: YouTube oEmbed (no API key). Duration comes from the last transcript timestamp.
"""
import json
import re

import httpx
from youtube_transcript_api import (
    AgeRestricted,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeTranscriptApi,
)

YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/)|youtu\.be/)([\w-]{11})"
)
# No duration cap (personal use). Only a safety ceiling for the model context window
# (gpt-4o-mini: 128k tokens) so the OpenAI call cannot fail on oversized input.
MAX_INPUT_TOKENS = 100_000


class AnalyzeError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def parse_video_id(url: str) -> str:
    m = YT_ID_RE.search(url.strip())
    if not m:
        raise AnalyzeError("invalid_url", "That doesn't look like a YouTube link.")
    return m.group(1)


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fetch_metadata(video_id: str) -> dict:
    """oEmbed: title/channel/thumbnail. Failure is non-fatal; transcript fetch decides availability."""
    meta = {"title": f"YouTube video {video_id}", "channel": None,
            "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"}
    try:
        r = httpx.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            meta["title"] = d.get("title") or meta["title"]
            meta["channel"] = d.get("author_name")
            meta["thumbnail_url"] = d.get("thumbnail_url") or meta["thumbnail_url"]
    except (httpx.HTTPError, ValueError):
        pass
    return meta


def _primary(video_id: str) -> list[dict]:
    api = YouTubeTranscriptApi()
    tl = api.list(video_id)
    available = list(tl)
    if not available:
        raise NoTranscriptFound(video_id, [], tl)
    # Prefer English (manual, then generated), otherwise whatever exists.
    chosen = next((t for t in available if t.language_code.startswith("en") and not t.is_generated), None) \
        or next((t for t in available if t.language_code.startswith("en")), None) \
        or next((t for t in available if not t.is_generated), None) \
        or available[0]
    return [{"start": s.start, "text": s.text} for s in chosen.fetch()]


def _fallback_ytdlp(video_id: str) -> list[dict]:
    import yt_dlp

    opts = {"skip_download": True, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    tracks = {**(info.get("automatic_captions") or {}), **(info.get("subtitles") or {})}
    if not tracks:
        raise AnalyzeError("no_captions", "This video has no captions, so I can't read it.")
    lang = next((k for k in tracks if k.startswith("en")), next(iter(tracks)))
    fmt = next((f for f in tracks[lang] if f.get("ext") == "json3"), None)
    if not fmt:
        raise AnalyzeError("no_captions", "This video has no captions, so I can't read it.")
    data = httpx.get(fmt["url"], timeout=20).json()
    out = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs", []) or []).replace("\n", " ").strip()
        if text:
            out.append({"start": ev.get("tStartMs", 0) / 1000, "text": text})
    return out


def fetch_transcript(video_id: str) -> list[dict]:
    try:
        return _primary(video_id)
    except (VideoUnavailable, VideoUnplayable, AgeRestricted):
        raise AnalyzeError("unavailable", "Can't open this video — it may be private, removed or region-blocked.")
    except (TranscriptsDisabled, NoTranscriptFound, Exception) as primary_err:
        # Includes IP blocks / parse failures: try yt-dlp before giving up.
        try:
            segs = _fallback_ytdlp(video_id)
        except AnalyzeError:
            raise
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("private", "unavailable", "removed", "not available", "blocked")):
                raise AnalyzeError("unavailable", "Can't open this video — it may be private, removed or region-blocked.")
            raise AnalyzeError("no_captions", "This video has no captions, so I can't read it.")
        if not segs:
            raise AnalyzeError("no_captions", "This video has no captions, so I can't read it.")
        return segs


def build_prompt_text(segs: list[dict]) -> tuple[str, int]:
    """Group snippets into ~30s lines prefixed with [m:ss]. Returns (text, duration_sec)."""
    lines, bucket_start, buf = [], None, []
    for s in segs:
        if bucket_start is None:
            bucket_start = s["start"]
        buf.append(s["text"].strip())
        if s["start"] - bucket_start >= 30:
            lines.append(f"[{fmt_ts(bucket_start)}] {' '.join(buf)}")
            bucket_start, buf = None, []
    if buf:
        lines.append(f"[{fmt_ts(bucket_start or 0)}] {' '.join(buf)}")
    duration = int(segs[-1]["start"]) if segs else 0
    return "\n".join(lines), duration


def prepare(video_id: str) -> dict:
    """Everything needed before the OpenAI call. Raises AnalyzeError."""
    meta = fetch_metadata(video_id)
    segs = fetch_transcript(video_id)
    text, duration = build_prompt_text(segs)
    if len(text) / 4 > MAX_INPUT_TOKENS:
        raise AnalyzeError("too_long", "This transcript is too long for the model to read in one go.")
    return {**meta, "duration_sec": duration, "transcript": text}
