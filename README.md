# Frigate Déjà Vu

```text
 ____  _____      _     _     __     __ _   _
|  _ \| ____|    | |   / \    \ \   / /| | | |
| | | |  _|   _  | |  / _ \    \ \ / / | | | |
| |_| | |___ | |_| | / ___ \    \ V /  | |_| |
|____/|_____| \___/ /_/   \_\    \_/    \___/

                  D É J À   V U

        live -> prepare freeze/loop -> restart -> privacy
```

Privacy-mode appliance for Frigate. On demand it replaces every camera's feed
with a convincing fake, by rewriting `go2rtc.streams` in Frigate's config (via
Frigate's own REST API). Frigate, recordings, detection, birdseye, HA cards,
and WebRTC viewers all keep working, unaware. Each source change is applied by
one coordinated Frigate restart; switching off restores the original sources
byte-exactly and restarts once more.

With the production `recordings` source, every stream gets a safe freeze
fallback before activation and loop searches run in parallel. Successful loops
replace their freeze clips atomically; a stream with no suitable loop uses its
freeze. The finalized set is then applied with one coordinated restart. The
search prefers LONG, quiet windows because the tell is *repetition*, not a
single car passing — a car once in 20 minutes
reads as ordinary; the same car every 20 seconds screams "loop." With the
production `recordings` source, the fake feed never loops a person, repeats
audio, or serves mismatched lighting. The opt-in `restream` source is a direct
live capture and intentionally bypasses those recordings-based guards.

`SPEC.md` is the design contract; this file is the operator's manual.

## Quick start

Start from the tracked examples; your real configuration and Compose files are
gitignored. Edit every line marked `EDIT`, and create the host directory you
choose for `/clips` before starting the container. Frigate restarts through its
API; the appliance does not need or mount the Docker socket.

```sh
cp config.example.yaml config.yaml
cp compose.example.yaml compose.yaml
# Edit config.yaml and compose.yaml; replace every value marked EDIT.
mkdir -p data
docker compose config          # validate the edited files
docker compose up -d --build

docker exec frigate-dejavu dejavu status
docker exec frigate-dejavu dejavu on --dry-run          # plan only, touches nothing
docker exec frigate-dejavu dejavu on                    # default profile (all cameras, loop)
docker exec frigate-dejavu dejavu on --profile indoor
docker exec frigate-dejavu dejavu on --mode freeze      # engage-and-stay on the freeze frame
docker exec frigate-dejavu dejavu off                   # restore (also cancels a capture)
```

The CLI is blocking and prints progress; the REST API spawns the exact same
CLI detached and returns immediately (poll `/api/dejavu/status`).

## Production reference architecture

The tracked examples are a sanitized reference for the deployed architecture,
not merely a minimal demo: every Frigate camera consumes a local go2rtc
restream; Déjà Vu shares Frigate's existing config mount for clips, reads the
recordings mount read-only, joins the same Docker network, and restarts Frigate
through its API. It neither mounts the Docker socket nor sits in the live media
path. Different addresses, mounts, and camera brands are expected elsewhere;
changing that topology or restart model requires revalidation. See SPEC §2 for
the production versions and verified constraints.

## REST API

The example publishes `http://127.0.0.1:8898` on the Docker host only. It is
also reachable by container name (`http://frigate-dejavu:8898`) from containers
on Frigate's Docker network. Before exposing it to the LAN for Home Assistant,
set `DEJAVU_API_TOKEN` and change the Compose port mapping to `8898:8898` (or
put it behind an authenticated reverse proxy).

```sh
curl -X POST http://127.0.0.1:8898/api/dejavu/on     # 202; default profile (all cameras in the reference config)
curl -X POST http://127.0.0.1:8898/api/dejavu/on \
     -H 'Content-Type: application/json' \
     -d '{"profile": "indoor"}'                              # 202; also: mode, cameras, capture_seconds, source
curl -X POST http://127.0.0.1:8898/api/dejavu/off     # 202 (409 while applying/restoring)
curl      http://127.0.0.1:8898/api/dejavu/status     # state, per-stream phases, drift, frigate health
curl      http://127.0.0.1:8898/api/dejavu/profiles
curl      http://127.0.0.1:8898/healthz                # liveness, never needs auth
```

Bearer auth is optional for loopback/trusted-network use: set
`DEJAVU_API_TOKEN` in `.env` (see `.env.example`) and every `/api/*` request
must send `Authorization: Bearer <token>`. Empty/unset = auth disabled; do not
publish the API beyond the Docker host without a token or equivalent proxy
authentication.

An empty `on` request uses the default profile. `"cameras": []` explicitly
means all Frigate cameras. Malformed JSON, unknown fields, blank camera names,
and invalid field types are rejected with `400` rather than silently ignored.

Codes: `202` accepted · `409` busy or debounced (`retry_after` included) ·
`400` invalid · `401` bad/missing token. CLI exit codes: `0` ok · `1` failure
· `2` busy/debounced · `3` invalid.

## Configuration (`config.yaml`)

- **Modes** — **loop** (the configured default profile) searches for a replay
  of a quiet window from the camera's own recordings (keeps the scene's usual
  micro-motion), using a prepared freeze frame when no loop is suitable.
  **freeze** (`--mode freeze`) prepares one frame through the fallback ladder—a
  truly frozen scene, with no loop search.
- `capture.seconds` / `max_loop_seconds` — target and maximum loop length
  (default 300 / 1200 s). Longer loops hide transients better; the search fills
  up to `max_loop_seconds` when the scene allows.
- `capture.source` — `recordings` (production default): loops sourced from
  Frigate's own recordings. Segment files are the fast path; a missing or
  unreadable candidate, or failed direct assembly, automatically retries through
  Frigate's export API. A failed export advances to the next ranked candidate.
  `restream` captures a live loop directly and
  therefore does not apply recordings metadata, lighting, or person-exclusion
  guards.
- **Loop window search** (`capture.recordings`) picks windows that are
  event-clear and lighting-matched to the live frame in BOTH dimensions:
  day/night (IR) mode via verified monochrome classification (`match_ir_mode`
  — IR switchover keeps luma day-like but goes monochrome, so brightness
  alone can't see it; a frame counts as IR only when nearly every probe cell
  is individually neutral, so dark/muted colour scenes don't false-match) and
  hotspot-trimmed mean brightness (`max_brightness_delta`, ±60). Loop
  windows also reject internal drift (`max_brightness_drift`, ±20) and windows
  containing the IR flip. Equivalent survivors are ranked by visual similarity
  between their first and last frames, reducing the visible loop seam. Lookback
  `search_hours` (4 h); overlapping cameras loop the same moment
  (`sync_tolerance_minutes`, ±10); audio replaced with silence
  (`recordings.audio`).
- **Dilution** (`capture.recordings.dilute`) — when no absolutely-quiet window
  is long enough, accept a LONGER window with sparse transient activity
  (≤ `max_activity_fraction`, default 10 %) as long as it contains none of
  `block_labels` (always `person` — a looping person is a disclosure). One car
  in a 20-minute loop is invisible; a short perfect loop isn't always
  available, and fooling the eye beats absolute quiet you can't get. Full quiet
  or diluted windows are searched before short fallbacks, even when the short
  window is newer; the lighting/IR guards decide whether the older loop is safe.
- `streams.include` / `exclude` — glob rails on go2rtc stream names, applied
  after camera resolution.
- `profiles.<name>` — optional `mode: loop|freeze` (omitted = the safe `freeze`
  default) + `cameras:` (frigate camera names,
  `[]` = all; `stream:<name>` entries target go2rtc streams directly) +
  optional `capture_seconds` / `source`.
- There is **no `on_failure` and no `fallback`**: a stream is never left live.
  With the production recordings source, if nothing loopable exists it stays on
  its freeze frame; an offline camera descends recorded → cached → black. An
  opt-in restream loop capture instead refuses activation if capture fails. If
  any requested camera cannot be resolved to a replaceable go2rtc stream, or any
  stream fails the engage ladder, the whole activation is refused while
  Frigate's config is still untouched; partial privacy is never reported as on.
- `dejavu on --dry-run` shows the replacement plan and metadata-derived window
  candidates without touching anything. Lighting, IR, drift, and seam guards
  run only during real capture, so dry-run does not promise the final window.
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
  (or restore) actually landed. A failed activation is rolled back through the
  Frigate API when it remains reachable; otherwise the error calls for an
  operator-managed Frigate restart.
- Pristine whole-file backups live in `data/state/backup.*.yaml`. Restore is
  surgical (only the recorded streams are swapped back, other config edits
  made while on are preserved; hand-edits to replaced streams are overwritten
  with a drift copy saved). `dejavu force-restore` writes the pristine
  backup verbatim if all else fails.
- Appliance restarts while `on` are safe — state, clips, and backups are on
  disk; frigate keeps looping regardless.
- The first activation installs the namespaced
  `go2rtc.ffmpeg.frigate_dejavu_loop` support template. Normal OFF retains it;
  it is unused in the live media path and avoids lifecycle churn. A whole-file
  `force-restore` can remove it with the pristine backup, and the next activation
  reinstalls it. A conflicting value under that reserved key is refused rather
  than replaced.

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
~3.5 min (exports serialize — see §16). Both directions also include a full
Frigate restart and its cold-boot time.

To add or repoint a profile: edit `profiles:` in `config.yaml` +
`docker restart frigate-dejavu`, then add a matching `rest_command`, a new
option in the select's `options` list, and a `choose:` branch in its
`select_option`. Apply HA changes with **Developer Tools → Actions →
`homeassistant.reload_all`** (no restart) or a container restart.
`shell_command: docker exec frigate-dejavu dejavu on --profile <name>`
works wherever the docker CLI is available.

## Caveats

- Recordings/detection during privacy contain the fake feed — that is the
  feature. Camera-burned timestamp overlays show the prepared freeze frame or
  recorded loop window's source time — the one
  tell that can't be fixed without transcoding.
- Lighting is matched at engage time; a multi-hour session that crosses
  dusk/dawn can drift from the preselected freeze or loop. Re-toggle across a
  big transition to run preparation and lighting matching again.
- Engage and restore each use one coordinated Frigate restart so go2rtc
  producers and Frigate camera consumers are rebuilt in the correct order.
  Loop searches finish before engage; unsuccessful streams use their prepared
  freeze frames.
- Offline cameras can't be looped, but they are NEVER left live: they engage
  on a live frame if reachable, else the newest recorded / last cached / a
  black frame (ladder in SPEC §5c), flagged by rung in `status` and the logs.
  Cached frames retain the last known codec, dimensions, frame rate, and audio
  shape so permanently offline cameras still produce compatible freeze clips.
- The first restore may normalize a few folded long lines in `config.yml`
  (values identical; comments/quoting/secrets placeholders preserved).
- The clips dir rides frigate's existing `./config` mount
  (`frigate/config/dejavu-clips` ⇄ `/clips` here ⇄ `/config/dejavu-clips`
  in frigate) — no change to the frigate container, ever.
