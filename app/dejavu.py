#!/usr/bin/env python3
"""dejavu — Frigate Déjà Vu CLI (SPEC §10.1).

Blocking; the process IS the transition job. The REST layer spawns this same
program, so `docker exec frigate-dejavu dejavu on` and POST /api/dejavu/on
are literally one code path.

Exit codes: 0 ok · 1 failure · 2 busy/debounced · 3 invalid request/config.
"""

import argparse
import json
import logging
import os
import sys

import config as config_mod
import core
import frigate as frigate_mod
from config import ConfigError
from core import Busy, Invalid, JobError

MUTATING = {"on", "off", "cancel", "force-restore"}


def _setup_logging(cfg, args):
    """One log stream: the container stdout, at DEJAVU_LOG_LEVEL (default INFO;
    set DEBUG for full forensics). When we were started via `docker exec`
    (stdout is the exec session, not the container log), a mirror to
    /proc/1/fd/1 makes sure `docker logs` / Portainer / Dozzle still see the
    run. REST-spawned jobs inherit the API's stdout (already the container
    log), so the inode check below makes the mirror a no-op there — nothing
    double-logs."""
    level = getattr(
        logging, os.environ.get("DEJAVU_LOG_LEVEL", "INFO").upper(), logging.INFO
    )
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
    )
    stdout_h = logging.StreamHandler(sys.stdout)
    stdout_h.setFormatter(fmt)
    root.addHandler(stdout_h)
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.command in MUTATING and not getattr(args, "dry_run", False):
        core.prune_state_snapshots(cfg["paths"]["state_dir"])

    try:
        pid1 = os.stat("/proc/1/fd/1")
        mine = os.fstat(sys.stdout.fileno())
        if (pid1.st_dev, pid1.st_ino) != (mine.st_dev, mine.st_ino):
            mirror = logging.StreamHandler(
                open("/proc/1/fd/1", "w", buffering=1, errors="replace")
            )
            mirror.setFormatter(
                logging.Formatter(
                    f"%(asctime)s %(levelname)-7s [exec {args.command} "
                    f"pid {os.getpid()}] %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            root.addHandler(mirror)
    except OSError:
        pass


def build_parser():
    p = argparse.ArgumentParser(
        prog="dejavu", description="Frigate dejavu-mode appliance control"
    )
    p.add_argument(
        "--config",
        help="appliance config path (default: $DEJAVU_CONFIG "
        "or /config/config.yaml)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    on = sub.add_parser(
        "on", help="capture streams, swap go2rtc sources, restart frigate"
    )
    on.add_argument("--profile", default=None, help="profile name (default: 'default')")
    on.add_argument(
        "--mode",
        choices=["loop", "freeze"],
        default=None,
        help="override the profile's mode",
    )
    on.add_argument(
        "--cameras",
        default=None,
        help="comma-separated camera names/globs (or stream:<name>); "
        "overrides the profile's cameras",
    )
    on.add_argument(
        "--capture-seconds",
        type=int,
        default=None,
        help="override the capture period / quiet-window length (loop mode)",
    )
    on.add_argument(
        "--source",
        choices=["recordings", "restream"],
        default=None,
        help="clip source: event-clear window from recordings (default) "
        "or live restream capture",
    )
    on.add_argument(
        "--dry-run",
        action="store_true",
        help="print the replacement plan (incl. metadata window candidates) "
        "without acting",
    )

    sub.add_parser(
        "off", help="restore original sources " "(cancels an in-flight capture)"
    )
    sub.add_parser("cancel", help="alias of 'off' while a capture is running")

    status = sub.add_parser("status", help="show appliance + frigate state")
    status.add_argument("--json", action="store_true", dest="as_json")

    profiles = sub.add_parser("profiles", help="list configured profiles")
    profiles.add_argument("--json", action="store_true", dest="as_json")

    sub.add_parser(
        "force-restore",
        help="write the pristine whole-file backup " "verbatim and restart frigate",
    )
    return p


def _print_status(snap):
    print(f"state:    {snap['state']}")
    for key in ("profile", "mode", "since", "session", "capture_seconds"):
        if snap.get(key) is not None:
            print(f"{key + ':':<10}{snap[key]}")
    frigate = snap.get("frigate") or {}
    print(
        f"frigate:  reachable={frigate.get('reachable')} "
        f"version={frigate.get('version')} go2rtc={frigate.get('go2rtc')}"
    )
    if snap.get("drift_detected"):
        print(f"DRIFT:    {', '.join(snap['drift_detected'])} (config edited while on)")
    if snap.get("note"):
        print(f"note:     {snap['note']}")
    if snap.get("last_error"):
        print(f"error:    {snap['last_error']}")
    streams = snap.get("streams") or {}
    if streams:
        print("streams:")
        for name in sorted(streams):
            info = streams[name]
            cams = ", ".join(info.get("cameras") or [])
            print(f"  {name:<36} {info.get('phase', '?'):<28} {cams}")


def _print_profiles(profiles):
    for name in sorted(profiles):
        profile = profiles[name]
        cameras = (
            profile["cameras"]
            if isinstance(profile["cameras"], str)
            else ", ".join(profile["cameras"])
        )
        print(
            f"{name:<16} mode={profile['mode']:<7} "
            f"capture={profile['capture_seconds']}s  cameras: {cameras}"
        )


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        cfg = config_mod.load_config(args.config)
        _setup_logging(cfg, args)
        frigate_mod.init_http(cfg)

        if args.command == "on":
            cameras = (
                [c.strip() for c in args.cameras.split(",") if c.strip()]
                if args.cameras is not None
                else None
            )
            return core.cmd_on(
                cfg,
                profile=args.profile,
                mode=args.mode,
                cameras=cameras,
                capture_seconds=args.capture_seconds,
                source=args.source,
                dry_run=args.dry_run,
            )
        mutations = {
            "off": core.cmd_off,
            "cancel": core.cmd_cancel,
            "force-restore": core.cmd_force_restore,
        }
        if args.command in mutations:
            return mutations[args.command](cfg)
        if args.command in ("status", "profiles"):
            result = (
                core.get_status(cfg, live=True)
                if args.command == "status"
                else core.resolved_profiles(cfg)
            )
            if args.as_json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                (_print_status if args.command == "status" else _print_profiles)(result)
            return 0
        raise Invalid(f"unknown command {args.command!r}")

    except Busy as exc:
        print(f"busy: {exc}", file=sys.stderr)
        return 2
    except (Invalid, ConfigError) as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 3
    except JobError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
