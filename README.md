# frigate déjà vu

```text
       ____   ____     _     _     __ _   _
      |  _ \ |___ |   | |   / \   / /| | | |
      | | | |  |_ |_  | |  / _ \ / / | | | |
      | |_| | __| | |_| | / ___ V /  | |_| |
      |____/ |____|\___/ /_/   \_/    \___/

                    D É J À   V U

                live -> freeze -> loop
```

## Frigate Incognito mode

Frigate dejavu allows you to swap live go2rtc camera feeds with convincing quality video loops or frozen frames by rewriting the go2rtc streams in Frigate’s configuration through its REST API on demand. After an automatic restart, recordings, detection, birdseye, Home Assistant cards, and WebRTC viewers all keep working. Turning privacy off restores the original stream sources and restarts Frigate again.

Dejavu privacy engages in two stages. Stage 1 prepares a freeze frame for every profiled stream, rewrites the go2rtc sources to point at those clips, and goes on through one coordinated Frigate restart—privacy is immediate, guaranteed, and permanent, and a stream is never left live. Stage 2 (loop mode only) then runs in the background while already private: it searches each camera's recent recordings for quiet or sparsely active scenes using Frigate activity, lighting, IR mode, drift, and loop-seam similarity, copies the winner while silencing audio (by default), atomically swaps each loop over its freeze clip at the same path, and swaps them in through a second coordinated Frigate restart. A stream whose loop is not ready in time stays on its freeze frame permanently. 

If you want more raw video, set `mode: loop` with the opt-in `restream` source to capture live video directly, bypassing all recordings-based safety and ranking guards. 

It's so convincing that it's unreal. Don't forget to turn privacy mode off!

## Quick start

Start with the tracked examples. Real configuration, Compose files, state, and
clips are gitignored.

```sh
cp config.example.yaml config.yaml
cp compose.example.yaml compose.yaml
mkdir -p data

# Edit every value marked EDIT, then validate and start.
docker compose config
docker compose up -d
```

Common controls:

```sh
docker exec frigate-dejavu dejavu status
docker exec frigate-dejavu dejavu on --dry-run
docker exec frigate-dejavu dejavu on
docker exec frigate-dejavu dejavu on --profile indoor
docker exec frigate-dejavu dejavu on --mode freeze
docker exec frigate-dejavu dejavu off
```

The CLI blocks and prints progress. The REST API starts the same CLI job in the
background and returns immediately; poll `/api/dejavu/status` for progress.

Examples consistently use `frigate` for the Frigate service and
`frigate-dejavu` for this container and its client-facing hostname.

## Production reference architecture

The tracked examples are a sanitized version of a tuned architecture, not
a toy configuration:

```text
cameras -------> go2rtc ┬-> Frigate -> recordings / detection / Birdseye
prepared clips ---^     └-> WebRTC / Home Assistant

Déjà Vu -> Frigate REST API: validate config, save sources, restart, verify
```

- Every Frigate camera consumes a local `go2rtc` restream.
- Déjà Vu shares Frigate's existing config mount for clips.
- Frigate's recordings mount is read-only; its export API covers missing or
  unreadable segments.
- Déjà Vu reaches Frigate and embedded `go2rtc` through the configured endpoints.
- Restarts go through Frigate's API. No Docker socket is mounted.

Different addresses, mounts, and camera brands are expected. Changing the
topology or restart model requires revalidation; see SPEC §2 for the verified
production versions and constraints.

## REST API

The example publishes the API at `http://frigate-dejavu:8898`. If that name does
not resolve, replace it with the Docker host's hostname or address.

```sh
# Engage the default profile; returns 202.
curl -X POST http://frigate-dejavu:8898/api/dejavu/on

# Engage a named profile; mode, cameras, capture_seconds, and source are optional.
curl -X POST http://frigate-dejavu:8898/api/dejavu/on \
     -H 'Content-Type: application/json' \
     -d '{"profile": "indoor"}'

# Restore live sources; returns 202, or 409 while another transition is active.
curl -X POST http://frigate-dejavu:8898/api/dejavu/off

# Current state, per-stream phases, drift, and Frigate health.
curl http://frigate-dejavu:8898/api/dejavu/status

# Available profiles and their effective settings.
curl http://frigate-dejavu:8898/api/dejavu/profiles

# Liveness; never requires authentication.
curl http://frigate-dejavu:8898/healthz
```

An empty `on` request uses the default profile. `"cameras": []` explicitly means
all Frigate cameras. Malformed JSON, unknown fields, blank names, and invalid
types return `400`.

API responses: `202` accepted · `400` invalid · `401` bad or missing token ·
`409` busy or debounced (`retry_after` included).

CLI exits: `0` success · `1` failure · `2` busy or debounced · `3` invalid.

### Authentication

API authentication is optional on loopback or a trusted network. When
`api.bearer_token` resolves from `DEJAVU_API_TOKEN`, every `/api/*` request must
send `Authorization: Bearer <token>`. `/healthz` remains public.

Do not expose an unauthenticated API beyond the Docker host without an
authenticating reverse proxy.

## Configuration

Copy [config.example.yaml](config.example.yaml) to `config.yaml`. Most deployments
only need to change the Frigate endpoints, volume paths, and profile camera names.

### Capture modes and sources

| Mode | Source | What it does | Fallback |
|---|---|---|---|
| `loop` | `recordings` | Freeze on via a restart, then a background loop upgrade lands via a second restart | Stay on freeze |
| `loop` | `restream` | Captures a live loop directly; bypasses recordings-based guards | Abort activation |
| `freeze` | `recordings` | Builds one freeze clip | Live → recorded → cached → black |
| `freeze` | `restream` | Builds one freeze clip without searching recordings | Live → cached → black |

`recordings` is the production default. The stage-2 loop upgrade reads directly
from the `/recordings` mount only—there is no export-API fallback for loop
assembly, because stage 2 runs after the engage restart while Frigate's export
API is still settling. Loop mode therefore requires a directly-readable
`/recordings` mount; without one, its streams simply stay on their freeze frames.
A profile without `mode` defaults safely to `freeze`.

`capture.seconds` and `max_loop_seconds` set the target and maximum loop lengths
(300 and 1200 seconds by default).

### Recording search

The defaults favor believable loops without turning tuning into a science project:

- `search_hours: 4` bounds the lookback.
- `match_ir_mode` rejects day/night mismatches.
- `max_brightness_delta` compares a candidate with the camera now.
- `max_brightness_drift` rejects dawn, dusk, and lighting ramps inside a loop.
- Equivalent candidates are ranked by first/last-frame similarity to reduce the
  visible seam.
- `sync_tolerance_minutes` keeps overlapping cameras near the same source time.
- `recordings.audio: silence` removes repeating audio.

`recordings.dilute` is the practical fallback for busy cameras. It permits a long
window with sparse tracked activity, bounded by `max_activity_fraction`, while
`block_labels` must include `person`. These are Frigate metadata guarantees, not
independent video recognition.

### Streams and profiles

- `streams.include` and `streams.exclude` are glob rails applied after camera names
  resolve to `go2rtc` streams.
- `profiles.<name>.cameras` contains Frigate camera names; `[]` means all cameras.
- `stream:<name>` targets a `go2rtc` stream directly.
- Profiles may override `mode`, `capture_seconds`, and `source`.
- Request and CLI options override the selected profile.

There is no configurable `on_failure` or `fallback`: a selected stream is never
quietly left live. Unresolvable streams or a failed engage ladder abort the whole
activation before Frigate's configuration changes. Partial privacy is never
reported as `on`.

### Variables, dry runs, and logs

String values may contain `{DEJAVU_NAME}` placeholders. Only names beginning with
`DEJAVU_` are expanded, and every referenced variable must exist. An explicitly
empty value is valid for optional credentials.

`dejavu on --dry-run` shows the replacement plan and metadata-derived candidate
windows without touching Frigate. Lighting, drift, IR, and seam guards run during
real capture, so the final winner may differ.

Use `docker logs frigate-dejavu` first when a camera behaves unexpectedly. Every
job ends with a per-stream result table. Set `DEJAVU_LOG_LEVEL=DEBUG` for individual
candidate decisions.

## Safety and recovery

State is persisted in `data/state/state.json` and shared by the CLI and API:

```text
off -> capturing -> applying -> on -> restoring -> off
```

- Stage-1 freeze capture finishes before any configuration change (the loop
  upgrade runs later, in stage 2).
- Frigate validates the proposed configuration before saving it.
- After restart, Déjà Vu verifies through `go2rtc` that every replacement landed.
- A failed activation rolls back automatically while Frigate remains reachable.
- Turning off during capture cancels workers, removes partial clips, and leaves
  Frigate untouched.
- State, clips, and backups survive a Déjà Vu restart while privacy is active.

Pristine backups live in `data/state/backup.*.yaml`. Normal restore is surgical:
it restores only the source lists Déjà Vu replaced, preserving unrelated edits.
Conflicting edits to an active replacement are saved as drift copies.
`dejavu force-restore` writes the pristine backup verbatim when surgical recovery
is not enough.

The first activation installs the reserved
`go2rtc.ffmpeg.frigate_dejavu_loop` template. It remains installed but unused after
normal restore, avoiding needless config churn. A conflicting value is refused,
never overwritten.

## Home Assistant

Ready-to-use examples live in [homeassistant/](homeassistant/README.md). They add:

- REST commands for the example profiles;
- a status sensor polled every 20 seconds; and
- `select.frigate_privacy` with `Off`, `Perimeter`, `Indoor`, and `All Cameras`.

The selector follows changes made through Home Assistant, the API, or the CLI.
Profiles are mutually exclusive, so select `Off` before changing active profiles.

## Limits worth knowing

- Recordings and detections created during privacy contain the replacement feed.
- Camera-burned timestamps retain the freeze or loop's source time. Fixing that
  would require transcoding.
- Lighting is matched when privacy engages. Re-toggle after a major dawn/dusk
  transition during a long session.
- Engaging in `loop` mode restarts Frigate twice a short time apart: restart #1
  turns privacy on with freeze frames, then the background loop upgrade lands via
  restart #2 (up to `capture.loop_assembly_budget_seconds` later — a second,
  delayed, brief recording/detection gap after the appliance already reported
  `on`). `freeze` mode and switch-off each restart Frigate once. Every restart
  lets `go2rtc` and Frigate consumers rebuild in the correct order.
- Offline cameras cannot produce a new loop. Their freeze ladder is live frame →
  recorded frame → cached frame → black, with the selected rung visible in status
  and logs.
- The first restore may reformat a few folded lines in Frigate's YAML without
  changing their values, comments, quoting, or secret placeholders.
- The clip directory must be the same storage mounted as `/clips` in Déjà Vu and
  `/config/dejavu-clips` in Frigate.
