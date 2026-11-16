import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph import model, protocol, repository
from ralph.evidence import ToolFailure


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.checkout = Path(directory.name)
        self.framework = "test/functional/test_framework/test_framework.py"
        self.caller = "test/functional/feature_pruning.py"
        self.call = "        self.sync_blocks([self.nodes[0], self.nodes[5]], wait=5, timeout=300)"
        repository.git(self.checkout, "init", "-q")
        for path, content in (
            (
                self.framework,
                "def sync_blocks(self, nodes=None, wait=1, timeout=60):\n    pass\n",
            ),
            (self.caller, "# fixture\n" * 470 + self.call + "\n"),
        ):
            target = self.checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        repository.git(self.checkout, "add", ".")
        self.commit()
        base = repository.git(self.checkout, "rev-parse", "HEAD").strip()
        (self.checkout / self.framework).write_text(
            "def sync_blocks(self, nodes=None, *, wait=0.05, timeout=60):\n    pass\n"
        )
        repository.git(self.checkout, "add", ".")
        self.commit()
        head = repository.git(self.checkout, "rev-parse", "HEAD").strip()
        self.snapshot = repository.snapshot_repository(self.checkout, base, head)
        self.evidence = self.citation("head", self.caller, 471, 471)
        self.finding = {
            "kind": "defect",
            "severity": "major",
            "path": self.framework,
            "line": 1,
            "side": "head",
            "title": "Positional caller breaks",
            "summary": "Existing positional arguments no longer match the helper.",
            "body": "The caller supplies wait and timeout positionally.",
            "security": {
                "classification": "non_security",
                "reason": "The issue changes how the test invokes the helper.",
            },
            "source_evidence": [self.evidence],
        }

    def commit(self):
        repository.git(
            self.checkout,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Snapshot",
        )

    def verify(self, finding):
        payload = {
            "coverage": {"status": "complete", "limitations": []},
            "decisions": [
                {
                    "candidate_ids": ["independent:1"],
                    "disposition": "supported",
                    "reason": "Checked caller",
                    "finding": finding,
                }
            ],
        }
        return protocol.verification(
            json.dumps(payload), [{"id": "independent:1"}], self.snapshot
        )

    def citation(self, side, path, line, end_line=None, **kwargs):
        output = repository.cite_file(
            self.snapshot, side, path, line, end_line, **kwargs
        )
        return {"citation_id": output.splitlines()[0].removeprefix("Citation: ")}

    def test_fabricated_or_prior_review_ids_and_model_excerpts_are_withheld(self):
        prior = repository.snapshot_repository(
            self.checkout, self.snapshot.base_sha, self.snapshot.head_sha
        )
        output = repository.cite_file(prior, "head", self.caller, 471, 471)
        prior_id = output.splitlines()[0].removeprefix("Citation: ")
        for evidence in (
            {"citation_id": "invented"},
            {"citation_id": prior_id},
            {**self.evidence, "excerpt": self.call},
        ):
            result, accepted = self.verify(
                {**self.finding, "source_evidence": [evidence]}
            )
            self.assertEqual(accepted, [])
            self.assertEqual(result["decisions"][0]["disposition"], "unresolved")

    def test_citations_resolve_pinned_source_and_do_not_share_mutable_records(self):
        (self.checkout / self.caller).write_text("overwritten working tree\n")
        repository.git(self.checkout, "checkout", "-q", self.snapshot.merge_base)
        for side in ("base", "head"):
            evidence = self.citation(side, self.caller, 471, 471)
            result, accepted = self.verify(
                {**self.finding, "source_evidence": [evidence]}
            )
            self.assertEqual(result["coverage"]["status"], "complete")
            source = accepted[0]["source_evidence"][0]
            self.assertEqual(source["excerpt"], self.call)
            self.assertEqual((source["line"], source["end_line"]), (471, 471))
            source["excerpt"] = "tampered"
            self.assertEqual(
                self.snapshot.citations[evidence["citation_id"]]["excerpt"], self.call
            )

    def test_failed_or_oversized_reads_issue_no_citation(self):
        before = dict(self.snapshot.citations)
        for path, start, end in (
            ("missing.cpp", 1, 1),
            (self.caller, 471, 472),
            (self.caller, 2, 1),
            (self.caller, 1, 151),
            (self.caller, True, 1),
        ):
            with (
                self.subTest(path=path, start=start, end=end),
                self.assertRaises(ToolFailure),
            ):
                self.citation("head", path, start, end)
        self.assertEqual(self.snapshot.citations, before)
        evidence = self.citation("head", self.caller, 1)
        self.assertEqual(
            self.snapshot.citations[evidence["citation_id"]]["end_line"], 150
        )

    def test_commit_citations_keep_revision_authorization(self):
        reference = self.citation(
            "commit",
            self.framework,
            1,
            1,
            commit=self.snapshot.head_sha,
            parent=self.snapshot.base_sha,
        )
        with self.assertRaises(protocol.InvalidReview):
            protocol._source_evidence([reference], self.snapshot)
        source = protocol._source_evidence(
            [reference], self.snapshot, allow_commit=True
        )[0]
        self.assertEqual(source["parent"], self.snapshot.base_sha)
        self.assertIn("wait=1", source["excerpt"])
        before = dict(self.snapshot.citations)
        with self.assertRaises(ToolFailure):
            self.citation("commit", self.framework, 1, 1, commit=self.snapshot.base_sha)
        self.assertEqual(self.snapshot.citations, before)

    def test_model_read_to_saved_evidence_preserves_escaping_and_blank_lines(self):
        source = r'LogDebug("Unknown inv type \"%s\"\n");' + "\n\n12: source text\n"
        (self.checkout / self.framework).write_text(source)
        repository.git(self.checkout, "add", ".")
        self.commit()
        self.snapshot = repository.snapshot_repository(
            self.checkout,
            self.snapshot.base_sha,
            repository.git(self.checkout, "rev-parse", "HEAD").strip(),
        )
        calls = []

        def request(_key, data, *_args, **_kwargs):
            calls.append(data)
            if len(calls) == 1:
                output = [
                    {
                        "type": "function_call",
                        "call_id": "read",
                        "name": "read_file",
                        "arguments": json.dumps(
                            {"path": self.framework, "start_line": 1, "end_line": 3}
                        ),
                    }
                ]
            else:
                tool_output = data["input"][-1]["output"]
                citation_id = tool_output.splitlines()[0].removeprefix("Citation: ")
                value = {
                    "coverage": {"status": "complete", "limitations": []},
                    "findings": [],
                    "requires_sensitive_review": False,
                    "test_coverage": [
                        {
                            "paths": [self.framework],
                            "status": "checked",
                            "evidence": [{"citation_id": citation_id}],
                            "regression": "Changed logging punctuation",
                            "reason": "Inspected source",
                        }
                    ],
                }
                output = [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(value)}],
                    }
                ]
            response = {"status": "completed", "output": output}
            return response, json.dumps(data).encode(), 0.01

        with patch.object(model, "request_response", side_effect=request):
            answer = model.openai_review(
                "key",
                "Review",
                self.snapshot,
                prompt="Review",
                model="gpt-6-luna",
                max_output_tokens=1000,
                tools=[tool for tool in model.TOOLS if tool["name"] == "read_file"],
            )
        result = protocol.tests_discovery(answer, self.snapshot, [self.framework])
        self.assertEqual(result["coverage"]["status"], "complete")
        saved = json.loads(json.dumps(result))["test_coverage"][0]["evidence"][0]
        self.assertEqual(saved["excerpt"], source.removesuffix("\n"))
        self.assertEqual(saved["path"], self.framework)
        self.assertEqual((saved["line"], saved["end_line"]), (1, 3))


if __name__ == "__main__":
    unittest.main()
