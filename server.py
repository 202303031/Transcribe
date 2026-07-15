"""FastAPI server: POST a YouTube URL, stream progress back over SSE."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import pipeline
from pipeline import Job, PipelineError

load_dotenv()

app = FastAPI(title="YouTube Transcriber")
STATIC = Path(__file__).parent / "static"

# job_id -> event queue. A job is created by POST /api/jobs and consumed by a
# single SSE listener; entries are dropped once the stream closes.
JOBS: dict[str, asyncio.Queue] = {}


class TranscribeRequest(BaseModel):
    url: str
    language: Literal["english", "hinglish"]
    openai_key: str = ""
    sarvam_key: str = ""
    sarvam_mode: Literal["codemix", "translit", "transcribe"] = "codemix"
    transcribe_model: str = "gpt-4o-transcribe"
    refine_model: str = "gpt-4o"
    diarize: bool = False
    num_speakers: int = Field(default=2, ge=1, le=10)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/jobs")
async def create_job(req: TranscribeRequest) -> dict[str, str]:
    openai_key = req.openai_key.strip() or os.getenv("OPENAI_API_KEY", "")
    sarvam_key = req.sarvam_key.strip() or os.getenv("SARVAM_API_KEY", "")

    if not openai_key:
        raise HTTPException(400, "An OpenAI API key is required (it powers the refinement pass).")
    if req.language == "hinglish" and not sarvam_key:
        raise HTTPException(400, "A Sarvam API key is required for Hinglish transcription.")

    job = Job(
        url=req.url.strip(),
        language=req.language,
        openai_key=openai_key,
        sarvam_key=sarvam_key,
        sarvam_mode=req.sarvam_mode,
        transcribe_model=req.transcribe_model,
        refine_model=req.refine_model,
        diarize=req.diarize,
        num_speakers=req.num_speakers,
    )

    job_id = uuid.uuid4().hex
    JOBS[job_id] = asyncio.Queue()
    asyncio.create_task(_run_job(job_id, job))
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}/events")
async def stream(job_id: str) -> StreamingResponse:
    queue = JOBS.get(job_id)
    if queue is None:
        raise HTTPException(404, "Unknown job")

    async def gen():
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event["type"] in ("done", "error"):
                    break
        finally:
            JOBS.pop(job_id, None)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _run_job(job_id: str, job: Job) -> None:
    queue = JOBS[job_id]
    loop = asyncio.get_running_loop()

    def emit(event: dict[str, Any]) -> None:
        queue.put_nowait(event)

    def stage(name: str, status: str, detail: str = "") -> None:
        emit({"type": "stage", "stage": name, "status": status, "detail": detail})

    # Called from worker threads, so it has to hop back onto the loop.
    def note(name: str):
        def _note(detail: str) -> None:
            loop.call_soon_threadsafe(stage, name, "running", detail)
        return _note

    workdir = Path(tempfile.mkdtemp(prefix="ytx-"))
    try:
        pipeline.preflight()

        stage("fetch", "running", "Locating video")
        title = await asyncio.to_thread(pipeline.fetch_title, job.url)
        emit({"type": "meta", "title": title})

        stage("fetch", "running", "Downloading audio track")
        raw = await asyncio.to_thread(pipeline.download_audio, job.url, workdir)
        audio = await asyncio.to_thread(pipeline.normalize, raw, workdir)
        secs = await asyncio.to_thread(pipeline.duration_seconds, audio)
        emit({"type": "meta", "title": title, "duration": secs})
        stage("fetch", "done", _hms(secs) if secs else "Audio ready")

        engine = "OpenAI Whisper" if job.language == "english" else "Sarvam Saaras v3"
        stage("transcribe", "running", f"Preparing audio for {engine}")

        if job.language == "english":
            chunks = await asyncio.to_thread(
                pipeline.segment, audio, workdir, pipeline.OPENAI_CHUNK_SECONDS, "mp3"
            )
            transcript = await asyncio.to_thread(
                pipeline.transcribe_openai, chunks, job, note("transcribe")
            )
        else:
            chunks = await asyncio.to_thread(
                pipeline.segment, audio, workdir, pipeline.SARVAM_CHUNK_SECONDS, "wav"
            )
            transcript = await asyncio.to_thread(
                pipeline.transcribe_sarvam, chunks, job, workdir, note("transcribe")
            )

        stage("transcribe", "done", f"{len(transcript.split()):,} words")
        emit({"type": "transcript", "text": transcript})

        stage("refine", "running", f"Cleaning up with {job.refine_model}")
        refined = await asyncio.to_thread(pipeline.refine, transcript, job, note("refine"))
        stage("refine", "done", f"{len(refined.split()):,} words")
        emit({"type": "refined", "text": refined})

        emit({"type": "done"})

    except PipelineError as exc:
        emit({"type": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - surface anything the UI can show
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _hms(seconds: float) -> str:
    total = int(seconds)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


app.mount("/", StaticFiles(directory=STATIC), name="static")
