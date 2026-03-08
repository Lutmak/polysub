"""
transcribe.py — Audio extraction, Whisper transcription, and word segmentation.

Produces a list of Segments, each with:
  - text: display string (UPPERCASE for main language)
  - words: list of WordToken with word/start/end
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

console = Console()

# ── Language config ────────────────────────────────────────────────────────────

SUPPORTED_LANGS = ("es", "en", "ru")
WHISPER_LANG_MAP = {"es": "Spanish", "en": "English", "ru": "Russian"}

# Conjunctions that make good split points
SPLIT_WORDS: dict[str, set[str]] = {
    "es": {"y", "e", "pero", "sino", "porque", "que", "cuando", "si", "aunque"},
    "en": {"and", "but", "or", "so", "because", "when", "if", "that", "yet"},
    "ru": {"и", "а", "но", "или", "что", "когда", "если", "хотя", "потому"},
}

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class WordToken:
    word: str       # display word (punctuation stripped)
    raw: str        # original from Whisper (punctuation intact, for split detection)
    start: float
    end: float
    prob: float = 1.0  # Whisper confidence (0.0–1.0)


@dataclass
class Segment:
    words: list[WordToken] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Display text — joined words, uppercased."""
        return " ".join(w.word for w in self.words).upper()

    @property
    def start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def end(self) -> float:
        return self.words[-1].end if self.words else 0.0


# ── Audio extraction ──────────────────────────────────────────────────────────

def extract_audio(video_path: Path) -> Path:
    """Extract audio to a temp WAV file. 16kHz mono PCM — what Whisper expects."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    out = Path(tmp.name)

    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg audio extraction failed:\n{result.stderr}")
    return out


# ── Segmentation ──────────────────────────────────────────────────────────────

SILENCE_GAP = 0.5   # seconds — soft split within phrases
LONG_PAUSE  = 1.0   # seconds — hard boundary (phrase/sentence transition)
MAX_WORDS   = 5
MIN_WORDS   = 2

_PUNCT_STRIP = re.compile(r"^[^\w]+|[^\w]+$")
_PUNCT_END   = re.compile(r"[.!?,;:]+$")


def _strip(word: str) -> str:
    return _PUNCT_STRIP.sub("", word).strip()


def _has_end_punct(word: str) -> bool:
    return bool(_PUNCT_END.search(word))


def _is_conjunction(word: str, lang: str) -> bool:
    return _strip(word).lower() in SPLIT_WORDS.get(lang, set())


def _subsegment(tokens: list[WordToken], lang: str) -> list[Segment]:
    """
    Sub-segment a phrase chunk using soft heuristics (silence, punctuation,
    conjunctions, max/min word counts).
    """
    if not tokens:
        return []

    segments: list[Segment] = []
    current: list[WordToken] = []

    for i, tok in enumerate(tokens):
        current.append(tok)
        is_last = i == len(tokens) - 1

        if is_last:
            segments.append(Segment(words=current))
            break

        next_tok = tokens[i + 1]
        count = len(current)

        silence     = (next_tok.start - tok.end) >= SILENCE_GAP
        end_punct   = _has_end_punct(tok.raw)
        conjunction = _is_conjunction(next_tok.raw, lang) and count >= MIN_WORDS
        at_max      = count >= MAX_WORDS

        if (silence or end_punct or conjunction or at_max) and count >= MIN_WORDS:
            segments.append(Segment(words=current))
            current = []

    # Merge any dangling tail that's too short into the previous segment
    if segments and len(segments[-1].words) < MIN_WORDS and len(segments) > 1:
        prev_words = segments[-2].words + segments[-1].words
        segments[-2] = Segment(words=prev_words)
        segments.pop()

    return segments


def segment_words(whisper_words: list[dict], lang: str) -> list[Segment]:
    """
    Two-pass segmentation of Whisper's flat word list.

    Pass 1: Split on long pauses (>= LONG_PAUSE) into phrase chunks.
            These are hard boundaries where subtitles SHOULD disappear.
    Pass 2: Sub-segment each chunk using soft heuristics (silence, punctuation,
            conjunctions, word count limits).
    """
    if not whisper_words:
        return []

    tokens: list[WordToken] = []
    for w in whisper_words:
        raw = w["word"].strip()
        if not raw:
            continue
        tokens.append(WordToken(
            word=_strip(raw),
            raw=raw,
            start=float(w["start"]),
            end=float(w["end"]),
            prob=float(w.get("probability", 1.0)),
        ))

    if not tokens:
        return []

    # Pass 1: split into phrase chunks on long pauses
    chunks: list[list[WordToken]] = []
    current_chunk: list[WordToken] = []

    for i, tok in enumerate(tokens):
        current_chunk.append(tok)
        if i < len(tokens) - 1:
            gap = tokens[i + 1].start - tok.end
            if gap >= LONG_PAUSE:
                chunks.append(current_chunk)
                current_chunk = []

    if current_chunk:
        chunks.append(current_chunk)

    # Pass 2: sub-segment each chunk
    segments: list[Segment] = []
    for chunk in chunks:
        segments.extend(_subsegment(chunk, lang))

    return segments


# ── Transcription entry point ─────────────────────────────────────────────────

def transcribe(video_path: Path, lang: str, model_name: str = "turbo") -> list[Segment]:
    """
    Full pipeline: extract audio → Whisper with word timestamps → segment.
    Returns list of Segments ready for the review loop.
    """
    if lang not in SUPPORTED_LANGS:
        raise ValueError(f"Language '{lang}' not supported. Choose from: {SUPPORTED_LANGS}")

    console.print("[dim]Extracting audio...[/dim]")
    audio_path = extract_audio(video_path)

    console.print(f"[dim]Loading Whisper '{model_name}'...[/dim]")
    import whisper  # deferred import — slow to load, only needed at runtime
    model = whisper.load_model(model_name)

    console.print(f"[dim]Transcribing ({WHISPER_LANG_MAP[lang]})...[/dim]")
    result = model.transcribe(
        str(audio_path),
        language=lang,
        word_timestamps=True,
        verbose=False,
    )

    all_words: list[dict] = []
    for seg in result["segments"]:
        for w in seg.get("words", []):
            all_words.append(w)

    audio_path.unlink(missing_ok=True)

    segments = segment_words(all_words, lang)
    console.print(
        f"[green]✓[/green] Transcription complete — "
        f"[bold]{len(segments)}[/bold] segments, "
        f"[bold]{sum(len(s.words) for s in segments)}[/bold] words\n"
    )
    return segments
