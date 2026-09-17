import copy
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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

EXAMPLE_CONFIG = ROOT / "config.example.yaml"
EXAMPLE_ENV = {
    "DEJAVU_API_TOKEN": "",
    "DEJAVU_FRIGATE_USER": "",
    "DEJAVU_FRIGATE_PASSWORD": "",
}
with mock.patch.dict(os.environ, {"DEJAVU_CONFIG": str(EXAMPLE_CONFIG), **EXAMPLE_ENV}):
    import api  # noqa: E402


def load_example_config():
    with mock.patch.dict(os.environ, EXAMPLE_ENV):
        return config.load_config(str(EXAMPLE_CONFIG))


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
        cfg = load_example_config()
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

    def test_dejavu_env_placeholders_are_explicit(self):
        text = """\
frigate:
  api_url: "http://{DEJAVU_TEST_HOST}:5000"
  api_auth:
    user: "{DEJAVU_TEST_USER}"
api:
  bearer_token: literal
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(text)
            path = handle.name
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "DEJAVU_TEST_HOST": "frigate.local",
                    "DEJAVU_TEST_USER": "someone",
                    "DEJAVU_API_TOKEN": "must-not-override",
                },
            ):
                cfg = config.load_config(path)
        finally:
            Path(path).unlink()
        self.assertEqual("http://frigate.local:5000", cfg["frigate"]["api_url"])
        self.assertEqual("someone", cfg["frigate"]["api_auth"]["user"])
        self.assertEqual("literal", cfg["api"]["bearer_token"])

    def test_tls_verify_accepts_bools_paths_and_env_strings(self):
        cfg = load_example_config()
        self.assertIs(True, cfg["frigate"]["tls_verify"])
        for given, want in (
            (False, False),
            ("false", False),
            ("True", True),
            ("", True),
            ("/etc/ssl/certs/frigate.pem", "/etc/ssl/certs/frigate.pem"),
        ):
            cfg["frigate"]["tls_verify"] = given
            config.validate(cfg)
            self.assertEqual(want, cfg["frigate"]["tls_verify"], f"given {given!r}")

    def test_tls_verify_rejects_nonsense(self):
        cfg = load_example_config()
        for bad in (0, "relative/ca.pem", None):
            cfg["frigate"]["tls_verify"] = bad
            with self.assertRaisesRegex(config.ConfigError, "tls_verify"):
                config.validate(cfg)

    def test_init_http_applies_tls_verify_to_the_shared_session(self):
        cfg = load_example_config()
        cfg["frigate"]["tls_verify"] = False
        cfg["frigate"]["api_auth"] = {"user": "", "password": ""}
        original = frigate.HTTP.verify
        try:
            frigate.init_http(cfg)
            self.assertIs(False, frigate.HTTP.verify)
        finally:
            frigate.HTTP.verify = original

    def test_static_auth_headers_are_applied_to_the_session(self):
        cfg = load_example_config()
        cfg["frigate"]["api_auth"] = {
            "user": "",
            "password": "",
            "headers": {"X-Proxy-Secret": "s3cret", "X-Auth-Request-Groups": "/ops"},
        }
        saved = dict(frigate.HTTP.headers)
        try:
            frigate.init_http(cfg)
            self.assertEqual("s3cret", frigate.HTTP.headers["X-Proxy-Secret"])
            self.assertEqual("/ops", frigate.HTTP.headers["X-Auth-Request-Groups"])
        finally:
            frigate.HTTP.headers.clear()
            frigate.HTTP.headers.update(saved)

    def test_auth_headers_reject_non_string_values(self):
        cfg = load_example_config()
        cfg["frigate"]["api_auth"] = {"user": "", "password": "", "headers": {"X": 1}}
        with self.assertRaisesRegex(config.ConfigError, "headers"):
            config.validate(cfg)

    def test_missing_auth_env_placeholder_is_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(config.ConfigError, "DEJAVU_FRIGATE_USER"):
                config.load_config(str(EXAMPLE_CONFIG))

    def test_missing_dejavu_env_placeholder_is_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write('api:\n  bearer_token: "{DEJAVU_TEST_MISSING}"\n')
            path = handle.name
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(config.ConfigError, "DEJAVU_TEST_MISSING"):
                    config.load_config(path)
        finally:
            Path(path).unlink()

    def test_implicit_profile_mode_is_consistently_freeze(self):
        cfg = load_example_config()
        cfg["profiles"]["implicit"] = {"cameras": []}

        self.assertEqual(
            "freeze", core.effective_request(cfg, profile="implicit")["mode"]
        )
        self.assertEqual("freeze", core.resolved_profiles(cfg)["implicit"]["mode"])

    def test_state_write_drops_retired_template_flag(self):
        cfg = load_example_config()
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
        self.cfg = load_example_config()
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


class LoopSearchDeadlineTests(unittest.TestCase):
    """Privacy is already on via freeze while stage 2 runs, but loop assembly is
    still bounded so a slow export or scan cannot run forever."""

    def test_any_cancel_follows_its_source(self):
        source = core.CancelToken()
        cancel = core._AnyCancel(source)
        self.assertFalse(cancel.cancelled)
        source.cancel()  # e.g. a SIGTERM cancels the whole job
        self.assertTrue(cancel.cancelled)
        with self.assertRaises(core.Cancelled):
            cancel.check()

    def test_search_within_budget_is_collected(self):
        cancel = core._AnyCancel()
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            futures = {
                "a": pool.submit(lambda: {"clip": "a"}),
                "b": pool.submit(lambda: {"clip": "b"}),
            }
            upgrades = core._collect_upgrades(futures, {"a", "b"}, cancel, 30)
        finally:
            pool.shutdown(wait=True)
        self.assertEqual({"a", "b"}, set(upgrades))
        self.assertFalse(cancel.cancelled)  # generous budget never trips the timer

    def test_slow_search_is_abandoned_at_the_deadline(self):
        cancel = core._AnyCancel()
        pool = ThreadPoolExecutor(max_workers=2)

        def slow():
            # a real search polls the cancel token; stop once the deadline trips
            # it, exactly as source_clip_from_recordings would.
            while not cancel.cancelled:
                time.sleep(0.01)
            raise core.Cancelled()

        try:
            futures = {
                "fast": pool.submit(lambda: {"clip": "fast"}),
                "slow": pool.submit(slow),
            }
            upgrades = core._collect_upgrades(futures, {"fast", "slow"}, cancel, 0.2)
        finally:
            pool.shutdown(wait=True)
        self.assertEqual({"fast"}, set(upgrades))  # slow stream stays on freeze
        self.assertTrue(cancel.cancelled)

    def test_time_spent_before_collection_counts_against_budget(self):
        cancel = core._AnyCancel()
        pool = ThreadPoolExecutor(max_workers=1)

        def cooperative_search():
            while not cancel.cancelled:
                time.sleep(0.01)
            raise core.Cancelled()

        started = time.monotonic() - 1
        try:
            futures = {"cam": pool.submit(cooperative_search)}
            upgrades = core._collect_upgrades(
                futures, {"cam"}, cancel, 0.2, budget_started_at=started
            )
        finally:
            pool.shutdown(wait=True)
        self.assertEqual({}, upgrades)
        self.assertTrue(cancel.cancelled)


class TwoStageEngageTests(unittest.TestCase):
    """Stage 1 is a permanent freeze baseline; stage 2 is a best-effort loop
    upgrade that a concurrent 'off' can cancel and that never turns privacy off."""

    def _store(self, tmp):
        cfg = load_example_config()
        cfg["paths"]["state_dir"] = str(Path(tmp) / "state")
        cfg["paths"]["tmp_dir"] = str(Path(tmp) / "tmp")
        cfg["paths"]["clips_local"] = str(Path(tmp) / "clips")
        return cfg, core.StateStore(cfg)

    def test_reconcile_clears_dead_upgrade_and_stays_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, store = self._store(tmp)
            store._write({"state": "on", "job_pid": None, "upgrade_pid": 424242})
            with mock.patch.object(core, "pid_alive", return_value=False):
                with store.locked():
                    st = core.reconcile_locked(store)
            self.assertEqual("on", st["state"])  # freeze baseline stays, NOT error
            self.assertIsNone(st.get("upgrade_pid"))  # phantom marker cleared

    def test_off_preempts_upgrade_without_debounce(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, store = self._store(tmp)
            # a live upgrade + a fresh completion timestamp that WOULD debounce
            store._write(
                {
                    "state": "on",
                    "job_pid": None,
                    "upgrade_pid": os.getpid(),
                    "last_completed_ts": time.time(),
                }
            )
            self.assertEqual({"noop": False}, core.preflight(cfg, "off"))

    def test_off_still_debounces_plain_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, store = self._store(tmp)
            cfg["api"]["debounce_seconds"] = 30
            store._write(
                {
                    "state": "on",
                    "job_pid": None,
                    "upgrade_pid": None,
                    "last_completed_ts": time.time(),
                }
            )
            with self.assertRaises(core.Busy):
                core.preflight(cfg, "off")

    def test_stage2_superseded_by_off_does_not_promote_or_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, store = self._store(tmp)
            os.makedirs(cfg["paths"]["clips_local"], exist_ok=True)
            # 'off' already claimed the transition out from under the upgrade
            store._write({"state": "restoring", "job_pid": 999, "session": "s1"})
            job = core.Job.__new__(core.Job)
            job.cfg, job.store, job.cancel, job.phase = (
                cfg,
                store,
                core.CancelToken(),
                "upgrading",
            )
            job.cfg["frigate"]["swap"] = "restart"  # exercise the restart seam
            job.soft_restart = mock.Mock()
            record = {
                "cam": {"clip_source": "restream", "rung": "live", "mode": "loop"}
            }
            with (
                mock.patch.object(
                    core,
                    "_start_upgrades",
                    return_value=(mock.Mock(), {"cam": mock.Mock()}),
                ),
                mock.patch.object(
                    core, "_collect_upgrades", return_value={"cam": {"clip": "x"}}
                ),
                mock.patch.object(core, "_promote_upgrades") as promote,
            ):
                core._run_loop_upgrade(
                    cfg,
                    job,
                    {"seconds": 300},
                    {"cam": {}},
                    {"cam": {}},
                    record,
                    {"cam": "src"},
                    "s1",
                )
            job.soft_restart.assert_not_called()  # never restarts once superseded
            promote.assert_not_called()  # never promotes
            self.assertEqual("restoring", store.read()["state"])  # off's state intact

    def test_budget_expiry_still_promotes_completed_loops(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, store = self._store(tmp)
            os.makedirs(cfg["paths"]["clips_local"], exist_ok=True)
            store._write(
                {
                    "state": "on",
                    "job_pid": None,
                    "upgrade_pid": os.getpid(),
                    "session": "s1",
                    "streams": {"cam": {"phase": "searching"}},
                }
            )
            job = core.Job.__new__(core.Job)
            job.cfg, job.store, job.cancel, job.phase = (
                cfg,
                store,
                core.CancelToken(),
                "upgrading",
            )
            job.cfg["frigate"]["swap"] = "restart"  # exercise the restart seam
            job.soft_restart = mock.Mock()
            record = {
                "cam": {"clip_source": "recordings", "rung": "loop", "mode": "loop"}
            }

            def fake_collect(futures, allowed, cancel, budget, budget_started):
                cancel.cancel()  # simulate the budget timer firing during collect
                return {"cam": {"clip": "x"}}

            with (
                mock.patch.object(
                    core,
                    "_start_upgrades",
                    return_value=(mock.Mock(), {"cam": mock.Mock()}),
                ),
                mock.patch.object(core, "_collect_upgrades", side_effect=fake_collect),
                mock.patch.object(
                    core, "_promote_upgrades", return_value=["cam"]
                ) as promote,
            ):
                core._run_loop_upgrade(
                    cfg,
                    job,
                    {"seconds": 300},
                    {"cam": {}},
                    {"cam": {}},
                    record,
                    {"cam": "src"},
                    "s1",
                )
            # budget expiry must NOT discard the loops that finished in time
            promote.assert_called_once()
            job.soft_restart.assert_called_once()
            self.assertEqual("on", store.read()["state"])

    def test_no_loop_found_clears_stale_upgrading_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, store = self._store(tmp)
            os.makedirs(cfg["paths"]["clips_local"], exist_ok=True)
            store._write(
                {
                    "state": "on",
                    "job_pid": None,
                    "upgrade_pid": os.getpid(),
                    "session": "s1",
                    "note": "restart=frigate-api; upgrading",
                    "streams": {"cam": {"phase": "searching"}},
                }
            )
            job = core.Job.__new__(core.Job)
            job.cfg, job.store, job.cancel, job.phase = (
                cfg,
                store,
                core.CancelToken(),
                "upgrading",
            )
            job.cfg["frigate"]["swap"] = "restart"  # exercise the restart seam
            job.soft_restart = mock.Mock()
            record = {
                "cam": {"clip_source": "restream", "rung": "live", "mode": "loop"}
            }
            with (
                mock.patch.object(
                    core,
                    "_start_upgrades",
                    return_value=(mock.Mock(), {"cam": mock.Mock()}),
                ),
                mock.patch.object(core, "_collect_upgrades", return_value={}),
            ):
                core._run_loop_upgrade(
                    cfg,
                    job,
                    {"seconds": 300},
                    {"cam": {}},
                    {"cam": {}},
                    record,
                    {"cam": "src"},
                    "s1",
                )
            s = store.read()
            self.assertEqual("on", s["state"])  # freeze baseline stands
            self.assertIsNone(s.get("upgrade_pid"))  # marker cleared
            self.assertNotIn("upgrading", s.get("note") or "")  # note no longer stale
            job.soft_restart.assert_not_called()

    def test_register_terminates_proc_registered_after_cancel(self):
        tok = core.CancelToken()
        tok.cancel()
        proc = mock.Mock()
        tok.register(proc)  # registered AFTER cancel -> must be killed at once
        proc.terminate.assert_called_once()

    def test_soft_restart_reports_staged_when_no_restart_observed(self):
        # A no-op /api/restart (frigate never goes down) must NOT be reported as
        # loops-live: go2rtc still holds the freeze inode. The path check alone
        # (verify_dejavu_applied) would falsely pass, so it must not be reached.
        job = core.Job.__new__(core.Job)
        job.cfg = {"frigate": {"health_timeout_seconds": 5}}
        job.client = mock.Mock()
        job.client.wait_down.return_value = False  # frigate NOT observed restarting
        job.client.wait_healthy.return_value = True
        with mock.patch.object(frigate, "verify_dejavu_applied") as verify:
            result = job.soft_restart({"cam": "cam.dejavu.mp4"})
        self.assertFalse(result)  # loops staged, not live
        verify.assert_not_called()  # never trust the tautological path check here
        job.client.restart_api.assert_called_once()

    def test_soft_restart_reports_live_when_restart_observed(self):
        job = core.Job.__new__(core.Job)
        job.cfg = {"frigate": {"health_timeout_seconds": 5}}
        job.client = mock.Mock()
        job.client.wait_down.return_value = True  # observed down -> go2rtc re-opened
        job.client.wait_healthy.return_value = True
        with mock.patch.object(
            frigate, "verify_dejavu_applied", return_value=(True, [])
        ):
            result = job.soft_restart({"cam": "cam.dejavu.mp4"})
        self.assertTrue(result)

    def test_busy_message_has_no_baked_in_prefix(self):
        # The CLI (dejavu.py) renders `busy: {exc}`; the exception message must
        # NOT carry its own 'busy:' prefix or it double-prefixes.
        with tempfile.TemporaryDirectory() as tmp:
            cfg, store = self._store(tmp)
            store._write({"state": "applying", "job_pid": os.getpid()})
            with self.assertRaises(core.Busy) as ctx:
                core.preflight(cfg, "off")
        self.assertNotIn("busy:", str(ctx.exception).lower())


class LoopSourcingTests(unittest.TestCase):
    """Loop sourcing: export fallback when /recordings is not readable, and
    freeze-frame lighting references carried for offline (non-black) rungs."""

    def _cfg(self):
        cfg = load_example_config()
        cfg["paths"]["recordings_dir"] = "/nonexistent-recordings"
        return cfg

    def test_no_mount_uses_export_api_after_settle(self):
        cfg = self._cfg()
        engaged = {"cam": {"ref": {"luma": 1}, "has_audio": False}}
        plan = {"cam": {}}
        with (
            mock.patch.object(
                core.recordings_mod, "files_retrieval_available", return_value=False
            ),
            mock.patch.object(
                core.recordings_mod, "wait_exports_ready", return_value=True
            ) as settle,
            mock.patch.object(core, "_make_upgrade_task", return_value=lambda: None),
        ):
            cancel = core.CancelToken()
            gate = core._ExportReadyGate(
                cfg["frigate"]["api_url"],
                cfg["frigate"]["settle_timeout_seconds"],
                cancel,
            )
            pool, futures = core._start_upgrades(
                cfg, {}, mock.Mock(), mock.Mock(), plan, engaged, cancel, gate
            )
        try:
            settle.assert_called_once()  # export path gated on the settle check
            self.assertEqual({"cam"}, set(futures))  # search STILL runs (exports)
        finally:
            if pool:
                pool.shutdown(wait=True)

    def test_no_mount_and_unsettled_exports_stays_on_freeze(self):
        cfg = self._cfg()
        engaged = {"cam": {"ref": {"luma": 1}, "has_audio": False}}
        with (
            mock.patch.object(
                core.recordings_mod, "files_retrieval_available", return_value=False
            ),
            mock.patch.object(
                core.recordings_mod, "wait_exports_ready", return_value=False
            ),
        ):
            cancel = core.CancelToken()
            gate = core._ExportReadyGate(
                cfg["frigate"]["api_url"],
                cfg["frigate"]["settle_timeout_seconds"],
                cancel,
            )
            pool, futures = core._start_upgrades(
                cfg, {}, mock.Mock(), mock.Mock(), {"cam": {}}, engaged, cancel, gate
            )
        self.assertIsNone(pool)
        self.assertEqual({}, futures)

    def test_export_gate_is_shared_and_capped_by_remaining_budget(self):
        cancel = core._AnyCancel()
        with (
            mock.patch.object(core.time, "monotonic", return_value=100),
            mock.patch.object(
                core.recordings_mod, "wait_exports_ready", return_value=True
            ) as wait,
        ):
            gate = core._ExportReadyGate("api", 30, cancel, deadline=103)
            self.assertTrue(gate.wait())
            self.assertTrue(gate.wait())

        wait.assert_called_once_with("api", 3, cancel)

    def test_readiness_poll_itself_does_not_overrun_its_timeout(self):
        with (
            mock.patch.object(
                recordings.time,
                "monotonic",
                side_effect=[100, 100.2, 100.8, 101.0],
            ),
            mock.patch.object(
                recordings, "exports_api_ready", return_value=False
            ) as ready,
            mock.patch.object(recordings.time, "sleep") as sleep,
        ):
            self.assertFalse(recordings.wait_exports_ready("api", 1))

        self.assertAlmostEqual(0.8, ready.call_args.kwargs["timeout"])
        self.assertAlmostEqual(0.2, sleep.call_args.args[0])

    def test_actual_export_fallback_checks_readiness_before_starting(self):
        ready = mock.Mock(return_value=False)
        progress = mock.Mock()
        win = {"start": 10, "end": 20, "duration": 10, "tier": 0}
        with (
            mock.patch.object(
                recordings, "files_retrieval_available", return_value=False
            ),
            mock.patch.object(recordings, "start_export") as start,
        ):
            with self.assertRaisesRegex(
                recordings.CaptureError, "did not become ready"
            ):
                recordings._fetch_window_clip(
                    "api",
                    "camera",
                    "stream",
                    win,
                    "/tmp",
                    None,
                    progress,
                    100,
                    export_ready=ready,
                )

        ready.assert_called_once_with()
        start.assert_not_called()

    def test_only_black_rung_is_skipped(self):
        cfg = load_example_config()
        engaged = {
            "live_cam": {"ref": {"luma": 1}, "has_audio": False},
            "offline_cam": {"ref": {"luma": 2}, "has_audio": False},  # rung 2/3
            "black_cam": {"ref": None, "has_audio": False},  # rung 4
        }
        plan = {s: {} for s in engaged}
        with (
            mock.patch.object(
                core.recordings_mod, "files_retrieval_available", return_value=True
            ),
            mock.patch.object(core.recordings_mod, "wait_exports_ready") as wait,
            mock.patch.object(core, "_make_upgrade_task", return_value=lambda: None),
        ):
            cancel = core.CancelToken()
            gate = core._ExportReadyGate(
                cfg["frigate"]["api_url"],
                cfg["frigate"]["settle_timeout_seconds"],
                cancel,
            )
            pool, futures = core._start_upgrades(
                cfg, {}, mock.Mock(), mock.Mock(), plan, engaged, cancel, gate
            )
        try:
            self.assertEqual({"live_cam", "offline_cam"}, set(futures))
            wait.assert_not_called()  # direct fast path is not delayed by exports
        finally:
            if pool:
                pool.shutdown(wait=True)


class ExportLifecycleTests(unittest.TestCase):
    @staticmethod
    def _response(status, body=None, text=""):
        response = mock.Mock(status_code=status, text=text)
        response.json.return_value = body
        return response

    def test_start_uses_export_id_from_response(self):
        response = self._response(200, {"success": True, "export_id": "front_abc123"})
        with mock.patch.object(recordings, "http_post", return_value=response) as post:
            export_id = recordings.start_export("http://frigate", "front", 10, 20)

        self.assertEqual("front_abc123", export_id)
        self.assertIn(
            "/api/export/front/start/10.000/end/20.000", post.call_args.args[0]
        )

    def test_start_rejects_response_without_export_id(self):
        response = self._response(200, {"success": True})
        with mock.patch.object(recordings, "http_post", return_value=response):
            with self.assertRaisesRegex(recordings.CaptureError, "export_id"):
                recordings.start_export("http://frigate", "front", 10, 20)

    def test_wait_polls_only_returned_export_id(self):
        exports = iter(
            [
                self._response(404, {"success": False}),
                self._response(200, {"id": "front_abc123", "in_progress": True}),
                self._response(200, {"id": "front_abc123", "in_progress": False}),
            ]
        )
        job = self._response(200, {"id": "front_abc123", "status": "running"})

        def get(url, **_kw):
            return job if "/api/jobs/export/" in url else next(exports)

        cancel = mock.Mock()
        with (
            mock.patch.object(recordings, "http_get", side_effect=get) as get_mock,
            mock.patch.object(recordings.time, "sleep"),
        ):
            entry = recordings.wait_export("http://frigate", "front_abc123", cancel)

        self.assertFalse(entry["in_progress"])
        urls = [call.args[0] for call in get_mock.call_args_list]
        self.assertEqual(
            ["http://frigate/api/exports/front_abc123"] * 3,
            [u for u in urls if "/jobs/" not in u],
        )
        self.assertEqual(
            ["http://frigate/api/jobs/export/front_abc123"] * 3,
            [u for u in urls if "/jobs/" in u],
        )

    def test_delete_uses_only_returned_export_id(self):
        with (
            mock.patch.object(
                recordings, "http_post", return_value=self._response(200)
            ) as post,
            mock.patch.object(recordings, "http_delete") as legacy,
        ):
            recordings.delete_export("http://frigate", "front_abc123")

        self.assertEqual("http://frigate/api/exports/delete", post.call_args.args[0])
        self.assertEqual({"ids": ["front_abc123"]}, post.call_args.kwargs["json"])
        legacy.assert_not_called()

    def test_delete_falls_back_to_the_legacy_route_on_0_17(self):
        with (
            mock.patch.object(
                recordings, "http_post", return_value=self._response(404)
            ),
            mock.patch.object(
                recordings, "http_delete", return_value=self._response(200)
            ) as legacy,
        ):
            recordings.delete_export("http://frigate", "front_abc123")

        self.assertEqual(
            "http://frigate/api/export/front_abc123", legacy.call_args.args[0]
        )


class RecordingWindowTests(unittest.TestCase):
    LOOP_META = {
        "video_codec": "h264",
        "audio_codec": None,
        "duration": 300,
        "width": 1920,
        "height": 1080,
        "fps": 15,
    }

    @staticmethod
    def _window(start=0):
        return {
            "start": start,
            "end": start + 300,
            "duration": 300,
            "full": True,
            "tier": recordings.TIER_QUIET,
            "motion_ps": 0,
            "activity_fraction": 0,
            "segments": [start, start + 10],
        }

    def _source_with(self, wins, files, fetch, pick=None):
        patches = {
            "files_retrieval_available": mock.Mock(return_value=files),
            "_resolve_candidates": mock.Mock(return_value=wins),
            "_reference": mock.Mock(return_value=None),
            "_fetch_window_clip": fetch,
            "_lighting_ok": mock.Mock(return_value=None),
            "frame_stats": mock.Mock(return_value={}),
            "frame_stats_tail": mock.Mock(return_value={}),
            "_stable_across": mock.Mock(return_value=None),
            "probe_file": mock.Mock(return_value={}),
            "summarize_probe": mock.Mock(return_value=self.LOOP_META),
            "apply_audio_policy": mock.Mock(return_value=False),
        }
        if pick is not None:
            patches["_pick_window"] = mock.Mock(return_value=pick)
        with (
            mock.patch.multiple(recordings, **patches),
            mock.patch.object(recordings.log, "warning"),
        ):
            return recordings.source_clip_from_recordings(
                "api",
                "camera",
                "stream",
                "/clips",
                "/tmp",
                300,
                {"min_seconds": 20, "audio": "silence"},
                None,
                lambda *_: None,
                recordings_dir="/recordings" if files else None,
                max_seconds=1200,
            )

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
                "camera",
                "stream",
                wins,
                None,
                {},
                None,
                now=1200,
                recordings_dir="/recordings",
                check_drift=True,
            )
        self.assertFalse(reasons)
        self.assertFalse(unavailable)
        self.assertEqual(10, picked["start"])
        self.assertEqual(5.0, picked["seam_delta"])

    def test_missing_direct_segments_fall_back_to_frigate_export(self):
        win = self._window()
        fetch = mock.Mock(return_value="export.mp4")
        result = self._source_with([win], True, fetch, (None, ["missing"], [win]))

        self.assertEqual("h264", result["clip_meta"]["video_codec"])
        self.assertIsNone(fetch.call_args.kwargs["recordings_dir"])

    def test_failed_export_advances_to_next_candidate(self):
        wins = [self._window(start) for start in (0, 600)]
        fetch = mock.Mock(
            side_effect=[recordings.CaptureError("first export failed"), "second.mp4"]
        )
        result = self._source_with(wins, False, fetch)

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
        cfg = load_example_config()
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


class LiveSwapTests(unittest.TestCase):
    def _job(self, swap="auto"):
        job = core.Job.__new__(core.Job)
        job.cfg = {"frigate": {"health_timeout_seconds": 5, "swap": swap}}
        job.client = mock.Mock()
        job.store = mock.Mock()
        job.store.read_streams_record.return_value = {"session": "test"}
        job.client.live_swap_available.return_value = (True, None)
        job.client.go2rtc_streams.return_value = {}
        job.client.camera_states.return_value = {"front_door": True, "garage": False}
        job.client.go2rtc_config_read.return_value = b"streams: {}\n"
        job.client.frigate_consumers.return_value = 1
        job.client.wait_healthy.return_value = True
        return job

    def test_push_fed_detection_covers_external_and_pull_producers(self):
        js = {
            "tablet": {
                "producers": [
                    {"url": "ffmpeg:tablet#video=h264"},
                    {"remote_addr": "192.0.2.1:1234", "protocol": "webrtc"},
                ]
            },
            "empty_url": {"producers": [{"url": ""}]},
            "no_url": {"producers": [{}]},
            "external": {"producers": [{"url": "external"}]},
            "external_source": {"producers": [{"source": "external"}]},
            "pull": {
                "producers": [{"url": "rtsp://cam", "remote_addr": "192.0.2.2:554"}]
            },
            "source_only": {"producers": [{"source": "ffmpeg:cam#video=h264"}]},
            "idle_pull": {"producers": [{"url": "rtsp://offline"}]},
            "idle": {"producers": None},
            "missing": None,
        }
        self.assertEqual(
            {"tablet", "empty_url", "no_url", "external", "external_source"},
            frigate.FrigateClient.push_fed_streams(js),
        )

    def test_any_planned_push_stream_restarts_the_whole_apply(self):
        for direction in ("on", "off"):
            with self.subTest(direction=direction):
                job = self._job()
                job.client.go2rtc_streams.return_value = {
                    "tablet": {"producers": [{"remote_addr": "192.0.2.1:1234"}]}
                }
                swaps = {
                    "front": {"sources": ["rtsp://cam"], "cameras": ["front_door"]},
                    "tablet": {"sources": ["ffmpeg:tablet"], "cameras": ["tablet"]},
                }
                with self.assertLogs("dejavu", level="INFO") as logs:
                    how = job.apply(swaps, {}, direction)
                self.assertEqual(core.NOTE_RESTART, how)
                self.assertIn("push-fed streams: tablet", "\n".join(logs.output))
                job.client.set_camera_enabled.assert_not_called()
                job.client.go2rtc_put_stream.assert_not_called()
                job.client.restart_api.assert_called_once_with()

    def test_push_stream_outside_plan_keeps_pull_stream_live(self):
        job = self._job()
        job.client.go2rtc_streams.return_value = {
            "tablet": {"producers": [{"url": "external"}]},
            "front": {"producers": [{"url": "rtsp://cam"}]},
        }
        swaps = {"front": {"sources": ["rtsp://cam"], "cameras": ["front_door"]}}
        with mock.patch.object(core.Job, "apply_live") as live:
            self.assertEqual(core.NOTE_LIVE, job.apply(swaps, {}, "off"))
        live.assert_called_once()
        job.client.restart_api.assert_not_called()

    def test_listing_failure_falls_back_before_camera_toggle(self):
        job = self._job()
        job.client.go2rtc_streams.side_effect = frigate.FrigateError("unreachable")
        swaps = {"front": {"sources": ["rtsp://cam"], "cameras": ["front_door"]}}
        self.assertEqual(core.NOTE_RESTART, job.apply(swaps, {}, "off"))
        job.client.set_camera_enabled.assert_not_called()
        job.client.go2rtc_put_stream.assert_not_called()
        job.client.restart_api.assert_called_once_with()

    def test_push_provenance_survives_until_restore_in_a_new_job(self):
        job = self._job()
        job.client.go2rtc_streams.return_value = {
            "tablet": {"producers": [{"url": "external"}]}
        }
        record = {
            "tablet": {"original_sources": ["ffmpeg:tablet"], "cameras": ["tablet"]}
        }
        swaps = {"tablet": {"sources": ["ffmpeg:/clip.mp4"], "cameras": ["tablet"]}}

        def restart(expect, direction):
            # Must be durable before the publisher disappears from the listing.
            saved = job.store.write_streams_record.call_args.args[0]
            self.assertTrue(saved["streams"]["tablet"]["push_fed"])
            self.assertEqual("test", saved["session"])

        with mock.patch.object(job, "restart_and_verify", side_effect=restart):
            self.assertEqual(core.NOTE_RESTART, job.apply(swaps, {}, "on", record))
        # Round-trip through JSON to model a separate CLI process restoring.
        saved_record = json.loads(
            json.dumps(job.store.write_streams_record.call_args.args[0])
        )
        restored = self._job()
        restored.client.go2rtc_streams.return_value = {
            "tablet": {"producers": [{"url": "ffmpeg:/clip.mp4"}]}
        }
        swaps["tablet"]["sources"] = ["ffmpeg:tablet"]
        self.assertEqual(
            core.NOTE_RESTART,
            restored.apply(swaps, {}, "off", saved_record["streams"]),
        )
        restored.client.set_camera_enabled.assert_not_called()
        restored.client.go2rtc_put_stream.assert_not_called()
        restored.client.restart_api.assert_called_once_with()

    def test_loop_upgrade_honors_current_or_remembered_push_stream(self):
        for remembered in (False, True):
            with self.subTest(remembered=remembered):
                job = self._job()
                job.client.go2rtc_streams.return_value = {
                    "tablet": {
                        "producers": [
                            {"url": "ffmpeg:/clip.mp4" if remembered else "external"}
                        ]
                    }
                }
                record = {
                    "tablet": {
                        "dejavu_source": "ffmpeg:/clip.mp4",
                        "cameras": ["tablet"],
                        "push_fed": remembered,
                    }
                }
                with mock.patch.object(
                    job, "soft_restart", return_value=True
                ) as restart:
                    self.assertEqual(
                        (True, core.NOTE_RESTART), job.apply_loops(record, ["tablet"])
                    )
                restart.assert_called_once_with({"tablet": "tablet.dejavu.mp4"})
                job.client.set_camera_enabled.assert_not_called()
                job.client.go2rtc_put_stream.assert_not_called()

    def test_pull_only_loop_upgrade_remains_live(self):
        job = self._job()
        record = {
            "front": {"dejavu_source": "ffmpeg:/clip.mp4", "cameras": ["front_door"]}
        }
        with mock.patch.object(job, "apply_live") as live:
            self.assertEqual((True, core.NOTE_LIVE), job.apply_loops(record, ["front"]))
        live.assert_called_once()
        job.client.restart_api.assert_not_called()

    def test_slow_reconnect_is_a_warning_not_a_restart(self):
        job = self._job()
        swaps = {"front": {"sources": ["ffmpeg:/a.mp4"], "cameras": ["front_door"]}}
        with mock.patch.object(core.Job, "_wait_consumers", return_value=["front"]):
            with mock.patch.object(
                frigate, "verify_dejavu_applied", return_value=(True, [])
            ):
                how = job.apply(swaps, {"front": "front.dejavu.mp4"}, "on")
        self.assertEqual(core.NOTE_LIVE, how)
        job.client.restart_api.assert_not_called()

    def test_streams_without_a_frigate_consumer_are_not_waited_for(self):
        job = self._job()
        job.client.camera_states.return_value = {"front_door": True, "dead_cam": True}
        job.client.frigate_consumers.side_effect = lambda s, js=None: {"front": 1}.get(
            s, 0
        )
        waits = []

        def wait(plan, want_zero, timeout):
            waits.append((sorted(plan), want_zero))
            return []

        swaps = {
            "front": {"sources": ["ffmpeg:/a.mp4"], "cameras": ["front_door"]},
            "dead": {"sources": ["ffmpeg:/b.mp4"], "cameras": ["dead_cam"]},
        }
        with mock.patch.object(core.Job, "_wait_consumers", side_effect=wait):
            with mock.patch.object(
                frigate, "verify_dejavu_applied", return_value=(True, [])
            ):
                how = job.apply(swaps, {}, "on")
        self.assertEqual(core.NOTE_LIVE, how)
        # the dead camera's stream is swapped but never gates the result
        self.assertEqual([(["front"], True), (["front"], False)], waits)
        self.assertEqual(2, job.client.go2rtc_put_stream.call_count)
        job.client.restart_api.assert_not_called()

    def test_placeholders_expand_from_env_like_frigate(self):
        with mock.patch.dict(os.environ, {"FRIGATE_PW": "s3cret"}):
            out, missing = frigate.expand_frigate_placeholders(
                "rtsp://u:{FRIGATE_PW}@cam/1#video=copy"
            )
        self.assertEqual(("rtsp://u:s3cret@cam/1#video=copy", []), (out, missing))
        out, missing = frigate.expand_frigate_placeholders(
            "rtsp://{FRIGATE_NOPE_X}@cam"
        )
        self.assertEqual("rtsp://{FRIGATE_NOPE_X}@cam", out)
        self.assertEqual(["FRIGATE_NOPE_X"], missing)

    def test_version_parse(self):
        self.assertEqual((0, 18, 0), frigate.parse_version("0.18.0-77a66e7"))
        self.assertEqual((0, 17, 2), frigate.parse_version("0.17.2"))
        self.assertIsNone(frigate.parse_version(None))

    def test_apply_live_stops_puts_starts_and_restores_go2rtc_file(self):
        job = self._job()
        calls = []
        job.client.set_camera_enabled.side_effect = lambda cam, on: calls.append(
            ("toggle", cam, on)
        )
        job.client.go2rtc_put_stream.side_effect = lambda name, srcs: calls.append(
            ("put", name, list(srcs))
        )
        job.client.go2rtc_config_write.side_effect = lambda data: calls.append(
            ("cfg", data)
        )
        swaps = {
            "front": {
                "sources": ["ffmpeg:/clips/front.dejavu.mp4#video=copy"],
                "cameras": ["front_door", "garage"],
            }
        }
        with mock.patch.object(core.Job, "_wait_consumers", return_value=[]):
            with mock.patch.object(
                frigate, "verify_dejavu_applied", return_value=(True, [])
            ):
                how = job.apply(swaps, {"front": "front.dejavu.mp4"}, "on")
        self.assertEqual(core.NOTE_LIVE, how)
        # garage was disabled at runtime by the operator and is left alone
        self.assertEqual(
            [
                ("toggle", "front_door", False),
                ("put", "front", ["ffmpeg:/clips/front.dejavu.mp4#video=copy"]),
                ("toggle", "front_door", True),
                ("cfg", b"streams: {}\n"),
            ],
            calls,
        )
        job.client.restart_api.assert_not_called()

    def test_live_failure_restarts_cameras_then_falls_back_to_restart(self):
        job = self._job()
        job.client.go2rtc_put_stream.side_effect = frigate.FrigateError("boom")
        swaps = {"front": {"sources": ["ffmpeg:/x.mp4"], "cameras": ["front_door"]}}
        with mock.patch.object(core.Job, "_wait_consumers", return_value=[]):
            how = job.apply(swaps, {}, "on")
        self.assertEqual(core.NOTE_RESTART, how)
        job.client.set_camera_enabled.assert_any_call("front_door", True)
        job.client.go2rtc_config_write.assert_called_once_with(b"streams: {}\n")
        job.client.restart_api.assert_called_once_with()

    def test_unresolved_placeholder_or_old_frigate_uses_restart(self):
        job = self._job()
        swaps = {
            "front": {
                "sources": ["rtsp://{FRIGATE_MISSING_X}@cam"],
                "cameras": ["front_door"],
            }
        }
        self.assertEqual(core.NOTE_RESTART, job.apply(swaps, {}, "off"))
        job.client.set_camera_enabled.assert_not_called()
        job.client.live_swap_available.assert_not_called()

        job = self._job()
        job.client.live_swap_available.return_value = (False, "frigate 0.17.2")
        swaps = {"front": {"sources": ["rtsp://cam"], "cameras": ["front_door"]}}
        self.assertEqual(core.NOTE_RESTART, job.apply(swaps, {}, "off"))
        job.client.set_camera_enabled.assert_not_called()
        job.client.restart_api.assert_called_once_with()

    def test_swap_restart_never_touches_the_bridge(self):
        job = self._job(swap="restart")
        swaps = {"front": {"sources": ["rtsp://cam"], "cameras": ["front_door"]}}
        self.assertEqual(core.NOTE_RESTART, job.apply(swaps, {}, "on"))
        job.client.live_swap_available.assert_not_called()
        job.client.restart_api.assert_called_once_with()

    def test_swap_option_is_validated(self):
        cfg = load_example_config()
        self.assertEqual("auto", cfg["frigate"]["swap"])
        cfg["frigate"]["swap"] = "sometimes"
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

    def test_frigate_consumers_count_only_frigates_ffmpeg(self):
        client = frigate.FrigateClient.__new__(frigate.FrigateClient)
        js = {
            "s": {
                "consumers": [
                    {"user_agent": "FFmpeg Frigate/0.18.0-77a66e7"},
                    {"user_agent": "Mozilla/5.0"},
                    {},
                ]
            }
        }
        self.assertEqual(1, client.frigate_consumers("s", js))
        self.assertEqual(0, client.frigate_consumers("other", js))


class ExportApiTests(unittest.TestCase):
    def test_start_export_accepts_the_0_18_queued_202(self):
        resp = mock.Mock(status_code=202)
        resp.json.return_value = {"export_id": "abc", "status": "queued"}
        with mock.patch.object(recordings, "http_post", return_value=resp):
            self.assertEqual(
                "abc", recordings.start_export("http://f", "cam", 1.0, 2.0)
            )

    def test_wait_export_fails_fast_on_a_failed_job(self):
        job = mock.Mock(status_code=200)
        job.json.return_value = {"status": "failed", "error_message": "no recordings"}
        with mock.patch.object(recordings, "http_get", return_value=job):
            with self.assertRaisesRegex(recordings.CaptureError, "no recordings"):
                recordings.wait_export("http://f", "abc", None)
