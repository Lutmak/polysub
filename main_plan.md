# Trilingual Subtitle CLI — Execution Plan

> Ground truth document for development, modification, and feature additions.
> Last updated: 2026-03-02

---

## 1. What We Are Building

A local CLI tool that takes a video in any resolution, transcribes the speech using Whisper, lets the user review and correct the transcript, translates it to the other 2 languages, and burns trilingual subtitles directly into the video — with a per-word "current word" cyan highlight — all without degrading video quality.

**Target use case:** Short-form vertical video (Instagram Reels, 9:16) where the creator speaks one of three languages (Spanish, English, Russian) per video, and wants subtitles in all three visible simultaneously.

---

## 2. Design Decisions (Ground Truth)

These values come from research and visual iteration. They are the source of truth for all text rendering.

### 2.1 Safe Zone — Instagram Reels

- Canvas: **1080 × 1920 px** (9:16)
- Bottom unsafe zone: **320 px** from bottom (covered by IG UI: username, caption, like/comment/share buttons)
- Top unsafe zone: **108 px** from top
- Left margin: **60 px** | Right margin: **120 px** (right side has action buttons)
- **Subtitle block bottom edge must sit at or above y = 1600 px** (1920 − 320)
- Recommended subtitle vertical center: **~y = 1480–1560 px range** (just above the danger zone, enough breathing room)

For 16:9 input: subtitles render at full resolution and are positioned proportionally using the same percentage-based rules before any reframe.

### 2.2 Typography

| Property | Value | Notes |
|---|---|---|
| Font | **Montserrat** | Bold, clean, proven on mobile |
| Main language weight | **900 (Black)** | Maximum readability |
| Secondary language weight | **400 (Regular)** | Intentionally understated |
| Main font size | **~45–50 px** at 1080px width | ~4.2% of canvas width |
| Secondary font size | **~22–24 px** at 1080px width | ~2.1% of canvas width — roughly half of main |
| Letter spacing | Main: **0.3px** / Secondary: **0.6px** | |
| Text transform | Main: **UPPERCASE** | Secondaries: normal case |
| Line height | **1.15** | |

> **Note on font:** Montserrat must be embedded/loaded as a TTF at render time. Use the `fonts/` directory in the project. Download Montserrat-Black.ttf and Montserrat-Regular.ttf.

### 2.3 Colors

| Element | Color | Hex | Notes |
|---|---|---|---|
| Main language text | White | `#FFFFFF` | Full opacity |
| Secondary language text | Dim white | `rgba(255,255,255,0.35)` → FFmpeg: white with alpha ~89/255 | Faded, non-competing |
| **Current word highlight** | Cyan | `#00E5FF` | **Same color across ALL three languages** |
| Background text shadow | Black | `rgba(0,0,0,0.95)` | Critical — ensures readability on any scene |

The text shadow is non-negotiable. Without it, white text on bright scenes becomes unreadable. In FFmpeg drawtext this is implemented via `shadowcolor=black@0.95:shadowx=2:shadowy=2` plus a second wider shadow pass.

### 2.4 Spacing

| Property | Value |
|---|---|
| Gap between main line and secondary block | **3 px** |
| Gap between secondary line 1 and line 2 | **2 px** |
| Left/right text padding from safe zone edge | **60 px** from left, respect 120 px from right |
| Text alignment | **Center** |

### 2.5 Word Count Per Subtitle Segment

- **Target: 3–5 words per segment** (research-backed optimal for retention on mobile)
- Split at natural pauses first (Whisper silence gaps, punctuation)
- If no natural pause and segment > 5 words: split at conjunctions (y, and, но, but, que, that) or after 4 words
- Never split mid-clause if avoidable
- **Never exceed 7 words** in a single segment

### 2.6 Word Highlight Behavior

- Whisper returns **word-level timestamps** (`word_timestamps=True`)
- Each word has `start` and `end` float seconds
- At any given frame time `t`: the "current word" in the main language is the word where `word.start <= t < word.end`
- Secondary language translations do NOT have their own timestamps — they are **proportionally mapped** to the main language's word timing: `sec_word_idx = round((main_word_idx / (main_len - 1)) * (sec_len - 1))`
- All three highlighted words switch simultaneously, driven by the main language timeline

---

## 3. Project Structure

```
polysub/
├── pyproject.toml          # uv project config + dependencies
├── .env.example            # template for any secrets (e.g. DeepL key if used)
├── .env                    # local only, gitignored
├── .gitignore
├── README.md
├── fonts/
│   ├── Montserrat-Black.ttf
│   └── Montserrat-Regular.ttf
├── main.py                 # CLI entry point, orchestrates the full workflow
├── transcribe.py           # Whisper transcription + word segmentation logic
├── subtitles.py            # Translation + FFmpeg rendering + word highlight
└── PLAN.md                 # This document
```

**3 Python modules only.** No unnecessary abstraction.

---

## 4. Dependencies

Managed with **uv**. After cloning: `uv sync` then `uv run main.py <video>`.

```toml
[project]
name = "polysub"
version = "0.1.0"
requires-python = ">=3.11"

[project.dependencies]
openai-whisper = "*"       # local transcription + word timestamps
deep-translator = "*"      # free Google Translate wrapper, no API key needed
ffmpeg-python = "*"        # pythonic FFmpeg wrapper for video rendering
torch = "*"                # required by whisper
rich = "*"                 # pretty CLI output (tables, progress bars)
python-dotenv = "*"        # .env loading
```

> **FFmpeg binary** must be installed system-wide (`brew install ffmpeg` / `apt install ffmpeg`). ffmpeg-python is just a Python wrapper.
> **No paid API keys required** by default. deep-translator uses Google Translate free tier. If translation quality needs to improve, swap to DeepL (requires free API key in `.env`).

---

## 5. Full Workflow

### Step 1 — Input
```
uv run main.py <path/to/video.mp4> --lang es
```
- `--lang`: the language spoken in the video (`es`, `en`, `ru`)
- Other two languages are derived automatically
- Optional: `--model` for Whisper model size (default: `small`, recommended: `medium` for Russian)

### Step 2 — Transcription (`transcribe.py`)
1. Extract audio from video using FFmpeg (lossless WAV, no re-encode)
2. Run Whisper with `word_timestamps=True` on the extracted audio
3. Segment the raw transcript into chunks of **3–5 words** using the segmentation rules (§2.5)
4. Each segment = `{ text: str, words: [{ word, start, end }] }`

### Step 3 — Review & Correction (`main.py` CLI loop)
Display the full segmented transcript in the terminal:

```
Transcript review — 14 segments detected
─────────────────────────────────────────
▶ LA CONSISTENCIA  ▶ LO ES TODO  ▶ CADA DÍA  ▶ IMPORTA  ▶ NO TE  ▶ RINDAS NUNCA ...
─────────────────────────────────────────
Corrections needed? [y/N]:
```

- Each segment is separated by ` ▶ ` (triangle ASCII separator — clean, unambiguous, not a word character)
- All on as few lines as possible (wrap at terminal width), **not one segment per line**
- If user answers `y`: prompt `Enter: [original phrase] > [corrected phrase]` — can be run multiple times until user hits Enter on empty input
- Correction is a simple string replace on the matching segment text — no LLM, no fuzzy matching, exact match only
- User input is respected verbatim (case, punctuation, abbreviations)
- After corrections: redisplay updated transcript and confirm

### Step 4 — Translation (`subtitles.py`)
- For each segment, translate from main language to the other two
- Translations are stored alongside the original: `{ es: "...", en: "...", ru: "..." }`
- Translation happens segment-by-segment (short strings = better accuracy)
- Progress shown in terminal

### Step 5 — Subtitle Rendering (`subtitles.py`)
For each frame in the video, determine:
1. Which segment is active (segment whose words span current timestamp)
2. Which word index is "current" within that segment
3. Map current word index to secondary language word indices (proportional)

Render using **FFmpeg drawtext filter**:
- One filter pass per text element (main line, 2 secondary lines)
- Each word is rendered individually with its own `enable` time expression
- Highlighted word uses cyan (`#00E5FF`), all others use white (main) or dim white (secondary)
- Text shadow applied to all elements for readability

**Quality preservation:**
- Input video is **never re-encoded** for the video stream — use `-c:v copy` where possible
- Subtitles are burned in via FFmpeg's `ass` subtitle format (Advanced SubStation Alpha) — this is the highest-quality method for burned-in text
- ASS allows per-word color overrides, font embedding, and shadow — all in a single lossless video pass
- Audio stream: `-c:a copy` (never re-encode)
- Output container: same as input (mp4 → mp4)
- If input is already H.264, output stays H.264 with `-c:v copy` up to the subtitle burn step — the burn step requires one encode pass, done at the **highest CRF the input quality supports** (default CRF 18, configurable)

> **Why ASS over drawtext?** FFmpeg's drawtext filter requires one `-vf` filter per text element and gets unwieldy with per-word timing. ASS subtitles express the entire subtitle track in one file, support per-character color via `{\c&H...&}` tags, and burn in via a single `-vf ass=file.ass` — cleaner, faster, and more maintainable.

### Step 6 — Output
```
✓ Output saved: video_subtitled.mp4
  Duration: 0:00:47
  Resolution: 1080x1920
  Segments: 14
  Words highlighted: 67
```

---

## 6. ASS Subtitle Format — Key Specs

The `.ass` file generated will use these styles:

```
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
; Main language line
Style: Main,Montserrat,50,&H00FFFFFF,&H0000E5FF,&H00000000,&H99000000,0,0,0,0,100,100,0,0,1,2,1,2,1080,320,0

; Secondary language lines  
Style: Secondary,Montserrat,24,&H59FFFFFF,&H0000E5FF,&H00000000,&H99000000,0,0,0,0,100,100,0,0,1,1,0,2,1080,320,0
```

ASS color format: `&HAABBGGRR` (alpha, blue, green, red — reversed from HTML)
- White `#FFFFFF` → `&H00FFFFFF`
- Cyan `#00E5FF` → `&H00FFE500`  
- Dim white 35% opacity → `&H99FFFFFF` (0x99 = 153 = ~60% alpha in ASS convention)
- Shadow black → `&H99000000`

Per-word highlight in ASS is expressed inline:
```
{\c&H00FFE500&}CONSISTENCIA{\c&H00FFFFFF&}
```
The style switches color for just that word, then resets.

**Subtitle vertical position:**
- `MarginV: 320` — this is the bottom margin in ASS (pixels from bottom of PlayRes)
- This maps exactly to Instagram's 320px unsafe zone
- Text is bottom-anchored by default in ASS (alignment 2 = bottom center)

---

## 7. Segmentation Algorithm (Detail)

```python
def segment_words(words, max_words=5, min_words=2):
    """
    Split Whisper word list into subtitle segments of 3-5 words.
    Priority order for split points:
    1. Silence gap >= 0.4s between consecutive words
    2. Punctuation on word end (. , ! ? : ;)
    3. Conjunction words (and/y/но/but/que/that/и/а)
    4. Every max_words words as fallback
    """
```

Whisper sometimes merges punctuation into the preceding word token. Strip trailing punctuation before display, keep it internally for split-point detection.

---

## 8. CLI UX Summary

```bash
# Basic usage
uv run main.py video.mp4 --lang es

# With larger Whisper model (better for Russian/multilingual)
uv run main.py video.mp4 --lang ru --model medium

# Skip review (useful for re-runs)
uv run main.py video.mp4 --lang es --no-review

# Custom output path
uv run main.py video.mp4 --lang es -o output/final.mp4
```

Transcript review display uses `rich` for clean terminal rendering. Progress bars shown during transcription and translation. Errors are descriptive (e.g., "FFmpeg not found — install with: brew install ffmpeg").

---

## 9. What Is NOT In Scope

- No GUI
- No cloud processing (everything runs locally)
- No speaker diarization (single speaker assumed)
- No automatic language detection (user declares `--lang`)
- No subtitle style selection at runtime (this plan defines the one style)
- No video trimming, cropping, or reframing
- The visual mock-up elements from the design exploration (glass pill, left bars, dots divider, neon effects, phone frame, progress bar animation) — **these were exploration only and are not implemented**

---

## 10. Extension Points (Future, Not Now)

- `--style` flag to select from multiple subtitle designs
- DeepL backend for translation (better quality, needs free API key)
- Whisper large-v3 model support for improved accuracy
- Batch processing multiple videos
- Auto-detect spoken language (remove `--lang` requirement)
- SRT export option (for platforms that accept external subtitle files)