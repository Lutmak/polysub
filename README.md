# Polysub

Trilingual subtitle burner for short-form video (Instagram Reels, TikTok, Shorts).

Transcribes speech with OpenAI Whisper, translates to three languages via DeepL, and burns per-word highlighted subtitles directly into the video — all from a single command.

## Features

- **Trilingual subtitles** — Spanish, English, and Russian displayed simultaneously
- **Per-word highlight** — karaoke-style cyan highlight synced to speech timing
- **Confidence-aware review** — low-confidence words flagged in color for easy correction
- **Full-screen inline editor** — fix transcription errors before rendering
- **Interactive mode** — guided prompts, no CLI flags needed
- **Batch processing** — process multiple videos in one run
- **9:16 auto-crop** — automatically crops and scales to 1080x1920 for Reels/Shorts

## Requirements

- Python 3.11+
- [FFmpeg](https://ffmpeg.org/) (with libass support)
- [DeepL API key](https://www.deepl.com/pro-api) (free tier works)
- [uv](https://docs.astral.sh/uv/) package manager

## Installation

```bash
git clone https://github.com/your-username/Polysub.git
cd Polysub
uv sync
```

Copy the environment template and add your DeepL API key:

```bash
cp .env.example .env
# Edit .env and paste your DEEPL_API_KEY
```

### Font setup

Place these font files in the `fonts/` directory:

- `Montserrat-Black.ttf` — main language (bold, large)
- `Montserrat-Regular.ttf` — secondary languages (smaller, dimmed)

Download from [Google Fonts](https://fonts.google.com/specimen/Montserrat).

## Quick start

Drop a video into `input-videos/` and run:

```bash
uv run main.py
```

The interactive mode will guide you through language selection, model choice, and transcript review. Output goes to `output-videos/`.

## CLI usage

For scripting or advanced usage:

```bash
uv run main.py path/to/video.mp4 --lang es [--model turbo] [--no-review] [-o output.mp4]
```

| Flag          | Description                                      | Default   |
|---------------|--------------------------------------------------|-----------|
| `--lang`      | Source language (`es`, `en`, `ru`)                | required  |
| `--model`     | Whisper model (`tiny`/`base`/`small`/`medium`/`large`/`turbo`) | `turbo` |
| `--no-review` | Skip the transcript review editor                | off       |
| `-o`          | Output file path                                 | `<input>_subtitled.mp4` |

## Folder structure

```
Polysub/
├── main.py              # Entry point (interactive + CLI)
├── transcribe.py        # Whisper transcription & segmentation
├── subtitles.py         # DeepL translation, ASS generation, FFmpeg rendering
├── fonts/               # Montserrat font files
├── input-videos/        # Drop source videos here
├── output-videos/       # Rendered results
├── transcripts/         # Exported transcript text files
├── pyproject.toml
└── uv.lock
```

## Pipeline overview

1. **Extract audio** — FFmpeg extracts 16kHz mono WAV
2. **Transcribe** — Whisper generates word-level timestamps with confidence scores
3. **Segment** — Words grouped into subtitle segments using pauses, punctuation, and conjunctions
4. **Review** — Interactive editor with color-coded confidence (red/yellow/green)
5. **Translate** — DeepL batch-translates segments to the other two languages
6. **Save transcripts** — Plain-text transcript files saved per language
7. **Generate ASS** — Per-word karaoke-style subtitle file with three language rows
8. **Render** — FFmpeg burns subtitles, crops to 9:16, outputs at 1080x1920
