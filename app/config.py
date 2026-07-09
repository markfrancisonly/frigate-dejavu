"""Appliance configuration: load /config/config.yaml, merge defaults, validate.

Validated at startup and before every transition — fail fast with a precise
key-path error (SPEC §11).
"""

import copy
import os

from ruamel.yaml import YAML


class ConfigError(Exception):
    pass


DEFAULT_CONFIG_PATH = "/config/config.yaml"

DEFAULTS = {
    "frigate": {
        "api_url": "http://frigate:5000",
        "go2rtc_api_url": "http://frigate:1984",
        "restream_url": "rtsp://frigate:8554",
        "container_name": "frigate",
        "restart_method": "api",
        "restart_fallback": True,
        "swap_method": "go2rtc",
        "health_timeout_seconds": 300,
        "settle_timeout_seconds": 240,
        # Optional credentials for Frigate's AUTHENTICATED API port (8971).
        # The default internal :5000 port needs none. token wins over
        # user/password; user/password performs /api/login and rides the JWT
        # cookie, re-logging in once on 401.
        "api_auth": {
            "token": "",
            "user": "",
            "password": "",
        },
    },
    "paths": {
        "clips_local": "/clips",
        "clips_frigate": "/config/dejavu-clips",
        "recordings_dir": "/recordings",
        "state_dir": "/data/state",
        "tmp_dir": "/data/tmp",
    },
    "capture": {
        "source": "recordings",
        "seconds": 300,
        "max_loop_seconds": 1200,
        "freeze_clip_seconds": 4,
        "rtsp_query": "mp4",
        "parallel": 4,
        "parallel_frames": 8,
        "engage_deadline_seconds": 15,
        "keep_clips_after_off": False,
        "recordings": {
            "search_hours": 4,
            "min_seconds": 20,
            "audio": "silence",
            "max_brightness_delta": 60,
            "max_brightness_drift": 20,
            "match_ir_mode": True,
            "sync_tolerance_minutes": 10,
            "dilute": {
                "enabled": True,
                "min_seconds": 300,
                "max_activity_fraction": 0.10,
                "block_labels": ["person"],
            },
        },
    },
    "streams": {
        "include": [],
        "exclude": [],
    },
    "profiles": {
        "default": {"mode": "freeze", "cameras": []},
    },
    "api": {
        "listen": "0.0.0.0:8898",
        "bearer_token": "",
        "debounce_seconds": 5,
    },
}


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _require(cond, path, msg):
    if not cond:
        raise ConfigError(f"config error at '{path}': {msg}")


def _pos_int(cfg, path, minimum=1):
    node = cfg
    for part in path.split("."):
        node = node[part]
    _require(
        isinstance(node, int) and not isinstance(node, bool) and node >= minimum,
        path,
        f"must be an integer >= {minimum} (got {node!r})",
    )


def _str_list(value, path):
    _require(
        isinstance(value, list), path, f"must be a list (got {type(value).__name__})"
    )
    for i, item in enumerate(value):
        _require(
            isinstance(item, str) and item.strip(),
            f"{path}[{i}]",
            f"must be a non-empty string (got {item!r})",
        )


def validate(cfg):
    f = cfg["frigate"]
    for key in ("api_url", "go2rtc_api_url", "restream_url", "container_name"):
        _require(
            isinstance(f.get(key), str) and f[key],
            f"frigate.{key}",
            "must be a non-empty string",
        )
    _require(
        f.get("restart_method") in ("api", "docker"),
        "frigate.restart_method",
        "must be 'api' or 'docker'",
    )
    _require(
        f.get("swap_method") in ("go2rtc", "restart", "patch"),
        "frigate.swap_method",
        "must be 'go2rtc' (service-only reload) or 'restart' (full frigate)",
    )
    _require(
        isinstance(f.get("restart_fallback"), bool),
        "frigate.restart_fallback",
        "must be a boolean",
    )
    _pos_int(cfg, "frigate.health_timeout_seconds")
    _pos_int(cfg, "frigate.settle_timeout_seconds")

    auth = cfg["frigate"].get("api_auth") or {}
    for key in ("token", "user", "password"):
        _require(
            isinstance(auth.get(key, ""), str),
            f"frigate.api_auth.{key}",
            "must be a string",
        )
    _require(
        not (auth.get("password") and not auth.get("user")),
        "frigate.api_auth",
        "password requires user",
    )

    for key in (
        "clips_local",
        "clips_frigate",
        "recordings_dir",
        "state_dir",
        "tmp_dir",
    ):
        _require(
            isinstance(cfg["paths"].get(key), str)
            and cfg["paths"][key].startswith("/"),
            f"paths.{key}",
            "must be an absolute path",
        )

    cap = cfg["capture"]
    _pos_int(cfg, "capture.seconds")
    _pos_int(cfg, "capture.max_loop_seconds")
    _pos_int(cfg, "capture.freeze_clip_seconds")
    _pos_int(cfg, "capture.parallel")
    _pos_int(cfg, "capture.parallel_frames")
    _pos_int(cfg, "capture.engage_deadline_seconds")
    _require(
        cap["max_loop_seconds"] >= cap["seconds"],
        "capture.max_loop_seconds",
        f"must be >= capture.seconds ({cap['seconds']})",
    )
    _require(
        isinstance(cap.get("rtsp_query"), str),
        "capture.rtsp_query",
        "must be a string ('' to disable)",
    )
    _require(
        isinstance(cap.get("keep_clips_after_off"), bool),
        "capture.keep_clips_after_off",
        "must be a boolean",
    )
    _require(
        cap.get("source") in ("recordings", "restream"),
        "capture.source",
        "must be 'recordings' or 'restream'",
    )
    rec = cap.get("recordings")
    _require(isinstance(rec, dict), "capture.recordings", "must be a mapping")
    _pos_int(cfg, "capture.recordings.search_hours")
    _require(
        isinstance(rec.get("min_seconds"), int) and rec["min_seconds"] >= 5,
        "capture.recordings.min_seconds",
        "must be an integer >= 5",
    )
    _require(
        rec.get("audio") in ("silence", "keep", "strip"),
        "capture.recordings.audio",
        "must be 'silence', 'keep' or 'strip'",
    )

    dil = rec.get("dilute")
    _require(isinstance(dil, dict), "capture.recordings.dilute", "must be a mapping")
    _require(
        isinstance(dil.get("enabled"), bool),
        "capture.recordings.dilute.enabled",
        "must be a boolean",
    )
    _require(
        isinstance(dil.get("min_seconds"), int)
        and not isinstance(dil["min_seconds"], bool)
        and dil["min_seconds"] >= 30,
        "capture.recordings.dilute.min_seconds",
        "must be an integer >= 30 (a short loop cannot dilute anything)",
    )
    frac = dil.get("max_activity_fraction")
    _require(
        isinstance(frac, (int, float))
        and not isinstance(frac, bool)
        and 0 <= frac <= 0.5,
        "capture.recordings.dilute.max_activity_fraction",
        "must be a number 0-0.5 (fraction of the loop that may contain activity)",
    )
    _str_list(dil.get("block_labels", []), "capture.recordings.dilute.block_labels")
    _require(
        "person" in [l.lower() for l in dil.get("block_labels", [])],
        "capture.recordings.dilute.block_labels",
        "must include 'person' — replaying a person is a disclosure, not a loop tell",
    )

    for key in ("max_brightness_delta", "max_brightness_drift"):
        val = rec.get(key)
        _require(
            isinstance(val, int) and not isinstance(val, bool) and 0 <= val <= 255,
            f"capture.recordings.{key}",
            "must be an integer 0-255 (0 disables)",
        )
    _require(
        isinstance(rec.get("match_ir_mode"), bool),
        "capture.recordings.match_ir_mode",
        "must be a boolean",
    )
    tol = rec.get("sync_tolerance_minutes")
    _require(
        isinstance(tol, int) and not isinstance(tol, bool) and 0 <= tol <= 1440,
        "capture.recordings.sync_tolerance_minutes",
        "must be an integer 0-1440 minutes (0 disables cross-camera sync)",
    )

    _str_list(cfg["streams"].get("include", []), "streams.include")
    _str_list(cfg["streams"].get("exclude", []), "streams.exclude")

    profiles = cfg.get("profiles")
    _require(
        isinstance(profiles, dict) and profiles,
        "profiles",
        "must be a non-empty mapping",
    )
    _require("default" in profiles, "profiles", "must define a 'default' profile")
    for name, prof in profiles.items():
        prof = prof or {}
        _require(isinstance(prof, dict), f"profiles.{name}", "must be a mapping")
        mode = prof.get("mode", "freeze")
        _require(
            mode in ("loop", "freeze"),
            f"profiles.{name}.mode",
            "must be 'loop' or 'freeze'",
        )
        _str_list(prof.get("cameras", []), f"profiles.{name}.cameras")
        if "capture_seconds" in prof:
            _require(
                isinstance(prof["capture_seconds"], int)
                and prof["capture_seconds"] >= 1,
                f"profiles.{name}.capture_seconds",
                "must be an integer >= 1",
            )
        if "source" in prof:
            _require(
                prof["source"] in ("recordings", "restream"),
                f"profiles.{name}.source",
                "must be 'recordings' or 'restream'",
            )

    api = cfg["api"]
    _require(
        isinstance(api.get("bearer_token"), str), "api.bearer_token", "must be a string"
    )
    _require(
        isinstance(api.get("debounce_seconds"), int) and api["debounce_seconds"] >= 0,
        "api.debounce_seconds",
        "must be an integer >= 0",
    )
    api_listen(cfg)  # raises on malformed listen


def api_listen(cfg):
    listen = cfg["api"].get("listen", "")
    host, sep, port = str(listen).rpartition(":")
    _require(
        sep == ":" and host and port.isdigit() and 0 < int(port) < 65536,
        "api.listen",
        f"must be 'host:port' (got {listen!r})",
    )
    return host, int(port)


def load_config(path=None):
    path = path or os.environ.get("DEJAVU_CONFIG") or DEFAULT_CONFIG_PATH
    if not os.path.isfile(path):
        raise ConfigError(f"appliance config not found: {path}")
    yaml = YAML(typ="safe")
    try:
        with open(path) as f:
            user = yaml.load(f)
    except Exception as exc:  # noqa: BLE001 - surface parse errors verbatim
        raise ConfigError(f"could not parse {path}: {exc}") from exc
    if user is None:
        user = {}
    if not isinstance(user, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    cfg = _deep_merge(DEFAULTS, user)

    # profiles are authored as a whole; a user-supplied profile replaces the
    # same-named default rather than deep-merging camera lists into it.
    if isinstance(user.get("profiles"), dict) and user["profiles"]:
        profiles = {}
        for name, prof in user["profiles"].items():
            profiles[name] = copy.deepcopy(prof) if prof else {}
        if "default" not in profiles:
            profiles["default"] = copy.deepcopy(DEFAULTS["profiles"]["default"])
        cfg["profiles"] = profiles

    env_token = os.environ.get("DEJAVU_API_TOKEN", "")
    if env_token:
        cfg["api"]["bearer_token"] = env_token

    # Frigate API credentials may come from env instead of the config file.
    for env, key in (
        ("DEJAVU_FRIGATE_TOKEN", "token"),
        ("DEJAVU_FRIGATE_USER", "user"),
        ("DEJAVU_FRIGATE_PASSWORD", "password"),
    ):
        val = os.environ.get(env, "")
        if val:
            cfg["frigate"]["api_auth"][key] = val

    validate(cfg)
    return cfg
