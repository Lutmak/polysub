"""
subtitles.py — Translation, ASS subtitle generation, and FFmpeg rendering.

Handles:
  - Batch translation via DeepL API with full-text context
  - ASS file generation with per-word cyan highlight across 3 languages
  - FFmpeg burn-in with 9:16 center crop for Instagram Reels
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import deepl
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn

from transcribe import Segment

console = Console()

# ── Language config ───────────────────────────────────────────────────────────

ALL_LANGS = ("es", "en", "ru")
LANG_NAMES = {"es": "Spanish", "en": "English", "ru": "Russian"}

# DeepL uses uppercase codes; EN needs a variant
DEEPL_SOURCE = {"es": "ES", "en": "EN", "ru": "RU"}
DEEPL_TARGET = {"es": "ES", "en": "EN-US", "ru": "RU"}

# Languages where DeepL supports formality control
DEEPL_FORMALITY = {"es": "less", "ru": "less"}

# ── Output resolution ────────────────────────────────────────────────────────

TARGET_W = 1080
TARGET_H = 1920

# ── ASS constants ────────────────────────────────────────────────────────────

# Colors in ASS &HAABBGGRR format
WHITE      = "&H00FFFFFF"
CYAN       = "&H00FFE500"   # #00E5FF in ASS BGR order
DIM_WHITE  = "&H59FFFFFF"   # ~35% opacity (0x59 ≈ 89 alpha → 65% transparent)
SHADOW_BLK = "&H99000000"

FONT_DIR_NAME = "fonts"

# ── Translation ──────────────────────────────────────────────────────────────


def translate(
    segments: list[Segment], source_lang: str
) -> list[dict[str, str]]:
    """
    Translate all segments to the other two languages using DeepL.

    Sends the full segment list as a single batch per target language,
    with the entire transcript passed as context (free, not billed).
    Returns list of dicts: [{es: "...", en: "...", ru: "..."}, ...]
    """
    load_dotenv()
    api_key = os.getenv("DEEPL_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DEEPL_API_KEY not found. Add it to your .env file."
        )

    translator = deepl.Translator(api_key)
    target_langs = [l for l in ALL_LANGS if l != source_lang]

    # Build texts list and full-text context
    texts = [seg.text for seg in segments]
    full_context = " ".join(texts)

    # Pre-fill with source text
    translations: list[dict[str, str]] = [
        {source_lang: t} for t in texts
    ]

    src_code = DEEPL_SOURCE[source_lang]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(
            "Translating segments", total=len(target_langs)
        )

        for tl in target_langs:
            tl_code = DEEPL_TARGET[tl]
            formality = DEEPL_FORMALITY.get(tl, "default")

            try:
                results = translator.translate_text(
                    texts,
                    source_lang=src_code,
                    target_lang=tl_code,
                    context=full_context,
                    formality=formality,
                )
                for i, r in enumerate(results):
                    translations[i][tl] = r.text
            except Exception as e:
                console.print(
                    f"[yellow]Warning:[/yellow] DeepL translation failed "
                    f"for {LANG_NAMES[tl]}: {e}"
                )
                # Fallback: keep source text
                for i in range(len(texts)):
                    translations[i][tl] = texts[i]

            progress.advance(task)

    console.print(
        f"[green]\u2713[/green] Translation complete — "
        f"{len(translations)} segments \u00d7 {len(target_langs)} languages\n"
    )
    return translations


# ── ASS generation ───────────────────────────────────────────────────────────


def _fmt_time(seconds: float) -> str:
    """Format seconds as ASS timestamp: H:MM:SS.cc (centiseconds)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _word_highlight_line(
    words: list[str],
    highlight_idx: int,
    base_color: str,
    highlight_color: str = CYAN,
) -> str:
    """
    Build ASS override string with one word highlighted in cyan.
    Other words use base_color.
    """
    parts = []
    for i, w in enumerate(words):
        if i == highlight_idx:
            parts.append(f"{{\\c{highlight_color}}}{w}{{\\c{base_color}}}")
        else:
            parts.append(w)
    return " ".join(parts)


def _even_timings(
    seg_start: float, seg_end: float, word_count: int
) -> list[tuple[float, float]]:
    """Distribute word_count words evenly across [seg_start, seg_end]."""
    if word_count <= 0:
        return []
    dur = (seg_end - seg_start) / word_count
    return [(seg_start + i * dur, seg_start + (i + 1) * dur) for i in range(word_count)]


def _active_idx(t: float, timings: list[tuple[float, float]]) -> int:
    """Find which word slot contains time t."""
    for i, (start, end) in enumerate(timings):
        if t < end:
            return i
    return len(timings) - 1


def generate_ass(
    segments: list[Segment],
    translations: list[dict[str, str]],
    source_lang: str,
    output_path: Path,
    font_dir: Path,
) -> Path:
    """
    Generate an ASS subtitle file with per-word highlight timing.

    Layout (bottom to top):
      - Line 3 (bottom): secondary language 2 — small, dim
      - Line 2: secondary language 1 — small, dim
      - Line 1 (top of subtitle block): main language — large, bold, uppercase
    """
    sec_langs = [l for l in ALL_LANGS if l != source_lang]
    # Language display order: main on top, then secondary 1 & 2 below
    lang_order = [source_lang] + sec_langs

    # Use a temp file so video players don't auto-load it as soft subs
    ass_path = output_path.with_suffix(".ass")

    lines: list[str] = []

    # ── Script Info
    lines.append("[Script Info]")
    lines.append("ScriptType: v4.00+")
    lines.append(f"PlayResX: {TARGET_W}")
    lines.append(f"PlayResY: {TARGET_H}")
    lines.append("WrapStyle: 0")
    lines.append("")

    # ── Styles
    # ASS Style format:
    # Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,
    # Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,
    # BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding

    lines.append("[V4+ Styles]")
    lines.append(
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    # Main: large bold, white, shadow, bottom-center (alignment 2)
    # MarginV=320 pushes above IG unsafe zone
    lines.append(
        f"Style: Main,Montserrat Black,60,{WHITE},{CYAN},{SHADOW_BLK},{SHADOW_BLK},"
        f"-1,0,0,0,100,100,0.3,0,1,2,1,2,60,120,320,1"
    )
    # Secondary: smaller regular, dim white
    lines.append(
        f"Style: Sec1,Montserrat,38,{DIM_WHITE},{CYAN},{SHADOW_BLK},{SHADOW_BLK},"
        f"-1,0,0,0,100,100,0.6,0,1,2,0,2,60,120,320,1"
    )
    lines.append(
        f"Style: Sec2,Montserrat,38,{DIM_WHITE},{CYAN},{SHADOW_BLK},{SHADOW_BLK},"
        f"-1,0,0,0,100,100,0.6,0,1,2,0,2,60,120,320,1"
    )
    lines.append("")

    # ── Fonts (embedded reference)
    lines.append("[Fonts]")
    lines.append("")

    # ── Events
    lines.append("[Events]")
    lines.append(
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    )

    for seg_idx, seg in enumerate(segments):
        trans = translations[seg_idx]
        main_words = [w.word.upper() for w in seg.words]
        sec1_words = trans[sec_langs[0]].split()
        sec2_words = trans[sec_langs[1]].split()

        # Build time slots for each language.
        # Main: real Whisper timestamps, bridged so each slot extends to the
        # next word's start (no gaps within a segment).
        main_timings: list[tuple[float, float]] = []
        for w_idx, wtok in enumerate(seg.words):
            w_end = seg.words[w_idx + 1].start if w_idx < len(seg.words) - 1 else seg.end
            main_timings.append((wtok.start, w_end))

        # Auxiliary: evenly distributed across the segment duration.
        seg_start = seg.words[0].start
        seg_end = seg.end
        sec1_timings = _even_timings(seg_start, seg_end, len(sec1_words))
        sec2_timings = _even_timings(seg_start, seg_end, len(sec2_words))

        # Merge all time boundaries so every word in every language gets
        # its own highlight window — no words are ever skipped.
        boundary_set: set[float] = set()
        for s, e in main_timings:
            boundary_set.add(s)
            boundary_set.add(e)
        for s, e in sec1_timings:
            boundary_set.add(s)
        for s, e in sec2_timings:
            boundary_set.add(s)
        boundary_set.add(seg_end)
        boundaries = sorted(boundary_set)

        # Emit one event per time slice
        for b_idx in range(len(boundaries) - 1):
            t_start = boundaries[b_idx]
            t_end = boundaries[b_idx + 1]
            if t_end - t_start < 0.01:
                continue  # skip negligible slices

            main_idx = _active_idx(t_start, main_timings)
            sec1_idx = _active_idx(t_start, sec1_timings)
            sec2_idx = _active_idx(t_start, sec2_timings)

            main_text = _word_highlight_line(main_words, main_idx, WHITE)
            sec1_text_hl = _word_highlight_line(sec1_words, sec1_idx, DIM_WHITE)
            sec2_text_hl = _word_highlight_line(sec2_words, sec2_idx, DIM_WHITE)

            ts = _fmt_time(t_start)
            te = _fmt_time(t_end)

            lines.append(
                f"Dialogue: 2,{ts},{te},Main,,60,120,405,,{main_text}"
            )
            lines.append(
                f"Dialogue: 1,{ts},{te},Sec1,,60,120,360,,{sec1_text_hl}"
            )
            lines.append(
                f"Dialogue: 0,{ts},{te},Sec2,,60,120,320,,{sec2_text_hl}"
            )

    lines.append("")

    ass_path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]\u2713[/green] ASS subtitle file written: {ass_path.name}")
    return ass_path


# ── Video probe ──────────────────────────────────────────────────────────────


def _probe_video(video_path: Path) -> dict:
    """Get video stream info via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed:\n{result.stderr}")
    return json.loads(result.stdout)


def _get_video_dimensions(probe: dict) -> tuple[int, int]:
    """Extract width and height from the first video stream."""
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return int(stream["width"]), int(stream["height"])
    raise RuntimeError("No video stream found in input file.")


# ── FFmpeg rendering ─────────────────────────────────────────────────────────


def _get_video_bitrate(probe: dict) -> int:
    """
    Estimate the input video bitrate from ffprobe data.

    Strategy:
      1. Use format.bit_rate minus audio stream bitrates
      2. Fallback: calculate from format.size / format.duration * 8 minus audio
      3. Floor at 500 kbps, default to 2 Mbps if all methods fail
    """
    audio_bps = 0
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "audio":
            br = stream.get("bit_rate")
            if br:
                audio_bps += int(br)

    # Method 1: format-level bit_rate
    fmt = probe.get("format", {})
    total_br = fmt.get("bit_rate")
    if total_br:
        video_bps = int(total_br) - audio_bps
        if video_bps >= 500_000:
            return video_bps

    # Method 2: calculate from size / duration
    size = fmt.get("size")
    duration = fmt.get("duration")
    if size and duration:
        total_calc = int(size) * 8 / float(duration)
        video_bps = int(total_calc) - audio_bps
        if video_bps >= 500_000:
            return video_bps

    # Default fallback
    return 2_000_000


def _get_duration(probe: dict) -> float:
    """Extract duration in seconds from ffprobe output."""
    # Try format-level duration first
    dur = probe.get("format", {}).get("duration")
    if dur:
        return float(dur)
    # Fallback to first video stream
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("duration"):
            return float(stream["duration"])
    return 0.0


def _escape_filter_path(p: Path) -> str:
    """Escape a path for use inside FFmpeg filter expressions."""
    s = str(p)
    # FFmpeg filter syntax needs : and \ escaped
    s = s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return s


def render(
    video_path: Path,
    output_path: Path,
    ass_path: Path,
    font_dir: Path,
) -> None:
    """
    Burn ASS subtitles into video with 9:16 center crop.

    Pipeline:
      1. Center-crop input to 9:16 aspect ratio
      2. Scale to 1080x1920 (IG Reels resolution)
      3. Burn ASS subtitles onto the cropped+scaled canvas
      4. Re-encode at near-lossless quality (CRF 17), copy audio
    """
    probe = _probe_video(video_path)
    in_w, in_h = _get_video_dimensions(probe)
    duration = _get_duration(probe)
    video_bitrate = _get_video_bitrate(probe)

    # Calculate 9:16 crop dimensions from input
    target_ratio = 9 / 16
    input_ratio = in_w / in_h

    if abs(input_ratio - target_ratio) < 0.01:
        # Already 9:16 — no crop needed
        crop_w, crop_h = in_w, in_h
        crop_x, crop_y = 0, 0
        need_crop = False
    elif input_ratio > target_ratio:
        # Input is wider than 9:16 — crop width
        crop_h = in_h
        crop_w = int(in_h * target_ratio)
        crop_w = crop_w - (crop_w % 2)
        crop_x = (in_w - crop_w) // 2
        crop_y = 0
        need_crop = True
    else:
        # Input is taller than 9:16 — crop height
        crop_w = in_w
        crop_h = int(in_w / target_ratio)
        crop_h = crop_h - (crop_h % 2)
        crop_x = 0
        crop_y = (in_h - crop_h) // 2
        need_crop = True

    # Build filter chain: crop (if needed) → scale → ASS burn
    ass_escaped = _escape_filter_path(ass_path)
    font_escaped = _escape_filter_path(font_dir)

    filter_parts = []
    if need_crop:
        filter_parts.append(f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}")
    # Scale to IG Reels target resolution
    if crop_w != TARGET_W or crop_h != TARGET_H:
        filter_parts.append(f"scale={TARGET_W}:{TARGET_H}:flags=lanczos")
    # Burn subtitles AFTER crop+scale so coordinates match PlayRes
    filter_parts.append(f"ass={ass_escaped}:fontsdir={font_escaped}")

    filters = ",".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", filters,
        "-c:v", "libx264",
        "-preset", "medium",
        "-b:v", str(video_bitrate),
        "-maxrate", str(int(video_bitrate * 1.5)),
        "-bufsize", str(video_bitrate * 2),
        "-c:a", "copy",
        "-sn",  # strip any subtitle streams from input
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        str(output_path),
    ]

    crop_label = f"crop {crop_w}x{crop_h} \u2192 " if need_crop else ""
    console.print(
        f"\n[dim]Rendering: {in_w}x{in_h} \u2192 "
        f"{crop_label}{TARGET_W}x{TARGET_H}[/dim]"
    )

    # Redirect stderr to a temp file to avoid pipe deadlock.
    # FFmpeg writes verbose logs to stderr; if we pipe it and don't read it
    # concurrently, the buffer fills and FFmpeg blocks — deadlocking with us.
    stderr_tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False, prefix="ffmpeg_"
    )

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=stderr_tmp, text=True
    )

    time_re = re.compile(r"out_time_us=(\d+)")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>5.1f}%"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "Encoding video",
            total=duration if duration > 0 else None,
        )

        for line in process.stdout:
            m = time_re.search(line)
            if m:
                current_us = int(m.group(1))
                current_s = current_us / 1_000_000
                if duration > 0:
                    progress.update(task, completed=min(current_s, duration))

        process.wait()

    stderr_tmp.close()
    stderr_path = Path(stderr_tmp.name)

    if process.returncode != 0:
        stderr_text = stderr_path.read_text(errors="replace")
        stderr_path.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg rendering failed:\n{stderr_text}")

    stderr_path.unlink(missing_ok=True)

    # Clean up the .ass file so video players don't auto-load it as soft subs
    ass_path.unlink(missing_ok=True)

    console.print(f"[green]\u2713[/green] Output saved: {output_path}")
