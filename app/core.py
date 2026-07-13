"""State machine, locking, debounce, and transition jobs (SPEC §6-§9).

States: off -> capturing -> applying -> on -> restoring -> off
        cancel = off during capturing; failures past validation -> error.

All coordination lives in the filesystem (state.json under an fcntl flock), so
the CLI and the REST layer — which spawns the CLI — always agree.
"""

import contextlib
import fcntl
import glob
import json
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import capture as capture_mod
import frigate as frigate_mod
import recordings as recordings_mod
from capture import Cancelled, CancelToken
from frigate import FrigateClient, FrigateError

log = logging.getLogger("dejavu.core")

TRANSITIONAL = ("capturing", "applying", "restoring")


class Busy(Exception):
    """Transition refused: another job runs, or debounced (exit code 2 / HTTP 409)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class Invalid(Exception):
    """Bad request/config (exit code 3 / HTTP 400)."""


class JobError(Exception):
    """Transition failed (exit code 1)."""


# Hard cap on stored history: the newest BACKUP_KEEP timestamped config
# backups and drift snapshots are kept; everything older is deleted at the
# start of every toggle. State CANNOT grow unbounded — at ~30 KB per backup
# the ceiling is a few hundred KB. backup.current.yaml is exempt.
BACKUP_KEEP = 10


def prune_state_snapshots(state_dir, keep=BACKUP_KEEP):
    """Enforce BACKUP_KEEP over the timestamped state snapshots.

    write_backup() drops a backup.<session>.yaml on every engage and
    write_drift() a drift.<ts>.yaml on every drift event. Keep the newest
    `keep` of each — filenames are timestamp-prefixed, so a plain sort is
    chronological. backup.current.yaml (the live restore source) is never a
    candidate.
    """
    for pattern in ("backup.*.yaml", "drift.*.yaml"):
        try:
            snaps = sorted(
                f
                for f in glob.glob(os.path.join(state_dir, pattern))
                if os.path.basename(f) != "backup.current.yaml"
            )
            for path in snaps[:-keep]:
                with contextlib.suppress(OSError):
                    os.remove(path)
        except OSError:
            pass


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# --------------------------------------------------------------------------
# persistent state
# --------------------------------------------------------------------------


def _atomic_write(path, payload, *, binary=False):
    """Write to a temporary file, then atomically replace the destination."""
    tmp = path + ".tmp"
    try:
        if binary:
            with open(tmp, "wb") as handle:
                handle.write(payload)
        else:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


class StateStore:
    def __init__(self, cfg):
        paths = cfg["paths"]
        self.state_dir = paths["state_dir"]
        self.tmp_dir = paths["tmp_dir"]
        self.clips_dir = paths["clips_local"]
        for d in (self.state_dir, self.tmp_dir):
            os.makedirs(d, exist_ok=True)
        self.state_path = os.path.join(self.state_dir, "state.json")
        self.lock_path = os.path.join(self.state_dir, "lock")
        self.streams_path = os.path.join(self.state_dir, "streams.json")
        self.backup_current = os.path.join(self.state_dir, "backup.current.yaml")
        self._thread_lock = threading.Lock()

    @contextlib.contextmanager
    def locked(self):
        with self._thread_lock:
            with open(self.lock_path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def read(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {"state": "off"}

    def _write(self, st):
        st = dict(st)
        st.pop("template_loaded", None)  # migrate retired live-swap state
        payload = json.dumps(st, indent=2, sort_keys=True)
        _atomic_write(self.state_path, payload)

    def update(self, **fields):
        """Read-modify-write; call under locked()."""
        st = self.read()
        st.update(fields)
        self._write(st)
        return st

    def update_stream(self, stream, phase):
        with self.locked():
            st = self.read()
            st.setdefault("streams", {}).setdefault(stream, {})["phase"] = phase
            self._write(st)

    def mark_completed(self, **fields):
        return self.update(
            last_completed_at=now_iso(),
            last_completed_ts=time.time(),
            job_pid=None,
            **fields,
        )

    # -- artifacts -----------------------------------------------------------

    def read_streams_record(self):
        try:
            with open(self.streams_path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return None

    def write_streams_record(self, record):
        payload = json.dumps(record, indent=2, sort_keys=True)
        _atomic_write(self.streams_path, payload)

    def clear_streams_record(self):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.streams_path)

    def write_backup(self, raw, session):
        stamped = os.path.join(self.state_dir, f"backup.{session}.yaml")
        for path in (stamped, self.backup_current):
            _atomic_write(path, raw)
        return stamped

    def read_backup(self):
        try:
            with open(self.backup_current) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def write_drift(self, raw):
        path = os.path.join(
            self.state_dir, f"drift.{time.strftime('%Y%m%d-%H%M%S')}.yaml"
        )
        _atomic_write(path, raw)
        return path

    def cleanup_clips(self):
        removed = 0
        for pattern in ("*.dejavu.mp4", "*.part.mp4", "*.upgrade.mp4"):
            for path in glob.glob(os.path.join(self.clips_dir, pattern)):
                with contextlib.suppress(OSError):
                    os.unlink(path)
                    removed += 1
        for path in glob.glob(os.path.join(self.tmp_dir, "*.frame.png")):
            with contextlib.suppress(OSError):
                os.unlink(path)
        return removed

    def cleanup_upgrade_parts(self):
        for path in glob.glob(os.path.join(self.clips_dir, "*.upgrade.mp4")):
            with contextlib.suppress(OSError):
                os.unlink(path)


def reconcile_locked(store):
    """Call under store.locked(): a transitional state whose job PID is dead
    becomes 'error' (SPEC §9)."""
    st = store.read()
    if st.get("state") in TRANSITIONAL and not pid_alive(st.get("job_pid")):
        detail = f"job (pid {st.get('job_pid')}) died during '{st.get('state')}'"
        log.warning("reconciling stale state: %s", detail)
        st = store.mark_completed(state="error", last_error=detail)
    return st


def _check_debounce(cfg, st):
    window = cfg["api"]["debounce_seconds"]
    elapsed = time.time() - float(st.get("last_completed_ts") or 0)
    if elapsed < window:
        wait = window - elapsed
        raise Busy(
            f"debounced: last transition finished {elapsed:.1f}s ago; "
            f"retry in {wait:.1f}s",
            retry_after=wait,
        )


def preflight(cfg, op):
    """API-side gate for correct HTTP codes; the spawned CLI re-checks
    authoritatively under the same lock. Returns {"noop": bool}."""
    store = StateStore(cfg)
    with store.locked():
        st = reconcile_locked(store)
        state = st.get("state", "off")
        if op == "on":
            if state == "on":
                raise Busy("dejavu already on")
            if state in TRANSITIONAL:
                raise Busy(f"busy: {state} in progress")
            if state == "error":
                raise Busy(
                    "state is 'error' — run off (restore) or force-restore first"
                )
            _check_debounce(cfg, st)
            return {"noop": False}
        if op == "off":
            if state == "off":
                return {"noop": True}
            if state in ("applying", "restoring"):
                raise Busy(f"busy: {state} in progress — retry shortly")
            if state in ("on", "error"):
                _check_debounce(cfg, st)
            return {"noop": False}
        raise Invalid(f"unknown op {op!r}")


# --------------------------------------------------------------------------
# request resolution
# --------------------------------------------------------------------------


def effective_request(
    cfg, profile=None, mode=None, cameras=None, capture_seconds=None, source=None
):
    profiles = cfg["profiles"]
    name = profile or "default"
    if name not in profiles:
        raise Invalid(
            f"unknown profile {name!r} (available: {', '.join(sorted(profiles))})"
        )
    prof = profiles[name] or {}

    mode = (mode or prof.get("mode") or "freeze").lower()
    if mode not in ("loop", "freeze"):
        raise Invalid(f"mode must be 'loop' or 'freeze' (got {mode!r})")

    source = (source or prof.get("source") or cfg["capture"]["source"]).lower()
    if source not in ("recordings", "restream"):
        raise Invalid(f"source must be 'recordings' or 'restream' (got {source!r})")

    if cameras is None:
        cameras = list(prof.get("cameras") or [])
    seconds = (
        capture_seconds or prof.get("capture_seconds") or cfg["capture"]["seconds"]
    )
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 1:
        raise Invalid(f"capture seconds must be a positive integer (got {seconds!r})")

    return {
        "profile": name,
        "mode": mode,
        "cameras": cameras,
        "seconds": seconds,
        "source": source,
    }


def build_plan(cfg, client, req):
    raw = client.get_raw_config()
    y, data = frigate_mod.parse_config(raw)
    plan, notes = frigate_mod.resolve_streams(data, cfg, req["cameras"])
    if notes["unknown_patterns"]:
        raise Invalid(
            "no camera/stream matches for: " + ", ".join(notes["unknown_patterns"])
        )
    return raw, y, data, plan, notes


def _log_notes(notes):
    if notes["unsupported_cameras"]:
        log.warning(
            "cameras without a go2rtc restream input (activation will be refused): %s",
            ", ".join(notes["unsupported_cameras"]),
        )
    if notes["excluded"]:
        log.info(
            "excluded by streams.include/exclude: %s", ", ".join(notes["excluded"])
        )
    if notes["missing_streams"]:
        log.warning(
            "referenced by cameras but missing from go2rtc.streams "
            "(activation will be refused): %s",
            ", ".join(notes["missing_streams"]),
        )


# --------------------------------------------------------------------------
# transition job
# --------------------------------------------------------------------------


class Job:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = StateStore(cfg)
        self.client = FrigateClient(cfg)
        self.cancel = CancelToken()
        self.phase = "init"

    def install_signal_handlers(self):
        def handler(signum, _frame):
            if self.phase == "capturing":
                log.warning("received signal %s: cancelling capture", signum)
                self.cancel.cancel()
            else:
                log.warning(
                    "received signal %s during %r — ignored (transition "
                    "runs to completion)",
                    signum,
                    self.phase,
                )

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    # -- restart + verify (SPEC §8) ------------------------------------------

    def _restart(self):
        self.client.restart_api()
        self.client.wait_down(30)

    def _wait_healthy_or_fail(self):
        timeout = self.cfg["frigate"]["health_timeout_seconds"]
        log.info("waiting for frigate to come back (up to %ss)...", timeout)
        if not self.client.wait_healthy(timeout):
            raise JobError(
                f"frigate did not become healthy within {timeout}s after "
                "restart — restart Frigate externally, then run 'off' "
                "or 'force-restore'"
            )

    def restart_and_verify(self, expect_clips, direction):
        """Restart frigate, wait healthy, verify go2rtc picked up the change;
        automatically roll back a failed activation when the API is available."""
        timeout = self.cfg["frigate"]["health_timeout_seconds"]
        log.info("restarting frigate via API...")
        self._restart()

        if not self.client.wait_healthy(timeout):
            # Slow boots happen (tensorrt model load took ~3 min once) — a
            # timed-out gate is a reason to wait harder, not to panic. Only a
            # truly dark API is unrecoverable from here.
            log.warning(
                "health gate (%ss) expired — extending wait for the API...", timeout
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not self.client.is_up():
                time.sleep(3)
            if not self.client.is_up():
                raise JobError(
                    f"frigate API did not come back within {2 * timeout}s "
                    "after restart — restart Frigate externally, then "
                    "run 'off' or 'force-restore'"
                )
            log.info("frigate API is back; waiting for go2rtc...")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not self.client.go2rtc_up():
                time.sleep(3)
            if not self.client.go2rtc_up():
                # API fine, go2rtc persistently down = bad generated config
                # (crash-loop). Roll the swap back instead of staying dark.
                if direction != "on":
                    raise JobError(
                        "go2rtc did not come back after restore restart "
                        "— run 'force-restore'"
                    )
                log.error("go2rtc is not coming up — rolling back the swap")
                try:
                    result = self.surgical_restore()
                    if result["changed"]:
                        self._restart()
                        self._wait_healthy_or_fail()
                except (FrigateError, JobError) as rb_exc:
                    raise JobError(
                        "go2rtc never came up AND automatic rollback "
                        f"failed: {rb_exc} — run 'force-restore'"
                    ) from rb_exc
                raise JobError(
                    "go2rtc did not come up with the dejavu config; "
                    "rolled back to the original config"
                )
            log.info("frigate healthy after extended wait — verifying")

        if not expect_clips:
            return

        verify = (
            frigate_mod.verify_dejavu_applied
            if direction == "on"
            else frigate_mod.verify_dejavu_removed
        )
        ok, bad = verify(self.client, expect_clips)
        if ok:
            log.info(
                "go2rtc verified: %s streams %s",
                len(expect_clips),
                "serving dejavu clips" if direction == "on" else "restored to live",
            )
            return

        if direction == "on":
            log.error(
                "verification failed for %s — rolling back to original sources",
                ", ".join(bad),
            )
            try:
                res = self.surgical_restore()
                if res["changed"]:
                    self._restart()
                    self._wait_healthy_or_fail()
            except (FrigateError, JobError) as exc:
                raise JobError(
                    f"go2rtc did not apply dejavu sources for {', '.join(bad)} "
                    f"AND rollback failed: {exc} — run force-restore"
                ) from exc
            raise JobError(
                f"go2rtc did not apply dejavu sources for: {', '.join(bad)}; "
                "rolled back to the original config"
            )
        raise JobError(
            "restore verification failed — still serving dejavu clips: "
            + ", ".join(bad)
        )

    # -- surgical restore (SPEC §7.2) -----------------------------------------

    def surgical_restore(self):
        """Swap recorded streams back to their originals inside the CURRENT
        config (other user edits preserved). Returns summary dict."""
        record = self.store.read_streams_record()
        if not record or not record.get("streams"):
            return {"changed": False, "restored": [], "drifted": [], "missing": []}

        raw = self.client.get_raw_config()
        y, data = frigate_mod.parse_config(raw)

        backup_raw = self.store.read_backup()
        backup_data = None
        if backup_raw:
            try:
                _, backup_data = frigate_mod.parse_config(backup_raw)
            except FrigateError as exc:
                log.warning(
                    "pristine backup unparseable (%s); restoring from recorded "
                    "source strings",
                    exc,
                )

        changed, restored, drifted, missing = frigate_mod.graft_restore(
            data, backup_data, record["streams"]
        )

        if drifted:
            drift_path = self.store.write_drift(raw)
            log.warning(
                "streams hand-edited while dejavu on (drifted copy saved to %s): %s",
                drift_path,
                ", ".join(drifted),
            )
        if missing:
            log.warning(
                "streams no longer present in config (skipped): %s", ", ".join(missing)
            )

        if changed:
            self.client.save_config(frigate_mod.dump_config(y, data))
            log.info("original sources restored for: %s", ", ".join(restored))
        return {
            "changed": changed,
            "restored": restored,
            "drifted": drifted,
            "missing": missing,
        }


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# The freeze ladder (SPEC §5c). Dejavu prepares rung 1 for every stream before
# activation. There is no "skip", and no whole-job "abort" for a camera that
# will not yield a frame. Each rung down is a cosmetic concession:
#
#   1 live      one frame of NOW. Instant; lighting-correct by construction.
#   2 recorded  newest lighting-matched event-clear frame (camera offline now).
#   3 cached    the last frame this camera ever froze on (offline for a while).
#   4 black     nothing exists anywhere. Loudly logged.
#
# Rung 1 also produces the lighting REFERENCE used to screen recorded loop
# candidates against — the reference frame the guards needed anyway.
# --------------------------------------------------------------------------


def _cache_path(cfg, stream):
    return os.path.join(cfg["paths"]["state_dir"], "lastframe", f"{stream}.png")


def _cache_meta_path(cfg, stream):
    return os.path.join(cfg["paths"]["state_dir"], "lastframe", f"{stream}.json")


_CACHE_META_FIELDS = (
    "video_codec",
    "width",
    "height",
    "pix_fmt",
    "fps",
    "audio_codec",
    "sample_rate",
    "channels",
)


def _remember_frame(cfg, stream, png, source_meta):
    try:
        dest = _cache_path(cfg, stream)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(png, "rb") as src:
            _atomic_write(dest, src.read(), binary=True)
        payload = {key: source_meta.get(key) for key in _CACHE_META_FIELDS}
        # The mtime binds the sidecar to this exact atomic frame write. If a
        # crash lands between the two replaces, stale metadata is ignored.
        payload["frame_mtime_ns"] = os.stat(dest).st_mtime_ns
        _atomic_write(
            _cache_meta_path(cfg, stream), json.dumps(payload, sort_keys=True)
        )
    except OSError as exc:
        log.debug("%s: could not cache frame/metadata: %s", stream, exc)


def _cached_frame_meta(cfg, stream):
    frame = _cache_path(cfg, stream)
    try:
        with open(_cache_meta_path(cfg, stream), encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("frame_mtime_ns") != os.stat(frame).st_mtime_ns:
            log.debug(
                "%s: cached frame metadata is stale; using compatibility defaults",
                stream,
            )
            return None
        return {key: payload.get(key) for key in _CACHE_META_FIELDS}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        log.debug("%s: cached frame metadata unavailable: %s", stream, exc)
        return None


def _freeze_from_png(cfg, job, stream, png, src_meta):
    out = os.path.join(cfg["paths"]["clips_local"], f"{stream}.dejavu.mp4")
    job.store.update_stream(stream, "synthesizing freeze clip")
    capture_mod.synthesize_freeze(
        png, out, src_meta, cfg["capture"]["freeze_clip_seconds"], job.cancel
    )
    clip_meta = capture_mod.validate_clip(out, cfg["capture"]["freeze_clip_seconds"])
    return {
        "clip": out,
        "has_audio": bool(clip_meta["audio_codec"]),
        "source": src_meta,
        "clip_meta": clip_meta,
    }


def _engage_freeze(cfg, req, job, store, stream, meta):
    """Rungs 1-4. Always returns a result; raises only on cancellation.
    On the live rung it also carries `ref` — the frame's lighting stats."""
    cap, paths = cfg["capture"], cfg["paths"]
    api = cfg["frigate"]["api_url"].rstrip("/")
    query = f"?{cap['rtsp_query']}" if cap["rtsp_query"] else ""
    url = f"{cfg['frigate']['restream_url'].rstrip('/')}/{stream}{query}"
    camera = (meta.get("record_cameras") or [None])[0]
    reasons = []

    # rung 1 — a frame of now
    t0 = time.monotonic()
    try:
        log.debug("%s: rung 1 (live): probing %s", stream, url)
        png, src_meta = capture_mod.frame_from_live(
            stream, url, paths["tmp_dir"], job.cancel, store.update_stream
        )
        try:
            ref = recordings_mod.frame_stats(png, job.cancel)
            result = _freeze_from_png(cfg, job, stream, png, src_meta)
            _remember_frame(cfg, stream, png, src_meta)
            log.debug(
                "%s: rung 1 (live) OK in %.1fs — %sx%s %s, luma=%.0f " "chroma=%.1f",
                stream,
                time.monotonic() - t0,
                src_meta.get("width"),
                src_meta.get("height"),
                src_meta.get("video_codec"),
                ref["luma"],
                ref["chroma"],
            )
            return {**result, "clip_source": "live", "rung": "live", "ref": ref}
        finally:
            if os.path.exists(png):
                os.unlink(png)
    except Cancelled:
        raise
    except capture_mod.CaptureError as exc:
        reasons.append(f"live: {exc}")
        log.warning(
            "%s: no live frame (%s) — camera offline? descending the " "freeze ladder",
            stream,
            exc,
        )

    # rung 2 — newest lighting-matched event-clear recorded frame
    if camera and req["source"] == "recordings":
        try:
            log.debug(
                "%s: rung 2 (recorded): searching %s's recordings", stream, camera
            )
            png, src_meta, window = recordings_mod.frame_from_recordings(
                api,
                camera,
                stream,
                paths["tmp_dir"],
                cap["recordings"],
                job.cancel,
                store.update_stream,
                recordings_dir=paths["recordings_dir"],
            )
            try:
                result = _freeze_from_png(cfg, job, stream, png, src_meta)
                _remember_frame(cfg, stream, png, src_meta)
                return {
                    **result,
                    "clip_source": "recordings",
                    "rung": "recorded",
                    "window": window,
                    "ref": None,
                }
            finally:
                if os.path.exists(png):
                    os.unlink(png)
        except Cancelled:
            raise
        except capture_mod.CaptureError as exc:
            reasons.append(f"recordings: {exc}")

    # rung 3 — the last frame this camera ever froze on
    cached = _cache_path(cfg, stream)
    cached_meta = _cached_frame_meta(cfg, stream)
    if os.path.isfile(cached):
        try:
            age = (time.time() - os.path.getmtime(cached)) / 3600
            log.warning(
                "%s: freezing on the CACHED frame (%.1fh old) — no live "
                "frame and no usable recording",
                stream,
                age,
            )
            result = _freeze_from_png(
                cfg, job, stream, cached, cached_meta or capture_mod.PLACEHOLDER_META
            )
            return {
                **result,
                "clip_source": "cache",
                "rung": "cached",
                "ref": None,
                "degraded": "; ".join(reasons),
            }
        except Cancelled:
            raise
        except capture_mod.CaptureError as exc:
            reasons.append(f"cache: {exc}")

    # rung 4 — the floor
    log.error(
        "%s: NO imagery available (%s) — serving a BLACK frame. Dejavu is "
        "intact; the stream will look dead.",
        stream,
        "; ".join(reasons),
    )
    out = os.path.join(paths["clips_local"], f"{stream}.dejavu.mp4")
    has_audio = capture_mod.synthesize_placeholder(
        out, cached_meta, cap["freeze_clip_seconds"], job.cancel
    )
    clip_meta = capture_mod.validate_clip(out, cap["freeze_clip_seconds"])
    return {
        "clip": out,
        "has_audio": has_audio,
        "source": capture_mod.PLACEHOLDER_META,
        "clip_meta": clip_meta,
        "clip_source": "placeholder",
        "rung": "black",
        "ref": None,
        "degraded": "; ".join(reasons),
    }


def _make_freeze_task(cfg, req, job, store, stream, meta):
    """One pre-activation task per stream that always yields a safe clip."""

    def task():
        if req["source"] == "restream" and req["mode"] == "loop":
            cap, paths = cfg["capture"], cfg["paths"]  # explicit live-loop: no ladder
            query = f"?{cap['rtsp_query']}" if cap["rtsp_query"] else ""
            url = f"{cfg['frigate']['restream_url'].rstrip('/')}/{stream}{query}"
            result = capture_mod.capture_stream(
                stream,
                url,
                paths["clips_local"],
                paths["tmp_dir"],
                "loop",
                req["seconds"],
                cap["freeze_clip_seconds"],
                job.cancel,
                store.update_stream,
            )
            return {
                **result,
                "clip_source": "restream",
                "rung": "live-loop",
                "ref": None,
            }
        return _engage_freeze(cfg, req, job, store, stream, meta)

    return task


def _make_upgrade_task(cfg, req, job, store, stream, meta, ref, want_audio):
    """Stage a loop from this stream's recent past for atomic promotion.

    Returns None when nothing beats the freeze frame.
    """
    cap, paths = cfg["capture"], cfg["paths"]
    rcfg = cap["recordings"]
    api = cfg["frigate"]["api_url"].rstrip("/")
    camera = (meta.get("record_cameras") or [None])[0]
    candidates = meta.get("window_candidates")  # anchor-ordered by the sync pre-pass

    def task():
        if not camera:
            return None
        # staged alongside the live clip so the final promote is a same-directory
        # Same-directory promotion is atomic; Frigate still has its original
        # config and consumers have not opened this clip yet.
        part = os.path.join(paths["clips_local"], f"{stream}.upgrade.mp4")
        try:
            result = recordings_mod.source_clip_from_recordings(
                api,
                camera,
                stream,
                paths["clips_local"],
                paths["tmp_dir"],
                req["seconds"],
                rcfg,
                job.cancel,
                store.update_stream,
                candidates=candidates,
                recordings_dir=paths["recordings_dir"],
                ref=ref,
                max_seconds=cap["max_loop_seconds"],
                want_audio=want_audio,
                out_path=part,
            )
            return {**result, "clip_source": "recordings", "rung": "loop"}
        except Cancelled:
            raise
        except capture_mod.CaptureError as exc:
            log.info("%s: staying on the freeze frame — %s", stream, exc)
            if os.path.exists(part):
                os.unlink(part)
            return None

    return task


RUNG_NOTE = {
    "live": "frame of now",
    "recorded": "recorded frame (camera offline)",
    "cached": "CACHED frame — camera offline and nothing usable recorded",
    "black": "BLACK frame — no imagery exists",
    "loop": "loop of its own recent past",
    "live-loop": "live capture",
    "restream": "live capture",
}


def _log_ladder(results):
    """One line per stream: which rung it landed on, and why that matters."""
    by_rung = {}
    for stream, r in sorted(results.items()):
        by_rung.setdefault(r.get("rung", "?"), []).append(stream)
    for rung, streams in by_rung.items():
        note = RUNG_NOTE.get(rung, rung)
        emit = (
            log.error
            if rung == "black"
            else (log.warning if rung == "cached" else log.info)
        )
        emit(
            "prepared on %s [%s]: %d stream(s): %s",
            rung,
            note,
            len(streams),
            ", ".join(streams),
        )


# --------------------------------------------------------------------------
# pre-activation loop search and atomic promotion
# --------------------------------------------------------------------------


def _start_upgrades(cfg, req, job, store, plan, engaged):
    """Kick off one loop search per stream. The clips they produce are staged,
    not published: promotion happens under _promote_upgrades."""
    paths = cfg["paths"]
    if not recordings_mod.files_retrieval_available(paths["recordings_dir"]):
        # export retrieval only: /api/version answers minutes before the export
        # machinery does after a restart. Freeze preparation never touches it,
        # so this gate delays activation but remains cancellable because config
        # is untouched.
        api = cfg["frigate"]["api_url"].rstrip("/")
        if not recordings_mod.wait_exports_ready(
            api, cfg["frigate"]["settle_timeout_seconds"], job.cancel
        ):
            log.warning(
                "frigate export API never settled — no recordings loop search "
                "this session; streams will use their freeze fallbacks"
            )
            return None, {}
    else:
        log.info("retrieval: direct segment reads from %s", paths["recordings_dir"])

    _plan_synced_windows(cfg, req, plan)
    pool = ThreadPoolExecutor(
        max_workers=max(1, cfg["capture"]["parallel"]), thread_name_prefix="upgrade"
    )
    futures = {}
    for stream, meta in plan.items():
        r = engaged.get(stream)
        if not r:
            continue
        task = _make_upgrade_task(
            cfg, req, job, store, stream, meta, r.get("ref"), r["has_audio"]
        )
        futures[stream] = pool.submit(task)
    log.info(
        "searching for loop windows on %d stream(s) before activation", len(futures)
    )
    return pool, futures


def _collect_upgrades(futures, allowed):
    """Wait for every parallel loop search and harvest successful candidates."""
    upgrades = {}
    for stream, fut in futures.items():
        try:
            result = fut.result()
        except Cancelled:
            continue
        except Exception as exc:  # noqa: BLE001 — an upgrade never fails the job
            log.warning(
                "%s: loop search failed (%s) — staying on the freeze frame", stream, exc
            )
            continue
        if result and stream in allowed:
            upgrades[stream] = result
    return upgrades


def _promote_upgrades(upgrades, record):
    """Publish staged loop clips over their freeze clips atomically."""
    promoted = []
    promoted_streams = []
    for stream, r in sorted(upgrades.items()):
        final = os.path.join(os.path.dirname(r["clip"]), f"{stream}.dejavu.mp4")
        try:
            os.replace(r["clip"], final)
        except OSError as exc:
            log.warning(
                "%s: could not promote loop clip (%s) — freeze frame stands",
                stream,
                exc,
            )
            continue
        if stream in record:
            record[stream].update(
                clip_source="recordings",
                rung="loop",
                mode="loop",
                window=r.get("window"),
            )
        promoted.append(
            f"{stream} ({r.get('tier')}, {r['clip_meta']['duration']:.0f}s)"
        )
        promoted_streams.append(stream)
    if promoted:
        log.info(
            "loop clips promoted for %d stream(s): %s",
            len(promoted),
            ", ".join(promoted),
        )
    return promoted_streams


def _stream_phase(rec):
    return f"on ({rec.get('rung') or rec.get('mode')}, {rec.get('clip_source')})"


def _plan_synced_windows(cfg, req, plan):
    """Pre-pass: pick every camera's candidate windows against one common
    anchor time so overlapping views loop the SAME moment (frigate's server
    timeline — the only clock all recordings share). Populates
    plan[stream]['window_candidates'].

    Freeze needs no sync pre-pass any more: every stream freezes on a frame of
    NOW, which is the same moment on every camera by construction."""
    cap = cfg["capture"]
    rcfg = cap["recordings"]
    api = cfg["frigate"]["api_url"].rstrip("/")
    camera_needs = {}
    for meta in plan.values():
        for cam in (meta.get("record_cameras") or [])[:1]:
            camera_needs.setdefault(cam, req["seconds"])
    if not camera_needs:
        return

    files = recordings_mod.files_retrieval_available(cfg["paths"]["recordings_dir"])
    margin = (
        recordings_mod.FILES_RECENT_MARGIN
        if files
        else recordings_mod.EXPORT_RECENT_MARGIN
    )
    tolerance = rcfg["sync_tolerance_minutes"] * 60
    anchor, ordered, errors = recordings_mod.select_synced_windows(
        api,
        camera_needs,
        rcfg["min_seconds"],
        rcfg["search_hours"],
        tolerance,
        recent_margin=margin,
        dilute=rcfg.get("dilute"),
        max_seconds=cap["max_loop_seconds"],
    )

    for stream, meta in plan.items():
        cams = meta.get("record_cameras") or []
        if cams and cams[0] in ordered:
            meta["window_candidates"] = ordered[cams[0]]
    for cam, err in errors.items():
        log.warning("sync plan: %s has no candidate windows (%s)", cam, err)

    if anchor and tolerance:
        synced = sum(
            1
            for cam, wins in ordered.items()
            if wins
            and (
                abs(wins[0]["end"] - anchor) <= tolerance
                or wins[0]["start"] <= anchor <= wins[0]["end"]
            )
        )
        log.info(
            "sync anchor %s — %d/%d cameras within ±%dmin",
            datetime.fromtimestamp(anchor).strftime("%H:%M:%S"),
            synced,
            len(camera_needs),
            rcfg["sync_tolerance_minutes"],
        )
    return anchor


def cmd_on(
    cfg,
    profile=None,
    mode=None,
    cameras=None,
    capture_seconds=None,
    source=None,
    dry_run=False,
):
    req = effective_request(cfg, profile, mode, cameras, capture_seconds, source)
    job = Job(cfg)
    cap = cfg["capture"]
    paths = cfg["paths"]
    query = f"?{cap['rtsp_query']}" if cap["rtsp_query"] else ""

    if dry_run:
        raw, y, data, plan, notes = build_plan(cfg, job.client, req)
        _print_dry_run(cfg, data, req, plan, notes, query)
        return 0

    store = job.store
    with store.locked():
        st = reconcile_locked(store)
        state = st.get("state", "off")
        if state == "on":
            raise Busy("dejavu already on — run 'off' first")
        if state in TRANSITIONAL:
            raise Busy(f"busy: {state} in progress")
        if state == "error":
            raise Busy(
                "state is 'error' — run 'off' (restore) or 'force-restore' first"
            )
        _check_debounce(cfg, st)
        session = time.strftime("%Y%m%d-%H%M%S") + f"-{req['mode']}-{req['profile']}"
        store.update(
            state="capturing",
            job_pid=os.getpid(),
            session=session,
            profile=req["profile"],
            mode=req["mode"],
            capture_seconds=req["seconds"],
            since=now_iso(),
            streams={},
            last_error=None,
            note=None,
        )

    job.phase = "capturing"
    job.install_signal_handlers()
    log.info(
        "dejavu ON requested: profile=%s mode=%s capture=%ss session=%s",
        req["profile"],
        req["mode"],
        req["seconds"],
        session,
    )

    config_touched = False
    pool = None  # loop-search pool; the finally reaps it on every exit
    try:
        raw, y, data, plan, notes = build_plan(cfg, job.client, req)
        _log_notes(notes)
        # Explicit include/exclude rails intentionally control scope. A targeted
        # camera that cannot be replaced is different: accepting that partial
        # plan would report ON while a requested camera was still live.
        blockers = frigate_mod.resolution_blockers(notes)
        if blockers:
            raise JobError("privacy activation refused; " + "; ".join(blockers))
        if not plan:
            raise JobError("no go2rtc streams resolved for this request")

        job.store.write_backup(raw, session)
        store.cleanup_clips()  # stale leftovers from previous sessions

        with store.locked():
            store.update(
                streams={
                    s: {"phase": "pending", "cameras": meta["cameras"]}
                    for s, meta in plan.items()
                }
            )

        # Prepare a safe clip for every stream before touching Frigate.
        tasks = {
            s: _make_freeze_task(cfg, req, job, store, s, meta)
            for s, meta in plan.items()
        }
        log.info(
            "preparing dejavu for %d stream(s) [%s/%s]: %s",
            len(tasks),
            req["mode"],
            req["source"],
            ", ".join(sorted(tasks)),
        )
        results = capture_mod.run_captures(
            tasks, cap["parallel_frames"], job.cancel, store.update_stream
        )

        ok = {s: r for s, r in results.items() if r["ok"]}
        failed = {s: r["error"] for s, r in results.items() if not r["ok"]}
        if failed:
            # the ladder bottoms out in a synthesized frame, so anything here is
            # a bug or a cancellation — never "the camera wouldn't cooperate"
            log.error(
                "preparation failed for %s — aborting before the config swap",
                "; ".join(f"{s}: {e}" for s, e in sorted(failed.items())),
            )
            raise JobError(
                "privacy activation refused; preparation failed for: "
                + "; ".join(f"{s}: {e}" for s, e in sorted(failed.items()))
            )
        _log_ladder(ok)

        # Search for loop candidates before touching Frigate's config. Searches
        # still run in parallel; a failed search leaves that stream on its
        # already-prepared freeze clip. This remains cancellable.
        upgrades, futures = {}, {}
        want_loops = (
            req["mode"] == "loop"
            and req["source"] == "recordings"
            and not job.cancel.cancelled
        )
        if want_loops:
            pool, futures = _start_upgrades(cfg, req, job, store, plan, ok)
            want_loops = bool(futures)  # nothing to search (no cameras / no export)
            if want_loops:
                upgrades = _collect_upgrades(futures, ok)

        # From this boundary onward the persisted config may change, so signals
        # are ignored and the transition must run through restart/verification.
        with store.locked():
            if job.cancel.cancelled:
                raise Cancelled()
            job.phase = "applying"
            store.update(state="applying")

        # fresh fetch so config edits made during capture are preserved
        raw2 = job.client.get_raw_config()
        y2, data2 = frigate_mod.parse_config(raw2)
        smap = frigate_mod.go2rtc_streams_map(data2)

        replacements, record = {}, {}
        for s, r in sorted(ok.items()):
            if s not in smap:
                log.warning(
                    "stream %s vanished from config during capture — "
                    "activation will be refused",
                    s,
                )
                failed[s] = "stream removed from config during capture"
                continue
            try:
                kind, sources = frigate_mod.normalize_sources(smap[s])
            except FrigateError as exc:
                log.warning(
                    "stream %s has an unsupported source shape (%s) — "
                    "activation will be refused",
                    s,
                    exc,
                )
                failed[s] = str(exc)
                continue
            extras = frigate_mod.audio_extras(sources)
            src = frigate_mod.build_dejavu_source(
                paths["clips_frigate"], s, r["has_audio"], extras
            )
            replacements[s] = src
            record[s] = {
                "original_kind": kind,
                "original_sources": sources,
                "dejavu_source": src,
                "clip": f"{paths['clips_frigate'].rstrip('/')}/{frigate_mod.clip_name(s)}",
                "has_audio": r["has_audio"],
                "cameras": plan[s]["cameras"],
                "mode": req["mode"],
                "clip_source": r.get("clip_source", "restream"),
                "rung": r.get("rung"),
                "window": r.get("window"),
            }
        if failed:
            raise JobError(
                "privacy activation refused; stream set changed during capture: "
                + "; ".join(f"{s}: {e}" for s, e in sorted(failed.items()))
            )
        if not replacements:
            raise JobError("no capturable streams left to replace")

        if upgrades:
            promoted = _promote_upgrades(
                {s: result for s, result in upgrades.items() if s in replacements},
                record,
            )
            log.info(
                "loop search completed before activation (%d/%d promoted)",
                len(promoted),
                len(replacements),
            )
        elif want_loops:
            log.info("no stream found a suitable loop; using freeze frames")
        if pool is not None:
            pool.shutdown(wait=True)
            store.cleanup_upgrade_parts()
            pool = None

        frigate_mod.apply_dejavu(data2, replacements)
        new_raw = frigate_mod.dump_config(y2, data2)
        store.write_streams_record(
            {"session": session, "created_at": now_iso(), "streams": record}
        )

        log.info(
            "saving dejavu config (%d stream(s) replaced, persistence layer)...",
            len(replacements),
        )
        job.client.save_config(new_raw)  # 400 => raises; nothing persisted
        config_touched = True

        expect = {s: frigate_mod.clip_name(s) for s in replacements}
        job.restart_and_verify(expect, direction="on")

        stream_status = {
            s: {"phase": _stream_phase(record[s]), "cameras": record[s]["cameras"]}
            for s in replacements
        }
        with store.locked():
            store.mark_completed(
                state="on",
                streams=stream_status,
                last_error=None,
                note="restart=frigate-api",
            )
        log.info(
            "dejavu is ON after coordinated restart (%d stream(s))", len(replacements)
        )
        for s in sorted(replacements):
            rec = record[s]
            win = rec.get("window") or {}
            detail = (
                f" — {win['tier']} {win['duration']:.0f}s window"
                if win.get("tier")
                else ""
            )
            log.info(
                "  %-34s %s/%s%s",
                s,
                rec.get("rung") or req["mode"],
                rec["clip_source"],
                detail,
            )
        return 0

    except Cancelled:
        log.info("cancelled during capture — cleaning up partial clips")
        store.cleanup_clips()
        store.clear_streams_record()
        with store.locked():
            store.mark_completed(
                state="off",
                streams={},
                last_error=None,
                note=f"capture cancelled at {now_iso()}",
            )
        log.info("dejavu remains OFF (frigate untouched)")
        return 0
    except Invalid:
        with store.locked():
            store.mark_completed(state="off", streams={})
        raise
    except (JobError, FrigateError) as exc:
        _fail_job(store, config_touched, exc)
        raise JobError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - never leave a wedged transitional state
        log.exception("unexpected failure during 'on'")
        _fail_job(store, config_touched, exc)
        raise JobError(f"unexpected: {exc}") from exc
    finally:
        # A pool still set here means an error fired after loop searches launched
        # (success paths clear it). Its worker threads are non-daemon
        # and would block interpreter exit on in-flight ffmpeg searches; cancel
        # (terminates the registered procs) and shut down without waiting.
        if pool is not None:
            job.cancel.cancel()
            pool.shutdown(wait=False)
            store.cleanup_upgrade_parts()


def _fail_job(store, config_touched, exc):
    with store.locked():
        if config_touched:
            # frigate config was modified; needs operator attention (off/force-restore)
            store.mark_completed(state="error", last_error=str(exc))
        else:
            # nothing persisted; clips kept for inspection (SPEC §6.6)
            store.mark_completed(state="off", last_error=str(exc))


def cmd_off(cfg):
    job = Job(cfg)
    store = job.store

    with store.locked():
        st = reconcile_locked(store)
        state = st.get("state", "off")
        if state == "off":
            log.info("dejavu already off")
            return 0
        if state in ("applying", "restoring"):
            raise Busy(f"busy: {state} in progress — retry shortly")
        if state == "capturing":
            pid = st.get("job_pid")
            log.info("cancelling in-flight capture (job pid %s)", pid)
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, TypeError, ValueError):
                pass  # next reconcile turns it into 'error'
            cancelling = True
        else:  # on / error
            _check_debounce(cfg, st)
            store.update(state="restoring", job_pid=os.getpid())
            cancelling = False

    if cancelling:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            time.sleep(0.5)
            with store.locked():
                st = reconcile_locked(store)
            state = st.get("state")
            if state == "off":
                log.info("capture cancelled; dejavu remains OFF")
                return 0
            if state != "capturing":
                log.error(
                    "capture finished before the cancel landed (state=%s) — "
                    "run 'off' again to restore",
                    state,
                )
                raise JobError(f"cancel raced completion; state is now {state!r}")
        raise JobError("cancel timed out waiting for the capture job to exit")

    job.phase = "restoring"
    job.install_signal_handlers()
    try:
        record = store.read_streams_record() or {}
        expect = {s: frigate_mod.clip_name(s) for s in (record.get("streams") or {})}
        result = job.surgical_restore()  # config/persistence layer (template stays)

        restarted = False
        if expect:
            job.restart_and_verify(expect, direction="off")
            restarted = True
        elif result["changed"]:
            job.restart_and_verify({}, direction="off")
            restarted = True
        else:
            log.info("config already clean — nothing to restore")

        if not cfg["capture"]["keep_clips_after_off"]:
            removed = store.cleanup_clips()
            log.info("removed %d clip file(s)", removed)
        store.clear_streams_record()

        notes = ["restart=frigate-api"] if restarted else []
        if result["drifted"]:
            notes.append(
                "drift detected during restore: " + ", ".join(result["drifted"])
            )
        with store.locked():
            store.mark_completed(
                state="off",
                streams={},
                last_error=None,
                profile=None,
                mode=None,
                session=None,
                since=None,
                note="; ".join(notes) or None,
            )
        log.info(
            "dejavu is OFF via %s (restored %d stream(s)%s)",
            "api restart" if restarted else "config",
            len(result["restored"]),
            "; DRIFT: " + ", ".join(result["drifted"]) if result["drifted"] else "",
        )
        return 0
    except (JobError, FrigateError) as exc:
        with store.locked():
            store.mark_completed(state="error", last_error=str(exc))
        raise JobError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected failure during 'off'")
        with store.locked():
            store.mark_completed(state="error", last_error=str(exc))
        raise JobError(f"unexpected: {exc}") from exc


def cmd_cancel(cfg):
    store = StateStore(cfg)
    with store.locked():
        st = reconcile_locked(store)
    if st.get("state") != "capturing":
        log.info("nothing to cancel (state=%s)", st.get("state"))
        return 0
    return cmd_off(cfg)


def cmd_force_restore(cfg):
    """Disaster fallback: write the pristine whole-file backup verbatim."""
    job = Job(cfg)
    store = job.store
    with store.locked():
        st = reconcile_locked(store)
        state = st.get("state", "off")
        if state == "capturing":
            raise Busy("capture in progress — run 'off' (cancel) first")
        if state in ("applying", "restoring"):
            raise Busy(f"busy: {state} in progress")
        backup = store.read_backup()
        if backup is None:
            raise Invalid("no pristine backup on record (state/backup.current.yaml)")
        store.update(state="restoring", job_pid=os.getpid())

    job.phase = "restoring"
    job.install_signal_handlers()
    try:
        log.info("force-restore: writing pristine backup verbatim")
        job.client.save_config(backup)
        record = store.read_streams_record() or {}
        expect = {s: frigate_mod.clip_name(s) for s in (record.get("streams") or {})}
        job.restart_and_verify(expect, direction="off")
        if not cfg["capture"]["keep_clips_after_off"]:
            store.cleanup_clips()
        store.clear_streams_record()
        with store.locked():
            store.mark_completed(
                state="off",
                streams={},
                last_error=None,
                profile=None,
                mode=None,
                session=None,
                since=None,
                note="force-restore applied",
            )
        log.info("force-restore complete; dejavu is OFF")
        return 0
    except (JobError, FrigateError) as exc:
        with store.locked():
            store.mark_completed(state="error", last_error=str(exc))
        raise JobError(str(exc)) from exc


# --------------------------------------------------------------------------
# status / profiles / dry-run
# --------------------------------------------------------------------------


def get_status(cfg, live=True):
    store = StateStore(cfg)
    with store.locked():
        st = reconcile_locked(store)

    snapshot = {
        "state": st.get("state", "off"),
        "profile": st.get("profile"),
        "mode": st.get("mode"),
        "since": st.get("since"),
        "session": st.get("session"),
        "capture_seconds": st.get("capture_seconds"),
        "streams": st.get("streams") or {},
        "last_error": st.get("last_error"),
        "note": st.get("note"),
        "drift_detected": False,
    }

    if live:
        client = FrigateClient(cfg)
        version = client.version()
        snapshot["frigate"] = {
            "reachable": version is not None,
            "version": version,
            "go2rtc": client.go2rtc_up() if version is not None else False,
        }
        if snapshot["state"] == "on" and version is not None:
            record = store.read_streams_record() or {}
            drifted = []
            try:
                raw = client.get_raw_config()
                for s, rec in (record.get("streams") or {}).items():
                    if rec.get("dejavu_source") and rec["dejavu_source"] not in raw:
                        drifted.append(s)
            except FrigateError:
                pass
            if drifted:
                snapshot["drift_detected"] = drifted
    return snapshot


def resolved_profiles(cfg):
    out = {}
    for name, prof in cfg["profiles"].items():
        prof = prof or {}
        out[name] = {
            "mode": prof.get("mode", "freeze"),
            "cameras": list(prof.get("cameras") or []) or "ALL",
            "capture_seconds": prof.get("capture_seconds", cfg["capture"]["seconds"]),
        }
    return out


def _print_dry_run(cfg, data, req, plan, notes, query):
    paths = cfg["paths"]
    cap = cfg["capture"]
    rcfg = cap["recordings"]
    now = time.time()
    smap = frigate_mod.go2rtc_streams_map(data)
    search_loops = req["mode"] == "loop" and req["source"] == "recordings"
    print(
        f"DRY RUN — dejavu ON plan (profile={req['profile']} mode={req['mode']} "
        f"source={req['source']} capture={req['seconds']}s)"
    )
    if req["mode"] == "loop" and req["source"] == "restream":
        print(
            "capture: direct live restream loop; no recordings guards or freeze fallback; "
            "a failed capture refuses activation"
        )
    else:
        print(
            "fallback: freeze ladder (live -> recorded when enabled -> cached -> black); "
            "never left live after activation"
        )
    print(f"restream: {cfg['frigate']['restream_url'].rstrip('/')}/<stream>{query}")
    print(f"clips dir: {paths['clips_local']} (frigate sees {paths['clips_frigate']})")
    anchor = None
    if search_loops:
        retrieval = (
            "direct segment files"
            if recordings_mod.files_retrieval_available(paths["recordings_dir"])
            else "export API"
        )
        dil = rcfg["dilute"]
        print(
            f"prepare a loop before the coordinated restart: retrieval {retrieval}; "
            f"window {req['seconds']}-{cap['max_loop_seconds']}s"
        )
        print(
            f"guards: lookback {rcfg['search_hours']}h, brightness vs live "
            f"±{rcfg['max_brightness_delta']}, intra-window drift "
            f"±{rcfg['max_brightness_drift']}, IR-mode match "
            f"{'on' if rcfg['match_ir_mode'] else 'OFF'}, cross-camera sync "
            f"±{rcfg['sync_tolerance_minutes']}min"
        )
        print(
            f"dilute: {'on' if dil['enabled'] else 'OFF'} — allow a window with "
            f"<= {dil['max_activity_fraction'] * 100:.0f}% activity and no "
            f"{'/'.join(dil['block_labels'])} (>= {dil['min_seconds']}s)"
        )
        anchor = _plan_synced_windows(cfg, req, plan)
    print()
    if not plan:
        print("!! no streams resolved — nothing would happen")
    for s in sorted(plan):
        kind, sources = frigate_mod.normalize_sources(smap.get(s))
        extras = frigate_mod.audio_extras(sources)
        src_audio = frigate_mod.build_dejavu_source(
            paths["clips_frigate"], s, True, extras
        )
        cams = ", ".join(plan[s]["cameras"]) or "(direct stream)"
        print(f"stream {s}   [cameras: {cams}]")
        for source in sources:
            print(f"  - {source}")
        print(f"  => {src_audio}")
        print("     (#audio params dropped if the clip turns out audio-less)")
        record_cameras = plan[s].get("record_cameras") or []
        if search_loops and record_cameras:
            wins = plan[s].get("window_candidates")
            if wins:
                offset = (
                    f" (anchor Δ{abs(wins[0]['end'] - (anchor or wins[0]['end'])):.0f}s)"
                    if anchor
                    else ""
                )
                print(
                    f"     metadata candidate: recordings of {record_cameras[0]} — "
                    f"{recordings_mod.describe_window(wins[0], now)}{offset}\n"
                    "     (lighting, IR, drift, and seam guards run only during capture)\n"
                )
            else:
                print("     metadata candidates: none -> freeze fallback planned\n")
        elif search_loops:
            print(
                "     metadata candidates: no record-role camera -> freeze fallback planned\n"
            )
        else:
            print("     no recordings search for this mode/source\n")
    if plan:
        print(
            f"plus go2rtc.ffmpeg.{frigate_mod.DEJAVU_TEMPLATE_NAME}: "
            f"{frigate_mod.DEJAVU_TEMPLATE_ARGS!r} "
            "(installed if absent; retained while privacy is off)"
        )
    for key, label in (
        ("unsupported_cameras", "unsupported cameras (no restream input)"),
        ("excluded", "excluded by streams.include/exclude"),
        ("missing_streams", "missing from go2rtc.streams"),
    ):
        if notes[key]:
            print(f"{label}: {', '.join(notes[key])}")
    blockers = frigate_mod.resolution_blockers(notes)
    if blockers:
        print(f"ACTIVATION WOULD BE REFUSED: {'; '.join(blockers)}")
    print("\nNo captures run, no config written, no restart. (dry run)")
