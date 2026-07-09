# Frigate Déjà Vu

Privacy-mode appliance for Frigate. On demand it replaces every camera's feed
with a convincing fake, by rewriting `go2rtc.streams` in Frigate's config (via
Frigate's own REST API). Frigate, recordings, detection, birdseye, HA cards,
and WebRTC viewers all keep working, unaware. Switching off restores the
original sources byte-exactly; Frigate itself never restarts (the swap reloads
only the embedded go2rtc service).

**Two-phase engage** (SPEC §5c): privacy engages INSTANTLY — every stream is
frozen on a live frame of the scene right now — then, in the background, each
stream is upgraded to a *loop* of a window from its own recent recordings, one
that can replay without a human noticing. A stream is **never left live**: the
freeze frame is the worst case, and it still provides privacy. The loop search
prefers LONG, quiet windows because the tell is *repetition*, not a single car
passing — a car once in 20 minutes reads as ordinary; the same car every
20 seconds screams "loop." Nothing in the fake feed ever loops a person,
repeats audio, or serves yesterday's lighting.

`SPEC.md` is the design contract; this file is the operator's manual.

## Quick start

```sh
bash up.sh                      # creates ../frigate/config/dejavu-clips + builds + starts

docker exec frigate-dejavu dejavu status
docker exec frigate-dejavu dejavu on --dry-run          # plan only, touches nothing
docker exec frigate-dejavu dejavu on                    # default profile (all cameras, loop)
docker exec frigate-dejavu dejavu on --profile indoor
docker exec frigate-dejavu dejavu on --mode freeze      # engage-and-stay on the freeze frame
docker exec frigate-dejavu dejavu off                   # restore (also cancels a capture)
```

The CLI is blocking and prints progress; the REST API spawns the exact same
CLI detached and returns immediately (poll `/api/dejavu/status`).

## REST API

Reachable at `http://<docker-host>:8898` on the published port (the example
compose maps `8898:8898`), or by container name (`http://frigate-dejavu:8898`)
from containers on the same Docker network as Frigate. If a LAN client like
Home Assistant is on a different subnet, publish a host port or give the
container a routable address to fit your network.

```sh
curl -X POST http://frigate-dejavu:8898/api/dejavu/on \
     -H 'Content-Type: application/json' \
     -d '{"profile": "indoor"}'                              # 202; also: mode, cameras, capture_seconds
curl -X POST http://frigate-dejavu:8898/api/dejavu/off     # 202 (409 while applying/restoring)
curl      http://frigate-dejavu:8898/api/dejavu/status     # state, per-stream phases, drift, frigate health
curl      http://frigate-dejavu:8898/api/dejavu/profiles
curl      http://frigate-dejavu:8898/healthz                # liveness, never needs auth
```

Bearer auth is optional: set `DEJAVU_API_TOKEN` in `.env` (see
`.env.example`) and every `/api/*` request must send
`Authorization: Bearer <token>`. Empty/unset = auth disabled.

Codes: `202` accepted · `409` busy or debounced (`retry_after` included) ·
`400` invalid · `401` bad/missing token. CLI exit codes: `0` ok · `1` failure
· `2` busy/debounced · `3` invalid.

## Configuration (`config.yaml`)

- **Modes** — **loop** (default) engages on a live freeze frame, then upgrades
  to a replay of a quiet window from the camera's own recordings (keeps the
  scene's usual micro-motion). **freeze** (`--mode freeze`) engages on the live
  frame and STAYS there — a truly frozen scene, no loop upgrade.
- `capture.seconds` / `max_loop_seconds` — target and maximum loop length
  (default 300 / 1200 s). Longer loops hide transients better; the search fills
  up to `max_loop_seconds` when the scene allows.
- `capture.source` — `recordings` (default): loops sourced from Frigate's own
  recordings (read directly from the segment files; export API is the
  automatic fallback). `restream` = loop from a live capture.
- **Loop window search** (`capture.recordings`) picks windows that are
  event-clear and lighting-matched to the live frame in BOTH dimensions:
  day/night (IR) mode via verified monochrome classification (`match_ir_mode`
  — IR switchover keeps luma day-like but goes monochrome, so brightness
  alone can't see it; a frame counts as IR only when nearly every probe cell
  is individually neutral, so dark/muted colour scenes don't false-match) and
  hotspot-trimmed mean brightness (`max_brightness_delta`, ±60). Loop
  windows also reject internal drift (`max_brightness_drift`, ±20) and windows
  containing the IR flip. Lookback `search_hours` (4 h); overlapping cameras
  loop the same moment (`sync_tolerance_minutes`, ±10); audio replaced with
  silence (`recordings.audio`).
- **Dilution** (`capture.recordings.dilute`) — when no absolutely-quiet window
  is long enough, accept a LONGER window with sparse transient activity
  (≤ `max_activity_fraction`, default 10 %) as long as it contains none of
  `block_labels` (always `person` — a looping person is a disclosure). One car
  in a 20-minute loop is invisible; a short perfect loop isn't always
  available, and fooling the eye beats absolute quiet you can't get.
- `streams.include` / `exclude` — glob rails on go2rtc stream names, applied
  after camera resolution.
- `profiles.<name>` — `mode: loop|freeze` + `cameras:` (frigate camera names,
  `[]` = all; `stream:<name>` entries target go2rtc streams directly) +
  optional `capture_seconds` / `source`.
- There is **no `on_failure` and no `fallback`**: a stream is never left live.
  If nothing loopable exists it stays on its freeze frame; if the camera is
  offline the freeze ladder descends recorded → cached → black. `abort` is
  reserved for infrastructure failure (config rejected, go2rtc won't reload).
- `dejavu on --dry-run` shows, per stream, the loop window that would be
  used (or why it would stay on the freeze frame) without touching anything.
- **Logging** (SPEC §10.4): `docker logs frigate-dejavu` (Portainer/Dozzle)
  shows every toggle run — REST-triggered AND `docker exec` runs — ending in
  a per-stream table of the ladder rung each stream landed on. Repeated
  in-loop entries (candidate rejections) are collapsed to one grouped summary
  per camera. For full per-candidate forensics set `DEJAVU_LOG_LEVEL=DEBUG`
  (env) and read `docker logs` — start there when a camera did something
  surprising.

## How it works / recovery

State machine (`data/state/state.json`, flock-guarded, shared by CLI + API):

    off → capturing → applying → on → restoring → off
    off during capturing = cancel (kills captures, deletes partials,
    frigate untouched); failures after the config save → error.

- Capture happens **before** any config change; Frigate's validation gates
  the save (a rejected save changes nothing).
- After each restart the appliance verifies via go2rtc's API that the swap
  (or restore) actually landed; on failure it retries with a
  `docker restart frigate` fallback, then rolls back automatically.
- Pristine whole-file backups live in `data/state/backup.*.yaml`. Restore is
  surgical (only the recorded streams are swapped back, other config edits
  made while on are preserved; hand-edits to replaced streams are overwritten
  with a drift copy saved). `dejavu force-restore` writes the pristine
  backup verbatim if all else fails.
- Appliance restarts while `on` are safe — state, clips, and backups are on
  disk; frigate keeps looping regardless.

## Home Assistant integration

The REST API makes a clean Home Assistant control. Point HA at the appliance
(`http://<appliance-host>:8898`) and wire it up:

- `secrets.yaml` — `frigate_privacy_{on,off,status}_url` (+ commented
  `frigate_privacy_bearer` for when a token is enabled).
- `rest_command.yaml` — one `on` command per profile
  (`frigate_privacy_{perimeter,indoor,all}_on`) + the global
  `frigate_privacy_off`.
- `rest.yaml` — `sensor.frigate_privacy_state` polls `/api/dejavu/status`
  every 20 s; state = `off|capturing|applying|on|restoring|error`, with
  `profile`, `mode`, `streams`, `drift_detected`, `last_error`, `frigate`
  as attributes.
- `template.yaml` — **`select.frigate_privacy`**: `Off / Perimeter / Indoor /
  All Cameras`. Profiles are mutually exclusive (the appliance runs one at a
  time and is the gatekeeper), so the HA model is a selector, not switches.
  Backed by the sensor (CLI/API toggles reflect here too), `optimistic` for
  instant UI, engaging/error states shown via the icon.

Nested applies are **gated by the appliance**: selecting a profile while
another is active returns 409 and the select snaps back on the next poll —
go through `Off` first. Engage times: perimeter/indoor ~1.5 min, all
~3.5 min (exports serialize — see §16); disengage ~20 s.

To add or repoint a profile: edit `profiles:` in `config.yaml` +
`docker restart frigate-dejavu`, then add a matching `rest_command`, a new
option in the select's `options` list, and a `choose:` branch in its
`select_option`. Apply HA changes with **Developer Tools → Actions →
`homeassistant.reload_all`** (no restart) or a container restart.
`shell_command: docker exec frigate-dejavu dejavu on --profile <name>`
works wherever the docker CLI is available.

## Caveats

- Recordings/detection during privacy contain the fake feed — that is the
  feature. Camera-burned timestamp overlays show the source window's time
  (frozen on the freeze frame, or the loop window once upgraded) — the one
  tell that can't be fixed without transcoding.
- Lighting is matched at engage time; a multi-hour session that crosses
  dusk/dawn will drift on a held freeze frame. Loop mode re-matches each time
  it upgrades; re-toggle across a big transition if you want a fresh match.
- No frigate restart (go2rtc-only swap); the loop upgrade lands a few seconds
  to a minute after engage, in the background.
- Offline cameras can't be looped, but they are NEVER left live: they engage
  on a live frame if reachable, else the newest recorded / last cached / a
  black frame (ladder in SPEC §5c), flagged by rung in `status` and the logs.
- The first restore may normalize a few folded long lines in `config.yml`
  (values identical; comments/quoting/secrets placeholders preserved).
- The clips dir rides frigate's existing `./config` mount
  (`frigate/config/dejavu-clips` ⇄ `/clips` here ⇄ `/config/dejavu-clips`
  in frigate) — no change to the frigate container, ever.
