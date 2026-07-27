from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr or proc.stdout or "ffmpeg failed")


def extract_frames_from_video(video: Path, out_dir: Path, fps: int | None = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "frame_%05d.png"
    cmd = ["ffmpeg", "-y", "-i", str(video)]
    if fps:
        cmd += ["-vf", f"fps={fps}"]
    cmd += ["-start_number", "0", str(pattern)]
    _run(cmd)
    return sorted(out_dir.glob("frame_*.png"))


def encode_frames_to_mp4(frames_dir: Path, out_path: Path, fps: int = 24) -> Path:
    """Encode a contiguous PNG sequence to H.264 once for delivery."""
    frames = sorted(frames_dir.glob("*.png"))
    if not frames:
        raise FFmpegError(f"no frames in {frames_dir}")
    # Normalize to frame_%05d.png sequence in a temp-ordered dir if needed
    seq_dir = frames_dir
    first = frames[0].name
    if not first.startswith("frame_"):
        seq_dir = frames_dir / "_seq"
        if seq_dir.exists():
            shutil.rmtree(seq_dir)
        seq_dir.mkdir(parents=True)
        for i, src in enumerate(frames):
            shutil.copy2(src, seq_dir / f"frame_{i:05d}.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pattern = str(seq_dir / "frame_%05d.png")
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        pattern,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "18",
        str(out_path),
    ]
    _run(cmd)
    return out_path


def concat_videos(videos: list[Path], out_path: Path) -> Path:
    """Lossless-ish concat of same-codec clips via ffmpeg concat demuxer."""
    if not videos:
        raise FFmpegError("no videos to concat")
    if len(videos) == 1:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(videos[0], out_path)
        return out_path
    list_file = out_path.parent / f".concat_{out_path.stem}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for v in videos:
        # ffmpeg concat demuxer needs escaped single quotes
        safe = str(v.resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_file.write_text("\n".join(lines) + "\n")
    try:
        _run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                str(out_path),
            ]
        )
    finally:
        list_file.unlink(missing_ok=True)
    return out_path
