# Frigate Déjà Vu — SPEC

Privacy-mode appliance for Frigate. A small sidecar container that, on demand,
replaces live camera streams with pre-recorded loops (or frozen frames) at the
go2rtc layer, so Frigate and every downstream consumer keep running unaware
while the cameras are effectively "off". This document is the contract for the
implementation.

The bearer token is the appliance's application-layer access control, not a
replacement for network segmentation or TLS. The example deployment binds the
host port to loopback; operators must configure a token or authenticated reverse
proxy before exposing the API to a LAN or wider network.

---

## 1. Problem & approach

**Guiding axiom.** Déjà Vu is never real, and that is the point: it produces a
convincing *false reality*. The objective is not fidelity to what the cameras
actually see — there is none to preserve — but **undetectability**: a
fabrication indistinguishable from real. A false reality is exposed only by
*contradiction*, on two surfaces:

- **Internal** — it disagrees with itself: a loop tell, an object teleporting
  between rotations, overlapping cameras out of sync (§5b), the out-of-band
  sensor mesh disagreeing with the served feed. Déjà Vu controls this surface
  completely.
- **External** — it disagrees with what an observer can *independently* verify:
  the weather, a delivery, having been present at the time. The forgery can be
  flawless with itself and still be caught by a fact that leaked in through a
  channel Déjà Vu does not own.

Every guard in this spec reduces to one rule — *keep the false world from
contradicting itself or the world* — and to one corollary: **assert as little
as possible**. A frozen frame is the strongest lie because it claims almost
nothing; long dynamic fabrication is the hardest because every second it claims
more. Freeze is therefore never a failure state but the minimal, most-defensible
false reality (the §5c ladder floor, the §19 rotation floor). "Best-effort
plausibility, not verified fidelity" (§19) is a direct consequence: mid-session
there is no live ground truth to match, only self-consistency to preserve.

The design frame is a professional privacy utility, not an alibi, and its
audience split is deliberate: **users are in the dark; admins see it plainly.**
The deception is user-wide. Every non-admin viewer of the Frigate interface,
every downstream video consumer (HA cards, WebRTC/MSE viewers, HomeKit,
birdseye), and Frigate's own record/detect pipeline keep operating unaware that
a stream was swapped at all — Frigate itself cannot tell Déjà Vu is active
(§4.3), and that consumer-blind layer is exactly where this tool lives. A
casual, non-admin observer should find nothing that reads as fabricated. That is
the magic.

A true administrator is the opposite audience. To an operator, Déjà Vu's active
state should be **plainly visible — even deliberately advertised** — at the
non-user/operator level: its own `status` and logs already say so, and an
unmistakable operator-facing indicator that privacy is engaged is a feature, not
a leak. There is no plausible deniability against an operator, by design: the
tool never lies to the hand that runs it. That is the honest line between a
privacy utility and an alibi — the deception points outward at consumers, never
inward at the operator, and the forensic/host record (mtimes, encoder
fingerprints, the Docker/host restart record) is likewise expected to reveal the
mechanism to anyone who inspects properly. That transparency is no license to be a
nuisance. Two courtesies apply, both manners rather than concealment. *Leave no
trace, pack it out:* the smallest possible persistent footprint — surgical
byte-identical restore, no needless config churn, no orphaned clips or state.
*Do not disturb the wildlife:* the smallest possible disruption to the living
system while operating — every downstream consumer, recording, detection, and
neighboring service should carry on with as little collateral perturbation as
the mechanism allows. The restart is the loudest disturbance on both counts,
which is a further reason seam elimination (§17.1/§18) is the goal: a swap the
ecosystem never feels, not merely one it can't see.

So the only tells worth suppressing are those a *non-admin user* could stumble
on in ordinary viewing — and here the goal is stronger than "no gap." **A user
scrubbing the recording timeline should not be able to LOCATE THE SWITCHOVER at
all, in either direction:** neither a missing-recording gap nor a content
discontinuity should mark the instant privacy engaged or disengaged; the
transition into the loop, and back out of it, should pass unnoticed under a
scrub. This is pursued purely on the content side — freeze-on-the-current-frame
for visual continuity at engage, gap continuity and loop-seam matching (§17.3),
and ultimately the restart-less swap that removes the gap outright (§17.1/§18) —
*not tipping off users*, never *hiding from admins*. Scrubbing Frigate's logs,
event DB, the host journal, or Docker's restart record is cover-tracks/alibi
work: a hard non-goal (§14), and — since admins are meant to see it — pointless
besides. Honest reach: some seams are inherent — the disengage back to a scene
that has since diverged, and any real activity present at the engage instant,
produce a genuine jump no replay fully hides — so the goal is *unlocatable
wherever the scene is quiet and continuous, and as close as the material allows
where it is not.* This remains a goal even where it is not fully attainable.
Current state: under the shipped `frigate-config` backend a restart briefly
shows in the logs panel and leaves a recording gap a user could notice; the §17
roadmap closes that user-facing tell while keeping the operator fully informed.

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

    prepare freeze frames → edit go2rtc.streams through Frigate's REST API →
    restart Frigate → privacy on (immediate, permanent) … then in loop mode,
    assemble loops from recordings in the background → swap each over its freeze
    clip → second restart → loops live … → restore original stream sources →
    restart → live again.

## 2. Production reference architecture (verified 2026-07-07)

This is the deployed, proven architecture and the baseline for the tracked
examples—not incidental test context. Other installations may change addresses,
mounts, camera brands, or authentication, but departures from the media topology
and restart model below must be treated as architecture changes and revalidated.

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

- A camera with no restream-style input is reported as `unsupported` and the
  whole activation is refused before Frigate's config is touched. Partial
  privacy must never be reported as on.
- `camera.birdseye` (a Frigate-generated composite) is excluded by default via
  config, not code. Tablet/wallpanel feeds remain included unless the operator
  explicitly excludes them.

### 4.2 The swap

For each stream `S`, the whole source list under `go2rtc.streams.S` in
Frigate's config is replaced with a single source:

```yaml
go2rtc:
  ffmpeg:
    frigate_dejavu_loop: -re -stream_loop -1 -i {input}   # persistent; unused while privacy is off
  streams:
    camera.kitchen:
      - "ffmpeg:/config/dejavu-clips/camera.kitchen.dejavu.mp4#input=frigate_dejavu_loop#video=copy#audio=copy#audio=opus#audio=pcmu"
```

- `#input=frigate_dejavu_loop` selects a named go2rtc input template. The
  appliance installs this namespaced support entry on first activation and
  retains it across normal OFF transitions; it has no media-path effect until a
  selected stream references it. A whole-file `force-restore` may remove it with
  the pristine backup, and the next activation reinstalls it. A different value
  under the reserved key is refused, not overwritten. ffmpeg reads the file at
  native speed and loops it forever.
  Each loop restarts at the file's leading IDR
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
touches only the replaced `go2rtc.streams.<S>` values and ensures the one
namespaced persistent template above.

### 4.3 Why consumers don't notice

- Frigate reconnects to the same `rtsp://127.0.0.1:8554/S?mp4` after restart;
  the SDP offers the same codec family/resolution, `preset-rtsp-restream` and
  `preset-record-generic-audio-aac` behave identically.
- Recordings record the loop; detection sees a (mostly) static scene; birdseye
  composes the loops; HA/WebRTC viewers renegotiate against the same offers.
- The restream codec-filter query (`?mp4`) keeps matching because captures are
  taken *through* that same filter.

## 5. Capture primitives

The building blocks the preparation (§5c) composes. All ffmpeg; all cancellable.
In stage 1 they all run **before** any config is modified, so cancellation during
capture leaves zero footprint; the stage-2 loop upgrade reuses the stitch/audio
primitives in the background *after* activation, but only on staged
`*.upgrade.mp4` clips (promoted atomically, cleaned up on abort), so privacy is
never affected.

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

## 5b. Loop sourcing from recordings (window chosen pre-activation, assembled in stage 2)

Live capture loops whatever happens during capture — a passing car re-appearing
every 60 s, or a person frozen mid-stride — so the production default sources
loops from Frigate's own recordings, which are codec-identical (same `?mp4`
restream) and already carry Frigate's object/event metadata. Loop assembly runs
in the background AFTER privacy is already on (stage 2, §5c): each stream's
candidate window is chosen before restart #1 while Frigate is up, then the loop
is assembled from the local segment files and atomically swapped over its freeze
clip, and swapped in via restart #2.

The single most important principle (owner directive): **the tell is
repetition, not the presence of a transient.** A 20-minute window in which a
car passes once reads as an ordinary quiet scene; a 60-second window in which a
bush sways every loop screams "loop." So the search prefers LONG windows and
tolerates sparse activity, rather than insisting on short absolute silence.

Per stream, parallelized (`capture.parallel`):

1. **Candidate search** over the last `recordings.search_hours` (default 3 h —
   ≈ one lighting period (§19), so a sourced loop is never engaged more than
   one period stale):
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

   Ranking: tier first, then hour-age bucket, LONGEST, least motion, most recent.
   A full quiet/diluted loop is preferable to an obvious 20–30 s repetition;
   the lighting guards authoritatively reject stale day/night matches. A diluted
   start backs down from an overactive long tail to the longest endpoint that
   satisfies the activity limit. Length tests tolerate ±0.5 s of segment jitter
   (a 20 s run of two nominal-10 s segments measures 19.99 s).
1b. **Cross-camera sync** (`recordings.sync_tolerance_minutes`, default 10;
   0 disables): overlapping views loop the SAME moment. A pre-pass picks the
   newest-hour anchor on Frigate's server timeline with the most full-window
   coverage, then total coverage. Synced full candidates are tried first, but an
   unsynced full loop still outranks a synchronized short fallback. (Freeze
   frames need no sync — every camera freezes on a frame of *now*, the same
   instant by construction.)
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
   probe, not two. Up to three survivors in the same recency/tier group are
   ranked by mean 16×16 endpoint luma difference to minimize the visible seam.
4. **Stitch** the winner from its segment files (`-f concat -c copy`). The
   Frigate export API is the per-candidate fallback when the mount is absent, a
   candidate's segment files are missing/unreadable, or direct assembly fails.
   The first actual export use (eagerly when the mount is absent, lazily after a
   failed direct read) passes through one shared readiness gate
   (`wait_exports_ready`, bounded, cancellable) — the worker lags minutes behind
   a Frigate restart and `/api/version` lies about readiness (§16). The readiness
   wait consumes the same overall loop-assembly budget as the searches. Exports
   remain strictly serialized (the worker wedges under concurrent jobs) and keep
   the export path's 30-second freshness margin; a
   failed export advances to the next ranked candidate, and if nothing
   assembles the stream stays on its freeze frame. Direct-file
   assembly applies the **audio policy in the same ffmpeg
   pass** (`recordings.audio`, default `silence`: same-rate/channel silent AAC —
   repeating audio is the most obvious tell). The clip's audio presence is
   pinned to match the prepared freeze fallback, so either finalized clip fits
   the same planned go2rtc source string at activation.

## 5c. Pre-activation preparation: freeze fallback, then loop search

A stream is **never left live after activation**. Before Frigate's config is
changed, every stream gets a freeze clip via the ladder:

| rung | source | when |
|---|---|---|
| 1 live | one frame of NOW (lighting-correct by construction; also the guard reference) | normal |
| 2 recorded | newest lighting-matched event-clear recorded frame | camera offline now |
| 3 cached | the last frame this camera ever froze on, with atomic media-metadata sidecar (kept under `state/lastframe/`) | offline a while |
| 4 black | synthesized placeholder | nothing exists anywhere — loudly logged |

There is no "skip" for an uncooperative camera: the ladder bottoms out in a
synthesized frame, which still provides privacy. The whole job is refused only
when a requested stream cannot be resolved, the ladder violates that invariant,
or infrastructure fails; partial privacy is never reported as on.

Loop mode is a **two-stage engage**. **Stage 1** prepares the freeze clip per
stream via the ladder above, selects each loop's candidate window while Frigate
is up (the only API-dependent step, §5b), persists the go2rtc source set — every
stream pointing at its `<stream>.dejavu.mp4` clip — and goes on through one
coordinated Frigate restart (**restart #1**). Privacy is now immediate,
guaranteed, and permanent: no stream is ever left live.

**Stage 2** runs in the background afterward, while already private. It assembles
each loop from the local `/recordings` segment files (§5b; export-API fallback
when the mount is absent or a candidate is unreadable, gated on the export
worker settling), atomically `os.replace`s each finished loop over its freeze
clip at the SAME path, and swaps them in through a SECOND coordinated Frigate
restart (**restart #2**). Because the config clip path is byte-identical for a
freeze and its loop, restart #2 needs no config change — it just makes go2rtc
re-open the swapped file. A stream whose loop is not ready within
`capture.loop_assembly_budget_seconds` (default 900), or that never finds a
suitable window, stays on its freeze frame permanently — a success, not a
failure. Every non-black freeze rung carries its own lighting reference (for an
offline camera the frozen frame IS what a loop must match), so offline cameras
with recent recordings upgrade too.

The safety floor remains "stream shows a still frame," never "stream stays live
after the appliance reports on."

`source: restream` with loop mode is an explicit, non-reference exception: it
captures the requested live interval directly, without the recordings metadata
or lighting guards and without preparing a freeze fallback. A failed restream
capture refuses the whole activation before Frigate's config is touched.

## 6. Switch-on sequence

1. Acquire the global flock; require state `off`; enforce debounce.
2. State → `capturing` (cancellable). Fetch `/api/config/raw`; store pristine
   backup `state/backup.<ts>.yaml` + `state/backup.current.yaml`.
3. Resolve profile → cameras → streams (§4.1). Empty set → error, state `off`.
4. Prepare a freeze clip per stream via the ladder (§5c), except for the
   explicit `source: restream` loop path, which captures its live loop directly,
   bounded by `capture.parallel_frames`. The freeze ladder always yields a clip;
   a failed direct restream capture refuses activation. Cancellation (an
   `off`/`cancel` arriving now) kills the ffmpeg process group, deletes partial
   clips, state → `off`.
5. In loop mode, select each stream's loop candidate window now, while Frigate
   is up and cameras are live (the only API-dependent step of the loop path,
   §5b/§5c); stash the candidates on the plan for stage 2. Freeze frames need no
   window selection.
6. State → `applying` (no longer cancellable). Re-fetch the raw config to
   preserve edits made during preparation. Edit `go2rtc.streams` (§4.2) — every
   selected stream points at its `<stream>.dejavu.mp4` freeze clip — and record
   per-stream original sources in `state/streams.json`.
7. `POST /api/config/save?save_option=saveonly` (body = edited raw YAML).
   Frigate validates server-side; on rejection → nothing was saved → state
   `off`, error surfaced, clips kept for inspection.
8. Apply (§8): a **live swap** on Frigate 0.18+ (stop the affected cameras,
   `PUT` each replaced stream into the running go2rtc, start the cameras
   again), else — or if the live swap fails — one coordinated Frigate restart,
   **restart #1**. Then **verify**: Frigate is consuming again and go2rtc `/api/streams` shows each replaced stream's
   source is the privacy clip. All verified → state `on`; **privacy is now
   immediate, guaranteed, and permanent**. Any stream still live → automatic
   rollback (restore sources, restart again), state `error` with detail.

**Stage 2 (loop mode only, best-effort, background).** Privacy is already on, so
this only ever upgrades a private freeze to a private loop and never fails the
job — every error leaves the permanent freeze baseline standing.

9. State `on` is published with `job_pid` cleared and a persisted `upgrade_pid`
   marker naming the live upgrade process, so a concurrent `off` can find and
   cancel it (and a stage-2 crash reconciles to a safe `on`, never `error`).
10. Assemble each loop from the local segment files when the mount is readable;
    otherwise wait for the export worker to settle (it lags minutes after
    restart #1, §16) and ride the serialized export API. The settle wait and
    assembly share the bound set by
    `capture.loop_assembly_budget_seconds` (default 900). A stream whose loop is
    not ready in time, or that finds no window, stays on its freeze frame
    permanently.
11. Atomically `os.replace` each finished loop over its freeze clip at the SAME
    path, then apply (§8): live, a `PUT` of the UNCHANGED dejavu source per
    promoted stream — go2rtc always spawns a fresh producer, so the swapped
    inode is loaded deterministically; otherwise a SECOND coordinated restart —
    **restart #2** — a bare restart, since the config clip path is byte-identical
    for freeze and loop. On the restart path verification is a no-op-safe check
    that Frigate was OBSERVED restarting; if it was a no-op the loops are staged
    and load on the next restart. Privacy is intact throughout. A concurrent `off` claims the
    restore transition first, so the upgrade's ownership gate makes it
    promote/restart nothing — `off` wins.

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
3. Save (`saveonly`) → apply (§8: live swap with the original sources,
   `{FRIGATE_*}` expanded from dejavu's own environment exactly as Frigate's
   go2rtc generator does, else a coordinated restart) → verify the privacy
   sources were removed → state `off`.
4. Clips deleted unless `capture.keep_clips_after_off: true`. State artifacts
   (backups, drift copies) are retained.

## 8. Apply & verification strategy

Every transition first SAVES the edited config (`save_option=saveonly`) so a
later restart reproduces it, then brings the RUNNING Frigate in line by one of
two paths (`frigate.swap`):

- **Live swap** (`auto`, Frigate ≥ 0.18; verified 2026-09-17, §16): for the
  affected cameras only — (1) `<cam>/enabled/set OFF` over Frigate's WebSocket
  bridge (the dispatcher stops that camera's ffmpeg, recording closes its
  segment, the config file is untouched), (2) `PUT /api/streams?name&src…`
  into go2rtc for each replaced stream (drops consumers, spawns a fresh
  producer; `PATCH` only rewrites the URL for the next dial and never
  preempts), (3) `enabled/set ON` (Frigate re-dials go2rtc and gets the new
  producer). Frigate never restarts, cameras outside the plan are untouched,
  and the per-camera recording gap is the replace + start time (about 4 s
  measured). Cameras the operator had disabled at runtime stay disabled. The
  bridge grants the admin role to every caller on the internal :5000 port; on
  :8971 the login must be an admin. go2rtc persists dynamic PUTs into its first
  config file, so that file is read via `GET /api/config` before and written
  back byte-exact after.
- **Coordinated restart** (`restart`, or `auto` when the live path is
  unavailable or fails at any step): `POST /api/restart`, wait healthy. This
  rebuilds go2rtc and Frigate's camera consumers in order. It is the fallback
  for Frigate < 0.18, a bridge that does not answer, an original source with a
  `{FRIGATE_*}` placeholder dejavu cannot expand (give it Frigate's
  environment), a source containing spaces (go2rtc's dynamic API rejects
  them), an unavailable go2rtc stream listing, a restore whose parked
  publisher name is gone (go2rtc restarted while private) or whose stream
  acquired a publisher while private with nothing parked, or a live swap whose
  verification fails — the cameras are started again before restarting.

**Publisher-fed streams are parked, not replaced.** A stream fed by an inbound
push (tablet WebRTC publish; detected as a producer entry with no configured
`url`/`source` — a `remote_addr` alone does not identify a push) keeps its
object alive: before the `PUT`, `PATCH /api/streams?name=dejavu.keep.<stream>
&src=rtsp://127.0.0.1:8554/<stream>` gives the SAME object a second name
(stock go2rtc links names when the source is a restream URL of an existing
stream), so the publisher keeps pushing into it, ignored. The public name then
gets the clip object as usual. Restore reverses it: `PATCH <stream>
src=rtsp://127.0.0.1:8554/dejavu.keep.<stream>` points the public name back at
the parked object and the alias name is deleted; the publisher never saw a
disconnect and Frigate reconnects within seconds. The alias is recorded in
`streams.json` (`alias`). Frigate only ever dials its own camera name; the
parked name is an internal detail.

The publisher is FOLLOWED, not trusted from the record: every apply reads
where each external producer sits right now. A tablet that reconnects while
private (wifi roam, kiosk reload) lands on the object holding the public name,
i.e. the clip, and its parked object goes dead. Stage 2 then re-parks the
current object under the same alias before its `PUT`. Restore, seeing the
publisher on the clip object, keeps THAT object and rewrites its configured
source back in place (`PATCH <stream> src=<original>` → `Stream.SetSource`,
which only changes the URL the next dial uses and never touches a connection),
then deletes the alias. Only a single-source original can be re-sourced (PATCH
takes one `src`); a multi-source original with a moved publisher, or a parked
name that vanished (go2rtc restarted while private), falls back to the
restart. The parked name is consumable like any go2rtc stream by anyone with
go2rtc API access, who can already see every camera live there; the owner
accepted that on 2026-09-17 (out of scope).

Verification is the same for both: go2rtc `/api/streams` references every
expected privacy clip (or no longer does, on restore); the live path also
waits up to 20 seconds for Frigate's own consumer (user agent
`FFmpeg Frigate/…`) on streams consumed before the swap whose cameras were
toggled. A slow reconnect is logged as an availability warning; offline
streams do not gate the swap. `/api/stats` is NOT a signal: its camera pids/fps
are never zeroed on disable.

- Restarts use Frigate's `POST /api/restart`. The appliance has no Docker API
  access and does not mount the Docker socket. If Frigate's API cannot recover,
  the transition reports `error` and an operator restarts Frigate externally.
- Loop mode applies twice — stage 1 (freeze) and the stage-2 loop upgrade —
  each one `PUT` per stream when live, or restart #1 and restart #2 on the
  restart path. Freeze mode and restore apply once. `dejavu status` `note`
  records which path ran: `swap=live` or `restart=frigate-api`.

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
  (restore always runs to completion). `off` during `applying` → 409 (the
  save/restart/verify operation runs to completion; issue `off` again once
  `on`).
- Appliance restart while privacy `on` is safe: state and backups are on
  disk; Frigate keeps looping clips (config + clips persist) until `off`.

## 10. Interfaces

### 10.1 CLI (`dejavu`, inside the container)

    dejavu on   [--profile NAME] [--mode loop|freeze] [--cameras a,b,…]
                    [--capture-seconds N] [--source recordings|restream]
                                              # blocking; progress on stdout
    dejavu off                             # blocking; cancels capture if running
    dejavu cancel                          # alias of off during capture
    dejavu status [--json]
    dejavu profiles
    dejavu force-restore                   # whole-file backup restore + restart

    docker exec frigate-dejavu dejavu on --profile indoor --mode freeze

Exit codes: `0` ok · `1` failure · `2` busy/debounced · `3` invalid request.
`--mode`/`--cameras`/`--capture-seconds`/`--source` override the chosen profile's
values (`--profile` defaults to `default`). An omitted profile mode resolves to
the safe `freeze` behavior; the tracked default profile explicitly selects
`loop`.

### 10.2 REST API

| Method & path | Body / params | Behavior |
|---|---|---|
| `POST /api/dejavu/on` | `{"profile"?, "mode"?, "cameras"?, "capture_seconds"?, "source"?}` | Spawns detached `dejavu on …`; returns **202** + state snapshot. `source` is `recordings` or `restream`; 409 busy/debounce, 400 invalid. |
| `POST /api/dejavu/off` | – | Spawns `dejavu off` (cancels capture if capturing); **202**. |
| `GET /api/dejavu/status` | – | **200** state JSON: `state, profile, mode, since, session, capture_seconds, streams{name: phase/result}, last_error, note, drift_detected, frigate{reachable, version, go2rtc}`. `frigate` is omitted from non-live internal snapshots. |
| `GET /api/dejavu/profiles` | – | Profiles as resolved from config. |
| `GET /healthz` | – | Liveness (no auth). |

For `POST /api/dejavu/on`, an empty body selects the default profile and
`"cameras": []` explicitly selects all Frigate cameras. Unknown fields,
malformed/non-object JSON, blank camera names, and invalid field types return
`400`.

Auth: iff `api.bearer_token` resolves to a value, every `/api/*` request must send
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
  health_timeout_seconds: 300          # production TensorRT cold boots can exceed 3 min
  api_auth:
    user: "{DEJAVU_FRIGATE_USER}"
    password: "{DEJAVU_FRIGATE_PASSWORD}"
    headers: {}                        # alternative: static headers for a
                                       # proxy-fronted frigate (auth.enabled false)
  tls_verify: true                     # false, or a CA bundle path

paths:
  clips_local: /clips                 # this container's view
  clips_frigate: /config/dejavu-clips  # same dir as seen by frigate/go2rtc
  state_dir: /data/state

capture:
  source: recordings                  # recordings (guarded loop search) | live restream capture
  seconds: 300                        # target loop length (longer = harder to spot)
  max_loop_seconds: 1200              # cap on a single loop window
  freeze_clip_seconds: 4
  rtsp_query: mp4                     # codec-filter query appended when capturing
  parallel: 4                         # concurrent loop searches
  parallel_frames: 8                  # concurrent live frame grabs
  keep_clips_after_off: false
  recordings:
    search_hours: 3
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
# (§5c). An unresolved stream or broken ladder refuses the whole activation.

streams:                              # global rails, applied AFTER camera→stream resolution
  include: []                         # globs; empty = all
  exclude:
    - camera.birdseye

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
  bearer_token: "{DEJAVU_API_TOKEN}"  # empty = auth disabled
  debounce_seconds: 5
```

Reaching Frigate's authenticated port needs more than credentials (verified
against Frigate 0.17.2, 2026-07-31). `:8971` is HTTPS and serves a self-signed
certificate by default (`O=FRIGATE DEFAULT CERT, CN=*`, reissued on every
container recreate), so `tls_verify` must name a CA bundle or be `false` —
otherwise every request fails the TLS handshake before auth is reached.

`api_auth` offers two mutually independent forms, matching Frigate's two auth
models. There is deliberately no static bearer-token field: Frigate has no
long-lived API key — a Frigate "bearer token" is just the JWT that `/api/login`
returns. It expires after `auth.session_length` (86400 s by default; operators
can raise it, but there is no non-expiring value), and every one of Frigate's
three JWT-issuing paths is bounded by it. Renewal cannot help a static token
either: the refresh branch is gated on `jwt_source == "cookie"`, so a JWT
presented in an `Authorization` header is never refreshed. A configured token
would be a credential that silently stops working, so the field was removed
rather than shipped.

- `user` / `password` — for a frigate with native auth (`auth.enabled: true`,
  the default). Logs in via `/api/login`, rides the JWT cookie, and
  re-authenticates on a 401, so the credential self-heals.
- `headers` — for a frigate behind an authenticating reverse proxy
  (`auth.enabled: false` + a `proxy` block). Identity arrives as request
  headers, so the map carries `proxy.auth_secret` (as `X-Proxy-Secret`) plus
  whatever user/role headers `proxy.header_map` expects. Static values, so
  nothing expires; the role mapped from the group header must resolve to
  `admin`, since engaging saves frigate's config and restarts it. Verified
  end-to-end against a keycloak/oauth2-proxy-fronted frigate 0.17.2
  (2026-07-31): status, full dry-run, and admin-gated endpoints all pass.

String values may reference `{DEJAVU_*}` environment variables. Referenced
variables must exist; an explicitly empty value disables optional authentication.
Environment values never override literal config fields.
Config is validated at startup and before each transition (fail fast with a
precise error). Profile `cameras` accepts Frigate camera names; entries
prefixed `stream:` name a go2rtc stream directly (advanced escape hatch). A
profile with no `mode` resolves to `freeze`; the production default profile
selects `loop` explicitly.

## 12. Deployment

```
frigate-dejavu/
  compose.example.yaml # build + run template — copy to compose.yaml and edit
  config.example.yaml  # appliance config template — copy to config.yaml and edit
  Dockerfile          # python:3.12-slim + ffmpeg + flask/requests/ruamel.yaml
  LICENSE  SPEC.md  README.md
  app/                # api.py, dejavu.py, core.py, frigate.py, capture.py, config.py
  # your real compose.yaml / config.yaml and data/ are gitignored
```

compose highlights:

- `volumes:`
  - `./config.yaml:/config/config.yaml:ro`
  - `./data:/data`
  - `../frigate/config/dejavu-clips:/clips` — **reuses frigate's existing
    `./config:/config` mount**, so the frigate container needs NO compose
    change and NO recreate; go2rtc sees clips at `/config/dejavu-clips/…`.
  - `/mnt/frigate/recordings:/recordings:ro` — Frigate's segment files, read
    directly for loop sourcing (§5b). The mount is a fast path, not a
    requirement: without it loop sourcing falls back to the (serialized)
    Frigate export API, after waiting for the export worker to settle
    post-restart. Direct reads are ~10-100× faster and avoid the export
    worker's quirks (§16), so mount it when you can.
- Frigate's configured API, go2rtc, and RTSP endpoints must be reachable from
  the appliance; network topology is deployment-specific. The example publishes
  `8898` on host loopback only; configure bearer authentication before making it
  routable to LAN clients like Home Assistant.
- `restart: unless-stopped`, healthcheck on `/healthz`.
- `logging: json-file, max-size 10m, max-file 5` — job output is teed to the
  container stdout (§10.2), so cap the driver's history.
- The example compose publishes `127.0.0.1:8898:8898`; drop the mapping if you
  instead reach the API by container name on Frigate's Docker network.

Monorepo hygiene: all tracked files match existing allowlist rules
(`*.yaml`, `*.py`, `Dockerfile`, `README.md`); `SPEC.md`
needs an allow rule (`!**/SPEC.md`). `data/` and the clips dir are already
ignored (`**/data/`, media-blob backstops).

## 13. Failure modes & recovery

| Failure | Handling |
|---|---|
| Frigate API unreachable | Transition refused up front; status shows reachability. |
| Camera offline / no live frame | Descend the freeze ladder (§5c): recorded frame → cached frame → black placeholder. The stream is never left live. |
| No loopable window for a stream | Stays on its freeze frame (still private); logged, not failed. |
| Requested camera is unsupported / stream is missing | Whole activation fails, state `off`, config untouched. |
| Phase-1 engage fails for any stream | Whole activation fails, state `off`, config untouched (a bug — the ladder should always yield a clip). |
| Config save rejected by Frigate validation | Nothing persisted; state `off`/`on` unchanged; error surfaced. |
| Frigate doesn't come back within timeout | State `error`; operator restarts Frigate externally, then runs `off` or `force-restore`. Backup + drift copies always remain under `data/state/`. |
| go2rtc didn't apply new config | Detected by §6.8 verification → restore original sources and restart through Frigate's API → `error`. If the API is unavailable, the operator restarts Frigate externally. |
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

While privacy is OFF, the appliance must not interpose in the media path.
Cameras → go2rtc → consumers stays byte-identical to a system without the
appliance: no proxying, no re-streaming, no transcoding of live feeds. The
standing footprint while privacy is off is an unused go2rtc template key, a
read-only recordings mount, and a 20 s status poll. Toggles change only the
selected config entries and restore the exact original source strings
(identical `nobuffer`/`low_delay` producer args).

Scope (amended 2026-07-14, owner directive): this invariant is ABSOLUTE and
binds every backend, including the §18 proxy design. The appliance never
interposes in a LIVE camera feed: it never accesses cameras (Frigate does), and
`proxy-interpose` only ever serves local clip content, only while privacy is on
— there is no live stream to delay while it is in the path, and `off` removes
it from the path entirely. A standing/permanent restream layer that fronts the
cameras full-time is out of scope precisely because it would violate this
invariant.

## 14. Limitations / non-goals

- Cameras not consumed via go2rtc restream would need input-path rewriting —
  out of scope (none exist here); such cameras are reported `unsupported` and
  activation is refused.
- Recordings/detection during privacy contain the loop (that's the feature).
  Loop-seam motion blips possible in loop mode.
- On Frigate < 0.18 (or `frigate.swap: restart`, or when the live swap falls
  back) every engage and restore performs a full Frigate restart (loop-mode
  engage performs two — freeze, then the background loop upgrade) and therefore
  causes a recording/availability gap while Frigate cold-boots each time. The
  live swap (§8) on Frigate ≥ 0.18 replaces that with a few-second gap on the
  affected cameras only.
- No scheduling, no per-camera partial privacy UI, no auth beyond the bearer
  token, no built-in TLS (use an authenticated TLS reverse proxy when needed).
- Frozen-frame clips re-encode once at capture (CPU seconds, one-off); loop
  mode never transcodes.
- **Detectability bar: users in the dark, admins informed (guiding axiom, §1).**
  The deception targets non-admin users, downstream video consumers, and
  Frigate's own pipeline — none can tell privacy is active (§4.3). An operator,
  by contrast, is meant to see it plainly (Déjà Vu's `status`/logs, and by
  design an operator-facing indicator); there is deliberately no plausible
  deniability against an admin, and forensic/host evidence (mtimes, encoder
  fingerprints, the Docker/host restart record) is left intact. The only tells
  suppressed are those a non-admin user could notice — chiefly the restart's
  recording-timeline gap — closed by ELIMINATING the seam at its source
  (restart-less swap §17.1/§18; content-side gap continuity §17.3), never by
  scrubbing Frigate's logs/event DB, the host journal, or Docker's restart
  record (cover-tracks/alibi work, a hard non-goal, and pointless when admins
  are meant to see it). Good manners still apply in two senses —
  *leave no trace* (surgical byte-identical restore, no needless config churn,
  no orphaned clips) and *do not disturb the wildlife* (minimal collateral
  disruption to live consumers, recordings, detection, and neighboring services
  while operating) — as hygiene, not concealment.

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

## 16. Production implementation findings (verified 2026-07-07)

Facts discovered while implementing and deploying the appliance that refine the
sections above; all were verified against the production Frigate 0.17.2 /
go2rtc 1.9.10 reference architecture in §2.

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
   templates prove it). The namespaced template is installed on first activation
   and retained as an unused support entry while privacy is off; a conflicting
   pre-existing value is refused rather than overwritten. The appliance
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
  producer can't reach them). The freeze ladder therefore falls through to a
  recorded, cached, or black frame; if the ladder itself fails, the entire
  activation is refused before the config swap.
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
- **Retired go2rtc-only swap experiment**, previously verified live end-to-end
  (ON 21.7 s, OFF 12.5 s, Frigate container untouched, byte-identical restore):
  - *Retrieval*: recordings are read DIRECTLY from the segment files
    (`/recordings/<UTC-date>/<HH>/<camera>/<MM.SS>.mp4`, ro mount) — 0.13 s
    per window vs 3-10 s via the export worker, fully parallel, windows are
    segment-aligned. Export API is the automatic fallback; live restream
    capture is opt-in only (`--source restream`), default fallback = skip.
  - *Effect tested*: `save_option=saveonly` (persistence) + restart of only the
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
    Later production use showed that this can leave Frigate camera consumers
    offline even after go2rtc itself recovers, so the mechanism and its helper
    code were removed. Current toggles always use a coordinated full restart.
  - *Sync anchor is recency-bounded* (hour buckets) — a stale max-coverage
    anchor (pre-sunset) dragged every camera's candidates into frames the
    IR guard had to reject (observed). Within the newest bucket, full-window
    coverage outranks short-window coverage; short synchronized loops no longer
    downgrade cameras that have substantially better full candidates.
  - Coordinated restart through Frigate's API is now the only apply path. The
    old strategy setting and all Docker-socket support were removed.
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

### Freeze fallback and loop-search redesign (2026-07-08; activation ordering revised 2026-07-13)

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
  fact that privacy is on*). (2) `abort` is not a config option; unresolved
  targets, invariant failures, or infrastructure errors refuse the whole
  activation rather than degrading.
  (3) The other way to hide a transient is to make the loop LONGER, so one car
  in 20 min reads as ordinary — repetition is the tell, not the car. Absolute
  quiet is still preferred; fooling the eye is the priority when it isn't
  achievable. (4) Activation ordering has come full circle (revised 2026-07-13):
  the original design engaged immediately on a freeze and searched in the
  background; production go2rtc-only reload failures once retired that ordering in
  favour of finishing the search before ONE coordinated restart — but that
  single-restart model was itself retired, because the loop search can take
  ~1-2 min and held every camera live that whole time. The freeze-first +
  background-loop-upgrade ordering is now REINSTATED, except the loop upgrade
  lands via a full coordinated Frigate restart (restart #2) rather than the
  retired go2rtc-only reload (which needed the Docker socket and dropped recording
  anyway). Freeze is immediate and permanent; the loop is a best-effort
  background upgrade.
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
- **Verified preparation costs** (2026-07-08, live frame grabs): one live freeze
  frame is ~2.4-6 s per camera (probe + grab + synth); at `parallel_frames: 8`
  a 22-camera profile prepares in ~3 waves. Loop assembly runs in parallel in the
  background (stage 2) after restart #1, so it no longer delays privacy.

### Restart-free swap (verified live 2026-09-17, Frigate 0.18.0 / go2rtc 1.9.14)

- **Frigate 0.18 toggles a camera at runtime**: `<cam>/enabled/set` (MQTT or the
  `/ws` bridge) → dispatcher `_on_enabled_command` → the camera process stops or
  starts ALL its ffmpeg; `config.yml` untouched; `enabled/state` echoed
  (retained on MQTT). `ON` is refused for a camera disabled in config.
- **The `/ws` bridge is admin on the internal port**: `/auth` answers every
  :5000 request as `remote-user: anonymous`, `remote-role: admin`, and nginx
  runs `/ws` through the same auth_request, so command topics are accepted with
  no login. Without a role header the bridge fails closed (viewer).
- **Runtime state is only on the bridge**: `{"topic":"onConnect"}` answers with
  `camera_activity`, whose `config.enabled` is the runtime flag. `/api/config`
  reports the file value and keeps `{FRIGATE_*}` placeholders unexpanded.
- **`PUT /api/streams` replaces a stream in place**: consumers dropped, producer
  respawned (new pid observed), so the clip inode at an unchanged path IS
  reloaded — the deterministic stage-2 seam. It also persists into go2rtc's
  first config file (`go2rtc_homekit.yml` here); `GET`/`POST /api/config`
  snapshot and byte-exact restore around the swap.
- **Push publishers cannot simply be PUT**: the map entry gets a new stream
  object, but the old object's external producer is never stopped (`stop()`
  skips `stateExternal`). Tablet WebRTC publishers keep sending into that
  orphan and see no disconnect, so they never re-publish into the restored
  stream. Four tablet feeds lost recording after the 2026-09-17 14:46 UTC
  restore (recovered with HA `script.wallpanel_reload`; a Frigate restart
  works too). Pull cameras re-dial normally. Resolution (§8, verified live
  the same day): PARK the object under a second name via stock go2rtc's
  alias `PATCH` before the `PUT`, and point the public name back at it on
  restore — the publisher's byte counter kept rising throughout and Frigate
  reconnected 4 s after the camera was re-enabled. A publisher that
  reconnects while private lands on the clip object and its parked object
  goes dead; blindly un-parking then orphans the live feed and reports
  success. Fixed by following the publisher: re-park it (stage 2) or
  re-source the clip object in place with `Stream.SetSource` (restore),
  verified live with a wallpanel reload mid-privacy. `SetSource` only
  rewrites `Producer.url`; external producers keep their connection.
- **Frigate's consumers are identifiable**: go2rtc lists them with user agent
  `FFmpeg Frigate/<version>`; counting those is the readiness signal.
  `/api/stats` camera `pid`/fps are shared values NEVER zeroed on disable — a
  proof that waited on them ran to its timeout and cost a 41 s recording gap.
- **Measured**: producer and Frigate consumer respawned in the same second,
  first new recording segment ~4 s after `ON`. Single-camera apply: on 2.0 s,
  loop upgrade 2.6 s, off 3.6–4.1 s; recording gaps 2–6 s. All 22 cameras:
  on 4.2 s (gaps 2.2–6.2 s), off 21.9 s (20 s reconnect wait; measured gaps
  3.9–8.6 s on recovering cameras). Frigate uptime was continuous. These
  all-camera timings preceded the push-fed fix and exclude the tablets'
  continuing outage; they are not evidence of successful tablet recovery.
  The old restart path cost 26–38 s of recording on every camera per restart.
- **Never `pkill -f` a pattern that can match Frigate's ffmpeg** from a test
  harness (did, twice — Frigate's watchdog respawned it).
- **Export API 0.18**: `POST /api/export/{cam}/start/…` answers **202**
  `{"status":"queued"}` (the old 200/201 check rejected every export);
  `GET /api/exports/{id}` still carries `in_progress`; `GET /api/jobs/export/{id}`
  gives `status` (`pending|queued|running|success|failed|cancelled`) and
  `error_message`; deletion is `POST /api/exports/delete {"ids":[…]}`
  (`DELETE /api/export/{id}` is gone).

- **Frigate 0.18 profiles coexist with the live swap** (verified 2026-09-17):
  activating or deactivating a profile (`frigate/profile/set`) resets every
  runtime toggle and republishes the changed sections, but dejavu's camera
  toggles are transient (re-enabled within the swap) and its swap lives in
  go2rtc plus the persisted config, which the ruamel round-trip preserves
  `profiles:` sections through. A freeze on `office` kept serving across a
  profile off/on flip with Frigate attached throughout; restore went live.

## 17. Roadmap

### 17.1 Hot swap without restarting Frigate

SHIPPED (2026-09-17) as the live swap of §8 for Frigate ≥ 0.18: per-camera
runtime stop/start over the WebSocket bridge around a go2rtc `PUT`. No restart
in either direction; the stage-2 loop upgrade is one `PUT` per stream. The
coordinated restart remains the automatic fallback. §18's proxy backend is no
longer needed for the restart problem and stays design-only.

### 17.2 Instant privacy

SHIPPED as the two-stage engage (§5c): privacy
engages as soon as every selected stream has a safe freeze frame; loop
discovery continues in the background and each camera moves to its best safe
loop when one becomes available. A camera with no suitable loop stays frozen,
and privacy is never reported as active while a selected stream remains live.
With the live swap (§8) the loop upgrade lands without a second Frigate
restart.

### 17.3 Switchover invisibility in the timeline (OPEN — content-side, not record erasure)

Goal (§1): a non-admin scrubbing the recording timeline cannot LOCATE the
switchover, in either direction — neither a gap nor a content jump marks where
privacy engaged or disengaged. Two content-side components:

1. **The gap.** Under the `frigate-config` backend each restart leaves a real
   recording gap (~17 s cold-boot) that shows as a discontinuity in Frigate's
   timeline. Open question: whether to backfill it on the CONTENT side — writing
   loop/freeze segments across the hole so a timeline scrubber shows no break —
   as an extension of what the tool already does (fabricate recorded content
   while private).
2. **The visual seam.** Engage already freezes on the current live frame, so the
   live→privacy transition is visually continuous where the scene is quiet. The
   inherent limits are the privacy→live disengage (the scene has since diverged)
   and any real activity present at the engage instant — a genuine jump no replay
   fully hides (best-effort there, per §1).

Backfill is distinct from, and must never be conflated with, log/DB/journal
scrubbing (§1, §14 non-goal): it *adds plausible content*, it does not *erase
the record that the appliance acted*. Honest limit: it serves the §1
user-opacity bar only — a non-admin scrubbing the timeline in ordinary viewing.
Frigate's `recordings` rows, segment mtimes, and the post-hoc authoring still
read as fabricated to an admin or forensic look (intended), and it does nothing
about the Docker/host restart record. Seam elimination (§17.1/§18) is the real
fix: if it lands there is no gap to backfill and the visual seam collapses to a
single instantaneous swap. Not scheduled; recorded here so the goal is captured
together with its boundary.

## 18. Dejavu-owned go2rtc proxy backend (DESIGN — not implemented; the §8 live swap already removes the restarts)

Status: design only. Nothing in this section is built or verified except where
explicitly marked; §18.7 lists the proofs required before implementation.

### 18.1 Motivation and shape

Every operational cost in this contract traces to one fact: the swap is
effected by restarting Frigate. Owning a go2rtc instance moves the swap into a
process the appliance controls, behind the same seam that today is
`soft_restart()` (the two-stage "swap seam"). The appliance BUNDLES go2rtc as a
supervised child process of its own container (no Docker socket, no host
access, no sibling-container control needed — reloading the proxy is
respawning our own child). Its native go2rtc config file is authored by the
appliance directly; Frigate's `str.format` brace trap (§16) does NOT apply to
it, so loop templates like `-re -stream_loop -1 -i {input}` are written
plainly.

Backend selection (proposed config):

    swap:
      backend: frigate-config     # default — current behavior (§4-§8)
              | proxy-interpose   # §18.3
    proxy:
      rtsp_url: rtsp://frigate-dejavu:8554   # how FRIGATE reaches our go2rtc
      api_url: http://127.0.0.1:1984          # how the appliance verifies it
      # go2rtc binary path, stream port, and health probe are appliance-internal.

Two hard scope invariants bound this design (owner directives, §13b):
- The appliance NEVER accesses cameras. Cameras remain Frigate's alone; our
  go2rtc only ever serves local clip files. IP-restricting cameras to the
  Frigate host stays valid.
- The appliance is NEVER a permanent proxy. It interposes only WHILE privacy is
  on, for the selected streams only; `off` restores Frigate's original sources
  and removes us from the media path entirely.
A standing/always-in-path restream layer that fronts cameras is explicitly out
of scope — it would require camera access and permanence, violating both.

Both backends share: profiles, the freeze ladder, loop search/guards, clips at
`<stream>.dejavu.mp4`, the state machine, `streams.json` restore records,
debounce, and the REST/CLI surface. Only the apply mechanism differs.

### 18.2 Proxy mechanics

- The appliance writes its OWN go2rtc config: one stream per selected Frigate
  stream name, each source an ffmpeg file-loop of that stream's
  `<stream>.dejavu.mp4` clip (same fixed-path convention as today).
- Apply = atomic config write + child-process respawn (~1-2 s). Frigate-side
  consumers of our RTSP streams drop once and re-dial via Frigate's watchdog /
  go2rtc on-demand redial — the same reconnect behavior cameras already
  exhibit after a brief network blip. Promptness of the two-layer redial is
  UNVERIFIED (§18.7-P1).
- Loop promotion keeps the same-path trick: `os.replace` the loop over the
  freeze clip, respawn the proxy. No config change, no Frigate involvement.
  (`-stream_loop` holds its open inode, so a respawn — not just the file swap —
  is required; per-producer kicks instead of a full respawn are an
  optimization, §18.7-P4.)
- Because updates are restart-free from Frigate's perspective, long sessions
  can ROTATE their loops as the time of day drifts — the §19 previous-day
  rotation is designed around exactly this economics (a rotation costs ~2 s
  here vs a full Frigate restart under `frigate-config`).
- Verification = our own `/api/streams` (producers serving, consumers attached
  — an attached consumer proves Frigate is pulling). The appliance never needs
  Frigate's API to verify content it serves itself.

### 18.3 `proxy-interpose`: interpose only while private

The one and only proxy backend, and the shape both §18.1 invariants permit. The
appliance's go2rtc carries NO camera access — cameras may be IP-restricted to
the Frigate host; the proxy serves only local clip files. It is in the media
path only WHILE privacy is on, for selected streams only. §13b holds: the
off-state media path is untouched, and while interposed there is no live stream
to delay (only clips).

Engage (replaces §6 steps 6-8 apply; everything before is unchanged):
1. Prepare freeze clips per stream (ladder, §5c) and select loop windows —
   both from Frigate's restream/API, BEFORE any swap, exactly as today.
2. Write our go2rtc config serving every selected stream's clip; respawn;
   verify our `/api/streams`.
3. Rewrite Frigate's `go2rtc.streams`: each selected stream's source list
   becomes the single interpose URL `rtsp://<proxy.rtsp_url>/<stream>`.
   `streams.json` records `original_kind`/`original_sources` verbatim —
   surgical restore (§7) is unchanged.
4. ONE coordinated Frigate restart + verify (+ rollback), identical to today's
   restart #1. Privacy is ON (freeze first, two-stage §5c).
5. Stage-2 loop upgrades land via §18.2 promotion — restart #2 CEASES TO EXIST
   (the second event is now a ~2 s proxy respawn, not a Frigate restart).

Off: surgical restore + ONE coordinated Frigate restart (unchanged §7), then
tear down our streams. Restore never depends on the proxy being alive.

Failure while interposed: our container dying takes the SELECTED streams dark
at Frigate (recording gap for those cameras until compose `restart: always`
revives it — the clips and config are on disk, recovery is automatic).
Unselected cameras are never touched. This bounded exposure — private streams
only, only while private — is the price of restart-free upgrades; §13b's
off-state guarantee is never at risk.

### 18.4 Loop content sourcing

| Source | frigate-config | proxy-interpose |
|---|---|---|
| `/recordings` direct reads | fast path | fast path |
| Frigate export API | fallback (§5b) | fallback — UNIQUELY enables a fully decoupled deployment: no camera access, no recordings mount, network-only |

Because the appliance never carries the live camera feed (§18.1: no camera
access, clips only), sourcing loops from a rolling passthrough buffer is NOT
available — loop content is always `/recordings` direct reads or the export
API. Under `proxy-interpose` with export sourcing the appliance touches Frigate
through exactly two channels: its REST API and one restart per direction.

### 18.5 State machine and seam changes

- `soft_restart(expect)` generalizes to the backend seam `apply_swap(expect)`:
  `frigate-config` → coordinated restart (§8); `proxy-interpose` → §18.2 respawn
  + self-verification. The observed-restart proof (§6 step 11) is replaced under
  the proxy backend by consumer-attachment verification on our own API.
- `upgrade_pid`/ownership-gate/off-cancel concurrency carries over unchanged;
  stage 2 merely becomes cheap.
- New states: none. New failure mapping: proxy respawn that never comes
  healthy → `error` (streams dark is an outage, not a privacy regression —
  Frigate still points at us and no live source is exposed).

### 18.6 What each backend costs (summary)

| | frigate restarts on/off | loop update | camera access | §13b | blast radius if appliance dies |
|---|---|---|---|---|---|
| frigate-config | 1-2 / 1 | frigate restart | never | holds | none (inert when idle) |
| proxy-interpose | 1 / 1 | ~2 s respawn | never | holds | selected streams, only while ON |

### 18.7 Proofs required before implementation (blocking)

- P1: two-layer redial promptness — swap+respawn our go2rtc; measure how fast
  Frigate's go2rtc → ffmpeg consumer chain re-dials and serves the new
  content, and that no producer wedges (the §16 SetSource lesson, one layer
  removed). Containerized PoC, no production Frigate required.
- P2: interpose-URL adoption — Frigate config pointing at an external go2rtc
  RTSP URL survives its config validator and one restart cycle (expected yes;
  verify).
- P3: bundled go2rtc supervision — child-process lifecycle inside the
  appliance container (spawn, health, respawn, clean shutdown), version pinned
  and shipped in the image.
- P4: per-producer refresh without full respawn (optimization; P1 decides
  whether it is even needed).
- P5: RTSP exposure — our go2rtc port is reachable by Frigate; decide bind/auth
  posture so clip streams are not world-readable on the LAN.

## 19. Previous-day sourcing & time-period loop rotation (DESIGN — not implemented)

Status: design only. This is the rolling-buffer idea reborn within the §18.1
invariants: the appliance never carries the live feed, but Frigate's own
recordings already hold "yesterday's stream" — so instead of buffering
forward, look BACKWARD whole days. Lighting drifts continuously, so a session
left on long enough goes stale by the clock: rotation re-sources every 90
minutes (default) from a previous day at the current clock time, and the
engage-time lookback was reduced 4 → 3 h (≈ one lighting period) so even the
first loop is never sourced more than one period from "now".

Risk posture (owner directive): this is BEST-EFFORT PLAUSIBILITY, not verified
fidelity. Mid-session there is nothing real to verify against — the goal is
content a viewer accepts as "now", assembled under every guard that still
applies, and a rotation that finds nothing acceptable leaves the current clip
standing. We do our best; we are never worse than the clip already serving.

### 19.1 Previous-day candidate tier (engage-time)

- In addition to the recent lookback (last `search_hours` ≤ 3 h), candidate
  windows are also mined from previous days at the SAME clock time: for
  `d = 1..previous_days`, the window around `now − 24 h·d`. Nearest day first,
  DESCENDING day by day until a guarded window lands — yesterday's hour may
  have been rainy, private (§19.3), or busy; the day before often is not.
- Same-clock-time sourcing is the point: sun geometry is near-identical at
  the same time on an adjacent day, and the footage carries its own correct
  IR state (a camera that was in IR at this clock time yesterday almost
  certainly is today). Every existing guard still applies — at engage a live
  reference exists, so the vs-now brightness and IR-mode guards screen
  previous-day candidates exactly like recent ones; day-over-day weather
  change is precisely what they catch. Event/person, dilution, drift, and
  seam guards are unchanged.
- Ranking: previous-day windows join the pool as an older recency bucket
  under the existing tier-first ordering — so a QUIET window from yesterday
  at this hour outranks a diluted or short window from the busy last hour.
  This materially raises loop success on cameras that are busy whenever
  someone is home to toggle privacy.
- Export-path synergy (§18.4): previous-day windows sit far outside every
  export freshness margin — the safest possible export candidates, which is
  exactly what the decoupled deployment needs.
- Retention bounds the descent naturally: only days whose segments AND event
  metadata still exist are searched (fewer retained days = shorter descent;
  zero = the recent lookback stands alone — graceful).

### 19.2 Rotation across periods (long sessions)

- Motivation: a loop engaged at 13:00 is a lie by 19:00 — wrong sun, wrong
  shadows, possibly wrong IR mode. `search_hours: 3` bounds that staleness at
  engage; rotation extends the bound across arbitrarily long sessions, keeping
  playback within ~90 minutes of the true clock.
- Every `rotation.period_minutes` (default 90, rolling from engage), the
  appliance re-runs the stage-2 pipeline anchored to "a previous day at the
  CURRENT clock time": select windows around `now − 24 h·d` (descending days,
  as §19.1), assemble, guard, promote onto the same `<stream>.dejavu.mp4`
  paths, and land them through the swap seam. One shared previous-day anchor
  keeps overlapping cameras rotating to the same moment (§5b sync).
- Mid-session there is NO live reference — the cameras are not being pulled,
  and `latest.jpg` shows the loop. The vs-now lighting guard is therefore
  unavailable; time-of-day alignment IS the lighting guard, and the recorded
  footage carries its own IR state. Intra-window drift, event/person,
  dilution, and seam guards still apply in full. Residual accepted risk:
  day-over-day weather change — best-effort plausibility by design (see the
  risk posture above); an optional tolerant sanity check against the OUTGOING
  loop's endpoint stats is an open item.
- Cost per rotation: `proxy-interpose` ≈ 2 s respawn (the designed home,
  §18.2); `frigate-config` = one full coordinated restart per boundary —
  permitted but discouraged and off by default there.
- Execution: the long-lived API service schedules an internal `dejavu rotate`
  transition while `state == on`. It reuses the stage-2 machinery verbatim —
  `upgrade_pid` marker, ownership gate, `off` cancels it and wins, budget
  bound, and the never-less-private rule: any failure (no guarded window, a
  stream's search timing out, the swap not landing) leaves the CURRENT clips
  standing. A rotation is an upgrade of a private stream to a fresher private
  stream, nothing else.

### 19.3 Never loop a loop (session-history exclusion)

Footage recorded while dejavu was ON is loop/freeze content; sourcing it would
compound copies of copies and can resurrect a stale scene. The appliance
persists an append-only session log (`state/sessions.log`: engage/restore
timestamps, written where the state file already transitions), and every
candidate window — recent, previous-day, or rotation — overlapping ANY logged
ON interval is excluded before ranking. This also excludes the current
session's own recordings during rotation by construction. History before the
log existed is treated as clean (the content guards still screen it).

### 19.4 Proposed configuration

    capture:
      recordings:
        search_hours: 3       # recent lookback ≈ one lighting period (SHIPPED)
        previous_days: 7      # same-clock-time days to descend through; 0 disables
      rotation:
        enabled: false        # design; intended default true under proxy-interpose
        period_minutes: 90

Open items: rotation-vs-debounce interplay; whether the outgoing-loop
endpoint-stats sanity check earns its complexity; retention shorter than
`previous_days` (degrades to however many days exist — verify gracefully).
