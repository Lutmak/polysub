"""
main.py — Entry point for Polysub.

Workflow: transcribe → review/correct (inline editor) → translate → render.

Two modes:
  Interactive: uv run main.py          (no args — guided prompts)
  CLI:         uv run main.py <video> --lang es [--model small] [--no-review] [-o output.mp4]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv
from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.lexers import Lexer
from rich.console import Console
from rich.panel import Panel

from transcribe import Segment, WordToken, transcribe, SUPPORTED_LANGS
from subtitles import translate, generate_ass, render, ALL_LANGS

load_dotenv()
console = Console()

TERM_WIDTH = min(shutil.get_terminal_size().columns, 120)
SEP_RICH  = " [cyan]▶[/cyan] "
SEP_PLAIN = " ▶ "

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
PROJECT_ROOT = Path(__file__).resolve().parent

# Confidence thresholds
CONF_LOW      = 0.5   # below → yellow (review suggested)
CONF_VERY_LOW = 0.3   # below → red (likely wrong)
CONF_CORRECTED = 2.0  # sentinel: word was edited by user → green


# ── Confidence lexer for prompt_toolkit ──────────────────────────────────────

# Populated before each editor session; the lexer reads from here.
_editor_probs: list[list[float]] = []


class ConfidenceLexer(Lexer):
    """Color words in the editor based on their Whisper confidence score."""

    def lex_document(self, document):
        def get_line(lineno):
            line = document.lines[lineno]
            words = line.split(" ")
            probs = _editor_probs[lineno] if lineno < len(_editor_probs) else []
            result = []
            for i, word in enumerate(words):
                if result:
                    result.append(("", " "))
                if not word:
                    continue
                prob = probs[i] if i < len(probs) else 1.0
                if prob >= CONF_CORRECTED:
                    result.append(("bold fg:green", word))
                elif prob < CONF_VERY_LOW:
                    result.append(("bold fg:red", word))
                elif prob < CONF_LOW:
                    result.append(("bold fg:yellow", word))
                else:
                    result.append(("fg:white", word))
            return result
        return get_line


# ── Transcript display ───────────────────────────────────────────────────────

def _rich_word(word: str, prob: float) -> str:
    """Wrap a word in Rich markup based on confidence."""
    if prob >= CONF_CORRECTED:
        return f"[bold green]{word}[/bold green]"
    if prob < CONF_VERY_LOW:
        return f"[bold red]{word}[/bold red]"
    if prob < CONF_LOW:
        return f"[yellow]{word}[/yellow]"
    return f"[white]{word}[/white]"


def display_transcript(segments: list[Segment]) -> None:
    """
    Print all segments on as few lines as possible, separated by ▶.
    Low-confidence words are colored yellow/red.
    """
    console.print("\n[bold]Transcript[/bold]  [dim](segments separated by ▶)[/dim]")
    console.rule(style="dim")

    line_parts: list[str] = []
    line_len = 0
    flagged_count = 0

    for i, seg in enumerate(segments):
        word_parts: list[str] = []
        for wtok in seg.words:
            w_upper = wtok.word.upper()
            if wtok.prob < CONF_LOW:
                flagged_count += 1
            word_parts.append(_rich_word(w_upper, wtok.prob))
        label_rich = " ".join(word_parts)
        label_plain = seg.text

        chunk_plain = ("" if i == 0 else SEP_PLAIN) + label_plain
        chunk_rich  = ("" if i == 0 else SEP_RICH) + label_rich

        if line_len + len(chunk_plain) > TERM_WIDTH and line_parts:
            console.print("".join(line_parts))
            line_parts = [label_rich]
            line_len = len(label_plain)
        else:
            line_parts.append(chunk_rich)
            line_len += len(chunk_plain)

    if line_parts:
        console.print("".join(line_parts))

    console.rule(style="dim")
    flag_info = f" · {flagged_count} flagged" if flagged_count else ""
    console.print(
        f"[dim]{len(segments)} segments · "
        f"{sum(len(s.words) for s in segments)} words{flag_info}[/dim]"
    )
    corrected = sum(1 for s in segments for w in s.words if w.prob >= CONF_CORRECTED)
    if flagged_count or corrected:
        legend = "[dim]  "
        if corrected:
            legend += f"[bold green]green[/bold green] = corrected  "
        if flagged_count:
            legend += (
                f"[yellow]yellow[/yellow] = low confidence  "
                f"[bold red]red[/bold red] = likely wrong"
            )
        legend += "[/dim]"
        console.print(legend)
    console.print()


# ── Inline editor ────────────────────────────────────────────────────────────

def _open_editor(segments: list[Segment], mode: str) -> str | None:
    """
    Open the prompt_toolkit full-screen editor.

    mode: "full" — all segments as continuous text (one per line, no separators)
          "segments" — same but visually identical (one per line either way)

    Returns the edited text, or None if cancelled.
    """
    global _editor_probs
    _editor_probs = [
        [wtok.prob for wtok in seg.words] for seg in segments
    ]

    text = "\n".join(seg.text for seg in segments)

    kb = KeyBindings()

    @kb.add("c-s")
    def save(event):
        event.app.exit(result=event.app.current_buffer.text)

    @kb.add("c-q")
    def cancel(event):
        event.app.exit(result=None)

    header = FormattedTextControl([
        ("bold fg:cyan", " TRANSCRIPT EDITOR "),
        ("fg:gray", " │ "),
        ("fg:yellow", "yellow"),
        ("fg:gray", "=review  "),
        ("bold fg:red", "red"),
        ("fg:gray", "=likely wrong  │  "),
        ("bold fg:green", "Ctrl+S"),
        ("fg:gray", " save  "),
        ("bold fg:red", "Ctrl+Q"),
        ("fg:gray", " cancel"),
    ])

    footer = FormattedTextControl([
        ("fg:gray", " One segment per line. Edit words directly. All text is uppercased on save."),
    ])

    buf = Buffer(document=Document(text, 0), multiline=True)

    layout = Layout(
        HSplit([
            Window(header, height=1, style="bg:#1a1a2e"),
            Window(BufferControl(buf, lexer=ConfidenceLexer()), wrap_lines=True),
            Window(footer, height=1, style="bg:#1a1a2e"),
        ])
    )

    app = Application(layout=layout, key_bindings=kb, full_screen=True)
    return app.run()


def apply_edits(segments: list[Segment], edited_lines: list[str]) -> list[Segment]:
    """
    Apply edited lines back to segments.

    - All text is uppercased.
    - Deleted lines → segments removed.
    - Same word count → keep original timestamps, update text.
    - Different word count → redistribute timestamps evenly.
    """
    # Truncate if lines were deleted
    if len(edited_lines) < len(segments):
        removed = len(segments) - len(edited_lines)
        segments = segments[:len(edited_lines)]
        console.print(f"[dim]{removed} segment(s) removed.[/dim]")

    for i, seg in enumerate(segments):
        if i >= len(edited_lines):
            break

        edited = edited_lines[i].strip().upper()
        if edited == seg.text:
            continue

        new_words = edited.split()
        if not new_words:
            continue

        if len(new_words) == len(seg.words):
            # Same word count — keep original timestamps, mark changed words green
            for j, tok in enumerate(seg.words):
                if tok.word != new_words[j]:
                    tok.prob = CONF_CORRECTED
                tok.word = new_words[j]
                tok.raw = new_words[j]
        else:
            # Word count changed — redistribute timestamps, mark all green
            seg_start = seg.start
            seg_end = seg.end
            duration = seg_end - seg_start
            n = len(new_words)
            seg.words = [
                WordToken(
                    word=w,
                    raw=w,
                    start=seg_start + (k / n) * duration,
                    end=seg_start + ((k + 1) / n) * duration,
                    prob=CONF_CORRECTED,
                )
                for k, w in enumerate(new_words)
            ]

    return segments


def review_transcript(segments: list[Segment]) -> list[Segment]:
    """
    Interactive review loop. Shows transcript, optionally opens inline editor.
    Loops until user is satisfied.
    """
    while True:
        display_transcript(segments)

        answer = console.input(
            "[bold]Modifications needed?[/bold] [dim]\\[y/N][/dim]: "
        ).strip().lower()

        if answer not in ("y", "yes"):
            break

        # Ask for view mode
        mode = console.input(
            "[bold]View mode:[/bold] [dim]\\[f]ull text / \\[s]egments (one per line)[/dim]: "
        ).strip().lower()

        if mode in ("s", "segments"):
            view = "segments"
        else:
            view = "full"

        result = _open_editor(segments, view)

        if result is None:
            console.print("[yellow]Cancelled.[/yellow] Transcript unchanged.\n")
            continue

        edited_lines = [l.strip() for l in result.split("\n") if l.strip()]
        segments = apply_edits(segments, edited_lines)
        console.print("[green]✓[/green] Transcript updated.\n")

    return segments


# ── Transcript export ────────────────────────────────────────────────────────

def save_transcripts(
    video_stem: str,
    segments: list[Segment],
    translations: list[dict[str, str]],
    source_lang: str,
) -> None:
    """Save plain-text transcript files for all three languages."""
    transcript_dir = PROJECT_ROOT / "transcripts"
    transcript_dir.mkdir(exist_ok=True)

    for lang in ALL_LANGS:
        path = transcript_dir / f"{video_stem}_{lang}.txt"
        lines = []
        for i, seg in enumerate(segments):
            if lang == source_lang:
                lines.append(seg.text)
            else:
                lines.append(translations[i].get(lang, seg.text))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    console.print(
        f"[green]✓[/green] Transcripts saved to transcripts/ "
        f"({', '.join(f'{video_stem}_{l}.txt' for l in ALL_LANGS)})"
    )


# ── Pipeline ─────────────────────────────────────────────────────────────────

def process_video(
    video: Path,
    lang: str,
    model: str = "turbo",
    no_review: bool = False,
    output: Path | None = None,
) -> None:
    """Run the full pipeline: transcribe → review → translate → render."""
    video = video.resolve()
    if not video.exists():
        console.print(f"[red]Error:[/red] File not found: {video}")
        sys.exit(1)

    if output is None:
        output = video.with_name(video.stem + "_subtitled.mp4")

    console.print(Panel(
        f"[bold cyan]Polysub[/bold cyan]\n"
        f"[dim]Input :[/dim]  {video.name}\n"
        f"[dim]Lang  :[/dim]  {lang.upper()}\n"
        f"[dim]Model :[/dim]  {model}\n"
        f"[dim]Output:[/dim]  {output.name}",
        expand=False,
    ))

    # Step 1: Transcribe
    segments = transcribe(video, lang=lang, model_name=model)

    # Step 2: Review
    if not no_review:
        segments = review_transcript(segments)

    console.print(f"[green]✓[/green] Transcript ready — {len(segments)} segments\n")

    # Step 3: Translate
    translations = translate(segments, source_lang=lang)

    # Step 4: Save transcripts
    save_transcripts(video.stem, segments, translations, lang)

    # Step 5: Generate ASS subtitles
    font_dir = PROJECT_ROOT / "fonts"
    if not (font_dir / "Montserrat-Black.ttf").exists():
        console.print(
            f"[red]Error:[/red] Font not found: {font_dir / 'Montserrat-Black.ttf'}\n"
            "[dim]Place Montserrat-Black.ttf and Montserrat-Regular.ttf in the fonts/ directory.[/dim]"
        )
        sys.exit(1)

    ass_path = generate_ass(segments, translations, lang, output, font_dir)

    # Step 6: Render video
    render(video, output, ass_path, font_dir)

    # Summary
    total_words = sum(len(s.words) for s in segments)
    console.print(Panel(
        f"[bold green]Done![/bold green]\n"
        f"[dim]Output    :[/dim]  {output.name}\n"
        f"[dim]Resolution:[/dim]  1080x1920\n"
        f"[dim]Segments  :[/dim]  {len(segments)}\n"
        f"[dim]Words     :[/dim]  {total_words}",
        expand=False,
    ))


# ── Interactive mode ─────────────────────────────────────────────────────────

def interactive_mode() -> None:
    """Guided interactive mode — runs when no CLI arguments are provided."""
    console.print(Panel(
        "[bold cyan]Polysub[/bold cyan] — Interactive Mode\n"
        "[dim]Trilingual subtitle burner for short-form video[/dim]",
        expand=False,
    ))

    # 1. Scan input-videos/
    input_dir = PROJECT_ROOT / "input-videos"
    input_dir.mkdir(exist_ok=True)
    videos = sorted(
        f for f in input_dir.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    )

    if not videos:
        console.print(
            "\n[red]No video files found in input-videos/[/red]\n"
            "[dim]Drop .mp4, .mov, .avi, .mkv, or .webm files into "
            "the input-videos/ folder and try again.[/dim]"
        )
        sys.exit(1)

    # 2. Present numbered list
    console.print("\n[bold]Available videos:[/bold]")
    for i, v in enumerate(videos, 1):
        console.print(f"  [cyan]{i}[/cyan]  {v.name}")
    console.print(f"  [cyan]A[/cyan]  All (batch)")

    # 3. User picks video(s)
    choice = console.input("\n[bold]Select video(s):[/bold] ").strip().lower()

    if choice in ("a", "all"):
        selected = videos
    else:
        try:
            indices = [int(x.strip()) for x in choice.replace(",", " ").split()]
            selected = [videos[i - 1] for i in indices if 1 <= i <= len(videos)]
        except (ValueError, IndexError):
            console.print("[red]Invalid selection.[/red]")
            sys.exit(1)

    if not selected:
        console.print("[red]No videos selected.[/red]")
        sys.exit(1)

    batch = len(selected) > 1

    # 4. Prompt for source language
    lang_choices = "/".join(SUPPORTED_LANGS)

    if batch:
        # Ask if same language for all
        same_lang_answer = console.input(
            f"\n[bold]Same language for all videos?[/bold] [dim]\\[Y/n][/dim]: "
        ).strip().lower()

        if same_lang_answer in ("n", "no"):
            # Per-video language
            langs: list[str] = []
            for v in selected:
                lang = console.input(
                    f"  [bold]Language for {v.name}[/bold] ({lang_choices}): "
                ).strip().lower()
                if lang not in SUPPORTED_LANGS:
                    console.print(f"[red]Invalid language: {lang}[/red]")
                    sys.exit(1)
                langs.append(lang)
        else:
            lang = console.input(
                f"\n[bold]Source language[/bold] ({lang_choices}): "
            ).strip().lower()
            if lang not in SUPPORTED_LANGS:
                console.print(f"[red]Invalid language: {lang}[/red]")
                sys.exit(1)
            langs = [lang] * len(selected)
    else:
        lang = console.input(
            f"\n[bold]Source language[/bold] ({lang_choices}): "
        ).strip().lower()
        if lang not in SUPPORTED_LANGS:
            console.print(f"[red]Invalid language: {lang}[/red]")
            sys.exit(1)
        langs = [lang]

    # 5. Whisper model
    model = console.input(
        "\n[bold]Whisper model[/bold] [dim](tiny/base/small/medium/large/turbo)[/dim] "
        "[dim]\\[turbo][/dim]: "
    ).strip().lower() or "turbo"

    valid_models = {"tiny", "base", "small", "medium", "large", "turbo"}
    if model not in valid_models:
        console.print(f"[red]Invalid model: {model}[/red]")
        sys.exit(1)

    # 6. Skip review?
    skip_review_answer = console.input(
        "\n[bold]Skip transcript review?[/bold] [dim]\\[y/N][/dim]: "
    ).strip().lower()
    no_review = skip_review_answer in ("y", "yes")

    # 7. Run pipeline
    output_dir = PROJECT_ROOT / "output-videos"
    output_dir.mkdir(exist_ok=True)

    console.print()
    for i, (video, vlang) in enumerate(zip(selected, langs)):
        if batch:
            console.print(f"\n[bold]── Video {i + 1}/{len(selected)}: {video.name} ──[/bold]\n")
        output = output_dir / f"{video.stem}_subtitled.mp4"
        process_video(video, vlang, model, no_review, output)

    if batch:
        console.print(f"\n[bold green]All {len(selected)} videos processed.[/bold green]")


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="polysub",
        description="Burn trilingual subtitles into a video.",
    )
    p.add_argument("video", type=Path, help="Input video file")
    p.add_argument(
        "--lang", required=True, choices=SUPPORTED_LANGS,
        help="Language spoken in the video: es / en / ru",
    )
    p.add_argument(
        "--model", default="turbo",
        choices=["tiny", "base", "small", "medium", "large", "turbo"],
        help="Whisper model size (default: turbo). Use medium/large for Russian.",
    )
    p.add_argument(
        "--no-review", action="store_true",
        help="Skip the transcript review step.",
    )
    p.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output path (default: <input>_subtitled.mp4)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    process_video(
        video=args.video,
        lang=args.lang,
        model=args.model,
        no_review=args.no_review,
        output=args.output,
    )


if __name__ == "__main__":
    if len(sys.argv) == 1:
        interactive_mode()
    else:
        main()
