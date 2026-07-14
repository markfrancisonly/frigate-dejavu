"""Capture pipeline: ffprobe/ffmpeg per stream, parallel, cancellable (SPEC §5).

Loop mode:   -c copy for N seconds — codec match by construction.
Freeze mode: grab one frame, synthesize a short still clip in the source's
             codec family (+ silent AAC iff the source has audio); playback is
             then identical to loop mode (zero transcode while dejavu is on).
"""

import json
import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("dejavu.capture")

RTSP_INPUT_ARGS = ["-rtsp_transport", "tcp", "-timeout", "10000000"]
FFMPEG_BASE = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]


class CaptureError(Exception):
    pass


class Cancelled(Exception):
    pass


class CancelToken:
    """Cooperative cancellation: flags the job and terminates live ffmpeg procs."""

    def __init__(self):
        self._event = threading.Event()
        self._procs = set()
        self._lock = threading.Lock()

    @property
    def cancelled(self):
        return self._event.is_set()

    def check(self):
        if self.cancelled:
            raise Cancelled()

    def register(self, proc):
        with self._lock:
            self._procs.add(proc)
            cancelled = self._event.is_set()
        # If cancellation already fired, cancel() will not run again for this
        # proc — terminate it now so a process started just after the deadline
        # (or an 'off') cannot run to completion and overshoot the budget.
        if cancelled:
            try:
                proc.terminate()
            except OSError:
                pass

    def unregister(self, proc):
        with self._lock:
            self._procs.discard(proc)

    def cancel(self):
        self._event.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass


def _run(cmd, timeout, cancel, what):
    """Run ffmpeg/ffprobe cancel-aware; returns stdout bytes."""
    if cancel:
        cancel.check()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
    )
    if cancel:
        cancel.register(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise CaptureError(f"{what}: timed out after {timeout:.0f}s")
    finally:
        if cancel:
            cancel.unregister(proc)
    if cancel and cancel.cancelled:
        raise Cancelled()
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip()[-600:]
        raise CaptureError(f"{what}: exit {proc.returncode}: {tail or 'no stderr'}")
    return out


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------


def _probe(cmd_input_args, target, timeout, cancel, what):
    cmd = (
        ["ffprobe", "-v", "error"]
        + cmd_input_args
        + ["-print_format", "json", "-show_streams", "-show_format", target]
    )
    out = _run(cmd, timeout, cancel, what)
    try:
        return json.loads(out or b"{}")
    except ValueError as exc:
        raise CaptureError(f"{what}: unparseable ffprobe output: {exc}") from exc


def probe_url(url, cancel=None, timeout=30):
    return _probe(RTSP_INPUT_ARGS, url, timeout, cancel, f"probe {url}")


def probe_file(path, timeout=20):
    return _probe([], path, timeout, None, f"probe {os.path.basename(path)}")


def _parse_fps(rate):
    try:
        num, _, den = str(rate or "").partition("/")
        num, den = float(num), float(den or 1)
        if num > 0 and den > 0:
            return num / den
    except ValueError:
        pass
    return 0.0


def summarize_probe(js):
    video = next(
        (s for s in js.get("streams", []) if s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in js.get("streams", []) if s.get("codec_type") == "audio"), None
    )
    fps = 0.0
    if video:
        fps = _parse_fps(video.get("avg_frame_rate")) or _parse_fps(
            video.get("r_frame_rate")
        )
    fps = min(max(fps, 1.0), 60.0) if fps else 15.0
    try:
        duration = float((js.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    return {
        "video_codec": (video or {}).get("codec_name"),
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "pix_fmt": (video or {}).get("pix_fmt"),
        "fps": round(fps, 3),
        "audio_codec": (audio or {}).get("codec_name"),
        "sample_rate": int((audio or {}).get("sample_rate") or 0) or None,
        "channels": (audio or {}).get("channels"),
        "duration": duration,
    }


# --------------------------------------------------------------------------
# capture + freeze synthesis
# --------------------------------------------------------------------------


def _write_clip(cmd, out_path, timeout, cancel, what):
    """Run ffmpeg into a disposable part file, then atomically publish it."""
    part = out_path + ".part.mp4"
    try:
        _run(cmd + ["-movflags", "+faststart", part], timeout, cancel, what)
        os.replace(part, out_path)
    finally:
        if os.path.exists(part):
            os.unlink(part)


def _silent_audio_input(meta):
    layout = "mono" if (meta.get("channels") or 1) == 1 else "stereo"
    return [
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=channel_layout={layout}:"
        f"sample_rate={meta.get('sample_rate') or 48000}",
    ]


def capture_clip(url, out_path, seconds, cancel):
    # first video track + first audio track (if any packets actually flow);
    # deterministic vs ffmpeg's "best stream" default selection
    cmd = (
        FFMPEG_BASE
        + RTSP_INPUT_ARGS
        + [
            "-i",
            url,
            "-t",
            str(seconds),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
        ]
    )
    _write_clip(
        cmd, out_path, seconds + 120, cancel, f"capture {os.path.basename(out_path)}"
    )


def grab_frame(url, png_path, cancel):
    cmd = FFMPEG_BASE + RTSP_INPUT_ARGS + ["-i", url, "-frames:v", "1", png_path]
    _run(cmd, 45, cancel, f"frame grab {os.path.basename(png_path)}")


def synthesize_freeze(png_path, out_path, meta, seconds, cancel):
    codec = (meta.get("video_codec") or "").lower()
    vcodec = {"h264": "libx264", "hevc": "libx265", "h265": "libx265"}.get(codec)
    if not vcodec:
        raise CaptureError(f"freeze mode unsupported for video codec {codec!r}")
    fps = meta.get("fps") or 15.0
    gop = max(1, int(round(fps * 2)))
    has_audio = bool(meta.get("audio_codec"))

    cmd = FFMPEG_BASE + ["-loop", "1", "-framerate", f"{fps:.3f}", "-i", png_path]
    if has_audio:
        cmd += _silent_audio_input(meta)
    cmd += ["-t", str(seconds), "-map", "0:v"]
    if has_audio:
        cmd += ["-map", "1:a", "-c:a", "aac"]
    cmd += [
        "-c:v",
        vcodec,
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(gop),
        "-r",
        f"{fps:.3f}",
    ]
    if vcodec == "libx264":
        cmd += ["-tune", "stillimage"]
    else:
        cmd += ["-x265-params", "log-level=error"]
    _write_clip(
        cmd, out_path, 300, cancel, f"freeze synth {os.path.basename(out_path)}"
    )


PLACEHOLDER_META = {
    "video_codec": "h264",
    "width": 1280,
    "height": 720,
    "fps": 15.0,
    "audio_codec": None,
    "channels": 1,
    "sample_rate": 48000,
}


def synthesize_placeholder(out_path, meta, seconds, cancel):
    """The floor of the ladder: a black frame, when the camera yields no imagery
    at all (offline, and nothing recorded or cached to fall back on). Maximally
    private and — for a camera that is already dark — indistinguishable from its
    normal 'no signal'. Loudly logged, because a working camera going black is
    the most obvious tell there is."""
    meta = {**PLACEHOLDER_META, **{k: v for k, v in (meta or {}).items() if v}}
    codec = (meta.get("video_codec") or "h264").lower()
    vcodec = {"h264": "libx264", "hevc": "libx265", "h265": "libx265"}.get(
        codec, "libx264"
    )
    fps = meta.get("fps") or 15.0
    w, h = meta.get("width") or 1280, meta.get("height") or 720
    cmd = FFMPEG_BASE + [
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={w}x{h}:r={fps:.3f}:d={seconds}",
    ]
    has_audio = bool(meta.get("audio_codec"))
    if has_audio:
        cmd += _silent_audio_input(meta) + ["-map", "1:a", "-c:a", "aac", "-shortest"]
    cmd += [
        "-map",
        "0:v",
        "-t",
        str(seconds),
        "-c:v",
        vcodec,
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(max(1, int(round(fps * 2)))),
    ]
    if vcodec != "libx264":
        cmd += ["-x265-params", "log-level=error"]
    _write_clip(
        cmd, out_path, 120, cancel, f"placeholder synth {os.path.basename(out_path)}"
    )
    return has_audio


def validate_clip(path, expect_seconds):
    """Output exists, parses, has video, duration >= 90% of requested (SPEC §5.3)."""
    if not os.path.isfile(path):
        raise CaptureError("output clip missing")
    meta = summarize_probe(probe_file(path))
    if not meta["video_codec"]:
        raise CaptureError("captured clip has no video stream")
    if meta["duration"] < 0.9 * expect_seconds:
        raise CaptureError(
            f"clip too short: {meta['duration']:.1f}s < 90% of {expect_seconds}s"
        )
    return meta


def _source_meta(url, cancel):
    meta = summarize_probe(probe_url(url, cancel))
    if not meta["video_codec"]:
        raise CaptureError("stream offers no video")
    return meta


# --------------------------------------------------------------------------
# per-stream pipeline + parallel runner
# --------------------------------------------------------------------------


def frame_from_live(stream, url, tmp_dir, cancel, progress):
    """One frame of what the camera sees RIGHT NOW, plus the stream's probe.

    This prepares dejavu's safe fallback and is lighting-correct by construction
    (no IR/brightness guard can disagree with the present moment). The caller
    keeps the PNG — its stats are the reference every recorded candidate is
    later screened against. Returns (png_path, source_meta)."""
    progress(stream, "probing")
    source_meta = _source_meta(url, cancel)
    png = os.path.join(tmp_dir, f"{stream}.frame.png")
    progress(stream, "grabbing live frame")
    grab_frame(url, png, cancel)
    if not os.path.isfile(png) or os.path.getsize(png) == 0:
        raise CaptureError("live frame grab produced no image")
    return png, source_meta


def capture_stream(
    stream, url, clips_dir, tmp_dir, mode, seconds, freeze_seconds, cancel, progress
):
    """Live-restream sourcing (capture.source: restream). Loops whatever happens
    right after the toggle — recordings sourcing is the default for good reason."""
    out_path = os.path.join(clips_dir, f"{stream}.dejavu.mp4")
    if mode == "loop":
        progress(stream, "probing")
        source_meta = _source_meta(url, cancel)
        progress(stream, f"capturing {seconds}s")
        capture_clip(url, out_path, seconds, cancel)
        clip_meta = validate_clip(out_path, seconds)
    else:
        png, source_meta = frame_from_live(stream, url, tmp_dir, cancel, progress)
        try:
            progress(stream, "synthesizing freeze clip")
            synthesize_freeze(png, out_path, source_meta, freeze_seconds, cancel)
            clip_meta = validate_clip(out_path, freeze_seconds)
        finally:
            if os.path.exists(png):
                os.unlink(png)

    return {
        "clip": out_path,
        "has_audio": bool(clip_meta["audio_codec"]),
        "source": source_meta,
        "clip_meta": clip_meta,
    }


def run_captures(tasks, parallel, cancel, progress):
    """Run one clip-sourcing task per stream in parallel; returns
    {stream: {"ok": bool, ...}}. `tasks` maps stream -> zero-arg callable
    (restream capture, recordings export, freeze synth — the runner does not
    care). Raises Cancelled if the job was cancelled."""
    results = {}
    workers = max(1, min(parallel, len(tasks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(task): s for s, task in tasks.items()}
        for fut in as_completed(futures):
            stream = futures[fut]
            try:
                results[stream] = {"ok": True, **fut.result()}
                progress(stream, "captured")
            except Cancelled:
                results[stream] = {"ok": False, "error": "cancelled"}
                progress(stream, "cancelled")
            except CaptureError as exc:
                results[stream] = {"ok": False, "error": str(exc)}
                progress(stream, f"failed: {exc}")
                log.warning("capture failed for %s: %s", stream, exc)
            except Exception as exc:  # noqa: BLE001 - report, don't wedge the pool
                results[stream] = {"ok": False, "error": f"unexpected: {exc}"}
                progress(stream, f"failed: {exc}")
                log.exception("unexpected capture failure for %s", stream)
    if cancel.cancelled:
        raise Cancelled()
    return results
