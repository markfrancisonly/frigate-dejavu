#!/usr/bin/env python3
"""Frigate Déjà Vu REST API (SPEC §10.2).

A thin shell over the CLI: mutating endpoints preflight against the shared
state file (for correct 409/400s), then spawn `dejavu` detached — the CLI
process owns the transition and re-checks under the same flock. Answers always
come from the state file.

Auth: iff api.bearer_token resolves to a value, every /api/* request must send
it; /healthz is open.
"""

import logging
import os
import subprocess
import sys
import threading
import time

import config as config_mod
import core
import frigate as frigate_mod
from core import Busy, Invalid
from flask import Flask, jsonify, request

log = logging.getLogger("dejavu.api")

try:
    CFG = config_mod.load_config()
except config_mod.ConfigError as exc:
    print(f"invalid appliance config: {exc}", file=sys.stderr)
    sys.exit(3)

frigate_mod.init_http(CFG)
TOKEN = CFG["api"]["bearer_token"]
app = Flask("frigate-dejavu")
_ON_FIELDS = frozenset({"profile", "mode", "cameras", "capture_seconds", "source"})


@app.before_request
def _auth():
    if request.path == "/healthz":
        return None
    if TOKEN and request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return jsonify({"error": "unauthorized"}), 401
    return None


def _spawn(cli_args, tag):
    """Run dejavu detached (own session). The child inherits our stdout — the
    container log — so `docker logs` / Portainer / Dozzle see the whole run at
    DEJAVU_LOG_LEVEL (default INFO; set DEBUG for full forensics)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    proc = subprocess.Popen(
        [
            sys.executable,
            os.path.join(os.path.dirname(__file__), "dejavu.py"),
            *cli_args,
        ],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=os.path.dirname(__file__) or ".",
    )

    def _reap():
        proc.wait()
        log.info(
            "job %s-%s (pid %s) exited rc=%s", stamp, tag, proc.pid, proc.returncode
        )

    threading.Thread(target=_reap, daemon=True).start()
    log.info("spawned dejavu %s (pid %s)", " ".join(cli_args), proc.pid)


def _busy(exc):
    body = {"error": str(exc), "state": core.get_status(CFG, live=False)["state"]}
    if exc.retry_after is not None:
        body["retry_after"] = round(exc.retry_after, 1)
    return jsonify(body), 409


def _bad(message, **details):
    return jsonify(error=message, **details), 400


@app.post("/api/dejavu/on")
def dejavu_on():
    # An empty POST intentionally means "use the default profile" (all cameras
    # in the reference config). Once a body is supplied, require a valid JSON
    # object so a typo cannot silently broaden the request to that default.
    raw_body = request.get_data(cache=True)
    body = {}
    if raw_body:
        body = request.get_json(silent=True) if request.is_json else None
    if not isinstance(body, dict):
        return _bad("request body must be a JSON object")
    unknown = sorted(set(body) - _ON_FIELDS)
    if unknown:
        return _bad("unknown request field(s): " + ", ".join(unknown))

    profile = body.get("profile")
    mode = body.get("mode")
    cameras = body.get("cameras")
    capture_seconds = body.get("capture_seconds")
    source = body.get("source")

    if profile is not None and (
        not isinstance(profile, str) or profile not in CFG["profiles"]
    ):
        return _bad(f"unknown profile {profile!r}", profiles=sorted(CFG["profiles"]))
    for name, value, choices in (
        ("mode", mode, ("loop", "freeze")),
        ("source", source, ("recordings", "restream")),
    ):
        if value is not None and value not in choices:
            return _bad(f"{name} must be one of: {', '.join(choices)}")
    if isinstance(cameras, str):
        cameras = [c.strip() for c in cameras.split(",")]
    if cameras is not None:
        if not isinstance(cameras, list) or not all(
            isinstance(c, str) for c in cameras
        ):
            return _bad("cameras must be a list of strings")
        cameras = [c.strip() for c in cameras]
        if any(not c for c in cameras):
            return _bad("camera names must not be blank; use [] for all")
    if capture_seconds is not None and (
        not isinstance(capture_seconds, int)
        or isinstance(capture_seconds, bool)
        or capture_seconds < 1
    ):
        return _bad("capture_seconds must be a positive integer")

    try:
        core.preflight(CFG, "on")
    except Busy as exc:
        return _busy(exc)
    except Invalid as exc:
        return _bad(str(exc))

    args = ["on"]
    options = {
        "profile": profile,
        "mode": mode,
        "cameras": cameras,
        "capture-seconds": capture_seconds,
        "source": source,
    }
    for flag, value in options.items():
        if value is not None:
            args += [f"--{flag}", ",".join(value) if flag == "cameras" else str(value)]
    _spawn(args, "on")
    return (
        jsonify(
            {
                "accepted": True,
                "op": "on",
                "status_url": "/api/dejavu/status",
                "state": core.get_status(CFG, live=False),
            }
        ),
        202,
    )


@app.post("/api/dejavu/off")
def dejavu_off():
    try:
        pre = core.preflight(CFG, "off")
    except Busy as exc:
        return _busy(exc)
    if pre["noop"]:
        return jsonify({"state": "off", "message": "dejavu already off"}), 200
    _spawn(["off"], "off")
    return (
        jsonify(
            {
                "accepted": True,
                "op": "off",
                "status_url": "/api/dejavu/status",
                "state": core.get_status(CFG, live=False),
            }
        ),
        202,
    )


@app.get("/api/dejavu/status")
def dejavu_status():
    return jsonify(core.get_status(CFG, live=True)), 200


@app.get("/api/dejavu/profiles")
def dejavu_profiles():
    return jsonify(core.resolved_profiles(CFG)), 200


@app.get("/healthz")
def healthz():
    return (
        jsonify({"ok": True, "state": core.get_status(CFG, live=False)["state"]}),
        200,
    )


def main():
    level = getattr(
        logging, os.environ.get("DEJAVU_LOG_LEVEL", "INFO").upper(), logging.INFO
    )
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    store = core.StateStore(CFG)  # ensures state/tmp dirs exist
    with store.locked():
        core.reconcile_locked(store)  # stale-PID reconciliation on API start (SPEC §9)

    host, port = config_mod.api_listen(CFG)
    log.info(
        "Frigate Déjà Vu API listening on %s:%s (auth %s)",
        host,
        port,
        "enabled" if TOKEN else "disabled",
    )
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
