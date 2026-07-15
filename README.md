# Transcribe

YouTube URL in, clean transcript out. Pick the language in the UI and the app routes
to the right speech model, then runs a second OpenAI pass to rewrite the raw ASR output
into something readable.

| Language you pick | Transcription engine | Refinement |
| --- | --- | --- |
| English | OpenAI (`gpt-4o-transcribe`) | OpenAI (`gpt-4o`) |
| Hindi + English (Hinglish) | Sarvam Saaras v3 | OpenAI (`gpt-4o`) |

The UI shows the **raw transcript** first, and the **refined transcript** directly below it.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
brew install ffmpeg          # ffmpeg + ffprobe must be on PATH
```

## Run

```bash
./.venv/bin/uvicorn server:app --reload --port 8420
```

Open <http://127.0.0.1:8420>, click **API keys**, and paste your OpenAI key (and your
Sarvam key if you want Hinglish). Keys live in browser `localStorage` and are sent to
your own local backend — nothing else. Alternatively, copy `.env.example` to `.env` and
set `OPENAI_API_KEY` / `SARVAM_API_KEY` as server-side fallbacks.

An OpenAI key is always required, because refinement runs on OpenAI for both languages.

## How it works

`yt-dlp` pulls the audio stream only (no video, so long uploads stay fast), `ffmpeg`
normalizes it to 16 kHz mono — what both Whisper and Saaras are tuned for — and splits
it into chunks:

- **English:** 10-minute mp3 chunks, to stay under OpenAI's 25 MB upload cap.
- **Hinglish:** 90-minute wav chunks, under Sarvam's 2-hour-per-file limit. Sarvam's
  batch API takes at most 20 files per job, which caps a video at ~30 hours.

Refinement is chunked on sentence boundaries, and each section is given the tail of the
previous one so tone and paragraphing stay continuous across the seams. The Hinglish
prompt explicitly tells the model to preserve code-mixing and **not** translate.

Progress streams to the browser over Server-Sent Events, so you see each stage as it
happens rather than watching a spinner.

## Hinglish output script

Set under **Advanced options** — this is Sarvam's `mode`:

- `codemix` — मेरा phone number है (Devanagari + English words in Latin)
- `translit` — mera phone number hai (all Roman)
- `transcribe` — मेरा फोन नंबर है (all Devanagari)

## Keys

Bring your own. An OpenAI key is always required (refinement runs on OpenAI for both
languages); a Sarvam key is only needed for the Hinglish path. Never commit either —
`.env` is gitignored, and the UI keeps keys in browser `localStorage` only.
