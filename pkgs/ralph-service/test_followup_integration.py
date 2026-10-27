import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph import model, repository, spend
from ralph_service import followup_runner, stats
from ralph_service.jobs import JobStore


class FollowupIntegrationTests(unittest.TestCase):
    def test_fetch_heads_hydrates_existing_filtered_checkout_for_offline_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            origin.mkdir()

            def git(checkout, *args):
                return subprocess.run(
                    ["git", "-C", str(checkout), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=repository.git_environment(),
                ).stdout.strip()

            git(origin, "init", "-q")
            git(origin, "config", "uploadpack.allowFilter", "true")
            source = origin / "sample.cpp"
            source.write_text("return value / divisor;\n")
            git(origin, "add", "sample.cpp")
            commit = (
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
            )
            git(origin, *commit, "Original implementation")
            old_head = git(origin, "rev-parse", "HEAD")
            source.write_text(
                "if (divisor == 0) return error;\nreturn value / divisor;\n"
            )
            git(origin, "add", "sample.cpp")
            git(origin, *commit, "Reject zero before division")
            new_head = git(origin, "rev-parse", "HEAD")
            checkout = root / "checkout"
            followup_runner.prepare_checkout(checkout, origin.as_uri())
            git(checkout, "fetch", "--filter=blob:none", "origin", old_head)
            self.assertIn(
                "?", git(checkout, "rev-list", "--objects", "--missing=print", old_head)
            )

            followup_runner.fetch_heads(
                checkout, origin.as_uri(), 1, old_head, new_head
            )
            missing = git(
                checkout, "rev-list", "--objects", "--missing=print", old_head, new_head
            )
            self.assertFalse(any(line.startswith("?") for line in missing.splitlines()))
            origin.rename(root / "unavailable-origin")
            self.assertIn(
                "+if (divisor == 0)",
                repository.diff_between_heads(checkout, old_head, new_head),
            )
            self.assertIn(
                "return value / divisor;",
                repository.file_excerpt_at(checkout, old_head, "sample.cpp", 1),
            )
            self.assertIn(
                "if (divisor == 0)",
                repository.file_excerpt_at(checkout, new_head, "sample.cpp", 1),
            )

    def test_background_assessment_reaches_stats_without_changing_review_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            origin.mkdir()

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(origin), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            git("init", "-q")
            source = origin / "sample.cpp"
            source.write_text("return value / divisor;\n")
            git("add", "sample.cpp")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "Original implementation",
            )
            old_head = git("rev-parse", "HEAD")
            state = root / "state"
            state.mkdir()
            jobs = JobStore(state / "jobs.sqlite3")
            spend.Ledger(state / "spend.sqlite3", review_limit_usd=0.01)
            prompts = followup_runner.load_settings()
            finding = {
                "title": "Handle a zero divisor",
                "path": "sample.cpp",
                "line": 1,
                "side": "head",
                "summary": "A zero divisor crashes the operation.",
                "body": "Reject a zero divisor.",
                "kind": "defect",
                "severity": "medium",
            }

            def complete(head, debug):
                identity = {
                    "name": "current",
                    "code_sha256": None,
                    "config_sha256": None,
                    "config": None,
                }
                jobs.enqueue(
                    42, "master", head, "synchronize", engine_identity=identity
                )
                job = jobs.claim()
                exports = []
                for attribution in debug.get("finding_attribution", []):
                    finding = next(
                        (
                            decision["finding"]
                            for decision in debug.get("decisions", [])
                            if decision.get("disposition") == "publish"
                        ),
                        {},
                    )
                    exports.append(
                        {
                            "id": attribution["finding_id"],
                            **finding,
                            "presented_title": attribution.get(
                                "title", finding.get("title")
                            ),
                            "presented_body": finding.get("body", ""),
                            "presented_summary": finding.get("summary", ""),
                        }
                    )
                jobs.save_result(
                    job,
                    {
                        "schema_version": 1,
                        "base_sha": old_head,
                        "head_sha": head,
                        "content": "Reject a zero divisor." if debug else "No issues.",
                        "engine": identity,
                        "output": {
                            "schema_version": 2,
                            "data": {"schema_version": 2, **debug},
                            "exports": {"findings": exports},
                        },
                        "run": {
                            "status": "completed",
                            "metrics": {},
                            "attempt": {},
                            "skipped": False,
                        },
                    },
                )
                jobs.complete(job)
                return job

            old = complete(
                old_head,
                {
                    "finding_attribution": [{"finding_id": "finding:1", **finding}],
                    "decisions": [{"disposition": "publish", "finding": finding}],
                },
            )
            followup_runner.run_once(state, str(origin), "key", *prompts, 0.10)
            source.write_text(
                "if (divisor == 0) return error;\nreturn value / divisor;\n"
            )
            git("add", "sample.cpp")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "Reject zero before division",
            )
            new_head = git("rev-parse", "HEAD")
            complete(new_head, {})
            protected = [state / "jobs.sqlite3", state / "spend.sqlite3"]
            before = [hashlib.sha256(path.read_bytes()).digest() for path in protected]
            answer = {
                "findings": [
                    {
                        "source_job_id": old["id"],
                        "source_generation": old["generation"],
                        "finding_id": "finding:1",
                        "status": "addressed",
                        "reason": "Zero is checked before dividing.",
                        "evidence": "sample.cpp:1 adds a divisor == 0 guard.",
                    }
                ]
            }
            response = {
                "id": "followup-response",
                "model": "gpt-6-luna",
                "status": "completed",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "input_tokens_details": {"cached_tokens": 0},
                },
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(answer)}
                        ],
                    }
                ],
            }
            with patch.object(
                model.transport,
                "request",
                return_value=response,
            ) as send:
                self.assertEqual(
                    followup_runner.run_once(
                        state,
                        str(origin),
                        "proxy-key",
                        *prompts,
                        0.10,
                        api_base="http://127.0.0.1:8317/v1",
                    ),
                    1,
                )
                send.assert_called_once()
                self.assertEqual(
                    send.call_args.args[0], "http://127.0.0.1:8317/v1/responses"
                )
                self.assertEqual(send.call_args.args[2], "proxy-key")
                self.assertIn(finding["summary"], send.call_args.args[1].decode())
            self.assertEqual(
                before,
                [hashlib.sha256(path.read_bytes()).digest() for path in protected],
            )
            with sqlite3.connect(state / "followup" / "spend.sqlite3") as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1
                )
            summary = stats.collect_stats(state, root / "reports")
            addressed = summary["addressed_findings"]
            self.assertEqual(addressed["status_counts"]["addressed"], 1)
            self.assertEqual(addressed["findings"][0]["old_head"], old_head)
            self.assertEqual(addressed["findings"][0]["new_head"], new_head)
            with patch.object(model.transport, "request") as send:
                self.assertEqual(
                    followup_runner.run_once(state, str(origin), "key", *prompts, 0.10),
                    0,
                )
                send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
