"""Audio extraction + transcription + refinement.

Two transcription backends:
  english  -> OpenAI (gpt-4o-transcribe / whisper-1)
  hinglish -> Sarvam Saaras v3 batch API (code-mixed Hindi/English)

Both feed into a second OpenAI pass that cleans up the raw ASR output.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from openai import OpenAI
from sarvamai import SarvamAI

Progress = Callable[[str], None]

# Invoke yt-dlp through the interpreter that is running us, rather than as a
# bare `yt-dlp` binary. The console script lives in the venv's bin/, which is
# NOT on PATH unless the venv was activated — so `yt-dlp` is routinely absent
# even when the package is installed. `-m yt_dlp` always resolves.
YTDLP = [sys.executable, "-m", "yt_dlp"]

# OpenAI caps upload at 25 MB. 16 kHz mono mp3 @ 64 kbps is ~8 KB/s, so a
# 10-minute chunk lands near 5 MB — comfortably under, and small enough that
# progress ticks feel responsive on long videos.
OPENAI_CHUNK_SECONDS = 600

# Sarvam's batch API rejects files over 2h and takes at most 20 per job.
SARVAM_CHUNK_SECONDS = 5400
SARVAM_MAX_FILES = 20

# Refinement is chunked so a 3-hour transcript doesn't blow the output limit.
REFINE_CHUNK_CHARS = 6000


class PipelineError(RuntimeError):
    pass


@dataclass
class Job:
    url: str
    language: Literal["english", "hinglish"]
    openai_key: str
    sarvam_key: str | None = None
    sarvam_mode: str = "codemix"
    transcribe_model: str = "gpt-4o-transcribe"
    refine_model: str = "gpt-4o"
    diarize: bool = False
    num_speakers: int = 2


# ---------------------------------------------------------------- audio


def _run(cmd: list[str], label: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise PipelineError(f"{label} failed: {' '.join(tail[-3:]) or 'unknown error'}")


def fetch_title(url: str) -> str:
    proc = subprocess.run(
        [*YTDLP, "--no-warnings", "--print", "%(title)s", "--skip-download", url],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else url


def download_audio(url: str, workdir: Path) -> Path:
    """Pull the audio stream only — no video, so this stays fast even on long uploads."""
    _run([
        *YTDLP, "--no-warnings",
        "-f", "bestaudio",
        "-x", "--audio-format", "wav",
        "-o", str(workdir / "raw.%(ext)s"),
        url,
    ], "yt-dlp")
    raw = workdir / "raw.wav"
    if not raw.exists():
        raise PipelineError("yt-dlp produced no audio — is the URL public and playable?")
    return raw


def normalize(raw: Path, workdir: Path) -> Path:
    """16 kHz mono: what both Whisper and Saaras are tuned for."""
    out = workdir / "audio.wav"
    _run(["ffmpeg", "-y", "-i", str(raw), "-ac", "1", "-ar", "16000", str(out)], "ffmpeg")
    return out


def duration_seconds(audio: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def segment(audio: Path, workdir: Path, seconds: int, fmt: str) -> list[Path]:
    """Split into fixed-length chunks. mp3 for OpenAI (size cap), wav for Sarvam."""
    chunk_dir = workdir / "chunks"
    chunk_dir.mkdir(exist_ok=True)
    pattern = str(chunk_dir / f"chunk_%03d.{fmt}")

    cmd = ["ffmpeg", "-y", "-i", str(audio), "-f", "segment",
           "-segment_time", str(seconds)]
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", "64k"]
    else:
        cmd += ["-c", "copy"]
    cmd.append(pattern)
    _run(cmd, "ffmpeg")

    chunks = sorted(chunk_dir.glob(f"chunk_*.{fmt}"))
    if not chunks:
        raise PipelineError("ffmpeg produced no chunks")
    return chunks


# ------------------------------------------------------- transcription


def transcribe_openai(chunks: list[Path], job: Job, progress: Progress) -> str:
    client = OpenAI(api_key=job.openai_key)
    parts: list[str] = []

    for i, chunk in enumerate(chunks, 1):
        progress(f"Transcribing chunk {i} of {len(chunks)}")
        with chunk.open("rb") as fh:
            text = client.audio.transcriptions.create(
                model=job.transcribe_model,
                file=fh,
                language="en",
                response_format="text",
            )
        parts.append(str(text).strip())

    return "\n\n".join(p for p in parts if p)


def transcribe_sarvam(chunks: list[Path], job: Job, workdir: Path, progress: Progress) -> str:
    if not job.sarvam_key:
        raise PipelineError("Sarvam API key is required for Hinglish transcription")
    if len(chunks) > SARVAM_MAX_FILES:
        raise PipelineError(
            f"Video is too long: {len(chunks)} chunks exceeds Sarvam's {SARVAM_MAX_FILES}-file job limit"
        )

    client = SarvamAI(api_subscription_key=job.sarvam_key)

    progress("Creating Sarvam batch job")
    sj = client.speech_to_text_job.create_job(
        model="saaras:v3",
        mode=job.sarvam_mode,
        language_code="hi-IN",
        with_diarization=job.diarize,
        num_speakers=job.num_speakers if job.diarize else None,
    )

    progress(f"Uploading {len(chunks)} chunk(s) to Sarvam")
    sj.upload_files(file_paths=[str(c) for c in chunks])
    sj.start()

    progress("Sarvam is processing — this runs server-side and can take a few minutes")
    sj.wait_until_complete()

    results = sj.get_file_results()
    failed = results.get("failed", [])
    if failed and not results.get("successful"):
        reason = failed[0].get("error_message", "unknown error")
        raise PipelineError(f"Sarvam failed every chunk: {reason}")
    if failed:
        progress(f"Warning: {len(failed)} chunk(s) failed and will be missing from the transcript")

    out_dir = workdir / "sarvam_out"
    sj.download_outputs(output_dir=str(out_dir))

    parts: list[str] = []
    for path in sorted(out_dir.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        text = (data.get("transcript") or "").strip()
        if text:
            parts.append(text)

    if not parts:
        raise PipelineError("Sarvam returned no transcript text")
    return "\n\n".join(parts)


# --------------------------------------------------------- refinement

_ENGLISH_PROMPT = """You are an expert transcript editor. You will be given a raw \
speech-to-text transcript, which may contain ASR errors, missing punctuation, \
run-on text, and filler words.

Rewrite it into clean, readable prose:
- Fix punctuation, capitalisation, and obvious mis-hearings.
- Remove filler words (um, uh, you know, like) and false starts.
- Break the text into logical paragraphs.
- Preserve every substantive point, name, number, and claim. Do not summarise, \
do not add commentary, do not invent content.

Return only the cleaned transcript."""

_HINGLISH_PROMPT = """You are an expert transcript editor for Hinglish \
(code-mixed Hindi and English) speech. You will be given a raw speech-to-text \
transcript that may contain ASR errors, missing punctuation, and filler words.

Rewrite it into a clean, readable transcript:
- KEEP the code-mixed Hinglish exactly as spoken. Do NOT translate Hindi into \
English or English into Hindi. Preserve the original script of each word \
(Devanagari stays Devanagari, Roman stays Roman).
- Fix punctuation, obvious mis-hearings, and broken word boundaries.
- Remove filler words (matlab, yaani, umm, you know) and false starts.
- Break the text into logical paragraphs.
- Preserve every substantive point, name, number, and claim. Do not summarise, \
do not add commentary, do not invent content.

Return only the cleaned transcript."""


def _split_for_refine(text: str, limit: int = REFINE_CHUNK_CHARS) -> list[str]:
    """Chunk on sentence boundaries so the model never sees a severed clause."""
    if len(text) <= limit:
        return [text]

    sentences = re.split(r"(?<=[.!?।])\s+", text)
    chunks: list[str] = []
    buf = ""
    for sentence in sentences:
        if buf and len(buf) + len(sentence) + 1 > limit:
            chunks.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        chunks.append(buf)
    return chunks


def refine(text: str, job: Job, progress: Progress) -> str:
    client = OpenAI(api_key=job.openai_key)
    system = _ENGLISH_PROMPT if job.language == "english" else _HINGLISH_PROMPT
    chunks = _split_for_refine(text)
    refined: list[str] = []

    for i, chunk in enumerate(chunks, 1):
        progress(f"Refining section {i} of {len(chunks)}")
        messages = [{"role": "system", "content": system}]
        if refined:
            # Tail of the previous output keeps tone and paragraphing continuous
            # across chunk seams without re-sending the whole transcript.
            messages.append({
                "role": "system",
                "content": f"For continuity, here is the end of the previous section:\n\n...{refined[-1][-500:]}",
            })
        messages.append({"role": "user", "content": chunk})

        resp = client.chat.completions.create(
            model=job.refine_model,
            messages=messages,
            temperature=0.2,
        )
        refined.append((resp.choices[0].message.content or "").strip())

    return "\n\n".join(p for p in refined if p)


# ------------------------------------------------------------ preflight


def preflight() -> None:
    missing: list[str] = []

    if importlib.util.find_spec("yt_dlp") is None:
        missing.append("yt-dlp (pip install -r requirements.txt)")

    # ffmpeg/ffprobe are genuinely system binaries — these must be on PATH.
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            missing.append(f"{binary} (brew install ffmpeg)")

    if missing:
        raise PipelineError("Missing dependencies — " + "; ".join(missing))
