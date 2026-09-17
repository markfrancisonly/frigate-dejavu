"""Loop/freeze clip sourcing from Frigate's own recordings (SPEC §5b).

Dejavu first prepares a freeze frame (SPEC §5c); this module can source an
offline camera's recorded freeze rung and, after freeze activation, find a
better loop replacement from the camera's own recent past. Absolute quiet is
the ideal, but the thing that actually gives a loop away is REPETITION — so a
long window in which a car passes once beats a short window in which a bush
sways every 20 s.

Window tiers, best first (SPEC §5b.2):
  quiet    — every covering segment reports objects == 0 AND overlaps no event
             (±2 s pad); at least `target_seconds` long. Absolute privacy.
  diluted  — no `person` event ever, other tracked objects (car, cat, ...)
             allowed provided the window is >= dilute.min_seconds AND activity
             occupies <= dilute.max_activity_fraction of it. A transient is
             then rare enough per loop period to read as ordinary.
  short    — a quiet run shorter than target_seconds but >= min_seconds. Last
             resort before falling back to the freeze frame: a short loop is
             only invisible if the scene is genuinely still.

`person` events disqualify a window at EVERY tier: replaying a person is both
a disclosure and the single most obvious tell. Below the last tier the caller
keeps its freeze frame — a stream is never left live.
"""

import logging
import os
import shutil
import threading
import time
from collections import Counter
from datetime import datetime, timezone

import requests
from capture import FFMPEG_BASE, CaptureError, _run, probe_file, summarize_probe
from frigate import http_delete, http_get, http_post

log = logging.getLogger("dejavu.recordings")

EVENT_PAD = 2.0  # seconds of slack around events
SEGMENT_GAP = 2.0  # max gap between segments still considered contiguous
SEGMENT_JITTER = 0.5  # nominal 10 s segments actually run 9.99-10.04 s, so an
# N-segment run measures N*10 +/- ~0.05 s. Length tests
# must tolerate that or an exactly-min-length window is
# dropped for being 19.9905 s long (observed on kitchen).
EXPORT_TIMEOUT = 120  # seconds for frigate to stitch one export
CONCAT_TIMEOUT = 600  # a 20-minute window is ~120 segment files to concat

# Freshness margins. The export worker wedges (in_progress forever) on segments
# still moving out of /tmp/cache, so it keeps the old blunt 30 s rule. Direct
# segment reads have no such worker: all they need is a file that is closed and
# no longer growing, which _segment_settled() checks directly. The blunt margin
# cost us camera.kitchen on 2026-07-07 — the only IR-mode event-clear segments
# in the whole lookback were 11-21 s old and were skipped unread (SPEC §16).
EXPORT_RECENT_MARGIN = 30
FILES_RECENT_MARGIN = 5
SETTLE_RESTAT_DELAY = 0.35  # size must be unchanged across this gap


# Frigate's export machinery handles ONE job well; a burst of concurrent
# exports wedges the worker and even the /api/exports listing (verified live
# with 7 parallel jobs). All export lifecycles are serialized through here.
_EXPORT_LOCK = threading.Semaphore(1)


def _remove(path):
    if path and os.path.exists(path):
        os.unlink(path)


def _get(api, path, params=None, timeout=30):
    try:
        r = http_get(f"{api}{path}", params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise CaptureError(f"frigate API unreachable ({path}): {exc}") from exc
    if r.status_code != 200:
        raise CaptureError(f"GET {path} failed: HTTP {r.status_code}: {r.text[:200]}")
    return r


def exports_api_ready(api, timeout=8):
    """Frigate's export subsystem answers. /api/version comes up minutes
    before the export/DB machinery does after a restart — gating on this
    prevents an export storm against a still-settling Frigate."""
    try:
        r = http_get(f"{api}/api/exports", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def wait_exports_ready(api, timeout, cancel=None, progress=None):
    """Poll until the export API answers; True if ready within timeout."""
    deadline = time.monotonic() + timeout
    logged = False
    while True:
        if cancel:
            cancel.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # The readiness probe and retry sleep must fit inside the caller's
        # budget too.  An 8-second probe or fixed 5-second sleep after a short
        # remaining deadline otherwise makes this "bounded" wait overrun it.
        if exports_api_ready(api, timeout=min(8, remaining)):
            if logged:
                log.info("frigate export API is responsive — proceeding")
            return True
        if not logged:
            log.warning(
                "frigate export API not responsive (still settling after "
                "a restart?) — waiting up to %ss before sourcing",
                timeout,
            )
            if progress:
                progress("*", "waiting for frigate to settle")
            logged = True
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def fetch_segments(api, camera, after, before):
    r = _get(api, f"/api/{camera}/recordings", {"after": after, "before": before})
    segments = r.json() or []
    return sorted(segments, key=lambda s: s.get("start_time", 0))


def fetch_event_spans(api, camera, after, before):
    """[(start, end, label)] — padded. Open-ended events block everything after
    their start. Labels drive the never-loop-a-person rule."""
    r = _get(
        api,
        "/api/events",
        {"cameras": camera, "after": after, "before": before, "limit": 2000},
    )
    spans = []
    for ev in r.json() or []:
        start = float(ev.get("start_time") or 0)
        end = ev.get("end_time")
        end = float(end) if end is not None else before  # ongoing event blocks onward
        spans.append(
            (start - EVENT_PAD, end + EVENT_PAD, (ev.get("label") or "").lower())
        )
    return spans


def _overlaps(start, end, spans):
    return any(s < end and e > start for s, e, _ in spans)


def _overlapping_labels(start, end, spans):
    return {lab for s, e, lab in spans if s < end and e > start}


# tier ordering: lower is better. A long diluted loop outranks a short quiet
# one because the repetition period, not the presence of a car, is the tell.
TIER_QUIET, TIER_DILUTED, TIER_SHORT = 0, 1, 2
TIER_NAMES = {TIER_QUIET: "quiet", TIER_DILUTED: "diluted", TIER_SHORT: "short-quiet"}


def _tail_window(segs, limit_seconds):
    """Most recent <= limit_seconds of a run, SEGMENT-ALIGNED so the window maps
    exactly onto whole recording files (direct reads need no re-encode)."""
    run_end = segs[-1]["end_time"]
    covering = []
    for seg in reversed(segs):
        covering.insert(0, seg)
        if run_end - covering[0]["start_time"] >= limit_seconds:
            break
    return covering


def _is_active(seg, spans):
    return (seg.get("objects") or 0) > 0 or _overlaps(
        seg["start_time"], seg["end_time"], spans
    )


def _mk_window(covering, tier, spans):
    start, end = covering[0]["start_time"], covering[-1]["end_time"]
    duration = max(end - start, 1)
    motion = sum(g.get("motion") or 0 for g in covering)
    active = sum(
        (g["end_time"] - g["start_time"]) for g in covering if _is_active(g, spans)
    )
    return {
        "start": start,
        "end": end,
        "duration": end - start,
        "tier": tier,
        "full": tier != TIER_SHORT,
        "motion_ps": motion / duration,
        "activity_fraction": active / duration,
        "segments": [g["start_time"] for g in covering],
    }


def _slide_diluted(segs, need, max_seconds, max_frac, spans, now, per_run_cap=4):
    """Longest acceptable diluted window at each start offset of a run, kept
    one-per-hour-bucket (lowest activity wins). A run with no `person` in it can
    be hours long; only scoring its tail would miss the calm stretch in the
    middle, which is exactly where a car passes once instead of ten times."""
    n = len(segs)
    active = [0.0] * (n + 1)
    for i, g in enumerate(segs):
        span = g["end_time"] - g["start_time"]
        active[i + 1] = active[i] + (span if _is_active(g, spans) else 0.0)

    best, j = {}, 0
    for i in range(n):
        j = max(j, i)
        while (
            j + 1 < n and segs[j + 1]["end_time"] - segs[i]["start_time"] <= max_seconds
        ):
            j += 1
        # The longest duration-limited window is not necessarily acceptable: a
        # busy tail can push its activity fraction over the limit while a shorter
        # prefix is valid. Walk back to the longest endpoint that satisfies both.
        chosen = None
        for end_idx in range(j, i - 1, -1):
            duration = segs[end_idx]["end_time"] - segs[i]["start_time"]
            if duration < need - SEGMENT_JITTER:
                break
            frac = (active[end_idx + 1] - active[i]) / max(duration, 1)
            if frac <= max_frac:
                chosen = (end_idx, duration, frac)
                break
        if chosen is None:
            continue
        end_idx, duration, frac = chosen
        bucket = int((now - segs[end_idx]["end_time"]) // 3600)
        prev = best.get(bucket)
        if prev is None or (frac, -duration) < (prev[0], -prev[1]):
            best[bucket] = (frac, duration, segs[i : end_idx + 1])
    ordered = sorted(best.items())[:per_run_cap]
    # a slice that turned out to hold no activity at all IS a quiet window; the
    # slide just found it somewhere other than the run's tail
    return [
        _mk_window(cov, TIER_DILUTED if frac > 0 else TIER_QUIET, spans)
        for _, (frac, _, cov) in ordered
    ]


def _runs(segments, now, recent_margin, predicate):
    """Maximal runs of contiguous, settled segments satisfying `predicate`."""
    runs, run = [], []
    for seg in segments:
        s, e = seg.get("start_time"), seg.get("end_time")
        if s is None or e is None or e > now - recent_margin:
            continue
        contiguous = bool(run) and (s - run[-1]["end_time"]) <= SEGMENT_GAP
        if predicate(seg) and (not run or contiguous):
            run.append(seg)
        else:
            if run:
                runs.append(run)
            run = [seg] if predicate(seg) else []
    if run:
        runs.append(run)
    return runs


def find_candidate_windows(
    segments,
    event_spans,
    target_seconds,
    min_seconds,
    now,
    recent_margin=EXPORT_RECENT_MARGIN,
    dilute=None,
    max_seconds=None,
):
    """All candidate windows, best first. Each carries "tier" (see TIER_*).

    `dilute` = {"enabled","min_seconds","max_activity_fraction","block_labels"}
    or None to consider quiet windows only."""
    dilute = dilute or {}
    block = {label.lower() for label in (dilute.get("block_labels") or ["person"])}
    max_seconds = max_seconds or max(target_seconds, min_seconds)

    def quiet(seg):
        return (seg.get("objects") or 0) == 0 and not _overlaps(
            seg["start_time"], seg["end_time"], event_spans
        )

    def unblocked(seg):
        return not (
            _overlapping_labels(seg["start_time"], seg["end_time"], event_spans) & block
        )

    candidates = []
    for segs in _runs(segments, now, recent_margin, quiet):
        duration = segs[-1]["end_time"] - segs[0]["start_time"]
        if duration >= target_seconds - SEGMENT_JITTER:
            candidates.append(
                _mk_window(_tail_window(segs, max_seconds), TIER_QUIET, event_spans)
            )
        elif duration >= min_seconds - SEGMENT_JITTER:
            candidates.append(_mk_window(segs, TIER_SHORT, event_spans))

    if dilute.get("enabled", True):
        need = max(dilute.get("min_seconds") or target_seconds, target_seconds)
        max_frac = dilute.get("max_activity_fraction", 0.10)
        for segs in _runs(segments, now, recent_margin, unblocked):
            if segs[-1]["end_time"] - segs[0]["start_time"] < need - SEGMENT_JITTER:
                continue
            candidates.extend(
                _slide_diluted(segs, need, max_seconds, max_frac, event_spans, now)
            )

    # a fully-quiet run also satisfies `unblocked`; keep the better-tiered copy
    seen, deduped = {}, []
    for c in sorted(candidates, key=lambda c: c["tier"]):
        key = (round(c["start"]), round(c["end"]))
        if key not in seen:
            seen[key] = c
            deduped.append(c)

    # Quality tier FIRST: a recent 20-30 second loop is much more obvious than a
    # longer quiet/diluted window from an earlier hour. The lighting guards are
    # the authoritative regime check and cheaply reject stale day/night matches.
    # Within a tier: recent lighting bucket, longest, least motion, newest.
    deduped.sort(
        key=lambda c: (
            c["tier"],
            int((now - c["end"]) // 3600),
            -c["duration"],
            c["motion_ps"],
            -c["end"],
        )
    )
    return deduped


def describe_window(win, now):
    when = datetime.fromtimestamp(win["start"]).strftime("%H:%M:%S")
    age = (now - win["end"]) / 60
    extra = ""
    if win["tier"] == TIER_DILUTED:
        extra = f", activity {win['activity_fraction'] * 100:.0f}%"
    return (
        f"{TIER_NAMES[win['tier']]} {win['duration']:.0f}s window @ {when} "
        f"({age:.0f}m ago, motion {win['motion_ps']:.1f}/s{extra})"
    )


# --------------------------------------------------------------------------
# retrieval, plan A: direct reads of Frigate's segment files. The recordings
# are plain 10s mp4s at <recordings>/<UTC-date>/<UTC-hour>/<camera>/<MM.SS>.mp4
# — no export worker (the single most fragile Frigate subsystem we've hit),
# no serialization, fully parallel. Export API remains plan B.
# --------------------------------------------------------------------------


def segment_path(recordings_dir, camera, start_time):
    dt = datetime.fromtimestamp(int(start_time), tz=timezone.utc)
    return os.path.join(
        recordings_dir,
        dt.strftime("%Y-%m-%d"),
        dt.strftime("%H"),
        camera,
        dt.strftime("%M.%S") + ".mp4",
    )


def files_retrieval_available(recordings_dir):
    try:
        return (
            bool(recordings_dir)
            and os.path.isdir(recordings_dir)
            and bool(os.listdir(recordings_dir))
        )
    except OSError:
        return False


def _segment_settled(path):
    """The file exists, is non-empty, and is no longer growing. This is the
    direct test the blunt 30 s freshness margin was standing in for — a segment
    frigate has finished writing is safe to read no matter how young it is."""
    try:
        first = os.path.getsize(path)
        if first == 0:
            return False
        time.sleep(SETTLE_RESTAT_DELAY)
        return os.path.getsize(path) == first
    except OSError:
        return False


SETTLE_CHECK_AGE = 60  # only windows this fresh can still be mid-write


def segment_files(recordings_dir, camera, win, now=None):
    """Resolve a window's segment start-times to readable files on disk."""
    paths = []
    for st in win.get("segments") or []:
        p = segment_path(recordings_dir, camera, st)
        if not os.path.isfile(p):
            raise CaptureError(f"segment file missing: {p}")
        paths.append(p)
    if not paths:
        raise CaptureError("window carries no segment list")
    # Only the newest file of a fresh window can still be open for writing; an
    # hours-old window never needs the re-stat sleep.
    fresh = (now or time.time()) - win["end"] < SETTLE_CHECK_AGE
    if fresh and not _segment_settled(paths[-1]):
        raise CaptureError(f"newest segment still being written: {paths[-1]}")
    return paths


def _direct_audio_action(policy, want_audio, meta):
    """Resolve the final audio shape before the one-pass segment assembly."""
    has_audio = bool(meta.get("audio_codec"))
    if want_audio is True:
        return "keep" if policy == "keep" and has_audio else "silence"
    if want_audio is False:
        return "strip"
    return {"silence": "silence", "strip": "strip"}.get(policy, "keep")


def _fetch_window_via_files(
    recordings_dir,
    camera,
    stream,
    win,
    tmp_dir,
    cancel,
    progress,
    now,
    out_path=None,
    audio_policy=None,
    want_audio=None,
):
    paths = segment_files(recordings_dir, camera, win)
    progress(
        stream, f"reading {len(paths)} segment file(s) — {describe_window(win, now)}"
    )
    tmp_clip = out_path or os.path.join(tmp_dir, f"{stream}.export.mp4")
    source_meta = (
        summarize_probe(probe_file(paths[0])) if audio_policy is not None else None
    )
    action = (
        _direct_audio_action(audio_policy, want_audio, source_meta)
        if source_meta is not None
        else None
    )
    list_path = os.path.join(tmp_dir, f"{stream}.segments.txt")
    with open(list_path, "w") as f:
        for p in paths:
            f.write(f"file '{p}'\n")
    cmd = FFMPEG_BASE + ["-f", "concat", "-safe", "0", "-i", list_path]
    if audio_policy is None:
        cmd += ["-c", "copy"]
    else:
        if action == "silence":
            layout = "mono" if (source_meta.get("channels") or 1) == 1 else "stereo"
            rate = source_meta.get("sample_rate") or 48000
            cmd += [
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=channel_layout={layout}:sample_rate={rate}",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
            ]
        elif action == "strip":
            cmd += ["-map", "0:v:0", "-c:v", "copy", "-an"]
        else:
            cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
    cmd += ["-movflags", "+faststart", tmp_clip]
    try:
        _run(cmd, CONCAT_TIMEOUT, cancel, f"segment concat {stream}")
    except BaseException:
        if os.path.exists(tmp_clip):
            os.unlink(tmp_clip)
        raise
    finally:
        os.unlink(list_path)
    return tmp_clip


# --------------------------------------------------------------------------
# export lifecycle (Frigate returns the exact ID; never infer it from shared state)
# --------------------------------------------------------------------------


def start_export(api, camera, start, end):
    try:
        r = http_post(
            f"{api}/api/export/{camera}/start/{start:.3f}/end/{end:.3f}",
            json={
                "playback": "realtime",
                "source": "recordings",
                "name": f"dejavutmp {camera}"[:60],
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise CaptureError(f"export request failed: {exc}") from exc
    # 0.17 answered 200/201 synchronously; 0.18 queues the job and answers 202
    if r.status_code not in (200, 201, 202):
        raise CaptureError(f"export rejected: HTTP {r.status_code}: {r.text[:200]}")
    try:
        export_id = r.json().get("export_id")
    except (AttributeError, ValueError):
        export_id = None
    if not export_id:
        raise CaptureError("export response did not include export_id")
    return export_id


def _get_export(api, export_id, timeout=30):
    try:
        r = http_get(f"{api}/api/exports/{export_id}", timeout=timeout)
    except requests.RequestException as exc:
        raise CaptureError(f"export status request failed: {exc}") from exc
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise CaptureError(
            f"export status failed: HTTP {r.status_code}: {r.text[:200]}"
        )
    try:
        entry = r.json()
    except ValueError as exc:
        raise CaptureError("export status returned non-JSON") from exc
    if not isinstance(entry, dict):
        raise CaptureError("export status returned an invalid entry")
    return entry


def _get_export_job(api, export_id):
    """0.18's job-queue view of an export, or None when Frigate has no job
    record (0.17, or a pruned record)."""
    try:
        r = http_get(f"{api}/api/jobs/export/{export_id}", timeout=15)
    except requests.RequestException as exc:
        raise CaptureError(f"export job status request failed: {exc}") from exc
    if r.status_code != 200:
        return None
    try:
        job = r.json()
    except ValueError:
        return None
    return job if isinstance(job, dict) else None


def wait_export(api, export_id, cancel):
    deadline = time.monotonic() + EXPORT_TIMEOUT
    while time.monotonic() < deadline:
        if cancel:
            cancel.check()
        job = _get_export_job(api, export_id)
        if job and job.get("status") in ("failed", "cancelled"):
            raise CaptureError(
                f"export {job['status']}: {job.get('error_message') or 'no detail'}"
            )
        entry = _get_export(api, export_id)
        if entry is not None and not entry.get("in_progress"):
            return entry
        time.sleep(1)
    raise CaptureError(f"export not finished after {EXPORT_TIMEOUT}s")


def download_export(api, entry, dest, cancel):
    filename = os.path.basename(entry.get("video_path") or "")
    if not filename:
        raise CaptureError(f"export entry has no video_path: {entry}")
    try:
        with http_get(f"{api}/exports/{filename}", stream=True, timeout=30) as r:
            if r.status_code != 200:
                raise CaptureError(f"export download failed: HTTP {r.status_code}")
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if cancel:
                        cancel.check()
                    f.write(chunk)
    except requests.RequestException as exc:
        raise CaptureError(f"export download failed: {exc}") from exc
    if os.path.getsize(dest) == 0:
        raise CaptureError("export download is empty")


def delete_export(api, export_id):
    """Delete the exact export returned by Frigate: 0.18's bulk endpoint, else
    (0.17, or an id it no longer knows) the legacy per-id route."""
    try:
        r = http_post(
            f"{api}/api/exports/delete", json={"ids": [export_id]}, timeout=15
        )
        if r.status_code == 200:
            return
        if r.status_code not in (404, 405):
            log.warning(
                "could not delete export %s: HTTP %s %s",
                export_id,
                r.status_code,
                r.text[:120],
            )
            return
        r = http_delete(f"{api}/api/export/{export_id}", timeout=15)
        if r.status_code not in (200, 204, 404, 405):
            log.warning("could not delete export %s: HTTP %s", export_id, r.status_code)
    except requests.RequestException as exc:
        log.warning("could not delete export %s: %s", export_id, exc)


# --------------------------------------------------------------------------
# audio policy + assembly
# --------------------------------------------------------------------------


def apply_audio_policy(src, dest, policy, meta, cancel, want_audio=None):
    """silence: same-codec-family silent AAC (no repeating-sound loop tell);
    keep: passthrough; strip: drop audio track.

    `want_audio` pins the outcome regardless of policy. The freeze fallback and
    loop candidate share one planned go2rtc source string, which encodes whether
    the finalized clip carries audio — so both clips MUST agree before
    activation."""
    has_audio = bool(meta.get("audio_codec"))
    if want_audio is not None:
        if want_audio and not has_audio:
            policy = "silence"  # synthesize a track the source lacks
        elif want_audio and has_audio:
            policy = "keep" if policy == "keep" else "silence"
        elif not want_audio:
            policy = "strip" if has_audio else "keep"
    if policy == "silence":
        return _add_silent_track(src, dest, meta, cancel)
    if policy == "keep" or not has_audio:
        # src (tmp_dir) and dest (clips_dir) can be different mounts — os.replace
        # is same-filesystem only (EXDEV), so move cross-device-safely.
        _move(src, dest)
        return has_audio
    cmd = FFMPEG_BASE + [
        "-i",
        src,
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-an",
        "-movflags",
        "+faststart",
        dest,
    ]
    _run(cmd, 120, cancel, "strip audio")
    os.unlink(src)
    return False


def _move(src, dest):
    """Move src -> dest, tolerating a cross-filesystem hop (tmp_dir and clips_dir
    can be separate mounts). Atomic when same-device; copy+unlink otherwise."""
    try:
        os.replace(src, dest)
    except OSError:
        shutil.move(src, dest)


def _add_silent_track(src, dest, meta, cancel):
    """Replace (or add) a silent AAC track — a repeating sound is the most
    obvious loop tell."""
    layout = "mono" if (meta.get("channels") or 1) == 1 else "stereo"
    rate = meta.get("sample_rate") or 48000
    cmd = FFMPEG_BASE + [
        "-i",
        src,
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=channel_layout={layout}:sample_rate={rate}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-shortest",
        "-movflags",
        "+faststart",
        dest,
    ]
    _run(cmd, 180, cancel, "silence audio")
    os.unlink(src)
    return True


def plan_candidate_windows(
    api,
    camera,
    target_seconds,
    min_seconds,
    search_hours,
    now=None,
    recent_margin=EXPORT_RECENT_MARGIN,
    dilute=None,
    max_seconds=None,
):
    """Read-only: segments + events -> ordered candidate windows."""
    now = now or time.time()
    after = now - search_hours * 3600
    segments = fetch_segments(api, camera, after, now)
    if not segments:
        raise CaptureError(f"no recordings for {camera} in the last {search_hours}h")
    events = fetch_event_spans(api, camera, after, now)
    log.debug(
        "%s: window search over %sh: %d segments, %d event spans, "
        "target=%ss min=%ss max=%ss margin=%ss dilute=%s",
        camera,
        search_hours,
        len(segments),
        len(events),
        target_seconds,
        min_seconds,
        max_seconds,
        recent_margin,
        dilute,
    )
    wins = find_candidate_windows(
        segments,
        events,
        target_seconds,
        min_seconds,
        now,
        recent_margin=recent_margin,
        dilute=dilute,
        max_seconds=max_seconds,
    )
    if not wins:
        raise CaptureError(
            f"no loopable window >= {min_seconds}s for {camera} "
            f"in the last {search_hours}h ({len(events)} events)"
        )
    log.debug(
        "%s: %d candidate window(s); best 5: %s",
        camera,
        len(wins),
        "; ".join(describe_window(w, now) for w in wins[:5]),
    )
    return wins


# --------------------------------------------------------------------------
# lighting guards: time-limiting the lookback is a proxy — the real
# requirement is that the frozen scene matches how the camera looks RIGHT
# NOW. Three signals, all from one 16x16 yuv444p probe, using the SHARED
# cameralux/frigate-dejavu algorithm (owner directive — one definition,
# two implementations):
#   chroma — mean(|Cb-128|+|Cr-128|)/2: coarse pre-filter for the IR MODE
#            detector. IR switchover is chromatic, not luminous: IR
#            illuminators keep brightness day-like while the image goes
#            monochrome. Calibrated live: IR cams 0.75-3.7, color 7.3-16.6.
#   gray   — fraction of probe cells that are individually neutral
#            (per-cell chroma <= IR_GRAY_CELL_TOLERANCE). The DECIDING IR
#            test: true IR frames are exactly uniform gray (1.000 on every
#            validated frame), while dark or muted COLOR frames — whose mean
#            chroma also reads low — keep colored cells. Chroma alone
#            misclassified 91 of 114 color frames at this probe resolution;
#            requiring both signals caught 25/25 true-IR frames with zero
#            false positives (139-frame fleet validation, 2026-07).
#   luma   — PERCEIVED LUMINANCE on a 0-255 scale: linear-light mean through
#            the inverse sRGB gamma, then Stevens'-law brightness
#            (255 * mean_linear^0.5 — the shared cameralux transform with
#            its default exponent). The brightest LUMA_TRIM_TOP_FRACTION of
#            cells is dropped first: IR illuminators and headlights paint
#            point hotspots, and a subject crossing the beam moved the plain
#            mean 5-14 codes with zero ambient change — enough to poison a
#            delta match on both the reference and candidate sides. Valid
#            for chromatic and monochromatic frames alike; the probe forces
#            full-range output so JPEG and video sources normalize
#            identically.
# --------------------------------------------------------------------------

# Candidates are screened by probing their FIRST and LAST segment files (~0.1 s
# each) rather than concatenating the window first, so depth is cheap. Only the
# winner is ever stitched. The old top-5 cap screened 5 of 25 candidates.
MAX_EXPORT_CANDIDATES = 5  # export API is slow + serialized: keep the cap there
GUARD_BUDGET_SECONDS = 25  # wall-clock ceiling on screening one camera
MAX_SEAM_CANDIDATES = 3  # compare a few equivalent survivors, not the whole search
IR_CHROMA_THRESHOLD = 6.0  # shared pre-filter; below MAY be monochrome/IR
# Probe-resolution-specific (16x16 cell averaging blends small colored objects,
# so these are TIGHTER than cameralux's full-resolution 0.85/tol-2 constants):
IR_GRAY_CELL_TOLERANCE = 1  # a cell is neutral when (|Cb-128|+|Cr-128|)/2 <= 1
IR_GRAY_FRACTION_MIN = 0.95  # true IR measured 1.000; color max 0.934
LUMA_TRIM_TOP_FRACTION = 0.05  # drop the brightest 13 of 256 cells

# 256-entry inverse sRGB gamma LUT — identical math to cameralux's
# precompute_inverse_gamma_8bit_lut()
_SRGB_INV = [
    (n / 12.92 if (n := i / 255.0) <= 0.04045 else ((n + 0.055) / 1.055) ** 2.4)
    for i in range(256)
]
PERCEPTION_EXPONENT = 0.5  # Stevens' law for extended sources (shared default)


def frame_stats(path, cancel=None, tail=False):
    """{'luma','chroma','gray'} of an image / a video's first (~last) frame.
    luma = shared perceived-luminance (0-255, hotspot-trimmed); chroma = mean
    color deviation; gray = fraction of individually-neutral cells."""
    seek = ["-sseof", "-1.5"] if tail else []
    out = _run(
        FFMPEG_BASE
        + seek
        + [
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            "scale=16:16:in_range=auto:out_range=full,format=yuv444p",
            "-f",
            "rawvideo",
            "-",
        ],
        30,
        cancel,
        "frame stats probe",
    )
    if len(out) < 768:
        raise CaptureError("frame stats probe produced no data")
    y, u, v = out[0:256], out[256:512], out[512:768]
    cell_chroma = [(abs(u[i] - 128) + abs(v[i] - 128)) / 2 for i in range(256)]
    linear = sorted(_SRGB_INV[b] for b in y)
    keep = linear[: max(1, int(256 * (1.0 - LUMA_TRIM_TOP_FRACTION)))]
    return {
        "luma": 255.0 * (sum(keep) / len(keep)) ** PERCEPTION_EXPONENT,
        "chroma": sum(cell_chroma) / 256,
        "gray": sum(1 for c in cell_chroma if c <= IR_GRAY_CELL_TOLERANCE) / 256,
        "signature": tuple(y),
    }


def frame_stats_tail(path, cancel=None):
    try:
        return frame_stats(path, cancel, tail=True)
    except CaptureError:
        return None


def is_monochrome(stats):
    """True only for a verified IR/monochrome frame: low mean chroma AND
    nearly every cell individually neutral. Low mean chroma alone also
    matches dark or muted COLOR frames (sensor noise, pastel scenes), which
    made the mode guard inert on such cameras."""
    return (
        stats["chroma"] < IR_CHROMA_THRESHOLD
        and stats.get("gray", 1.0) >= IR_GRAY_FRACTION_MIN
    )


def _mode_name(stats):
    return "IR/mono" if is_monochrome(stats) else "color"


def live_reference_stats(api, camera, tmp_dir, cancel, recordings_dir=None):
    """Stats of the camera's current look (None if unavailable).

    Preferred reference: the LAST frame of the newest settled recording
    segment — same camera encode and same probe path as the candidates, so
    zero systematic bias, at most ~40s stale. Frigate's latest.jpg (the
    fallback) re-encodes detect frames and measures ~12-18 luma codes HOT
    vs the same-moment video frame (measured across 4 cameras); chroma is
    unaffected either way (delta <= 1 measured), so IR classification never
    depended on this."""
    if files_retrieval_available(recordings_dir):
        try:
            now = time.time()
            segs = [
                s
                for s in fetch_segments(api, camera, now - 180, now)
                if s.get("end_time", now) < now - FILES_RECENT_MARGIN
            ]
            if segs:
                p = segment_path(recordings_dir, camera, segs[-1]["start_time"])
                if os.path.isfile(p):
                    stats = frame_stats(p, cancel, tail=True)
                    if stats is not None:
                        log.debug(
                            "%s: lighting reference from segment %s: "
                            "luma=%.0f chroma=%.1f gray=%.2f (%s)",
                            camera,
                            os.path.basename(p),
                            stats["luma"],
                            stats["chroma"],
                            stats["gray"],
                            _mode_name(stats),
                        )
                        return stats
        except (CaptureError, requests.RequestException) as exc:
            log.debug(
                "%s: segment reference unavailable (%s); using latest.jpg", camera, exc
            )
    path = os.path.join(tmp_dir, f"{camera}.latest.jpg")
    try:
        r = http_get(f"{api}/api/{camera}/latest.jpg", timeout=10)
        if r.status_code != 200 or not r.content:
            log.warning(
                "%s: no live frame for the lighting guards (HTTP %s)",
                camera,
                r.status_code,
            )
            return None
        with open(path, "wb") as f:
            f.write(r.content)
        try:
            return frame_stats(path, cancel)
        finally:
            os.unlink(path)
    except (requests.RequestException, CaptureError) as exc:
        log.warning("%s: lighting guards unavailable: %s", camera, exc)
        return None


def _fetch_window_clip(
    api,
    camera,
    stream,
    win,
    tmp_dir,
    cancel,
    progress,
    now,
    recordings_dir=None,
    export_ready=None,
):
    """Fetch one candidate window: direct segment reads when available,
    else the (serialized) export API. Caller owns the returned tmp file."""
    if files_retrieval_available(recordings_dir):
        try:
            return _fetch_window_via_files(
                recordings_dir, camera, stream, win, tmp_dir, cancel, progress, now
            )
        except CaptureError as exc:
            log.warning(
                "%s: direct segment read failed (%s) — using export API", stream, exc
            )
    progress(stream, "waiting for export slot")
    with _EXPORT_LOCK:
        if cancel:
            cancel.check()
        # Stage 2 runs immediately after Frigate restart #1.  The main API can
        # be healthy while its export worker is still unavailable, so every
        # actual export fallback (including one reached after a failed direct
        # read) goes through the run's shared, one-time readiness gate.
        if export_ready is not None and not export_ready():
            raise CaptureError("frigate export API did not become ready in time")
        if cancel:
            cancel.check()
        progress(stream, f"exporting {describe_window(win, now)}")
        export_id = start_export(api, camera, win["start"], win["end"])
        tmp_clip = os.path.join(tmp_dir, f"{stream}.export.mp4")
        try:
            entry = wait_export(api, export_id, cancel)
            progress(stream, "downloading export")
            download_export(api, entry, tmp_clip, cancel)
        except BaseException:
            if os.path.exists(tmp_clip):
                os.unlink(tmp_clip)
            raise
        finally:
            delete_export(api, export_id)
    return tmp_clip


# --------------------------------------------------------------------------
# cross-camera sync: overlapping views must freeze the SAME moment — a chair
# present in one camera's frozen frame but not its neighbour's is a tell.
# Windows are chosen against a common anchor on Frigate's server timeline.
# --------------------------------------------------------------------------


def select_synced_windows(
    api,
    camera_needs,
    min_seconds,
    search_hours,
    tolerance_s,
    now=None,
    recent_margin=EXPORT_RECENT_MARGIN,
    dilute=None,
    max_seconds=None,
):
    """camera_needs: {camera: needed_seconds}. Fetches every camera's candidate
    windows once, picks the most recent anchor time covered by the most
    cameras (window end within tolerance, or window containing the anchor),
    and re-orders each camera's candidates anchor-first.

    Returns (anchor_ts | None, {camera: [windows]}, {camera: error_str})."""
    now = now or time.time()
    per_cam, errors = {}, {}
    for cam, need in camera_needs.items():
        try:
            per_cam[cam] = plan_candidate_windows(
                api,
                cam,
                need,
                min(min_seconds, need),
                search_hours,
                now,
                recent_margin=recent_margin,
                dilute=dilute,
                max_seconds=max_seconds,
            )
        except CaptureError as exc:
            per_cam[cam] = []
            errors[cam] = str(exc)

    def near(win, anchor):
        return (
            abs(win["end"] - anchor) <= tolerance_s
            or win["start"] <= anchor <= win["end"]
        )

    anchor, best_key = None, None
    if tolerance_s > 0:
        anchors = sorted(
            {round(w["end"]) for wins in per_cam.values() for w in wins}, reverse=True
        )  # most recent first
        full_cap = sum(
            any(w["tier"] != TIER_SHORT for w in wins) for wins in per_cam.values()
        )
        for a in anchors:
            cov = sum(1 for wins in per_cam.values() if any(near(w, a) for w in wins))
            full_cov = sum(
                1
                for wins in per_cam.values()
                if any(w["tier"] != TIER_SHORT and near(w, a) for w in wins)
            )
            # Stay within the newest likely lighting regime, then maximize full
            # quiet/diluted coverage. Short windows must not drag healthy cameras
            # away from substantially better loops merely to share an anchor.
            key = (int((now - a) // 3600), -full_cov, -cov, -a)
            if best_key is None or key < best_key:
                anchor, best_key = a, key
            if best_key[0] == 0 and full_cov == full_cap and cov == len(per_cam):
                break

    ordered = {}
    for cam, wins in per_cam.items():
        if anchor is None:
            ordered[cam] = wins
            continue
        synced_full = [w for w in wins if w["tier"] != TIER_SHORT and near(w, anchor)]
        other_full = [
            w for w in wins if w["tier"] != TIER_SHORT and w not in synced_full
        ]
        synced_short = [w for w in wins if w["tier"] == TIER_SHORT and near(w, anchor)]
        other_short = [
            w for w in wins if w["tier"] == TIER_SHORT and w not in synced_short
        ]
        synced_full.sort(key=lambda w: (w["tier"], abs(w["end"] - anchor)))
        synced_short.sort(key=lambda w: abs(w["end"] - anchor))
        ordered[cam] = synced_full + other_full + synced_short + other_short
    return anchor, ordered, errors


def _matches_now(stats, ref, rcfg, stream, win, now):
    """Candidate must match the reference frame in day/night (IR) MODE, then in
    gross brightness. Returns None when it matches (or no reference exists),
    else a SHORT reason string. Per-candidate detail is DEBUG — these fire once
    per candidate in a screening loop and would flood the operator log; the
    caller emits one grouped summary at INFO instead."""
    if ref is None:
        return None
    if rcfg.get("match_ir_mode", True) and is_monochrome(stats) != is_monochrome(ref):
        log.debug(
            "%s: rejected %s — day/night mode mismatch vs live: candidate "
            "%s (chroma %.1f gray %.2f) vs live %s (chroma %.1f gray %.2f)",
            stream,
            describe_window(win, now),
            _mode_name(stats),
            stats["chroma"],
            stats.get("gray", 1.0),
            _mode_name(ref),
            ref["chroma"],
            ref.get("gray", 1.0),
        )
        return "IR-mode mismatch vs live"
    max_delta = rcfg["max_brightness_delta"]
    if max_delta and abs(stats["luma"] - ref["luma"]) > max_delta:
        log.debug(
            "%s: rejected %s — brightness Δ%.0f > %s vs live (lighting changed)",
            stream,
            describe_window(win, now),
            abs(stats["luma"] - ref["luma"]),
            max_delta,
        )
        return "brightness mismatch vs live"
    return None


def _stable_across(first, last, rcfg, stream, win, now):
    """A loop window must not contain the day/night flip or a lighting ramp —
    either one pulses on every repetition. None when stable, else a reason."""
    if last is None:
        return None
    if rcfg.get("match_ir_mode", True) and is_monochrome(first) != is_monochrome(last):
        log.debug(
            "%s: rejected %s — day/night (IR) switchover INSIDE the window "
            "(would flip every loop)",
            stream,
            describe_window(win, now),
        )
        return "IR flip inside window"
    max_drift = rcfg["max_brightness_drift"]
    if max_drift and abs(last["luma"] - first["luma"]) > max_drift:
        log.debug(
            "%s: rejected %s — brightness drifts Δ%.0f within the window "
            "(would pulse every loop)",
            stream,
            describe_window(win, now),
            abs(last["luma"] - first["luma"]),
        )
        return "brightness drift inside window"
    return None


def _seam_delta(first, last):
    """Mean low-resolution luma difference across the loop boundary."""
    a, b = first.get("signature"), last.get("signature")
    if not a or not b or len(a) != len(b):
        return None
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _lighting_ok(clip_or_frame, ref, rcfg, stream, win, now, cancel):
    """Guard a materialized clip/frame (export-API path). None or a reason."""
    return _matches_now(frame_stats(clip_or_frame, cancel), ref, rcfg, stream, win, now)


def screen_window_files(
    recordings_dir, camera, win, ref, rcfg, stream, now, cancel, check_drift=True
):
    """Screen a candidate window by probing its LAST (and, only if that passes,
    FIRST) segment file — no concat, no export. Most candidates die on the
    vs-now guard, so the head probe is rarely paid for.
    Returns (reason_or_none, seam_delta_or_none)."""
    paths = segment_files(recordings_dir, camera, win, now)
    last = frame_stats_tail(paths[-1], cancel) or frame_stats(paths[-1], cancel)
    reason = _matches_now(last, ref, rcfg, stream, win, now)  # closest to now
    if reason:
        return reason, None
    if not check_drift:
        return None, None
    head = frame_stats(paths[0], cancel)  # start of the loop
    reason = _stable_across(head, last, rcfg, stream, win, now)
    return reason, None if reason else _seam_delta(head, last)


def _rejection_summary(rejections):
    """Group repeated reasons into one operator-log summary."""
    return ", ".join(
        f"{r} ×{n}" if n > 1 else r for r, n in Counter(rejections).items()
    )


FREEZE_WINDOW_SECONDS = 10  # one clean segment + slack is all a frame needs


def _win_info(win):
    info = {
        k: round(win[k], 3) if isinstance(win[k], float) else win[k]
        for k in ("start", "end", "duration", "full", "motion_ps", "activity_fraction")
    }
    info["tier"] = TIER_NAMES[win["tier"]]
    if win.get("seam_delta") is not None:
        info["seam_delta"] = round(win["seam_delta"], 3)
    return info


def _pick_window(
    camera, stream, wins, ref, rcfg, cancel, now, recordings_dir, check_drift
):
    """Screen candidates cheaply and seam-rank equivalent survivors.

    Only the selected survivor is stitched. Freeze-frame searches still return
    the first lighting-matched candidate because a still has no loop seam.

    Per-candidate rejections log at DEBUG (job file); the operator log gets ONE
    grouped summary line per camera."""
    reasons = []
    deadline = time.monotonic() + GUARD_BUDGET_SECONDS
    picked = None
    survivors = []
    survivor_group = None
    unavailable = []
    for i, win in enumerate(wins):
        if cancel:
            cancel.check()
        if time.monotonic() > deadline:
            log.warning(
                "%s: guard budget (%ss) spent after %d/%d candidates",
                stream,
                GUARD_BUDGET_SECONDS,
                i,
                len(wins),
            )
            reasons.append(f"guard budget spent after {i}/{len(wins)} candidates")
            break
        group = (int((now - win["end"]) // 3600), win["tier"])
        if survivors and group != survivor_group:
            break  # preserve the existing recency/tier preference over seam quality
        try:
            reason, seam = screen_window_files(
                recordings_dir, camera, win, ref, rcfg, stream, now, cancel, check_drift
            )
            if reason is None:
                if not check_drift:
                    picked = win
                    break
                survivor_group = group
                survivors.append(({**win, "seam_delta": seam}, seam))
                if len(survivors) >= MAX_SEAM_CANDIDATES:
                    break
                continue
            reasons.append(reason)
        except CaptureError as exc:
            log.debug(
                "%s: candidate %s unusable: %s", stream, describe_window(win, now), exc
            )
            reasons.append(str(exc))
            unavailable.append(win)
    if survivors:
        picked, seam = min(
            survivors, key=lambda item: item[1] if item[1] is not None else float("inf")
        )
        log.info(
            "%s: seam-ranked %d candidate(s); picked endpoint delta %.1f",
            stream,
            len(survivors),
            seam if seam is not None else -1,
        )
    if reasons:
        log.info(
            "%s: rejected %d candidate(s): %s%s",
            stream,
            len(reasons),
            _rejection_summary(reasons),
            (
                f" — picked {describe_window(picked, now)}"
                if picked
                else " — no survivor (details at DEBUG in the job log)"
            ),
        )
    return picked, reasons, unavailable


def _resolve_candidates(
    api,
    camera,
    stream,
    rcfg,
    candidates,
    progress,
    now,
    target_seconds,
    min_seconds,
    recent_margin,
    max_seconds,
):
    if candidates is not None:
        if not candidates:
            raise CaptureError("no loopable window candidates (see sync plan)")
        return candidates
    progress(stream, "searching for a loopable window")
    return plan_candidate_windows(
        api,
        camera,
        target_seconds,
        min_seconds,
        rcfg["search_hours"],
        now,
        recent_margin=recent_margin,
        dilute=rcfg.get("dilute"),
        max_seconds=max_seconds,
    )


def _exportable_windows(wins, now, reasons):
    """Apply export's stricter freshness margin to direct-file fallbacks."""
    safe = [win for win in wins if now - win["end"] >= EXPORT_RECENT_MARGIN]
    skipped = len(wins) - len(safe)
    if skipped:
        reasons.append(f"{skipped} candidate(s) too recent for safe export")
        log.debug(
            "skipped %d direct-file fallback candidate(s) newer than %ss",
            skipped,
            EXPORT_RECENT_MARGIN,
        )
    return safe


def _from_exports(
    api,
    camera,
    stream,
    wins,
    tmp_dir,
    cancel,
    progress,
    now,
    reasons,
    process,
    failure,
    export_ready=None,
):
    """Fetch guarded candidates serially, cleaning each temporary export."""
    wins = _exportable_windows(wins, now, reasons)[:MAX_EXPORT_CANDIDATES]
    for win in wins:
        if cancel:
            cancel.check()
        clip = None
        try:
            clip = _fetch_window_clip(
                api,
                camera,
                stream,
                win,
                tmp_dir,
                cancel,
                progress,
                now,
                recordings_dir=None,
                export_ready=export_ready,
            )
            return process(clip, win)
        except CaptureError as exc:
            log.debug(
                "%s: export candidate %s unusable: %s",
                stream,
                describe_window(win, now),
                exc,
            )
            reasons.append(str(exc))
        finally:
            _remove(clip)
    raise CaptureError(
        f"{failure} — {len(wins)} export candidate(s) rejected "
        f"({_rejection_summary(reasons) or 'none'})"
    )


def _reference(api, camera, tmp_dir, rcfg, cancel, recordings_dir, ref):
    if ref is not None:
        return ref
    if not (rcfg["max_brightness_delta"] or rcfg.get("match_ir_mode", True)):
        return None
    return live_reference_stats(api, camera, tmp_dir, cancel, recordings_dir)


def frame_from_recordings(
    api,
    camera,
    stream,
    tmp_dir,
    rcfg,
    cancel,
    progress,
    candidates=None,
    recordings_dir=None,
    ref=None,
):
    """Event-clear frame whose lighting matches the camera RIGHT NOW.
    Returns (png_path, source_meta, window)."""
    now = time.time()
    files = files_retrieval_available(recordings_dir)
    margin = FILES_RECENT_MARGIN if files else EXPORT_RECENT_MARGIN
    wins = _resolve_candidates(
        api,
        camera,
        stream,
        rcfg,
        candidates,
        progress,
        now,
        FREEZE_WINDOW_SECONDS,
        FREEZE_WINDOW_SECONDS,
        margin,
        FREEZE_WINDOW_SECONDS,
    )
    ref = _reference(api, camera, tmp_dir, rcfg, cancel, recordings_dir, ref)
    png = os.path.join(tmp_dir, f"{stream}.frame.png")

    def extract(src, win):
        meta = summarize_probe(probe_file(src))
        if not meta["video_codec"]:
            raise CaptureError("no video stream")
        try:  # last frame preferred (closest to now); first frame as fallback
            _run(
                FFMPEG_BASE
                + ["-sseof", "-1.5", "-i", src, "-frames:v", "1", "-update", "1", png],
                60,
                cancel,
                "frame extract (tail)",
            )
        except CaptureError:
            _run(
                FFMPEG_BASE + ["-i", src, "-frames:v", "1", "-update", "1", png],
                60,
                cancel,
                "frame extract (head)",
            )
        if not os.path.isfile(png) or os.path.getsize(png) == 0:
            raise CaptureError("frame extraction failed")
        return png, meta, win

    export_wins = wins
    reasons = []
    if files:
        # a still frame cannot pulse: no intra-window drift check
        win, reasons, unavailable = _pick_window(
            camera,
            stream,
            wins,
            ref,
            rcfg,
            cancel,
            now,
            recordings_dir,
            check_drift=False,
        )
        if win is not None:
            log.info("%s: freeze source %s", stream, describe_window(win, now))
            try:
                return extract(segment_files(recordings_dir, camera, win)[-1], win)
            except CaptureError as exc:
                reasons.append(str(exc))
                log.warning(
                    "%s: direct freeze extraction failed (%s) — trying Frigate export",
                    stream,
                    exc,
                )
                export_wins = [win]
        elif unavailable:
            export_wins = unavailable
            log.warning(
                "%s: %d direct recording candidate(s) unavailable — trying "
                "Frigate export",
                stream,
                len(unavailable),
            )
        else:
            raise CaptureError(
                f"no lighting-matched event-clear frame — {len(wins)} candidate(s), "
                f"all rejected ({_rejection_summary(reasons) or 'none'}); "
                "per-candidate detail at DEBUG in the job log"
            )

    def use_export(clip, win):
        reason = _lighting_ok(clip, ref, rcfg, stream, win, now, cancel)
        if reason:
            raise CaptureError(reason)
        return extract(clip, win)

    return _from_exports(
        api,
        camera,
        stream,
        export_wins,
        tmp_dir,
        cancel,
        progress,
        now,
        reasons,
        use_export,
        "no lighting-matched event-clear frame",
    )


def source_clip_from_recordings(
    api,
    camera,
    stream,
    clips_dir,
    tmp_dir,
    seconds,
    rcfg,
    cancel,
    progress,
    candidates=None,
    recordings_dir=None,
    ref=None,
    max_seconds=None,
    want_audio=None,
    out_path=None,
    export_ready=None,
):
    """Loop-clip pipeline: candidate windows -> lighting guards (vs-now AND
    intra-window drift: a clip spanning dawn/dusk pulses every loop) -> stitch
    the winner -> audio policy -> final clip."""
    now = time.time()
    files = files_retrieval_available(recordings_dir)
    margin = FILES_RECENT_MARGIN if files else EXPORT_RECENT_MARGIN
    wins = _resolve_candidates(
        api,
        camera,
        stream,
        rcfg,
        candidates,
        progress,
        now,
        seconds,
        rcfg["min_seconds"],
        margin,
        max_seconds,
    )
    ref = _reference(api, camera, tmp_dir, rcfg, cancel, recordings_dir, ref)
    out_path = out_path or os.path.join(clips_dir, f"{stream}.dejavu.mp4")

    def finish(tmp_clip, win, audio_ready=False):
        meta = summarize_probe(probe_file(tmp_clip))
        if not meta["video_codec"]:
            raise CaptureError("no video stream")
        if meta["duration"] < 0.9 * win["duration"]:
            raise CaptureError(
                f"clip too short ({meta['duration']:.1f}s "
                f"< 90% of {win['duration']:.0f}s)"
            )
        if audio_ready:
            final_meta = meta
            has_audio = bool(meta["audio_codec"])
            if want_audio is not None and has_audio != want_audio:
                raise CaptureError(
                    "assembled clip audio does not match the active source"
                )
        else:
            progress(stream, f"audio policy: {rcfg['audio']}")
            has_audio = apply_audio_policy(
                tmp_clip, out_path, rcfg["audio"], meta, cancel, want_audio
            )
            final_meta = summarize_probe(probe_file(out_path))
        log.info(
            "%s: loop source %s -> %.0fs clip",
            stream,
            describe_window(win, now),
            final_meta["duration"],
        )
        return {
            "clip": out_path,
            "has_audio": has_audio,
            "source": final_meta,
            "clip_meta": final_meta,
            "window": _win_info(win),
            "tier": TIER_NAMES[win["tier"]],
        }

    export_wins = wins
    reasons = []
    if files:
        win, reasons, unavailable = _pick_window(
            camera,
            stream,
            wins,
            ref,
            rcfg,
            cancel,
            now,
            recordings_dir,
            check_drift=True,
        )
        if win is not None:
            tmp_clip = None
            try:
                tmp_clip = _fetch_window_via_files(
                    recordings_dir,
                    camera,
                    stream,
                    win,
                    tmp_dir,
                    cancel,
                    progress,
                    now,
                    out_path=out_path,
                    audio_policy=rcfg["audio"],
                    want_audio=want_audio,
                )
                return finish(tmp_clip, win, audio_ready=True)
            except BaseException as exc:
                _remove(tmp_clip or out_path)
                if not isinstance(exc, CaptureError):
                    raise
                reasons.append(str(exc))
                log.warning(
                    "%s: direct loop assembly failed (%s) — trying Frigate export",
                    stream,
                    exc,
                )
                export_wins = [win] + [
                    candidate for candidate in wins if candidate != win
                ]
        elif unavailable:
            export_wins = unavailable
            log.warning(
                "%s: %d direct recording candidate(s) unavailable — trying "
                "Frigate export",
                stream,
                len(unavailable),
            )
        else:
            raise CaptureError(
                f"no lighting-stable loopable window — {len(wins)} candidate(s), "
                f"all rejected ({_rejection_summary(reasons) or 'none'}); "
                "per-candidate detail at DEBUG in the job log"
            )

    def use_export(clip, win):
        reason = _lighting_ok(
            clip, ref, rcfg, stream, win, now, cancel
        ) or _stable_across(
            frame_stats(clip, cancel),
            frame_stats_tail(clip, cancel),
            rcfg,
            stream,
            win,
            now,
        )
        if reason:
            raise CaptureError(reason)
        return finish(clip, win)

    return _from_exports(
        api,
        camera,
        stream,
        export_wins,
        tmp_dir,
        cancel,
        progress,
        now,
        reasons,
        use_export,
        "no lighting-stable loopable window",
        export_ready=export_ready,
    )
