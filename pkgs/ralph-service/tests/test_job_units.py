import json
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ralph_service.jobs import JobStore
from ralph_service.job_units import JobUnits


class JobUnitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.jobs = JobStore(self.root / "jobs.sqlite3", global_limit=1)
        self.jobs.enqueue(
            42, "master", "a" * 40, "opened", engine_identity={"name": "test"}
        )
        with patch.dict(
            "os.environ",
            {"RALPH_JOB_EXECUTABLE": "/nix/store/old-package/bin/ralph-job"},
        ):
            self.units = JobUnits(
                self.jobs,
                self.root,
                {
                    "core": {
                        "repo_id": "core",
                        "forgejo_token_file": "/run/secrets/token",
                    }
                },
                Path("/run/secrets/api"),
            )

    def test_restart_preserves_live_attempt_and_capacity(self):
        claim = self.jobs.claim()
        with patch("ralph_service.job_units.unit_state", return_value="live"):
            self.units.reconcile()
        self.assertEqual(
            self.jobs.get_claim(claim["id"], claim["token"])["token"], claim["token"]
        )
        self.jobs.complete(claim)
        self.jobs.enqueue(
            43, "master", "b" * 40, "opened", engine_identity={"name": "test"}
        )
        self.assertIsNone(self.jobs.claim())
        with patch("ralph_service.job_units.unit_state", return_value="absent"):
            self.units.reconcile()
        self.assertEqual(self.jobs.claim()["number"], 43)

    def test_execution_lock_fences_a_delayed_start_from_recovery(self):
        claim = self.jobs.claim()
        directory = self.units.attempts / f"{claim['id']}-{claim['token']}"
        directory.mkdir()
        with (directory / "execution.lock").open("a") as worker_lock:
            fcntl.flock(worker_lock, fcntl.LOCK_EX)
            with patch("ralph_service.job_units.unit_state", return_value="absent"):
                self.units.reconcile()
            self.assertTrue(self.jobs.is_current(claim))
        with patch("ralph_service.job_units.unit_state", return_value="absent"):
            self.units.reconcile()
        self.assertFalse(self.jobs.is_current(claim))
        self.assertFalse(directory.exists())

    def test_uncertain_bus_never_reclaims(self):
        claim = self.jobs.claim()
        with patch(
            "ralph_service.job_units.unit_state",
            side_effect=RuntimeError("bus unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.units.reconcile()
        self.assertTrue(self.jobs.is_current(claim))

    def test_lost_start_acknowledgement_requires_stop_before_recovery(self):
        claim = self.jobs.claim()
        directory = self.units.attempts / f"{claim['id']}-{claim['token']}"
        directory.mkdir()
        (directory / "launch-pending").touch()
        with (
            patch("ralph_service.job_units.unit_state", return_value="absent"),
            patch(
                "ralph_service.job_units.subprocess.run",
                side_effect=TimeoutError("stop pending"),
            ),
        ):
            with self.assertRaises(TimeoutError):
                self.units.reconcile()
        self.assertTrue(self.jobs.is_current(claim))
        with (
            patch("ralph_service.job_units.unit_state", return_value="absent"),
            patch("ralph_service.job_units.subprocess.run") as run,
        ):
            run.return_value.returncode = 0
            self.units.reconcile()
            self.assertEqual(
                run.call_args.args[0][1:3],
                [
                    "stop",
                    claim.get(
                        "unit_name", f"ralph-job@{claim['id']}-{claim['token']}.service"
                    ),
                ],
            )
        self.assertFalse(self.jobs.is_current(claim))
        self.assertFalse(directory.exists())

    def test_manifest_pins_configuration_and_survives_uncertain_launch(self):
        claim = self.jobs.claim()

        def run(arguments, **kwargs):
            if arguments[0] == "systemctl":
                raise TimeoutError("lost launch acknowledgement")

        with patch("ralph_service.job_units.subprocess.run", side_effect=run):
            with self.assertRaises(TimeoutError):
                self.units.start(claim)
        self.assertTrue(self.jobs.is_current(claim))
        manifest = json.loads(
            next(self.root.glob("attempts/*/manifest.json")).read_text()
        )
        self.assertEqual(manifest["executable"], "/nix/store/old-package/bin/ralph-job")
        self.assertEqual(manifest["entry"]["forgejo_token_file"], "/run/secrets/token")
        self.assertEqual(manifest["token"], claim["token"])
        with patch("ralph_service.job_units.unit_state", return_value="live"):
            self.units.reconcile()
        self.assertTrue(self.jobs.is_current(claim))
