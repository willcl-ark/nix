import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph import model, repository, usage
from ralph_service import followup, followup_runner, stats, stats_page
from ralph_service.jobs import JobStore


class FollowupIntegrationTests(unittest.TestCase):
    def test_history_never_crosses_repository_with_the_same_pr_number(self):
        core = {"id": 1, "repo_id": "core", "number": 42, "generation": 1}
        gui = {"id": 2, "repo_id": "gui", "number": 42, "generation": 1}
        current = {"id": 3, "repo_id": "gui", "number": 42, "generation": 2}
        self.assertEqual(
            followup_runner.history_before([core, gui, current], current), [gui]
        )

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

    def test_every_candidate_is_assessed_and_failed_batches_stay_separate(self):
        candidates = [
            {
                "source_job_id": 1,
                "source_generation": 1,
                "finding_id": f"finding:{index}",
                "title": "Title",
                "path": "sample.cpp",
                "source_head": "a" * 40,
            }
            for index in range(followup.BATCH_SIZE * 2 + 1)
        ]
        batches = []

        def model_input(_checkout, _previous, _current, batch, *_args, **_kwargs):
            batches.append(batch)
            return json.dumps(batch), {}

        def respond(_key, data, *_args, **_kwargs):
            if len(batches) == 2:
                raise OSError("unavailable")
            rows = [
                {
                    **{
                        field: item[field]
                        for field in (
                            "source_job_id",
                            "source_generation",
                            "finding_id",
                        )
                    },
                    "status": "unclear",
                    "reason": "Assessed.",
                    "evidence_ids": [],
                }
                for item in json.loads(data["input"][0]["content"])
            ]
            text = json.dumps({"findings": rows})
            return (
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    ],
                },
                b"",
                0,
            )

        with (
            patch.object(followup, "_candidate_findings", return_value=candidates),
            patch.object(followup, "_published_findings", return_value=[]),
            patch.object(followup, "_model_input", side_effect=model_input),
            patch.object(repository, "has_commit", return_value=True),
            patch.object(model, "request_response", side_effect=respond),
            patch.object(model, "response_record", return_value={}),
        ):
            result = followup.assess_completed_review(
                "key",
                Path("unused"),
                {"head": "b" * 40},
                [{"head": "a" * 40}],
                {},
                "prompt",
                "model",
                None,
            )
        self.assertEqual([len(batch) for batch in batches], [12, 12, 1])
        self.assertEqual(
            [item["finding_id"] for item in result["findings"]],
            [item["finding_id"] for item in candidates],
        )
        self.assertEqual(
            [item["evidence"] for item in result["findings"]],
            [""] * 12 + ["OSError"] * 12 + [""],
        )
        self.assertEqual(
            [batch.get("error_type") for batch in result["batches"]],
            [None, "OSError", None],
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
            usage.Ledger(state / "usage.sqlite3")
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

            def complete(head, debug, number=42):
                identity = {
                    "name": "current",
                    "code_sha256": None,
                    "config_sha256": None,
                    "config": None,
                }
                jobs.enqueue(
                    number, "master", head, "synchronize", engine_identity=identity
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
                jobs.release_attempt(job["id"], job["token"])
                return job

            old = complete(
                old_head,
                {
                    "finding_attribution": [{"finding_id": "finding:1", **finding}],
                    "decisions": [{"disposition": "publish", "finding": finding}],
                },
            )
            followup_runner.run_once(
                state, {"core": {"origin": str(origin)}}, "key", *prompts
            )
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
            other = complete(new_head, {}, number=43)
            followup_runner.save_assessment(
                state / "followup" / "assessments.sqlite3", other["id"], None
            )
            self.assertEqual(
                [
                    job["number"]
                    for job in followup_runner.list_completed_jobs(
                        state / "jobs.sqlite3"
                    )
                ],
                [42, 42, 43],
            )
            protected = [state / "jobs.sqlite3", state / "usage.sqlite3"]
            before = [hashlib.sha256(path.read_bytes()).digest() for path in protected]
            answer = {
                "findings": [
                    {
                        "source_job_id": old["id"],
                        "source_generation": old["generation"],
                        "finding_id": "finding:1",
                        "status": "addressed",
                        "reason": "Zero is checked before dividing.",
                        "evidence_ids": [],
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
            captured = {}

            def respond(_url, payload, *_args, **_kwargs):
                packet = json.loads(json.loads(payload)["input"][0]["content"])
                captured.update(packet)
                evidence = packet["findings"][0]["evidence"]
                answer["findings"][0]["evidence_ids"] = [
                    item["evidence_id"]
                    for item in evidence
                    if item["kind"] == "path_diff"
                ]
                response["output"][0]["content"][0]["text"] = json.dumps(answer)
                return response

            with (
                patch.object(
                    model.transport,
                    "request",
                    side_effect=respond,
                ) as send,
                patch.object(
                    followup_runner,
                    "_decode_result",
                    wraps=followup_runner._decode_result,
                ) as decode,
            ):
                self.assertEqual(
                    followup_runner.run_once(
                        state,
                        {"core": {"origin": str(origin)}},
                        "proxy-key",
                        *prompts,
                        api_base="http://127.0.0.1:8317/v1",
                    ),
                    1,
                )
                send.assert_called_once()
                self.assertEqual(decode.call_count, 2)
                self.assertEqual(
                    send.call_args.args[0], "http://127.0.0.1:8317/v1/responses"
                )
                self.assertEqual(send.call_args.args[2], "proxy-key")
                self.assertIn(finding["summary"], send.call_args.args[1].decode())
            self.assertEqual(
                before,
                [hashlib.sha256(path.read_bytes()).digest() for path in protected],
            )
            with sqlite3.connect(state / "followup" / "usage.sqlite3") as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1
                )
            summary = stats.collect_stats(state, root / "reports")
            addressed = summary["addressed_findings"]
            self.assertEqual(addressed["status_counts"]["addressed"], 1)
            self.assertEqual(addressed["findings"][0]["old_head"], old_head)
            self.assertEqual(addressed["findings"][0]["new_head"], new_head)
            saved = addressed["findings"][0]["source_evidence"][0]
            self.assertEqual(saved["old_head"], old_head)
            self.assertEqual(saved["new_head"], new_head)
            self.assertIn("+if (divisor == 0)", saved["text"])
            rendered = stats_page._addressed_table(
                addressed["findings"], "https://example.invalid/repo", ""
            )
            self.assertIn("+if (divisor == 0)", rendered)
            row = answer["findings"][0]
            candidate = {**captured["findings"][0], "source_head": old_head}
            registry = {item["evidence_id"]: item for item in candidate["evidence"]}
            old_id = next(
                key for key, value in registry.items() if value["kind"] == "old_source"
            )
            new_id = next(
                key for key, value in registry.items() if value["kind"] == "new_source"
            )
            for ids in (["invented"], [old_id], [new_id]):
                with self.subTest(ids=ids):
                    [rejected] = followup._parse_assessments(
                        json.dumps({"findings": [{**row, "evidence_ids": ids}]}),
                        [candidate],
                        old_head,
                        new_head,
                        registry,
                    )
                    self.assertEqual(rejected["status"], "unclear")
                    self.assertEqual(rejected["evidence"], "invalid_assessment")
                    self.assertNotIn("source_evidence", rejected)
            for bad_registry in (
                {},
                {
                    key: {**value, "finding_id": "another"}
                    for key, value in registry.items()
                },
                {
                    key: {**value, "new_head": old_head}
                    for key, value in registry.items()
                },
            ):
                [rejected] = followup._parse_assessments(
                    json.dumps(answer),
                    [candidate],
                    old_head,
                    new_head,
                    bad_registry,
                )
                self.assertEqual(rejected["status"], "unclear")
            siblings = [
                candidate,
                *({**candidate, "finding_id": name} for name in ("shared", "omitted")),
                {**candidate, "finding_id": "repeated"},
            ]
            repeated = {
                **row,
                "finding_id": "repeated",
                "status": "unclear",
                "evidence_ids": [],
            }
            rows = [row, {**row, "finding_id": "shared"}, repeated, repeated]
            self.assertEqual(
                [
                    item["status"]
                    for item in followup._parse_assessments(
                        json.dumps({"findings": rows}),
                        siblings,
                        old_head,
                        new_head,
                        registry,
                    )
                ],
                ["addressed", "unclear", "unclear", "unclear"],
            )

            with (
                patch.object(model.transport, "request") as send,
                patch.object(followup_runner, "completed_jobs") as load,
            ):
                self.assertEqual(
                    followup_runner.run_once(
                        state, {"core": {"origin": str(origin)}}, "key", *prompts
                    ),
                    0,
                )
                send.assert_not_called()
                load.assert_not_called()

            # A deletion is valid comparison evidence even without new source.
            source.unlink()
            git("add", "sample.cpp")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "Remove operation",
            )
            deleted_head = git("rev-parse", "HEAD")
            candidate["line"] = 1
            packet, deleted_registry = followup._model_input(
                origin, old, {"head": deleted_head}, [candidate], old_head, deleted_head
            )
            self.assertFalse(
                any(item["kind"] == "new_source" for item in deleted_registry.values())
            )
            deletion_id = next(
                key
                for key, item in deleted_registry.items()
                if item["kind"] == "path_diff"
            )
            result = followup._parse_assessments(
                json.dumps({"findings": [{**row, "evidence_ids": [deletion_id]}]}),
                [candidate],
                old_head,
                deleted_head,
                deleted_registry,
            )
            self.assertEqual(result[0]["status"], "addressed")
            self.assertIn(
                "new source is unavailable",
                json.loads(packet)["findings"][0]["gaps"][0],
            )


if __name__ == "__main__":
    unittest.main()
