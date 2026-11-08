import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import analyzer
import auth
import transcript
from db import get_conn, init_db

log = logging.getLogger("uvicorn.error")

# Error codes: invalid_url, no_captions, unavailable, too_long, generation_failed.


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="YouTube Notes Agent v2", lifespan=lifespan)
auth.install(app)  # login gate: only users listed in AUTH_USERS get in


class AnalyzeRequest(BaseModel):
    url: str


@app.exception_handler(transcript.AnalyzeError)
async def analyze_error_handler(_, exc: transcript.AnalyzeError):
    return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})


@app.get("/health")
def health():
    return {"status": "ok"}


def _upsert_processing(video_id: str, url: str, prep: dict) -> int:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO videos (video_id, url, title, channel, thumbnail_url, duration_sec, status)
            VALUES (?, ?, ?, ?, ?, ?, 'processing')
            ON CONFLICT(video_id) DO UPDATE SET
                url=excluded.url, title=excluded.title, channel=excluded.channel,
                thumbnail_url=excluded.thumbnail_url, duration_sec=excluded.duration_sec,
                status='processing', error_message=NULL, level=NULL, verdict=NULL, tldr=NULL,
                chapters=NULL, notes=NULL, quiz=NULL, updated_at=datetime('now')
            """,
            (video_id, url, prep["title"], prep["channel"], prep["thumbnail_url"], prep["duration_sec"]),
        )
        return conn.execute("SELECT id FROM videos WHERE video_id=?", (video_id,)).fetchone()["id"]


def _save(row_id: int, state: dict, status: str, error: str | None) -> None:
    notes = {"glossary": state["glossary"], "takeaways": state["takeaways"]}
    with get_conn() as conn:
        conn.execute(
            """UPDATE videos SET status=?, error_message=?, level=?, verdict=?, tldr=?,
               chapters=?, notes=?, quiz=?, updated_at=datetime('now') WHERE id=?""",
            (status, error, state["level"], state["verdict"], state["tldr"],
             json.dumps(state["chapters"]), json.dumps(notes), json.dumps(state["quiz"]), row_id),
        )


def _ndjson(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    video_id = transcript.parse_video_id(req.url)
    if not os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") == "your-key-here":
        raise HTTPException(500, detail={"code": "generation_failed", "message": "OPENAI_API_KEY is not set in .env."})
    prep = transcript.prepare(video_id)  # may raise AnalyzeError -> JSON error before streaming
    row_id = _upsert_processing(video_id, req.url.strip(), prep)

    def gen():
        state = analyzer.new_state()
        status, error = "error", "generation_failed"
        yield _ndjson({"type": "meta", "id": row_id, "video_id": video_id, "title": prep["title"],
                       "channel": prep["channel"], "thumbnail_url": prep["thumbnail_url"],
                       "duration_sec": prep["duration_sec"]})
        try:
            for attempt in (1, 2):
                try:
                    for obj in analyzer.stream_lines(prep["title"], prep["channel"], prep["transcript"], prep["duration_sec"]):
                        analyzer.apply_line(state, obj)
                        yield _ndjson(obj)
                except GeneratorExit:
                    raise
                except Exception:
                    log.exception("OpenAI stream failed (attempt %d)", attempt)
                    if state["chapters"] or attempt == 2:
                        raise  # partial output exists (or out of retries): keep it
                    continue
                if state["chapters"]:
                    break
                log.warning("empty model output (attempt %d)", attempt)
            if state["chapters"] and state["tldr"]:
                status, error = "done", None
            else:
                yield _ndjson({"type": "error", "code": "generation_failed",
                               "message": "The model returned an incomplete result."})
        except GeneratorExit:  # client went away: keep what we have
            raise
        except Exception as e:
            yield _ndjson({"type": "error", "code": "generation_failed", "message": f"Generation stopped: {type(e).__name__}"})
        finally:
            _save(row_id, state, status, error)
        if status == "done":
            yield _ndjson({"type": "done"})

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/videos")
def list_videos():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM videos ORDER BY updated_at DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/videos/{video_id}")
def get_video(video_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return dict(row)


@app.delete("/videos/{video_id}")
def delete_video(video_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    return {"deleted": video_id}


# Rows left 'processing' by a crash/restart can never finish; mark them.
@app.on_event("startup")
def _reap_stuck():
    with get_conn() as conn:
        conn.execute("UPDATE videos SET status='error', error_message='generation_failed' WHERE status IN ('processing','pending')")


# Serve the static frontend (mounted last so API routes win).
FRONTEND_DIR = Path(os.getenv("FRONTEND_DIR", "/app/frontend"))
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
