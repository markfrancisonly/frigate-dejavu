# Frigate Déjà Vu — SPEC

Privacy-mode appliance for Frigate. A small sidecar container that, on demand,
replaces live camera streams with pre-recorded loops (or frozen frames) at the
go2rtc layer, so Frigate and every downstream consumer keep running unaware
while the cameras are effectively "off". This document is the contract for the
implementation.

Security and cybersecurity considerations are explicitly OUT OF SCOPE (owner's
call): the bearer token is a convenience latch, not a security boundary, and
the API is assumed to live on a trusted network.

---

## 1. Problem & approach

Frigate is production CCTV (recording, detection, HA integration, WebRTC
viewers). Sometimes the household wants cameras "paused" without stopping
recording infrastructure, tipping off consumers, or leaving config drift.

Approach: every Frigate camera in this deployment consumes its video through
the embedded go2rtc restream (`ffmpeg.inputs[].path: rtsp://127.0.0.1:8554/
<stream>?mp4`, `input_args: preset-rtsp-restream`). Swapping the **source list
of the go2rtc stream** — from the camera URL to a looping local file with
matching codecs — changes what every consumer sees (Frigate record/detect,
birdseye, HA cards, WebRTC/MSE viewers, HomeKit) while none of their configs,
URLs, or negotiated codec families change. Frigate's own camera config is
never touched.

Sequence, in one line:

    capture N seconds of each stream → write clips → edit go2rtc.streams in
    Frigate config (via Frigate REST API) → restart Frigate → looped playback
    ("privacy on") … → restore original stream sources → restart → live again.

## 2. Verified environment (2026-07-07)

| Fact | Value |
|---|---|
| Frigate | `ghcr.io/blakeblackshear/frigate:stable-tensorrt`, version **0.17.2** |
| Config API | `GET /api/config/raw` → 200 (raw YAML), `POST /api/config/save?save_option=saveonly\|restart`, `POST /api/restart` |
| go2rtc API | `http://frigate:1984` → 200 (`/api/streams`, `/api/config`) |
| go2rtc RTSP | `rtsp://frigate:8554/<stream>` (+ codec filter query, e.g. `?mp4`) |
| Camera topology | ALL cameras use `rtsp://127.0.0.1:8554/camera.X?mp4` restream inputs; sources are Amcrest/Reolink/Hikvision RTSP, one `rtspx://` (Bambu), several HomeKit/tablet feeds, and `ffmpeg:birdseye` |
| Secrets in config | `{FRIGATE_*}` runtime-env placeholders — preserved verbatim (we never rewrite source lines we don't replace, and replaced lines are restored byte-identical) |
| Frigate networks | same Docker network as Frigate (name-resolvable as `frigate`) |
| Config mount | `./config:/config` (host `/docker/frigate/config`) — reused for the shared clips dir, so **no change to the frigate container** is needed |
| Restart policy | `restart: always` — required by Frigate's `/api/restart` semantics (exits the container; Docker brings it back, regenerating go2rtc config) |

## 3. Architecture

```
                        ┌──────────────────────────────────────────┐
 HA / curl / scripts ──▶│ frigate-dejavu container                │
   REST (bearer opt.)   │  api.py (Flask)  ──spawns──▶ dejavu  │
                        │                                (CLI)     │
 docker exec ─────────▶ │  dejavu on|off|status|cancel …       │
                        │      │ core.py: state machine, flock,    │
                        │      │          debounce, job runner     │
                        │      ├─ frigate.py: Frigate/go2rtc REST, │
                        │      │   raw-YAML edit (ruamel round-trip)│
                        │      └─ capture.py: ffprobe/ffmpeg       │
                        │  /clips  = /docker/frigate/config/       │
                        │            dejavu-clips (shared mount)  │
                        └──────────┬───────────────────────────────┘
                                   │ HTTP :5000 (config, restart)
                                   │ HTTP :1984 (verify streams)
                                   │ RTSP :8554 (capture source)
                                   ▼
                              frigate container
                              (go2rtc reads /config/dejavu-clips/*.mp4)
```

The REST layer is a thin shell over the CLI: **`api.py` executes `dejavu`
as a subprocess** — so `docker exec frigate-dejavu dejavu on` and
`POST /api/dejavu/on` are literally the same code path. All coordination
(lock, debounce, state) lives in the filesystem so both entry points agree.

## 4. Core mechanism: the go2rtc stream swap

### 4.1 Stream resolution

Profiles select **Frigate camera names** (e.g. `kitchen`, `back_patio`). For each
selected camera the appliance parses the Frigate config (fetched live from
`/api/config/raw`) and collects go2rtc stream names from
`cameras.<name>.ffmpeg.inputs[].path` matching
`rtsp://127.0.0.1:8554/<stream>[?query]` (also `localhost`). The union of
these, deduplicated, filtered through the global `streams.include` /
`streams.exclude` globs, is the replacement set.

- A camera with no restream-style input is reported as `unsupported` in the
  job result and skipped (none exist in this deployment today).
- `camera.birdseye` (frigate-generated composite) and the tablet/wallpanel
  feeds are excluded by default via config, not code.

### 4.2 The swap

For each stream `S`, the whole source list under `go2rtc.streams.S` in
Frigate's config is replaced with a single source:

```yaml
go2rtc:
  ffmpeg:
    frigate_dejavu_loop: -re -stream_loop -1 -i {input}   # installed with the swap, removed on restore
  streams:
    camera.kitchen:
      - "ffmpeg:/config/dejavu-clips/camera.kitchen.dejavu.mp4#input=frigate_dejavu_loop#video=copy#audio=copy#audio=opus#audio=pcmu"
```

- `#input=frigate_dejavu_loop` selects a named go2rtc input template the
  appliance installs alongside the swap: ffmpeg reads the file at native
  speed and loops it forever. Each loop restarts at the file's leading IDR
  frame, so consumers resync within one frame at the loop point.
  (A raw inline `#input=… {input}` is NOT usable here — Frigate's go2rtc
  config generator `str.format()`s stream source values and crash-loops
  go2rtc on any non-`{FRIGATE_*}` brace pattern; the `go2rtc.ffmpeg`
  template section is passed through untouched. Verified live — §16.)
- `#video=copy#audio=copy` passes the captured H.264/H.265 + AAC through
  untouched — codec parameters are byte-identical to what the camera produced
  (see §5). Additional `#audio=` transcode offers are mirrored from the
  *original* source line (e.g. Amcrest streams offer `opus` + `pcmu` for
  WebRTC/HomeKit consumers), so the stream's codec menu is unchanged.
- Sources with no audio (Hikvision `#video=copy`) get `#video=copy` only.
- The original source list is stored (exact scalars, order preserved) in the
  state dir for restore.

Everything else in the config — the `cameras:` tree, secrets placeholders,
comments, anchors — is preserved: the edit is a ruamel.yaml round-trip that
touches only the replaced `go2rtc.streams.<S>` values.

### 4.3 Why consumers don't notice

- Frigate reconnects to the same `rtsp://127.0.0.1:8554/S?mp4` after restart;
  the SDP offers the same codec family/resolution, `preset-rtsp-restream` and
  `preset-record-generic-audio-aac` behave identically.
- Recordings record the loop; detection sees a (mostly) static scene; birdseye
  composes the loops; HA/WebRTC viewers renegotiate against the same offers.
- The restream codec-filter query (`?mp4`) keeps matching because captures are
  taken *through* that same filter.

## 5. Capture primitives

The building blocks the two-phase engage (§5c) composes. All ffmpeg; all
cancellable; all run **before** any config is modified, so cancellation during
capture leaves zero footprint.

- **Live frame** (`frame_from_live`) — probe `rtsp://frigate:8554/S?mp4`
  (video codec, width, height, avg fps, audio presence), grab one frame
  (`-frames:v 1` → PNG at native resolution). This is rung 1 of the ladder and
  the source of the lighting reference (§5c).
- **Freeze synthesis** (`synthesize_freeze`) — a short clip
  (`capture.freeze_clip_seconds`, default 4 s) from one still: `libx264`
  (`libx265` if the source is H.265), source resolution/fps, `yuv420p`,
  GOP = 2 s, plus silent AAC (`anullsrc`) iff the source has audio. Served
  exactly like a loop, so go2rtc-side playback is always `-c copy` (zero
  transcode CPU while on).
- **Loop capture** (`capture_clip`, `--source restream` only) —
  `ffmpeg -i rtsp://…?mp4 -t {seconds} -c copy`. go2rtc starts new consumers
  at a keyframe, so the file begins with an IDR and `-c copy` matches the
  codec by construction.
- **Placeholder** (`synthesize_placeholder`) — a black clip in the source's
  codec family. The floor of the ladder (rung 4): maximally private, and for a
  camera that is already dark, indistinguishable from its normal no-signal.
- **Validate** — output exists, ffprobe parses it, video present, duration
  ≥ 90 % of requested.

H.264 and H.265 sources are both supported; a synthesized clip's encoder
profile/level may differ from the camera's — acceptable, consumers negotiate
via SDP. Loop-point timestamp reset produces a known, harmless ffmpeg DTS
warning in go2rtc logs.

## 5b. Loop sourcing from recordings (the loop upgrade)

Live capture loops whatever happens right after the toggle — a passing car
re-appearing every 60 s, or a person frozen mid-stride — so loops are sourced
from Frigate's own recordings, which are codec-identical (same `?mp4`
restream) and already carry Frigate's object/event metadata. This is the
**upgrade** half of §5c; the freeze frame is already serving before any of it
runs.

The single most important principle (owner directive): **the tell is
repetition, not the presence of a transient.** A 20-minute window in which a
car passes once reads as an ordinary quiet scene; a 60-second window in which a
bush sways every loop screams "loop." So the search prefers LONG windows and
tolerates sparse activity, rather than insisting on short absolute silence.

Per stream, parallelized (`capture.parallel`):

1. **Candidate search** over the last `recordings.search_hours` (default 4 h):
   `GET /api/<camera>/recordings?after&before` per-segment `objects`/`motion`
   + `GET /api/events` overlap (±2 s pad, labelled; open-ended events block
   onward). Candidates are produced in three tiers, best first:
   - **quiet** — every covering segment has zero objects and no event overlap;
     ≥ `capture.seconds` (default 300) long, capped at `capture.max_loop_seconds`
     (default 1200). Absolute privacy.
   - **diluted** (`recordings.dilute`, default on) — a longer window
     (≥ `dilute.min_seconds`) containing NO `dilute.block_labels` event (always
     includes `person` — replaying a person is a *disclosure*, not a cosmetic
     tell) and ≤ `dilute.max_activity_fraction` (default 10 %) of its length
     under any tracked object. A sliding scan finds the calmest such window
     anywhere in a person-free run, not just its tail.
   - **short-quiet** — a fully-quiet run shorter than target but
     ≥ `recordings.min_seconds`. Last resort before the freeze frame stands.

   Ranking: hour-age bucket first (the proxy for the current lighting regime,
   so the guards reject fewer candidates), then tier, then LONGEST, then least
   motion, then most recent. Length tests tolerate ±0.5 s of segment jitter
   (a 20 s run of two nominal-10 s segments measures 19.99 s).
1b. **Cross-camera sync** (`recordings.sync_tolerance_minutes`, default 10;
   0 disables): overlapping views loop the SAME moment. A pre-pass picks the
   most recent anchor on Frigate's server timeline covered by the most cameras;
   each camera's candidates are tried anchor-first. (Freeze frames need no
   sync — every camera freezes on a frame of *now*, the same instant by
   construction.)
2. **Segment settle** — a fresh window's newest segment is read only once its
   file size is stable across a re-stat (`_segment_settled`); older windows
   skip the check. This *directly* tests "Frigate finished writing this," which
   the old blunt 30 s freshness margin only approximated — and that margin was
   the 2026-07-07 kitchen bug (§16): the only IR-mode event-clear segments in
   the lookback were 11–21 s old and were skipped unread.
3. **Lighting guards** (the frame must match how the camera looks NOW), from
   one 16×16 yuv444p probe per candidate segment — probed **directly on the
   segment files**, no export or concat, so screening is cheap and deep (the
   whole candidate list within a `GUARD_BUDGET_SECONDS` wall-clock ceiling, not
   a top-5 cap). Only the survivor is ever stitched.
   - *day/night (IR) match* (`match_ir_mode`, default on): a candidate whose
     colour-vs-IR class differs from the reference is rejected; loop windows
     containing the flip are rejected too. Classification requires a verified
     monochrome frame (low mean chroma AND ≥95% individually-neutral cells) —
     mean chroma alone also matches dark/muted colour frames, which made this
     guard inert on such cameras.
   - *brightness*: hotspot-trimmed luma delta vs reference > `max_brightness_delta`
     (default 60) rejects; loop windows also reject on first-to-last drift
     > `max_brightness_drift` (default 20, dawn/dusk pulse). The last segment
     (nearest now) is probed first, so a stale-lighting candidate costs one
     probe, not two.
4. **Stitch** the winner from its segment files (`-f concat -c copy`; export
   API is the automatic fallback where the recordings mount is absent — still
   strictly serialized, since Frigate's export worker wedges under concurrent
   jobs), then apply the **audio policy** (`recordings.audio`, default
   `silence`: same-rate/channel silent AAC — repeating audio is the most
   obvious tell). The clip's audio presence is pinned to match the freeze frame
   it replaces, so the already-published go2rtc source string still fits.

## 5c. Two-phase engage: instant freeze, background loop upgrade

Privacy must engage the instant the switch is thrown; it must never wait on,
or be defeated by, a slow window search. And a stream is **never left live** —
that is the whole point of the appliance. Both follow from one structure:

**Phase 1 — engage.** Every stream gets a freeze clip via the ladder, and the
config swap makes privacy live:

| rung | source | when |
|---|---|---|
| 1 live | one frame of NOW (lighting-correct by construction; also the guard reference) | normal |
| 2 recorded | newest lighting-matched event-clear recorded frame | camera offline now |
| 3 cached | the last frame this camera ever froze on (kept under `state/lastframe/`) | offline a while |
| 4 black | synthesized placeholder | nothing exists anywhere — loudly logged |

There is no "skip" and no whole-job "abort" for an uncooperative camera: the
ladder bottoms out in a synthesized frame, which still provides privacy. `abort`
is reserved for infrastructure failure (config write rejected, go2rtc won't
reload) — the "died trying, see the logs" last word.

**Phase 2 — upgrade** (loop mode only): a background search per stream (§5b)
looks for a loopable window and, when found, replaces the freeze clip **in
place** — same clip path, so the go2rtc source string is unchanged and the swap
is a clip promote (atomic `os.replace`) plus one reload, not a config edit. If
the search beats `capture.engage_deadline_seconds` (default 15 s) it folds into
the first swap and privacy engages already-looped (one reload); otherwise
privacy engages on freeze frames and each stream upgrades as its window lands.
Every failure in phase 2 leaves the freeze frame serving — privacy is intact
throughout.

Net: time-to-private is ~one live frame + one go2rtc reload (seconds), not
capture-period + restart, and the failure floor moved from "stream stays LIVE"
to "stream shows a still frame."

## 6. Switch-on sequence

1. Acquire the global flock; require state `off`; enforce debounce.
2. State → `capturing` (cancellable). Fetch `/api/config/raw`; store pristine
   backup `state/backup.<ts>.yaml` + `state/backup.current.yaml`.
3. Resolve profile → cameras → streams (§4.1). Empty set → error, state `off`.
4. **Phase 1 — engage** (§5c): a freeze clip per stream via the ladder,
   bounded by `capture.parallel_frames`. Every stream yields a clip; the only
   failures here are bugs or cancellation. Cancellation (an `off`/`cancel`
   arriving now) kills the ffmpeg process group, deletes partial clips,
   state → `off`.
5. **Phase 2 kickoff** (loop mode): background loop searches start here (§5b),
   so their `engage_deadline_seconds` overlaps the config write and swap below.
6. State → `applying` (no longer cancellable). Edit go2rtc.streams (§4.2);
   record per-stream original sources in `state/streams.json`. If the loop
   search beat the deadline, its clips are already promoted so this engages
   already-looped.
7. `POST /api/config/save?save_option=saveonly` (body = edited raw YAML).
   Frigate validates server-side; on rejection → nothing was saved → state
   `off`, error surfaced, clips kept for inspection.
8. Swap via go2rtc service reload (§4.2 / §8) and wait healthy.
9. **Verify**: go2rtc `/api/streams` shows each replaced stream's source is
   the privacy clip. All verified → state `on`. Any stream still live →
   automatic rollback (restore sources, restart again), state `error` with
   detail.
10. **Phase 2 finish**: await stragglers, promote their loop clips in place,
    one more go2rtc reload. Privacy is already `on` throughout; any failure
    here leaves the freeze frames serving.

## 7. Switch-off sequence

1. flock; from `on` (or `error`): state → `restoring`. From `capturing`:
   cancel path per §6.4.
2. Fetch **current** `/api/config/raw` (NOT the backup): surgically swap each
   replaced stream's source list back to the stored originals. Edits the user
   made elsewhere in the config while privacy was on are preserved. If a
   replaced stream's entry was itself hand-edited meanwhile, it is overwritten
   with the original and the drifted version is saved to `state/drift.<ts>.
   yaml` (warned in status + logs). Whole-file backup remains as disaster
   fallback (`dejavu force-restore` writes `backup.current.yaml` verbatim).
3. Save (`saveonly`) → restart → wait healthy → verify original sources are
   live again → state `off`.
4. Clips deleted unless `capture.keep_clips_after_off: true`. State artifacts
   (backups, drift copies) are retained.

## 8. Restart & verification strategy

- Primary: `POST /api/restart` (documented; Frigate exits its container and
  the `restart: always` policy recreates the process tree, which regenerates
  the embedded go2rtc config from `config.yml`).
- Health wait: poll `GET /api/version` until 200, then go2rtc `/api/streams`
  reachable, up to `frigate.health_timeout_seconds` (default 120 s).
- If post-restart verification (§6.8) shows go2rtc did NOT pick up the new
  sources (e.g. an internal-only service restart that skipped go2rtc) and
  `frigate.restart_fallback: true`, the appliance falls back to
  `docker restart frigate` via the mounted Docker socket, then re-verifies.
  `frigate.restart_method: docker` forces the fallback path always.
- Each privacy session costs exactly two Frigate restarts (on + off); the
  recording gap (~10–30 s each) is inherent and accepted.

## 9. State machine, locking, debounce, cancellation

States (persisted in `state/state.json`, updated under an `fcntl` flock on
`state/lock`):

    off ─on→ capturing ─→ applying ─→ on ─off→ restoring ─→ off
                │  ▲                                │
                │  └── (cancel = off during capture)┘
                └──────────────→ off (cancelled)
    any failure past validation → error  (off / force-restore to clear)

- **Lock**: one transition at a time, cross-process (CLI and API share it).
  A transition job records its PID; a dead PID with a transitional state is
  reconciled to `error` on the next command or API start.
- **Debounce**: requests within `api.debounce_seconds` (default 5 s) of the
  last *completed* transition → HTTP 409 / exit code 2.
- **Busy rules**: duplicate direction while a job runs → 409/2. `off` during
  `capturing` → cooperative cancel (SIGTERM to the job's process group; job
  cleans up partial clips and exits to `off`). `on` during `restoring` → 409
  (restore always runs to completion). `off` during `applying` → 409 (window
  is a few seconds; issue `off` again once `on`).
- Appliance restart while privacy `on` is safe: state and backups are on
  disk; Frigate keeps looping clips (config + clips persist) until `off`.

## 10. Interfaces

### 10.1 CLI (`dejavu`, inside the container)

    dejavu on   [--profile NAME] [--mode loop|freeze] [--cameras a,b,…]
                    [--capture-seconds N]      # blocking; progress on stdout
    dejavu off                             # blocking; cancels capture if running
    dejavu cancel                          # alias of off during capture
    dejavu status [--json]
    dejavu profiles
    dejavu force-restore                   # whole-file backup restore + restart

    docker exec frigate-dejavu dejavu on --profile indoor --mode freeze

Exit codes: `0` ok · `1` failure · `2` busy/debounced · `3` invalid request.
`--mode`/`--cameras`/`--capture-seconds` override the chosen profile's values
(`--profile` defaults to `default`).

### 10.2 REST API

| Method & path | Body / params | Behavior |
|---|---|---|
| `POST /api/dejavu/on` | `{"profile"?, "mode"?, "cameras"?, "capture_seconds"?}` | Spawns detached `dejavu on …`; returns **202** + state snapshot. 409 busy/debounce, 400 invalid. |
| `POST /api/dejavu/off` | – | Spawns `dejavu off` (cancels capture if capturing); **202**. |
| `GET /api/dejavu/status` | – | **200** state JSON: `state, profile, mode, since, streams{name: phase/result}, last_error, drift_detected, frigate{reachable, version}`. |
| `GET /api/dejavu/profiles` | – | Profiles as resolved from config. |
| `GET /healthz` | – | Liveness (no auth). |

Auth: iff a bearer token is configured (`DEJAVU_API_TOKEN` env, or
`api.bearer_token`), every `/api/*` request must send
`Authorization: Bearer <token>`; otherwise auth is disabled. Errors are JSON:
`{"error": "...", "state": "..."}`.

The REST handler builds the corresponding `dejavu` argv, spawns it
detached (its own process group), and answers from the state file — it never
holds the transition itself.

### 10.4 Logging (one stream, level-gated)

Everything logs to the **container stdout** (`docker logs` / Portainer /
Dozzle); verbosity is a deployment choice via the `DEJAVU_LOG_LEVEL` env
(default `INFO`):

- **INFO** (default): the run narrative — request, engage ladder summary, ONE
  grouped line per camera for candidate rejections ("rejected 9 candidate(s):
  IR-mode mismatch ×7, brightness drift ×2"), swap/verify results, and the
  per-stream rung table. Anything that fires once per candidate/iteration
  inside a loop is NOT logged at this level.
- **DEBUG** (`DEJAVU_LOG_LEVEL=DEBUG`): full forensics — window search
  parameters and candidate lists, every per-candidate guard rejection with
  measured luma/chroma, lighting-reference stats, per-rung ladder attempts
  with timings.

`docker exec` runs mirror themselves to the container stdout (`/proc/1/fd/1`)
so every run is visible in `docker logs` regardless of how it was started;
the compose json-file driver caps history (10 MB × 5).

Both entry paths reach the container log: REST-spawned jobs inherit the API's
stdout (which IS the container log), and `docker exec dejavu …` runs
mirror INFO to `/proc/1/fd/1` under an `[exec <cmd> pid <n>]` tag — an inode
check makes the mirror a no-op when stdout already is the container log, so
nothing ever double-logs. The compose `json-file` driver is size-capped (§12).

### 10.3 Home Assistant (informative, not a deliverable)

`rest_command` / `switch.template` pointing at `/api/dejavu/on|off` +
`/status`; fits the existing HA setup later.

## 11. Appliance configuration (`config.yaml`)

```yaml
frigate:
  api_url: http://frigate:5000        # Frigate internal (unauthenticated) API
  go2rtc_api_url: http://frigate:1984
  restream_url: rtsp://frigate:8554
  container_name: frigate             # docker fallback restart target
  restart_method: api                 # api | docker
  restart_fallback: true              # docker restart if verify fails after api restart
  health_timeout_seconds: 120

paths:
  clips_local: /clips                 # this container's view
  clips_frigate: /config/dejavu-clips  # same dir as seen by frigate/go2rtc
  state_dir: /data/state

capture:
  source: recordings                  # recordings (loop upgrade) | restream
  seconds: 300                        # target loop length (longer = harder to spot)
  max_loop_seconds: 1200              # cap on a single loop window
  freeze_clip_seconds: 4
  rtsp_query: mp4                     # codec-filter query appended when capturing
  parallel: 4                         # concurrent loop searches (phase 2)
  parallel_frames: 8                  # concurrent live frame grabs (phase 1)
  engage_deadline_seconds: 15         # fold loops into the first swap if found in time
  keep_clips_after_off: false
  recordings:
    search_hours: 4
    min_seconds: 20
    audio: silence                    # silence | keep | strip
    max_brightness_delta: 60          # 0-255; 0 disables
    max_brightness_drift: 20          # intra-window; 0 disables
    match_ir_mode: true               # chromatic day/night guard
    sync_tolerance_minutes: 10        # 0 disables cross-camera sync
    dilute:                           # accept a long window with sparse transients
      enabled: true
      min_seconds: 300
      max_activity_fraction: 0.10     # <= this fraction under any tracked object
      block_labels: [person]          # never loop these (must include person)

# NOTE: no on_failure / recordings.fallback — a stream is never left live
# (§5c). The freeze ladder always yields a clip; abort is infrastructure-only.

streams:                              # global rails, applied AFTER camera→stream resolution
  include: []                         # globs; empty = all
  exclude:
    - camera.birdseye
    - camera.*_tablet
    - camera.*_wallpanel

profiles:
  default:
    mode: loop                        # loop | freeze
    cameras: []                       # empty = ALL frigate cameras
  indoor-freeze:
    mode: freeze                      # engage-and-stay on the freeze frame
    cameras: [office, kitchen, family]
    capture_seconds: 600              # optional per-profile loop-length override

api:
  listen: 0.0.0.0:8898
  bearer_token: ""                    # empty = auth disabled; prefer DEJAVU_API_TOKEN env
  debounce_seconds: 5
```

Config is validated at startup and before each transition (fail fast with a
line-precise error). Profile `cameras` accepts Frigate camera names; entries
prefixed `stream:` name a go2rtc stream directly (advanced escape hatch).

## 12. Deployment

```
frigate-dejavu/
  compose.example.yaml # build + run template — copy to compose.yaml and edit
  config.example.yaml  # appliance config template — copy to config.yaml and edit
  Dockerfile          # python:3.12-slim + ffmpeg + flask/requests/ruamel.yaml
  .env.example        # DEJAVU_API_TOKEN=
  LICENSE  SPEC.md  README.md
  app/                # api.py, dejavu.py, core.py, frigate.py, capture.py, config.py
  # your real compose.yaml / config.yaml / .env and data/ are gitignored
```

compose highlights:

- `volumes:`
  - `./config.yaml:/config/config.yaml:ro`
  - `./data:/data`
  - `../frigate/config/dejavu-clips:/clips` — **reuses frigate's existing
    `./config:/config` mount**, so the frigate container needs NO compose
    change and NO recreate; go2rtc sees clips at `/config/dejavu-clips/…`.
  - `/mnt/frigate/recordings:/recordings:ro` — Frigate's segment files, read
    directly for loop sourcing (§5b); export API is the automatic fallback.
  - `/var/run/docker.sock:/var/run/docker.sock` — only for the restart
    fallback; `restart_method: api` never uses it, but it stays mounted so the
    fallback works when needed.
- put the appliance on the same Docker network as Frigate so it reaches
  `frigate:5000/1984/8554` by name; publish `8898` (or assign a routable
  address) for LAN clients like Home Assistant.
- `restart: unless-stopped`, healthcheck on `/healthz`.
- `logging: json-file, max-size 10m, max-file 5` — job output is teed to the
  container stdout (§10.2), so cap the driver's history.
- The example compose publishes `8898:8898`; drop the mapping if you instead
  reach the API by container name on Frigate's Docker network.

Monorepo hygiene: all tracked files match existing allowlist rules
(`*.yaml`, `*.py`, `Dockerfile`, `README.md`, `.env.example`); `SPEC.md`
needs an allow rule (`!**/SPEC.md`). `data/` and the clips dir are already
ignored (`**/data/`, media-blob backstops).

## 13. Failure modes & recovery

| Failure | Handling |
|---|---|
| Frigate API unreachable | Transition refused up front; status shows reachability. |
| Camera offline / no live frame | Descend the freeze ladder (§5c): recorded frame → cached frame → black placeholder. The stream is never left live. |
| No loopable window for a stream | Stays on its freeze frame (still private); logged, not failed. |
| Phase-1 engage fails for every stream | Job fails, state `off`, config untouched (a bug — the ladder should always yield a clip). |
| Config save rejected by Frigate validation | Nothing persisted; state `off`/`on` unchanged; error surfaced. |
| Frigate doesn't come back within timeout | State `error`; operator runs `off` (restore attempt) or `force-restore`. Backup + drift copies always on disk under `data/state/`. |
| go2rtc didn't apply new config | Detected by §6.8 verification → automatic docker-restart fallback → re-verify → else rollback + `error`. |
| Appliance crash mid-transition | Stale-PID reconciliation → `error`; `off`/`force-restore` recover; pristine backup always available. |
| User edits Frigate config while privacy on | Preserved by surgical restore; edits to replaced stream entries themselves are overwritten (drift copy saved + warned). |

## 13a. Shared imaging algorithm with the cameralux HA integration (owner directive)

frigate-dejavu's lighting guards and the owner's cameralux integration
implement ONE canonical algorithm (two implementations — ffmpeg/pure-python
here, PIL/numpy there), classification FIRST, per-class brightness second:

1. **IR/monochrome classification** requires TWO signals from the YCbCr frame:
   *chroma* = mean(|Cb−128| + |Cr−128|) / 2 as a coarse pre-filter
   (`< 6.0`; calibrated live: IR 0.75-3.7, color 7.3-16.6), verified by
   *gray fraction* — the share of pixels/cells that are individually neutral.
   True IR frames are uniformly gray (gray = 1.000 on every validated frame);
   dark or muted COLOR frames also read low mean chroma but keep colored
   pixels, and chroma alone misclassified 91/114 color frames in a 139-frame
   fleet validation (2026-07). Thresholds are probe-resolution-specific:
   cameralux (full resolution) uses per-pixel channel-spread ≤ 2 and
   fraction ≥ 0.85; the guards here (16×16 probe, cell averaging) use
   per-cell chroma ≤ 1 and fraction ≥ 0.95.
2. **perceived luminance** = trimmed-mean(inverse-sRGB-gamma(Y)) ^ exponent —
   linear-light mean with the brightest ~5% dropped first (IR illuminators,
   headlights, and subjects in the beam paint point hotspots that moved a
   plain mean 5-14 luma codes with zero ambient change), then Stevens'-law
   brightness (exponent 0.5 default; 1.0 = physically linear). Valid for
   chromatic and monochromatic frames alike. cameralux maps it to lux
   (× per-class full scale: the IR profile); the guards here scale it ×255
   and compare deltas.
3. Chromatic and monochromatic images may use DIFFERENT brightness
   handling: the guards only ever compare within a class (mode mismatch
   rejects before any brightness delta), and cameralux switches calibration
   factor/bounds on the class. Measured note: frigate's `latest.jpg`
   re-encode reads ~12-18 luma codes hot vs the same-moment video frame, so
   the guard reference is the newest settled recording segment (same encode
   family, zero bias); latest.jpg is only the fallback. cameralux's
   photometric linearization is NOT adopted for absolute lux here — the
   guards are comparative gates, and probes force full-range output so both
   sides normalize identically.

## 13b. Design invariant: zero added latency on live streaming (owner directive)

The appliance must never interpose in the media path. Cameras → go2rtc →
consumers stays byte-identical to a system without the appliance: no
proxying, no re-streaming, no transcoding of live feeds, ever. Its standing
footprint while privacy is off is an unused go2rtc template key, a read-only
recordings mount, and a 20 s status poll. It touches streams only AT toggle
time, only the toggled streams (patch mode leaves every other camera
untouched), and restores the exact original source strings (identical
`nobuffer`/`low_delay` producer args). Any future design that routes live
media THROUGH the appliance (e.g. an appliance-side RTSP relay) violates
this contract.

## 14. Limitations / non-goals

- Cameras not consumed via go2rtc restream would need input-path rewriting —
  out of scope (none exist here); such cameras are reported `unsupported`.
- Recordings/detection during privacy contain the loop (that's the feature).
  Loop-seam motion blips possible in loop mode.
- Two-restart cost per session; ~10–30 s recording gap each.
- No scheduling, no per-camera partial privacy UI, no auth beyond the bearer
  latch, no TLS (front with a reverse proxy if ever needed).
- Frozen-frame clips re-encode once at capture (CPU seconds, one-off); loop
  mode never transcodes.

## 15. Milestones

1. **M1 — scaffold + read path**: config loading, `status`, Frigate client
   (`/api/config/raw` fetch, camera→stream resolution), dry-run resolver
   (`dejavu on --dry-run` prints the replacement plan without acting).
2. **M2 — capture**: parallel capture + freeze synthesis + validation into
   `/clips`, cancellable.
3. **M3 — switch**: config edit, save, restart, verify, restore, force-restore.
4. **M4 — API + debounce/lock hardening**, docker-exec parity, README, HA
   example snippet.

Each milestone independently testable against the live Frigate with read-only
or reversible operations; M3 end-to-end test is coordinated with the owner
(two production restarts).

## 16. Implementation findings (verified during build, 2026-07-07)

Facts discovered while implementing/testing M1-M2 that refine the sections
above; all verified against the live Frigate 0.17.2 / go2rtc 1.9.10.

- **`GET /api/config/raw` returns a JSON-encoded string** (with a
  `text/plain` content-type). The client unwraps it. `POST /api/config/save`
  is asymmetric and takes the raw YAML bytes as-is (handler does
  `body.decode()`).
- **Raw inline `#input=` is impossible under Frigate** (M3 finding, the hard
  way): Frigate's go2rtc config generator `str.format()`s every
  `go2rtc.streams` source value (only `{FRIGATE_*}` env vars are legal), so a
  literal `{input}` raises KeyError → "[ERROR] Invalid substitution found" →
  go2rtc crash-loops after restart while the Frigate API stays up. The swap
  therefore uses a **named input template** `go2rtc.ffmpeg.frigate_dejavu_loop`
  (that section is passed through verbatim — the deployment's own `{input}`
  templates prove it). The template is installed with the swap and removed on
  restore (prior value preserved if the key ever pre-exists). The appliance
  now also **rolls back automatically when the post-restart health gate fails
  while the Frigate API is reachable** — recovery from the M3 incident took
  one `off` (restore verified in 17 s); with the auto-rollback it needs
  nobody.
- **go2rtc's dynamic API rejects sources containing spaces**
  (`streams.Validate`, "not allow creating dynamic streams with spaces") —
  spaced sources can't be smoke-tested via `PUT /api/streams` either.
  M2 therefore proves loop mechanics locally (the exact templated ffmpeg
  args, 2.5 loops through the seam, `-c copy`) plus go2rtc RTSP file playback
  via a space-free ephemeral source; the config-loaded loop template is
  verified at M3 by §6.8.
- **`PUT`/`DELETE /api/streams` persist to the first go2rtc config file**
  (`/config/go2rtc_homekit.yml` here — frigate runs go2rtc with
  `-config=go2rtc_homekit.yml -config=/dev/shm/go2rtc.yaml`). Ephemeral test
  streams must be DELETEd, and the file restored byte-exact afterwards.
- **A stream's SDP codec menu can outrun reality**: `camera.kitchen` offers
  AAC but the camera sends no audio packets, so a `-c copy` capture yields a
  video-only clip. Captures therefore map `-map 0:v:0 -map 0:a:0?` and the
  privacy source's `#audio=` params are derived from the captured file (loop)
  or the source probe (freeze) — §4.2's "dropped if audio-less" rule.
- **Offline cameras answer RTSP DESCRIBE with 404** (go2rtc's on-demand
  producer can't reach them). At build time two cameras were down — one wired
  (no route to host), one wifi (timeout) — precisely the `on_failure: skip`
  path: they stay live-configured, loudly reported.
- **First restore normalizes a few folded YAML lines** (multi-line plain
  scalars re-emit single-line, values identical — proven semantically equal
  by round-trip tests against the production config). One-time cosmetic
  churn; comments, quoting, anchors, and `{FRIGATE_*}` placeholders survive.
- **Export API quirks (0.17.2)**: exporting a window that touches the newest
  ~seconds of recordings can wedge the export task (`in_progress: true`
  forever, no ffmpeg spawned, nothing logged at their `warning` level) —
  hence the 30 s segment-freshness margin. Export list entries also linger
  briefly after a successful DELETE — hence verified deletion. A stuck
  export is recoverable with `DELETE /api/export/<id>`. **Concurrent exports
  wedge the whole worker** (7 parallel jobs: exports stopped appearing,
  `/api/exports` reads timed out, jobs never finished) — all export
  lifecycles are serialized through one slot.
- **Slow boots are real**: one tensorrt cold boot took ~3 min (aggravated by
  the wedged export worker), blowing the original 120 s health gate while
  the toggle had actually succeeded. The gate is now 300 s and EXTENDS
  (waits for the API, then verifies-then-decides) instead of erroring — a
  timed-out gate is a reason to wait harder, not to panic.
- **`/api/version` lies about readiness**: after a restart, Frigate's export
  and DB machinery lag the version endpoint by MINUTES (semantic search,
  camera reconnects). A toggle started ~3 min after a previous toggle's
  restart saw every export time out and every camera silently fall back to
  live capture (observed live, twice in a row). Hence the **settle gate**
  (`frigate.settle_timeout_seconds`, default 240): before sourcing from
  recordings, wait until `/api/exports` actually answers.
- **NG swap engine (owner directive: no full-frigate restarts, recordings-
  only sourcing)**, verified live end-to-end (ON 21.7 s, OFF 12.5 s, frigate
  container untouched, byte-identical restore):
  - *Retrieval*: recordings are read DIRECTLY from the segment files
    (`/recordings/<UTC-date>/<HH>/<camera>/<MM.SS>.mp4`, ro mount) — 0.13 s
    per window vs 3-10 s via the export worker, fully parallel, windows are
    segment-aligned. Export API is the automatic fallback; live restream
    capture is opt-in only (`--source restream`), default fallback = skip.
  - *Effect*: `save_option=saveonly` (persistence) + restart of ONLY the
    embedded go2rtc service. Two traps found on the way: (1) a per-stream
    `PATCH /api/streams` cannot preempt an actively-consumed stream —
    `Producer.SetSource` only changes the URL for the NEXT dial and
    frigate's consumers never hang up; (2) go2rtc's own `/api/restart`
    SELF-RE-EXECS, so frigate's s6 run wrapper never regenerates
    /dev/shm/go2rtc.yaml and the STALE config reloads. The working kick is
    `docker exec frigate /command/s6-svc -t /run/service/go2rtc` — s6 reruns
    the wrapper, which regenerates the config from config.yml. Frigate's
    detect/record/DB never restart; per-camera ffmpeg consumers reconnect in
    seconds (activation probes spin the on-demand producers immediately).
  - *Sync anchor is recency-first* (hour buckets) — a stale max-coverage
    anchor (pre-sunset) dragged every camera's candidates into frames the
    IR guard had to reject (observed); recency ≈ lighting regime outranks
    coverage. Candidate depth raised to 5 (reads are cheap now).
  - The full frigate-restart machinery remains as automatic fallback and
    `swap_method: restart`.
- **IR switchover is invisible to a luma guard** (owner-reported from live
  screenshots: daylight frozen frames served against IR night views).
  Measured on this deployment at night: IR-lit lawns read luma 56-145 while
  a color-lit indoor room read 104 — and a daylight back_patio frame read
  luma 140 vs the IR live view's 145 (Δ4, sails through any brightness
  threshold). Chroma separates the classes perfectly: IR cams 0.75-1.9
  (one dim color scene read 3.7), color scenes 7.3-16.6; threshold 6.0.
  Hence `match_ir_mode` chromatic classification in the lighting guards.
- **HA model is a `select`, not switches** (owner directive): profiles are
  mutually exclusive and the appliance is the gatekeeper for nested applies
  (`on` while `on` → 409). `select.frigate_privacy` = Off/Perimeter/Indoor/
  All Cameras; picking a profile while another is active is rejected by the
  appliance and the select reconciles back — Off first, by design.
- **Real dusk drift measured**: at ~19:10 in July, back_patio's live frame
  read luma 155 vs 138 one minute earlier (Δ17) and Δ11-12 for 30-45 min old
  windows — cloud/exposure jitter alone reaches Δ~15, so guard defaults
  (Δ60 vs live, Δ20 intra-window) are set to catch transitions, not weather.

### Redesign to two-phase engage (2026-07-08)

- **The kitchen incident** (root cause, from the DEBUG log): in an all-camera
  freeze at 23:57, `camera.kitchen` stayed LIVE. The kitchen lights had gone
  out ~23:50, flipping the camera to IR. Every one of the 25 candidate windows
  in the 4 h lookback ended *before* the flip, so all were colour and all
  failed the IR guard against the IR live frame. The one correct source —
  event-clear IR segments at 23:57:02/:12/:22 — existed on disk but was
  excluded unread by the blunt `RECENT_MARGIN = 30` freshness rule. Two
  compounding bugs: `MAX_LUMA_CANDIDATES = 5` only screened 5 of 25, and an
  exactly-`min_seconds` window measured 19.99 s (segment jitter) and was
  dropped. Nothing appeared in `docker logs` because job output went only to a
  file.
- **Owner directives that reshaped the design**: (1) a stream is NEVER left
  live — `on_failure: skip` and `recordings.fallback` are gone; the worst case
  is a freeze frame, which still provides privacy (just not privacy *of the
  fact that privacy is on*). (2) `abort` is not a config option; it is the
  "died trying, see the logs" last word, reserved for infrastructure failure.
  (3) The other way to hide a transient is to make the loop LONGER, so one car
  in 20 min reads as ordinary — repetition is the tell, not the car. Absolute
  quiet is still preferred; fooling the eye is the priority when it isn't
  achievable. (4) Engage IMMEDIATELY (live freeze frame), do the expensive loop
  search in the background, upgrade when it lands.
- **Fixes**: freshness is now a direct `_segment_settled` size-stability test
  (young settled segments are fine), not a blunt age margin (the export-worker
  wedge still uses the 30 s margin, which is the only place it was ever needed).
  Guards probe segment files directly (no per-candidate concat), so the whole
  candidate list is screened within a wall-clock budget, not a top-5 cap.
  Length tests tolerate ±0.5 s jitter. Diluted tier + sliding scan find long
  windows with sparse, person-free activity. Verified offline against the exact
  23:57 segment data: kitchen now screens past the six pre-flip colour windows
  and picks the 23:57:02 IR window (and would freeze on a live IR frame even if
  it hadn't).
- **Verified engage costs** (2026-07-08, live frame grabs): one live freeze
  frame is ~2.4-6 s per camera (probe + grab + synth); at `parallel_frames: 8`
  a 22-camera profile engages in ~3 waves. The `engage_deadline_seconds` (15)
  decides per-run whether loops fold into the first swap or upgrade after.
