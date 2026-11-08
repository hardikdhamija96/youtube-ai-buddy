"""One streaming OpenAI call -> NDJSON lines, parsed as they arrive."""
import json
import logging
import os
from typing import Iterator

from openai import OpenAI

log = logging.getLogger("analyzer")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM = """You are an expert note-taker creating study notes from a video transcript,
written for someone who wants to learn the content without watching the
full video. Output ONLY newline-delimited JSON (NDJSON) — one complete,
valid JSON object per line, in this exact order:

1. First, a glossary line:
{"type": "glossary", "value": [{"term": "...", "definition": "one-sentence definition", "chapter": "which chapter below explains this"}]}
Include only terms that are actually defined or central to understanding the video.
Max 8 terms.

2. For each natural section/topic in the video, in the order they occur, output these four lines (skip chapter_table and chapter_caveat if not applicable):
{"type": "chapter_title", "value": "short title for this section"}
{"type": "chapter_why", "value": "one sentence: why this section matters / what problem it addresses"}
{"type": "chapter_notes", "value": ["bullet point", "bullet point", ...]}
{"type": "chapter_table", "value": {"headers": ["...", "..."], "rows": [["...", "..."]]}}
   — ONLY include this line if the section compares multiple named options, approaches, or items. Otherwise omit it entirely.
{"type": "chapter_caveat", "value": "a specific warning, gotcha, or limitation the video explicitly states"}
   — ONLY include this if the transcript actually states a caveat. Never invent one. Omit if none.

3. After all sections, output:
{"type": "level", "value": "beginner | intermediate | advanced"}
{"type": "verdict", "value": "watch | skim | skip"}
{"type": "tldr", "value": "one paragraph summarizing the whole video"}
{"type": "takeaways", "value": ["the single most important point", "second most important point", "third, if there is one"]}
{"type": "quiz", "value": [{"question": "...", "options": ["...", "...", "...", "..."], "answer_index": 0}]}
   — 3 to 5 questions testing understanding of the actual content, not trivia.

Rules:
- Stay grounded in what the transcript actually says. Do not invent examples,
  analogies, or caveats that aren't present in the source material.
- Bullet points should explain concepts, not just restate timestamps or
  filler phrases ("the speaker then talks about...").
- Keep each bullet under 25 words. Keep chapter_why under 20 words.
- Output nothing except the NDJSON lines — no markdown, no commentary,
  no code fences.
- Sections must cover the ENTIRE transcript from the first to the last line, in order,
  roughly one section per 8-12 minutes of video (at least 4, at most 30). Never stop covering early.
- Output "level" and "verdict" values as a single word, lowercase."""


def stream_lines(title: str, channel: str | None, transcript: str, duration_sec: int = 0) -> Iterator[dict]:
    """Yield one parsed dict per complete NDJSON line. Raises on API failure."""
    client = OpenAI(timeout=180)
    user = (f"Title: {title}\nChannel: {channel or 'unknown'}\n"
            f"Video length: {duration_sec // 60} minutes\n\nTranscript:\n{transcript}")
    stream = client.chat.completions.create(
        model=MODEL,
        temperature=0.3,
        stream=True,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
    )
    buf, raw, n = "", "", 0
    for chunk in stream:
        if not chunk.choices:
            continue
        piece = chunk.choices[0].delta.content or ""
        buf += piece
        if len(raw) < 600:
            raw += piece
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            obj = _parse(line)
            if obj:
                n += 1
                yield obj
    obj = _parse(buf)
    if obj:
        n += 1
        yield obj
    if n == 0:
        log.warning("model produced no parseable NDJSON lines; raw start: %r", raw[:400])


def _parse(line: str) -> dict | None:
    line = line.strip().strip("`")
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and "type" in obj else None


def _str_list(v) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else []


def _last_chapter(state: dict) -> dict:
    if not state["chapters"]:
        state["chapters"].append({"title": "", "why": "", "notes": [], "table": None, "caveat": None})
    return state["chapters"][-1]


def apply_line(state: dict, obj: dict) -> None:
    """Fold one NDJSON line into the accumulating result state."""
    t, v = obj.get("type"), obj.get("value")
    if t == "glossary" and isinstance(v, list):
        state["glossary"] = [
            {"term": str(g.get("term", "")), "definition": str(g.get("definition", "")), "chapter": str(g.get("chapter", ""))}
            for g in v if isinstance(g, dict)
        ]
    elif t == "chapter_title":
        state["chapters"].append({"title": str(v or ""), "why": "", "notes": [], "table": None, "caveat": None})
    elif t == "chapter_why":
        _last_chapter(state)["why"] = str(v or "")
    elif t == "chapter_notes":
        _last_chapter(state)["notes"] = _str_list(v)
    elif t == "chapter_table" and isinstance(v, dict):
        _last_chapter(state)["table"] = {
            "headers": _str_list(v.get("headers")),
            "rows": [_str_list(r) for r in v.get("rows", []) if isinstance(r, list)],
        }
    elif t == "chapter_caveat":
        _last_chapter(state)["caveat"] = str(v or "")
    elif t == "level":
        state["level"] = str(v or "").strip().lower() or None
    elif t == "verdict":
        state["verdict"] = str(v or "").strip().lower() or None
    elif t == "tldr":
        state["tldr"] = str(v or "")
    elif t == "takeaways":
        state["takeaways"] = _str_list(v)
    elif t == "quiz" and isinstance(v, list):
        quiz = []
        for q in v:
            if not isinstance(q, dict):
                continue
            opts = _str_list(q.get("options"))
            try:
                ai = int(q.get("answer_index", 0))
            except (TypeError, ValueError):
                ai = 0
            quiz.append({"question": str(q.get("question", "")), "options": opts,
                         "answer_index": ai if 0 <= ai < len(opts) else 0})
        state["quiz"] = quiz


def new_state() -> dict:
    return {"glossary": [], "chapters": [], "level": None, "verdict": None,
            "tldr": None, "takeaways": [], "quiz": []}
