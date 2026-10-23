import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ralph_service import config, followup_runner

from ralph_service.jobs import JobStore


class FollowupRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name)
        self.jobs = JobStore(self.state_dir / "jobs.sqlite3")
        self.prompt_config = ("Assess addressed findings.", "gpt-6-luna")

    def enqueue_complete(self, number, head, action="synchronize", result=None,
                         force=False):
        identity = {"name": "current", "code_sha256": None,
                    "config_sha256": None, "config": None}
        self.jobs.enqueue(number, "master", head, action, force=force,
                          engine_identity=identity)
        job = self.jobs.claim()
        self.assertTrue(self.jobs.save_result(job, result or {
            "schema_version": 1,
            "base_sha": "b" * 40,
            "head_sha": head,
            "content": "Review body",
            "engine": identity,
            "output": {"schema_version": 1, "data": {}, "exports": {"findings": []}},
            "run": {"status": "completed", "metrics": {}, "attempt": {}},
        }))
        self.assertTrue(self.jobs.complete(job))
        return job

    def assessment_rows(self):
        path = self.state_dir / "followup" / "assessments.sqlite3"
        with sqlite3.connect(path) as db:
            return db.execute(
                "SELECT job_id, result FROM assessments ORDER BY job_id"
            ).fetchall()

    def test_first_run_seeds_existing_completed_jobs_without_assessing(self):
        old = self.enqueue_complete(42, "a" * 40)
        with patch.object(followup_runner, "assess_job") as assess:
            processed = followup_runner.run_once(
                self.state_dir, "https://example.invalid/repo.git", "key",
                *self.prompt_config, 0.10)

        self.assertEqual(processed, 0)
        self.assertEqual(self.assessment_rows(), [(old["id"], None)])
        assess.assert_not_called()

    def test_empty_first_run_does_not_seed_later_completed_jobs(self):
        followup_runner.run_once(self.state_dir, "origin", "key",
                                 *self.prompt_config, 0.10)
        new = self.enqueue_complete(42, "b" * 40)
        result = {"status": "skipped", "reason": "no_previous_review",
                  "findings": []}

        with patch.object(followup_runner, "assess_job",
                          return_value=result) as assess:
            followup_runner.run_once(self.state_dir, "origin", "key",
                                     *self.prompt_config, 0.10)

        assess.assert_called_once()
        self.assertEqual(self.assessment_rows(), [
            (new["id"], json.dumps(result, sort_keys=True)),
        ])

    def test_subsequent_run_assesses_only_new_completed_synchronizations(self):
        old = self.enqueue_complete(42, "a" * 40)
        followup_runner.run_once(self.state_dir, "origin", "key",
                                 *self.prompt_config, 0.10)
        sync = self.enqueue_complete(42, "c" * 40, "synchronize")
        opened = self.enqueue_complete(43, "d" * 40, "opened")
        result = {"status": "complete", "findings": []}

        with patch.object(followup_runner, "assess_job",
                          return_value=result) as assess:
            processed = followup_runner.run_once(
                self.state_dir, "origin", "key", *self.prompt_config, 0.10,
                batch_size=5)

        self.assertEqual(processed, 2)
        assess.assert_called_once()
        self.assertEqual(assess.call_args.args[7]["id"], sync["id"])
        self.assertEqual(self.assessment_rows(), [
            (old["id"], None),
            (sync["id"], json.dumps(result, sort_keys=True)),
            (opened["id"], None),
        ])

    def test_jobs_database_is_opened_readonly(self):
        self.enqueue_complete(42, "a" * 40)
        seen = []
        real_connect = sqlite3.connect

        def connect(database, *args, **kwargs):
            seen.append((database, kwargs.get("uri")))
            return real_connect(database, *args, **kwargs)

        with patch.object(followup_runner.sqlite3, "connect", side_effect=connect):
            followup_runner.run_once(self.state_dir, "origin", "key",
                                     *self.prompt_config, 0.10)

        jobs_path = str(self.state_dir / "jobs.sqlite3")
        self.assertIn((f"file:{jobs_path}?mode=ro", True), seen)

    def test_assessment_connection_is_closed_before_classifier_runs(self):
        self.enqueue_complete(42, "a" * 40)
        followup_runner.run_once(self.state_dir, "origin", "key",
                                 *self.prompt_config, 0.10)
        new = self.enqueue_complete(42, "b" * 40)
        checked = Mock()

        def assess(*args):
            with sqlite3.connect(self.state_dir / "followup" / "assessments.sqlite3") as db:
                checked(db.execute(
                    "SELECT result FROM assessments WHERE job_id = ?",
                    (new["id"],)).fetchone())
            return {"status": "complete", "findings": []}

        with patch.object(followup_runner, "assess_job", side_effect=assess):
            followup_runner.run_once(self.state_dir, "origin", "key",
                                     *self.prompt_config, 0.10)

        checked.assert_called_once_with(None)

    def test_persisted_assessment_is_not_repaid_or_duplicated(self):
        self.enqueue_complete(42, "a" * 40)
        followup_runner.run_once(self.state_dir, "origin", "key",
                                 *self.prompt_config, 0.10)
        self.enqueue_complete(42, "b" * 40)
        with patch.object(followup_runner, "assess_job",
                          return_value={"status": "complete", "findings": []}):
            followup_runner.run_once(self.state_dir, "origin", "key",
                                     *self.prompt_config, 0.10)

        with patch.object(followup_runner, "assess_job") as assess:
            followup_runner.run_once(self.state_dir, "origin", "key",
                                     *self.prompt_config, 0.10)

        assess.assert_not_called()

    def test_non_synchronization_jobs_do_not_consume_batch_slots(self):
        old = self.enqueue_complete(42, "a" * 40)
        followup_runner.run_once(self.state_dir, "origin", "key",
                                 *self.prompt_config, 0.10)
        opened = self.enqueue_complete(43, "b" * 40, "opened")
        sync = self.enqueue_complete(42, "c" * 40, "synchronize")
        result = {"status": "complete", "findings": []}

        with patch.object(followup_runner, "assess_job",
                          return_value=result) as assess:
            processed = followup_runner.run_once(
                self.state_dir, "origin", "key", *self.prompt_config, 0.10,
                batch_size=1)

        self.assertEqual(processed, 2)
        assess.assert_called_once()
        self.assertEqual(self.assessment_rows(), [
            (old["id"], None),
            (opened["id"], None),
            (sync["id"], json.dumps(result, sort_keys=True)),
        ])

    def test_same_head_skip_does_not_fetch(self):
        self.enqueue_complete(42, "a" * 40)
        followup_runner.run_once(self.state_dir, "origin", "key",
                                 *self.prompt_config, 0.10)
        self.enqueue_complete(42, "a" * 40, force=True)

        with patch.object(followup_runner, "fetch_heads") as fetch:
            followup_runner.run_once(self.state_dir, "origin", "key",
                                     *self.prompt_config, 0.10)

        fetch.assert_not_called()

    def test_unexpected_failure_is_recorded_and_does_not_starve_batch(self):
        self.enqueue_complete(42, "a" * 40)
        followup_runner.run_once(self.state_dir, "origin", "key",
                                 *self.prompt_config, 0.10)
        failed = self.enqueue_complete(42, "b" * 40)
        with patch.object(followup_runner, "assess_job",
                          side_effect=TimeoutError("down")):
            processed = followup_runner.run_once(
                self.state_dir, "origin", "key", *self.prompt_config, 0.10)

        self.assertEqual(processed, 1)
        rows = self.assessment_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], failed["id"])
        self.assertEqual(json.loads(rows[1][1])["error_type"], "TimeoutError")

    def test_command_loads_its_own_prompt_and_model(self):
        secret = self.state_dir / "model-key"
        secret.write_text("test-key\n", encoding="utf-8")
        prompt = self.state_dir / "followup.md"
        prompt.write_text("Follow-up instructions.\n", encoding="utf-8")
        with patch.object(followup_runner, "run_once", return_value=0) as run:
            result = followup_runner.main([
                "--state-dir", str(self.state_dir), "--origin", "local-origin",
                "--openai-key-file", str(secret), "--prompt-file", str(prompt),
                "--model", "gpt-6-luna"])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.args[3:5],
                         ("Follow-up instructions.", "gpt-6-luna"))

    def test_default_settings_need_no_main_review_prompts(self):
        prompt, model = followup_runner.load_settings()
        self.assertTrue(prompt.strip())
        self.assertEqual(model, "gpt-6-luna")


if __name__ == "__main__":
    unittest.main()
