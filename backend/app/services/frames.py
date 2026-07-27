from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_tail_overlap(frames: list[Path], overlap: int, out_dir: Path) -> list[Path]:
    ensure_dir(out_dir)
    for p in out_dir.glob("*"):
        p.unlink()
    tail = frames[-overlap:] if overlap > 0 else []
    saved = []
    for i, src in enumerate(tail):
        dest = out_dir / f"tail_{i:03d}.png"
        shutil.copy2(src, dest)
        saved.append(dest)
    return saved


def discard_overlap(frames: list[Path], overlap: int) -> list[Path]:
    """Keep frames[overlap:] — drop duplicated/overlap region from new chunk."""
    if overlap <= 0:
        return frames
    if overlap >= len(frames):
        return []
    return frames[overlap:]


def write_kept_frames(frames: list[Path], out_dir: Path) -> list[Path]:
    ensure_dir(out_dir)
    for p in out_dir.glob("*"):
        p.unlink()
    saved = []
    for i, src in enumerate(frames):
        dest = out_dir / f"frame_{i:05d}.png"
        shutil.copy2(src, dest)
        saved.append(dest)
    return saved


def join_brightness_delta(prev_last: Path, new_first: Path) -> float:
    """Mean absolute brightness difference 0-255; higher = worse join."""
    a = Image.open(prev_last).convert("L").resize((64, 64))
    b = Image.open(new_first).convert("L").resize((64, 64))
    diff = ImageChops.difference(a, b)
    return float(ImageStat.Stat(diff).mean[0])


def qa_join(
    prev_last: Path | None,
    new_kept_first: Path | None,
    *,
    max_brightness_delta: float = 40.0,
) -> tuple[bool, str]:
    if prev_last is None or new_kept_first is None:
        return True, "no prior frame"
    if not prev_last.exists() or not new_kept_first.exists():
        return False, "missing join frames"
    delta = join_brightness_delta(prev_last, new_kept_first)
    if delta > max_brightness_delta:
        return False, f"brightness jump {delta:.1f} > {max_brightness_delta}"
    return True, f"ok delta={delta:.1f}"
