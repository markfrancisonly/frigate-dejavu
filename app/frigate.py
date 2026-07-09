"""Frigate + go2rtc HTTP clients, and Frigate raw-config YAML surgery.

All config edits are ruamel.yaml round-trips (comments, anchors, `{FRIGATE_*}`
placeholders preserved). Only `go2rtc.streams.<name>` values are ever touched
(SPEC §4).
"""

import copy
import fnmatch
import http.client
import io
import json
import logging
import re
import socket

import requests
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

log = logging.getLogger("dejavu.frigate")


class FrigateError(Exception):
    pass


# go2rtc restream reference used by every camera input in this deployment:
#   rtsp://127.0.0.1:8554/<stream>[?query]
RESTREAM_RE = re.compile(
    r"^rtsps?://(?:127\.0\.0\.1|localhost):\d+/([^?/#]+)(?:\?.*)?$"
)

# Loop playback lives in a NAMED go2rtc ffmpeg input template: Frigate's
# go2rtc config generator str.format()s every *stream source* value (only
# {FRIGATE_*} env vars are legal there — a literal {input} crash-loops
# go2rtc with "Invalid substitution found"), while the go2rtc.ffmpeg
# templates section is passed through untouched (SPEC §16).
DEJAVU_TEMPLATE_NAME = "frigate_dejavu_loop"
DEJAVU_TEMPLATE_ARGS = "-re -stream_loop -1 -i {input}"

# Audio offers that "#audio=copy" of an AAC capture already satisfies.
_AUDIO_COVERED_BY_COPY = ("copy", "aac")


def clip_name(stream):
    return f"{stream}.dejavu.mp4"


# --------------------------------------------------------------------------
# HTTP clients
# --------------------------------------------------------------------------

# One shared session for every Frigate/go2rtc call, so optional credentials
# (frigate.api_auth) ride along uniformly. Frigate's internal :5000 port is
# unauthenticated and needs none of this; point api_url at the authenticated
# :8971 port and supply either a bearer token or user/password (the latter
# performs /api/login and rides the JWT cookie, re-logging in once on a 401).
HTTP = requests.Session()
_AUTH = {"login_url": None, "user": "", "password": ""}


def init_http(cfg):
    """Apply frigate.api_auth to the shared session. Call once after config
    load; a no-op when no credentials are configured."""
    auth = cfg["frigate"].get("api_auth") or {}
    token = auth.get("token", "")
    user = auth.get("user", "")
    if token:
        HTTP.headers["Authorization"] = f"Bearer {token}"
        logging.getLogger("dejavu.http").info(
            "frigate API auth: bearer token configured"
        )
    if user:
        _AUTH.update(
            login_url=cfg["frigate"]["api_url"].rstrip("/") + "/api/login",
            user=user,
            password=auth.get("password", ""),
        )
        if _login():
            logging.getLogger("dejavu.http").info(
                "frigate API auth: logged in as %s", user
            )
        else:
            logging.getLogger("dejavu.http").warning(
                "frigate API auth: login as %s failed (will retry on 401)", user
            )


def _login():
    if not _AUTH["login_url"]:
        return False
    try:
        r = HTTP.post(
            _AUTH["login_url"],
            json={"user": _AUTH["user"], "password": _AUTH["password"]},
            timeout=10,
        )
    except requests.RequestException:
        return False
    return r.status_code == 200  # JWT cookie now in the session jar


def _request(method, url, **kw):
    r = HTTP.request(method, url, **kw)
    if r.status_code == 401 and _login():
        r = HTTP.request(method, url, **kw)
    return r


def http_get(url, **kw):
    return _request("GET", url, **kw)


def http_post(url, **kw):
    return _request("POST", url, **kw)


def http_patch(url, **kw):
    return _request("PATCH", url, **kw)


def http_delete(url, **kw):
    return _request("DELETE", url, **kw)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Minimal HTTP-over-unix-socket connection (docker restart fallback)."""

    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix_path)
        self.sock = sock


class FrigateClient:
    def __init__(self, cfg):
        f = cfg["frigate"]
        self.api = f["api_url"].rstrip("/")
        self.go2rtc = f["go2rtc_api_url"].rstrip("/")
        self.container = f["container_name"]
        self.docker_socket = "/var/run/docker.sock"

    # -- Frigate API --------------------------------------------------------

    def get_raw_config(self):
        try:
            r = http_get(f"{self.api}/api/config/raw", timeout=15)
        except requests.RequestException as exc:
            raise FrigateError(f"frigate API unreachable ({self.api}): {exc}") from exc
        if r.status_code != 200:
            raise FrigateError(
                f"GET /api/config/raw failed: HTTP {r.status_code}: {r.text[:300]}"
            )
        text = r.text
        # 0.17 serves the file as a JSON-encoded string (with a text/plain
        # content-type); unwrap it. Real YAML never json-decodes to a str.
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                text = decoded
        except ValueError:
            pass
        if not text.strip():
            raise FrigateError("GET /api/config/raw returned an empty config")
        return text

    def save_config(self, raw, save_option="saveonly"):
        """POST the raw YAML body; Frigate validates server-side (400 = rejected,
        nothing persisted)."""
        try:
            r = http_post(
                f"{self.api}/api/config/save",
                params={"save_option": save_option},
                data=raw.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise FrigateError(f"config save request failed: {exc}") from exc
        if r.status_code != 200:
            message = ""
            try:
                message = r.json().get("message", "")
            except ValueError:
                message = r.text[:500]
            raise FrigateError(
                f"frigate rejected the config (HTTP {r.status_code}): {message}"
            )

    def restart_api(self):
        """POST /api/restart — Frigate exits its container; restart: always
        brings it back. A dropped connection here just means it went down fast."""
        try:
            r = http_post(f"{self.api}/api/restart", timeout=10)
            if r.status_code != 200:
                raise FrigateError(
                    f"POST /api/restart failed: HTTP {r.status_code}: {r.text[:300]}"
                )
        except requests.RequestException as exc:
            log.warning(
                "restart request dropped (frigate likely already restarting): %s", exc
            )

    def processed_config(self):
        """Frigate's RUNNING config as JSON — {FRIGATE_*} placeholders already
        resolved. Reflects what Frigate loaded at its last (re)start, NOT
        unrestarted saveonly edits — which is exactly what a runtime
        PATCH-back needs."""
        try:
            r = http_get(f"{self.api}/api/config", timeout=15)
        except requests.RequestException as exc:
            raise FrigateError(f"GET /api/config failed: {exc}") from exc
        if r.status_code != 200:
            raise FrigateError(f"GET /api/config failed: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise FrigateError(f"GET /api/config returned non-JSON: {exc}") from exc

    def patch_stream(self, name, src):
        """Runtime go2rtc source swap for ONE stream — existing consumers are
        kicked (they reconnect within seconds), nothing else is touched, and
        nothing persists (persistence is ours via config/save saveonly)."""
        try:
            r = http_patch(
                f"{self.go2rtc}/api/streams",
                params={"name": name, "src": src},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise FrigateError(f"go2rtc PATCH {name} failed: {exc}") from exc
        if r.status_code != 200:
            raise FrigateError(
                f"go2rtc PATCH {name} failed: HTTP {r.status_code}: " f"{r.text[:150]}"
            )

    def version(self):
        try:
            r = http_get(f"{self.api}/api/version", timeout=4)
            if r.status_code == 200:
                return r.text.strip()
        except requests.RequestException:
            pass
        return None

    def is_up(self):
        return self.version() is not None

    # -- go2rtc API ---------------------------------------------------------

    def go2rtc_streams(self):
        try:
            r = http_get(f"{self.go2rtc}/api/streams", timeout=6)
        except requests.RequestException as exc:
            raise FrigateError(
                f"go2rtc API unreachable ({self.go2rtc}): {exc}"
            ) from exc
        if r.status_code != 200:
            raise FrigateError(f"go2rtc /api/streams failed: HTTP {r.status_code}")
        try:
            return r.json() or {}
        except ValueError as exc:
            raise FrigateError(f"go2rtc /api/streams returned non-JSON: {exc}") from exc

    def go2rtc_up(self):
        try:
            self.go2rtc_streams()
            return True
        except FrigateError:
            return False

    def _docker_request(self, method, path, body=None, timeout=30):
        conn = _UnixHTTPConnection(self.docker_socket, timeout=timeout)
        try:
            payload = json.dumps(body) if body is not None else None
            conn.request(
                method,
                path,
                body=payload,
                headers={"Content-Type": "application/json", "Host": "docker"},
            )
            resp = conn.getresponse()
            raw = resp.read()
        except OSError as exc:
            raise FrigateError(f"docker API {path} failed: {exc}") from exc
        finally:
            conn.close()
        return resp.status, raw

    def docker_exec(self, cmd, timeout=30):
        """Run a command inside the frigate container via the docker socket.
        Raises on non-zero exit."""
        status, raw = self._docker_request(
            "POST",
            f"/containers/{self.container}/exec",
            {"AttachStdout": True, "AttachStderr": True, "Cmd": cmd},
            timeout,
        )
        if status != 201:
            raise FrigateError(f"docker exec create failed: HTTP {status}: {raw[:150]}")
        exec_id = json.loads(raw)["Id"]
        status, _ = self._docker_request(
            "POST", f"/exec/{exec_id}/start", {"Detach": False, "Tty": False}, timeout
        )
        if status != 200:
            raise FrigateError(f"docker exec start failed: HTTP {status}")
        status, raw = self._docker_request(
            "GET", f"/exec/{exec_id}/json", None, timeout
        )
        code = json.loads(raw).get("ExitCode")
        if code != 0:
            raise FrigateError(f"docker exec {' '.join(cmd)} exited {code}")

    def go2rtc_restart(self):
        """Restart ONLY the embedded go2rtc SERVICE (s6 kick, not go2rtc's
        /api/restart — that self-re-execs and rereads the STALE generated
        config). The s6 run script re-runs create_config, regenerating
        /dev/shm/go2rtc.yaml from config.yml — picking up our saveonly edits.
        Frigate's detection/recording machinery never restarts; its ffmpeg
        consumers drop once and reconnect within seconds."""
        self.docker_exec(["/command/s6-svc", "-t", "/run/service/go2rtc"])

    def wait_go2rtc_up(self, timeout=45):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.go2rtc_up():
                return True
            time.sleep(2)
        return False

    # -- restart/health orchestration ---------------------------------------

    def wait_down(self, timeout=30):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_up():
                return True
            time.sleep(1)
        log.warning("frigate never went down within %ss after restart request", timeout)
        return False

    def wait_healthy(self, timeout):
        """Frigate /api/version AND go2rtc /api/streams up, twice in a row."""
        import time

        deadline = time.monotonic() + timeout
        consecutive = 0
        while time.monotonic() < deadline:
            if self.is_up() and self.go2rtc_up():
                consecutive += 1
                if consecutive >= 2:
                    return True
            else:
                consecutive = 0
            time.sleep(2)
        return False

    def docker_restart(self, stop_timeout=30):
        """Fallback: POST /containers/<name>/restart over the docker socket."""
        log.info(
            "docker restart fallback: restarting container %r (t=%ss)",
            self.container,
            stop_timeout,
        )
        conn = _UnixHTTPConnection(self.docker_socket, timeout=stop_timeout + 90)
        try:
            conn.request(
                "POST",
                f"/containers/{self.container}/restart?t={stop_timeout}",
                headers={"Host": "docker"},
            )
            resp = conn.getresponse()
            body = resp.read().decode(errors="replace")
        except OSError as exc:
            raise FrigateError(
                f"docker restart via {self.docker_socket} failed: {exc}"
            ) from exc
        finally:
            conn.close()
        if resp.status not in (200, 204):
            raise FrigateError(
                f"docker restart failed: HTTP {resp.status}: {body[:300]}"
            )


# --------------------------------------------------------------------------
# Raw-config YAML surgery (ruamel round-trip)
# --------------------------------------------------------------------------


def yaml_rt():
    y = YAML()
    y.preserve_quotes = True
    y.width = 2**20  # never re-wrap long source lines
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def parse_config(raw):
    y = yaml_rt()
    try:
        data = y.load(raw)
    except Exception as exc:  # noqa: BLE001 - any parse failure is fatal here
        raise FrigateError(f"could not parse Frigate config YAML: {exc}") from exc
    if not isinstance(data, (dict, CommentedMap)):
        raise FrigateError("Frigate config did not parse to a mapping")
    return y, data


def dump_config(y, data):
    buf = io.StringIO()
    y.dump(data, buf)
    return buf.getvalue()


def go2rtc_streams_map(data):
    go2rtc = data.get("go2rtc")
    if not isinstance(go2rtc, (dict, CommentedMap)):
        raise FrigateError("Frigate config has no go2rtc section")
    streams = go2rtc.get("streams")
    if not isinstance(streams, (dict, CommentedMap)):
        raise FrigateError("Frigate config has no go2rtc.streams section")
    return streams


def normalize_sources(value):
    """A go2rtc stream value is a string or a list of strings.
    Returns (kind, [source, ...]) with plain-python strings."""
    if value is None:
        return "list", []
    if isinstance(value, str):
        return "str", [str(value)]
    if isinstance(value, (list, CommentedSeq)):
        return "list", [str(v) for v in value]
    raise FrigateError(f"unsupported go2rtc stream source type: {type(value).__name__}")


def _match_any(name, patterns):
    return any(name == p or fnmatch.fnmatchcase(name, p) for p in patterns)


def camera_stream_refs(data, camera):
    """go2rtc stream names referenced by a camera's ffmpeg inputs, with the
    union of the roles each stream feeds ({stream: {"detect", "record", ...}})."""
    cam = (data.get("cameras") or {}).get(camera) or {}
    inputs = (cam.get("ffmpeg") or {}).get("inputs") or []
    refs = {}
    for inp in inputs:
        inp = inp or {}
        m = RESTREAM_RE.match(str(inp.get("path", "")))
        if m:
            roles = {str(r) for r in (inp.get("roles") or [])}
            refs.setdefault(m.group(1), set()).update(roles)
    return refs


def resolve_streams(data, cfg, camera_patterns):
    """Resolve profile/request camera patterns to the go2rtc replacement set.

    Returns (plan, notes):
      plan  = {stream: {"cameras": [frigate camera, ...]}}
      notes = {"unsupported_cameras", "unknown_patterns", "excluded", "missing_streams"}
    """
    cameras_map = data.get("cameras")
    if not isinstance(cameras_map, (dict, CommentedMap)) or not cameras_map:
        raise FrigateError("Frigate config has no cameras section")
    all_cameras = [str(c) for c in cameras_map.keys()]
    all_streams = [str(s) for s in go2rtc_streams_map(data).keys()]

    camera_patterns = list(camera_patterns or [])
    cam_pats = [p for p in camera_patterns if not p.startswith("stream:")]
    stream_pats = [
        p[len("stream:") :] for p in camera_patterns if p.startswith("stream:")
    ]

    unknown = []
    if not camera_patterns:
        cameras = list(all_cameras)
    else:
        cameras = []
        for pat in cam_pats:
            matches = [
                c for c in all_cameras if c == pat or fnmatch.fnmatchcase(c, pat)
            ]
            if not matches:
                unknown.append(pat)
            cameras.extend(m for m in matches if m not in cameras)

    plan = {}
    unsupported = []
    for cam in cameras:
        refs = camera_stream_refs(data, cam)
        if not refs:
            unsupported.append(cam)  # no restream-style input; cannot swap (SPEC §4.1)
            continue
        for s, roles in refs.items():
            entry = plan.setdefault(s, {"cameras": [], "record_cameras": []})
            entry["cameras"].append(cam)
            if "record" in roles:
                # this camera records THIS stream -> its recordings are
                # codec-identical loop material (SPEC §5b)
                entry["record_cameras"].append(cam)

    for pat in stream_pats:
        matches = [s for s in all_streams if s == pat or fnmatch.fnmatchcase(s, pat)]
        if not matches:
            unknown.append(f"stream:{pat}")
        for s in matches:
            plan.setdefault(s, {"cameras": [], "record_cameras": []})

    include = cfg["streams"].get("include") or []
    exclude = cfg["streams"].get("exclude") or []
    filtered, excluded, missing = {}, [], []
    for s in plan:
        if include and not _match_any(s, include):
            excluded.append(s)
        elif _match_any(s, exclude):
            excluded.append(s)
        elif s not in all_streams:
            missing.append(
                s
            )  # referenced by a camera but not defined in go2rtc.streams
        else:
            filtered[s] = plan[s]

    notes = {
        "unsupported_cameras": unsupported,
        "unknown_patterns": unknown,
        "excluded": sorted(excluded),
        "missing_streams": sorted(missing),
    }
    return filtered, notes


def audio_extras(sources):
    """Ordered #audio= transcode offers from the original source lines that
    '#audio=copy' of an AAC capture does not already provide (SPEC §4.2)."""
    out = []
    for src in sources:
        for part in str(src).split("#")[1:]:
            if part.startswith("audio="):
                val = part[len("audio=") :].strip().lower()
                if val and val not in _AUDIO_COVERED_BY_COPY and val not in out:
                    out.append(val)
    return out


def build_dejavu_source(clips_frigate_dir, stream, has_audio, extras):
    src = (
        f"ffmpeg:{clips_frigate_dir.rstrip('/')}/{clip_name(stream)}"
        f"#input={DEJAVU_TEMPLATE_NAME}#video=copy"
    )
    if has_audio:
        src += "#audio=copy" + "".join(f"#audio={a}" for a in extras)
    return src


def apply_dejavu(data, replacements):
    """Replace each stream's whole source list with the single dejavu source
    and install the (permanent) loop input template. Returns a record of any
    pre-existing template value so restore can put it back."""
    streams = go2rtc_streams_map(data)
    ffmpeg_map = (
        data["go2rtc"].get("ffmpeg")
        if isinstance(data["go2rtc"].get("ffmpeg"), (dict, CommentedMap))
        else None
    )
    prior = None
    if (
        ffmpeg_map
        and DEJAVU_TEMPLATE_NAME in ffmpeg_map
        and str(ffmpeg_map[DEJAVU_TEMPLATE_NAME]) != DEJAVU_TEMPLATE_ARGS
    ):
        prior = str(ffmpeg_map[DEJAVU_TEMPLATE_NAME])
    ensure_loop_template(data)
    template_rec = {"name": DEJAVU_TEMPLATE_NAME, "prior": prior}

    for stream, src in replacements.items():
        streams[stream] = CommentedSeq([DoubleQuotedScalarString(src)])
    return template_rec


def ensure_loop_template(data):
    """Make sure go2rtc.ffmpeg carries the loop template. It is PERMANENT:
    the running go2rtc must always have it loaded so runtime PATCH swaps can
    reference it without a restart. Returns True if the config changed."""
    go2rtc = data.get("go2rtc")
    if not isinstance(go2rtc, (dict, CommentedMap)):
        raise FrigateError("Frigate config has no go2rtc section")
    ffmpeg_map = go2rtc.get("ffmpeg")
    if not isinstance(ffmpeg_map, (dict, CommentedMap)):
        ffmpeg_map = CommentedMap()
        go2rtc["ffmpeg"] = ffmpeg_map
    if str(ffmpeg_map.get(DEJAVU_TEMPLATE_NAME, "")) == DEJAVU_TEMPLATE_ARGS:
        return False
    ffmpeg_map[DEJAVU_TEMPLATE_NAME] = DEJAVU_TEMPLATE_ARGS
    return True


def template_in_config(data):
    try:
        ffmpeg_map = (data.get("go2rtc") or {}).get("ffmpeg") or {}
        return str(ffmpeg_map.get(DEJAVU_TEMPLATE_NAME, "")) == DEJAVU_TEMPLATE_ARGS
    except AttributeError:
        return False


def _restore_template(current_data, template_rec):
    """The loop template is permanent — restore only ever puts back a
    pre-existing DIFFERENT value the user had under our key (improbable).
    Returns True if changed."""
    if not template_rec:
        return False
    prior = template_rec.get("prior")
    if prior is None or prior == DEJAVU_TEMPLATE_ARGS:
        return False
    go2rtc = current_data.get("go2rtc")
    if not isinstance(go2rtc, (dict, CommentedMap)):
        return False
    ffmpeg_map = go2rtc.get("ffmpeg")
    name = template_rec.get("name") or DEJAVU_TEMPLATE_NAME
    if not isinstance(ffmpeg_map, (dict, CommentedMap)) or name not in ffmpeg_map:
        return False
    if str(ffmpeg_map[name]) == prior:
        return False
    ffmpeg_map[name] = prior
    return True


def graft_restore(current_data, backup_data, records, template_rec=None):
    """Surgically restore original sources into the CURRENT config by
    transplanting the pristine nodes from the backup document (comments and
    scalar formatting ride along), then remove the loop template we installed.
    `records` is streams.json's per-stream dict.

    Returns (changed, restored, drifted, missing)."""
    cur = go2rtc_streams_map(current_data)
    bak = go2rtc_streams_map(backup_data) if backup_data is not None else None

    changed = False
    restored, drifted, missing = [], [], []
    for stream, rec in records.items():
        if stream not in cur:
            missing.append(stream)  # deleted while dejavu on; nothing to restore into
            continue
        _, cur_sources = normalize_sources(cur.get(stream))
        original_sources = [str(s) for s in rec.get("original_sources", [])]

        if cur_sources == original_sources:
            continue  # already original (hand-restored or rollback ran); no churn

        expected = rec.get("dejavu_source")
        if not (len(cur_sources) == 1 and cur_sources[0] == expected):
            drifted.append(
                stream
            )  # hand-edited while dejavu on; overwrite anyway (SPEC §7.2)

        if bak is not None and stream in bak:
            cur[stream] = copy.deepcopy(bak[stream])
        elif rec.get("original_kind") == "str" and original_sources:
            cur[stream] = original_sources[0]
        else:
            cur[stream] = CommentedSeq(original_sources)
        restored.append(stream)
        changed = True

    if _restore_template(current_data, template_rec):
        changed = True
    return changed, restored, drifted, missing


# --------------------------------------------------------------------------
# go2rtc post-restart verification (SPEC §6.8 / §7.3)
# --------------------------------------------------------------------------


def _stream_blob(streams_json, name):
    entry = streams_json.get(name)
    return json.dumps(entry) if entry is not None else ""


def verify_dejavu_applied(client, expect_clips, retries=3, delay=2):
    """Each replaced stream's go2rtc producer must reference its dejavu clip."""
    import time

    bad, last_exc = list(expect_clips), None
    for _ in range(retries):
        try:
            js = client.go2rtc_streams()
        except FrigateError as exc:
            last_exc = exc
            time.sleep(delay)
            continue
        bad = [s for s, clip in expect_clips.items() if clip not in _stream_blob(js, s)]
        if not bad:
            return True, []
        time.sleep(delay)
    if last_exc is not None:
        log.warning("go2rtc verification degraded: %s", last_exc)
    return False, bad


def verify_dejavu_removed(client, expect_clips, retries=3, delay=2):
    """No replaced stream may still reference its dejavu clip."""
    import time

    bad, last_exc = list(expect_clips), None
    for _ in range(retries):
        try:
            js = client.go2rtc_streams()
        except FrigateError as exc:
            last_exc = exc
            time.sleep(delay)
            continue
        bad = [s for s, clip in expect_clips.items() if clip in _stream_blob(js, s)]
        if not bad:
            return True, []
        time.sleep(delay)
    if last_exc is not None:
        log.warning("go2rtc verification degraded: %s", last_exc)
    return False, bad
