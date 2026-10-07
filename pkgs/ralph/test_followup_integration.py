import hashlib
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph import config, followup_runner, model, spend, stats
from ralph.jobs import JobStore


class FollowupIntegrationTests(unittest.TestCase):
    def test_background_assessment_reaches_stats_without_changing_review_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            origin.mkdir()

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(origin), *args], check=True,
                    capture_output=True, text=True,
                ).stdout.strip()

            git("init", "-q")
            source = origin / "sample.cpp"
            source.write_text("return value / divisor;\n")
            git("add", "sample.cpp")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-qm", "Original implementation")
            old_head = git("rev-parse", "HEAD")
            state = root / "state"
            state.mkdir()
            jobs = JobStore(state / "jobs.sqlite3")
            spend.Ledger(state / "spend.sqlite3", review_limit_usd=0.01)
            prompts = config.PromptConfig.load()
            finding = {
                "title": "Handle a zero divisor", "path": "sample.cpp",
                "line": 1, "side": "head", "body": "Reject a zero divisor.",
                "kind": "defect", "severity": "medium",
            }

            def complete(head, debug):
                jobs.enqueue(42, "master", head, "synchronize")
                job = jobs.claim()
                jobs.save_result(job, {
                    "base_sha": old_head, "head_sha": head,
                    "content": "Reject a zero divisor." if debug else "No issues.",
                    "debug": debug,
                })
                jobs.complete(job)
                return job

            old = complete(old_head, {
                "finding_attribution": [{"finding_id": "finding:1", **finding}],
                "decisions": [{"disposition": "publish", "finding": finding}],
            })
            followup_runner.run_once(state, str(origin), "key", prompts, 0.10)
            source.write_text("if (divisor == 0) return error;\nreturn value / divisor;\n")
            git("add", "sample.cpp")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-qm", "Reject zero before division")
            new_head = git("rev-parse", "HEAD")
            complete(new_head, {})
            protected = [state / "jobs.sqlite3", state / "spend.sqlite3"]
            before = [hashlib.sha256(path.read_bytes()).digest() for path in protected]
            answer = {"findings": [{
                "source_job_id": old["id"], "source_generation": old["generation"],
                "finding_id": "finding:1", "status": "addressed",
                "reason": "Zero is checked before dividing.",
                "evidence": "sample.cpp:1 adds a divisor == 0 guard.",
            }]}
            response = {
                "id": "followup-response", "model": "gpt-6-luna", "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 50,
                          "input_tokens_details": {"cached_tokens": 0}},
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps(answer)}]}],
            }
            with patch.object(model.urllib.request, "urlopen",
                              return_value=io.BytesIO(json.dumps(response).encode())) as send:
                self.assertEqual(followup_runner.run_once(
                    state, str(origin), "key", prompts, 0.10), 1)
                send.assert_called_once()
            self.assertEqual(before, [hashlib.sha256(path.read_bytes()).digest()
                                      for path in protected])
            with sqlite3.connect(state / "followup" / "spend.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
            summary = stats.collect_stats(state, root / "reports")
            addressed = summary["addressed_findings"]
            self.assertEqual(addressed["status_counts"]["addressed"], 1)
            self.assertEqual(addressed["findings"][0]["old_head"], old_head)
            self.assertEqual(addressed["findings"][0]["new_head"], new_head)
            with patch.object(model.urllib.request, "urlopen") as send:
                self.assertEqual(followup_runner.run_once(
                    state, str(origin), "key", prompts, 0.10), 0)
                send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
