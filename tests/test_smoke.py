import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import config  # noqa: E402

# The appliance runs on Linux, but keep the orchestration unit test runnable
# from a Windows checkout where the stdlib has no fcntl module.
if sys.platform == "win32":
    sys.modules.setdefault("fcntl", mock.Mock())
import core  # noqa: E402
import frigate  # noqa: E402
import recordings  # noqa: E402

_prior_config = os.environ.get("DEJAVU_CONFIG")
os.environ["DEJAVU_CONFIG"] = str(ROOT / "config.example.yaml")
try:
    import api  # noqa: E402
finally:
    if _prior_config is None:
        os.environ.pop("DEJAVU_CONFIG", None)
    else:
        os.environ["DEJAVU_CONFIG"] = _prior_config


SAMPLE_FRIGATE_CONFIG = """\
go2rtc:
  streams:
    front: rtsp://camera/live # preserve this comment
  ffmpeg: {}
cameras:
  front_door:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/front?video=copy
          roles: [detect, record]
  direct_camera:
    ffmpeg:
      inputs:
        - path: rtsp://camera/direct
          roles: [detect]
  missing_camera:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/not_defined
          roles: [detect]
"""


class ConfigTests(unittest.TestCase):
    def test_example_config_validates(self):
        cfg = config.load_config(str(ROOT / "config.example.yaml"))
        self.assertEqual("loop", cfg["profiles"]["default"]["mode"])
        for retired in (
            "swap_method",
            "container_name",
            "restart_method",
            "restart_fallback",
        ):
            self.assertNotIn(retired, cfg["frigate"])

    def test_minimal_config_defaults_to_loop(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write("{}\n")
            path = handle.name
        try:
            cfg = config.load_config(path)
        finally:
            Path(path).unlink()
        self.assertEqual("loop", cfg["profiles"]["default"]["mode"])

    def test_implicit_profile_mode_is_consistently_freeze(self):
        cfg = config.load_config(str(ROOT / "config.example.yaml"))
        cfg["profiles"]["implicit"] = {"cameras": []}

        self.assertEqual(
            "freeze", core.effective_request(cfg, profile="implicit")["mode"]
        )
        self.assertEqual("freeze", core.resolved_profiles(cfg)["implicit"]["mode"])

    def test_state_write_drops_retired_template_flag(self):
        cfg = config.load_config(str(ROOT / "config.example.yaml"))
        with tempfile.TemporaryDirectory() as tmp:
            cfg["paths"]["state_dir"] = str(Path(tmp) / "state")
            cfg["paths"]["tmp_dir"] = str(Path(tmp) / "tmp")
            cfg["paths"]["clips_local"] = str(Path(tmp) / "clips")
            store = core.StateStore(cfg)

            store._write({"state": "off", "template_loaded": True})

            self.assertNotIn("template_loaded", store.read())

    def test_profile_typos_and_boolean_seconds_are_rejected(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["profiles"] = {"default": {"mode": "loop", "camreas": ["front"]}}
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["profiles"]["default"]["capture_seconds"] = True
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


class ApiValidationTests(unittest.TestCase):
    def setUp(self):
        api.app.testing = True
        self.client = api.app.test_client()

    def test_invalid_bodies_never_spawn_default_all_camera_job(self):
        calls = (
            lambda: self.client.post("/api/dejavu/on", json=["front"]),
            lambda: self.client.post(
                "/api/dejavu/on", data="{", content_type="application/json"
            ),
            lambda: self.client.post("/api/dejavu/on", json={"camreas": ["front"]}),
            lambda: self.client.post("/api/dejavu/on", json={"cameras": "   "}),
            lambda: self.client.post("/api/dejavu/on", json={"capture_seconds": True}),
        )
        with mock.patch.object(api, "_spawn") as spawn:
            for call in calls:
                with self.subTest(call=call):
                    self.assertEqual(400, call().status_code)
            spawn.assert_not_called()

    def test_empty_post_is_the_simple_all_camera_request(self):
        with (
            mock.patch.object(api.core, "preflight", return_value={"noop": False}),
            mock.patch.object(api.core, "get_status", return_value={"state": "off"}),
            mock.patch.object(api, "_spawn") as spawn,
        ):
            response = self.client.post("/api/dejavu/on")

        self.assertEqual(202, response.status_code)
        spawn.assert_called_once_with(["on"], "on")


class FrigateConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load_config(str(ROOT / "config.example.yaml"))
        self.yaml, self.data = frigate.parse_config(SAMPLE_FRIGATE_CONFIG)

    def test_stream_resolution(self):
        plan, notes = frigate.resolve_streams(self.data, self.cfg, ["front_door"])
        self.assertEqual(["front"], list(plan))
        self.assertEqual(["front_door"], plan["front"]["record_cameras"])
        self.assertFalse(notes["unsupported_cameras"])

    def test_unsupported_camera_is_reported(self):
        plan, notes = frigate.resolve_streams(self.data, self.cfg, ["direct_camera"])
        self.assertFalse(plan)
        self.assertEqual(["direct_camera"], notes["unsupported_cameras"])
        self.assertEqual(
            ["unsupported cameras: direct_camera"], frigate.resolution_blockers(notes)
        )

    def test_missing_stream_blocks_resolution(self):
        plan, notes = frigate.resolve_streams(self.data, self.cfg, ["missing_camera"])
        self.assertFalse(plan)
        self.assertEqual(["not_defined"], notes["missing_streams"])
        self.assertEqual(
            ["missing go2rtc streams: not_defined"], frigate.resolution_blockers(notes)
        )

    def test_apply_and_restore_preserves_sources(self):
        backup_yaml, backup = frigate.parse_config(
            frigate.dump_config(self.yaml, self.data)
        )
        del backup_yaml
        original_kind, original_sources = frigate.normalize_sources(
            self.data["go2rtc"]["streams"]["front"]
        )
        source = frigate.build_dejavu_source("/config/dejavu-clips", "front", False, [])
        frigate.apply_dejavu(self.data, {"front": source})
        record = {
            "front": {
                "original_kind": original_kind,
                "original_sources": original_sources,
                "dejavu_source": source,
            }
        }
        changed, restored, drifted, missing = frigate.graft_restore(
            self.data, backup, record
        )
        self.assertTrue(changed)
        self.assertEqual(["front"], restored)
        self.assertFalse(drifted)
        self.assertFalse(missing)
        self.assertEqual(
            original_sources,
            frigate.normalize_sources(self.data["go2rtc"]["streams"]["front"])[1],
        )
        self.assertEqual(
            frigate.DEJAVU_TEMPLATE_ARGS,
            self.data["go2rtc"]["ffmpeg"][frigate.DEJAVU_TEMPLATE_NAME],
        )

    def test_apply_refuses_conflicting_reserved_template(self):
        self.data["go2rtc"]["ffmpeg"][frigate.DEJAVU_TEMPLATE_NAME] = "different"
        source = frigate.build_dejavu_source("/config/dejavu-clips", "front", False, [])

        with self.assertRaises(frigate.FrigateError):
            frigate.apply_dejavu(self.data, {"front": source})


class RestartTests(unittest.TestCase):
    def test_restart_uses_only_frigate_api(self):
        job = core.Job.__new__(core.Job)
        job.cfg = {"frigate": {"health_timeout_seconds": 5}}
        job.client = mock.Mock()
        job.client.wait_healthy.return_value = True

        job.restart_and_verify({}, direction="on")

        job.client.restart_api.assert_called_once_with()
        job.client.wait_down.assert_called_once_with(30)
        job.client.wait_healthy.assert_called_once_with(5)
        self.assertFalse(hasattr(frigate.FrigateClient, "docker_restart"))


class RecordingWindowTests(unittest.TestCase):
    def test_quiet_window_is_selected(self):
        now = 10_000.0
        segments = [
            {
                "start_time": now - 400 + index * 10,
                "end_time": now - 390 + index * 10,
                "objects": 0,
                "motion": 0,
            }
            for index in range(35)
        ]
        windows = recordings.find_candidate_windows(
            segments,
            [],
            target_seconds=300,
            min_seconds=20,
            now=now,
            recent_margin=30,
            max_seconds=300,
        )
        self.assertTrue(windows)
        self.assertGreaterEqual(windows[0]["duration"], 299)
        self.assertEqual(recordings.TIER_QUIET, windows[0]["tier"])

    def test_diluted_search_backs_off_from_busy_tail(self):
        segments = [
            {
                "start_time": index * 10,
                "end_time": (index + 1) * 10,
                "objects": 1 if index == 15 or index >= 30 else 0,
                "motion": 0,
            }
            for index in range(120)
        ]
        windows = recordings._slide_diluted(
            segments, need=300, max_seconds=1200, max_frac=0.10, spans=[], now=2000
        )
        self.assertTrue(windows)
        self.assertGreaterEqual(windows[0]["duration"], 300)
        self.assertLessEqual(windows[0]["activity_fraction"], 0.10)

    def test_full_window_outranks_newer_short_window(self):
        now = 10_000
        older = [
            {"start_time": i * 10, "end_time": (i + 1) * 10, "objects": 0, "motion": 0}
            for i in range(40)
        ]
        recent = [
            {
                "start_time": 9900 + i * 10,
                "end_time": 9910 + i * 10,
                "objects": 0,
                "motion": 0,
            }
            for i in range(3)
        ]
        windows = recordings.find_candidate_windows(
            older + recent,
            [],
            300,
            20,
            now,
            recent_margin=5,
            dilute={"enabled": False},
            max_seconds=1200,
        )
        self.assertEqual(recordings.TIER_QUIET, windows[0]["tier"])
        self.assertGreaterEqual(windows[0]["duration"], 300)

    def test_sync_does_not_promote_short_over_full(self):
        full = {"start": 0, "end": 400, "duration": 400, "tier": recordings.TIER_QUIET}
        short = {
            "start": 9000,
            "end": 9050,
            "duration": 50,
            "tier": recordings.TIER_SHORT,
        }

        def candidates(_api, camera, *_args, **_kwargs):
            return [full, short] if camera == "continuous" else [short]

        with mock.patch.object(
            recordings, "plan_candidate_windows", side_effect=candidates
        ):
            _anchor, ordered, _errors = recordings.select_synced_windows(
                "api",
                {"continuous": 300, "intermittent": 300},
                20,
                4,
                tolerance_s=60,
                now=10_000,
            )
        self.assertIs(full, ordered["continuous"][0])
        self.assertIs(short, ordered["intermittent"][0])

    def test_sync_anchor_prefers_full_coverage_within_hour(self):
        full = {
            "start": 8700,
            "end": 9000,
            "duration": 300,
            "tier": recordings.TIER_QUIET,
        }
        short = {
            "start": 9300,
            "end": 9350,
            "duration": 50,
            "tier": recordings.TIER_SHORT,
        }

        def candidates(_api, camera, *_args, **_kwargs):
            return [full, short] if camera != "intermittent" else [short]

        needs = {"continuous_a": 300, "continuous_b": 300, "intermittent": 300}
        with mock.patch.object(
            recordings, "plan_candidate_windows", side_effect=candidates
        ):
            anchor, _ordered, _errors = recordings.select_synced_windows(
                "api", needs, 20, 4, tolerance_s=60, now=10_000
            )
        self.assertEqual(9000, anchor)

    def test_direct_assembly_applies_silence_in_concat_pass(self):
        probe = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 1,
                    "sample_rate": "48000",
                },
            ],
            "format": {"duration": "10"},
        }
        win = {
            "start": 0,
            "end": 300,
            "duration": 300,
            "tier": recordings.TIER_QUIET,
            "motion_ps": 0,
            "activity_fraction": 0,
            "segments": [0, 10],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(
                    recordings, "segment_files", return_value=["a.mp4", "b.mp4"]
                ),
                mock.patch.object(recordings, "probe_file", return_value=probe),
                mock.patch.object(recordings, "_run") as run,
            ):
                out = str(Path(tmp) / "loop.mp4")
                result = recordings._fetch_window_via_files(
                    "/recordings",
                    "camera",
                    "stream",
                    win,
                    tmp,
                    None,
                    lambda *_: None,
                    now=1000,
                    out_path=out,
                    audio_policy="silence",
                    want_audio=True,
                )
        self.assertEqual(out, result)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn("anullsrc=channel_layout=mono:sample_rate=48000", command)
        self.assertIn(out, command)

    def test_seam_ranking_selects_best_equivalent_window(self):
        wins = [
            {
                "start": start,
                "end": 900 + start,
                "duration": 300,
                "tier": recordings.TIER_QUIET,
            }
            for start in (0, 10, 20)
        ]
        with (
            mock.patch.object(
                recordings, "files_retrieval_available", return_value=True
            ),
            mock.patch.object(
                recordings,
                "screen_window_files",
                side_effect=[(None, 20.0), (None, 5.0), (None, 10.0)],
            ),
        ):
            picked, reasons, unavailable = recordings._pick_window(
                "api",
                "camera",
                "stream",
                wins,
                None,
                {},
                None,
                lambda *_: None,
                now=1200,
                recordings_dir="/recordings",
                check_drift=True,
            )
        self.assertFalse(reasons)
        self.assertFalse(unavailable)
        self.assertEqual(10, picked["start"])
        self.assertEqual(5.0, picked["seam_delta"])

    def test_missing_direct_segments_fall_back_to_frigate_export(self):
        win = {
            "start": 0,
            "end": 300,
            "duration": 300,
            "full": True,
            "tier": recordings.TIER_QUIET,
            "motion_ps": 0,
            "activity_fraction": 0,
            "segments": [0, 10],
        }
        meta = {
            "video_codec": "h264",
            "audio_codec": None,
            "duration": 300,
            "width": 1920,
            "height": 1080,
            "fps": 15,
        }
        with (
            mock.patch.object(
                recordings, "files_retrieval_available", return_value=True
            ),
            mock.patch.object(recordings, "_resolve_candidates", return_value=[win]),
            mock.patch.object(recordings, "_reference", return_value=None),
            mock.patch.object(
                recordings, "_pick_window", return_value=(None, ["missing"], [win])
            ),
            mock.patch.object(
                recordings, "_fetch_window_clip", return_value="export.mp4"
            ) as fetch,
            mock.patch.object(recordings, "_lighting_ok", return_value=None),
            mock.patch.object(recordings, "frame_stats", return_value={}),
            mock.patch.object(recordings, "frame_stats_tail", return_value={}),
            mock.patch.object(recordings, "_stable_across", return_value=None),
            mock.patch.object(recordings, "probe_file", return_value={}),
            mock.patch.object(recordings, "summarize_probe", return_value=meta),
            mock.patch.object(recordings, "apply_audio_policy", return_value=False),
            mock.patch.object(recordings.log, "warning"),
        ):
            result = recordings.source_clip_from_recordings(
                "api",
                "camera",
                "stream",
                "/clips",
                "/tmp",
                300,
                {"min_seconds": 20, "audio": "silence"},
                None,
                lambda *_: None,
                recordings_dir="/recordings",
                max_seconds=1200,
            )

        self.assertEqual("h264", result["clip_meta"]["video_codec"])
        self.assertIsNone(fetch.call_args.kwargs["recordings_dir"])

    def test_failed_export_advances_to_next_candidate(self):
        wins = [
            {
                "start": start,
                "end": start + 300,
                "duration": 300,
                "full": True,
                "tier": recordings.TIER_QUIET,
                "motion_ps": 0,
                "activity_fraction": 0,
                "segments": [start, start + 10],
            }
            for start in (0, 600)
        ]
        meta = {
            "video_codec": "h264",
            "audio_codec": None,
            "duration": 300,
            "width": 1920,
            "height": 1080,
            "fps": 15,
        }
        with (
            mock.patch.object(
                recordings, "files_retrieval_available", return_value=False
            ),
            mock.patch.object(recordings, "_resolve_candidates", return_value=wins),
            mock.patch.object(recordings, "_reference", return_value=None),
            mock.patch.object(
                recordings,
                "_fetch_window_clip",
                side_effect=[
                    recordings.CaptureError("first export failed"),
                    "second.mp4",
                ],
            ) as fetch,
            mock.patch.object(recordings, "_lighting_ok", return_value=None),
            mock.patch.object(recordings, "frame_stats", return_value={}),
            mock.patch.object(recordings, "frame_stats_tail", return_value={}),
            mock.patch.object(recordings, "_stable_across", return_value=None),
            mock.patch.object(recordings, "probe_file", return_value={}),
            mock.patch.object(recordings, "summarize_probe", return_value=meta),
            mock.patch.object(recordings, "apply_audio_policy", return_value=False),
        ):
            result = recordings.source_clip_from_recordings(
                "api",
                "camera",
                "stream",
                "/clips",
                "/tmp",
                300,
                {"min_seconds": 20, "audio": "silence"},
                None,
                lambda *_: None,
                recordings_dir=None,
                max_seconds=1200,
            )

        self.assertEqual("h264", result["clip_meta"]["video_codec"])
        self.assertEqual(2, fetch.call_count)

    def test_export_fallback_skips_windows_too_fresh_for_export(self):
        now = 1_000.0
        fresh = {"start": 700, "end": 990}
        settled = {"start": 600, "end": 950}
        reasons = []

        result = recordings._exportable_windows([fresh, settled], now, reasons)

        self.assertEqual([settled], result)
        self.assertEqual(["1 candidate(s) too recent for safe export"], reasons)

    def test_seam_delta_uses_low_resolution_luma(self):
        first = {"signature": (0, 10, 20, 30)}
        last = {"signature": (0, 20, 40, 60)}
        self.assertEqual(15.0, recordings._seam_delta(first, last))


class CachedFrameTests(unittest.TestCase):
    def test_cached_frame_retains_codec_resolution_and_audio_shape(self):
        cfg = config.load_config(str(ROOT / "config.example.yaml"))
        meta = {
            "video_codec": "hevc",
            "width": 3840,
            "height": 2160,
            "pix_fmt": "yuv420p",
            "fps": 20.0,
            "audio_codec": "aac",
            "sample_rate": 48000,
            "channels": 2,
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg["paths"]["state_dir"] = str(Path(tmp) / "state")
            frame = Path(tmp) / "frame.png"
            frame.write_bytes(b"cached frame")

            core._remember_frame(cfg, "camera.front", str(frame), meta)
            cached = core._cached_frame_meta(cfg, "camera.front")

        self.assertEqual(meta, cached)


if __name__ == "__main__":
    unittest.main()
