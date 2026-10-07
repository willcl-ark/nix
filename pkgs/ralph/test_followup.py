import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ralph import config, followup, model
from ralph.spend import BudgetExceeded


SOURCE_HEAD = "0" * 40
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40


def response(text):
    return {
        "id": "resp",
        "model": "gpt-6-luna",
        "status": "completed",
        "usage": {},
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": text}]}],
    }


def completed_job(job_id, generation, head, debug, content="Review."):
    return {
        "id": job_id,
        "number": 42,
        "generation": generation,
        "head": head,
        "review_result": {
            "head_sha": head,
            "content": content,
            "debug": debug,
        },
    }


def finding_debug(title="Old point", path="src/a.cpp", body="Fix it."):
    return {
        "finding_attribution": [{
            "finding_id": "finding:1", "title": title,
            "path": path, "line": 2, "side": "head"}],
        "decisions": [{"disposition": "publish", "finding": {
            "title": title, "path": path, "line": 2, "body": body}}],
    }


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.prompt_config = config.PromptConfig("instructions", {
            "addressed_findings": "Assess addressed findings.",
        }, {name: "gpt-6-luna" for name in config.MODEL_NAMES})
        self.current = completed_job(8, 4, NEW_HEAD, {})
        self.previous = completed_job(7, 3, OLD_HEAD, finding_debug())
        self.budget = Mock()

    def assess(self, history=None, prior=None):
        return followup.assess_completed_review(
            "api-key", Path("/checkout"), self.current,
            [self.previous] if history is None else history,
            prior or {}, self.prompt_config, self.budget)

    def test_no_previous_review_skips_without_model_call(self):
        with patch.object(followup.model, "request_response") as request:
            result = self.assess(history=[])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_previous_review")
        request.assert_not_called()

    def test_same_head_review_skips_without_addressed_inference(self):
        current = completed_job(8, 4, OLD_HEAD, {})
        result = followup.assess_completed_review(
            "api-key", Path("/checkout"), current, [self.previous], {},
            self.prompt_config, self.budget)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "same_head")

    def test_missing_old_ref_records_unclear_for_previous_findings(self):
        with patch.object(followup.repository, "has_commit", return_value=False):
            result = self.assess()
        self.assertEqual(result["reason"], "missing_git_object")
        self.assertEqual(result["findings"][0]["status"], "unclear")
        self.assertEqual(result["findings"][0]["old_head"], OLD_HEAD)
        self.assertEqual(result["findings"][0]["new_head"], NEW_HEAD)
        self.assertEqual(result["findings"][0]["assessed_from_head"], OLD_HEAD)

    def test_invalid_or_failed_classifier_marks_candidates_unclear(self):
        empty_evidence = response(json.dumps({"findings": [{
            "source_job_id": 7, "source_generation": 3,
            "finding_id": "finding:1", "status": "addressed",
            "reason": "Fixed", "evidence": ""}]}))
        for failure in (response("{}"), empty_evidence, BudgetExceeded("limit"),
                        TimeoutError("down")):
            with self.subTest(failure=type(failure).__name__):
                if isinstance(failure, dict):
                    side_effect = None
                    return_value = (failure, b"{}", 0.1)
                else:
                    side_effect = failure
                    return_value = None
                with patch.object(followup.repository, "has_commit", return_value=True), \
                        patch.object(followup.repository, "diff_between_heads",
                                     return_value="diff"), \
                        patch.object(followup.repository, "diff_path_between_heads",
                                     return_value="path diff"), \
                        patch.object(followup.repository, "file_excerpt_at",
                                     return_value="excerpt"), \
                        patch.object(followup.model, "request_response",
                                     side_effect=side_effect,
                                     return_value=return_value):
                    result = self.assess()
                self.assertEqual(result["findings"][0]["status"], "unclear")

    def test_stale_review_propagates(self):
        with patch.object(followup.repository, "has_commit", return_value=True), \
                patch.object(followup.repository, "diff_between_heads", return_value="diff"), \
                patch.object(followup.repository, "diff_path_between_heads",
                             return_value="path diff"), \
                patch.object(followup.repository, "file_excerpt_at", return_value="excerpt"), \
                patch.object(followup.model, "request_response",
                             side_effect=model.StaleReview("new head")):
            with self.assertRaises(model.StaleReview):
                self.assess()

    def test_carries_unresolved_sources_and_deduplicates_republished_finding(self):
        source = completed_job(1, 1, SOURCE_HEAD, finding_debug())
        previous = completed_job(7, 3, OLD_HEAD, {
            "finding_attribution": [
                {"finding_id": "finding:1", "title": "Old point",
                 "path": "src/a.cpp", "line": 5, "side": "head"},
                {"finding_id": "finding:2", "title": "New point",
                 "path": "src/b.cpp", "line": 9, "side": "head"},
            ],
            "decisions": [
                {"disposition": "publish", "finding": {
                    "title": "Old point", "path": "src/a.cpp",
                    "line": 5, "body": "Fix it."}},
                {"disposition": "publish", "finding": {
                    "title": "New point", "path": "src/b.cpp",
                    "line": 9, "body": "Tighten the test."}},
            ],
        })
        prior = {7: {"findings": [
            {"source_job_id": 1, "source_generation": 1,
             "finding_id": "finding:1", "title": "Old point",
             "path": "src/a.cpp", "old_head": SOURCE_HEAD,
             "new_head": OLD_HEAD, "status": "still_present",
             "reason": "Still there", "evidence": "same branch"},
            {"source_job_id": 2, "source_generation": 2,
             "finding_id": "finding:1", "title": "Fixed point",
             "path": "src/fixed.cpp", "old_head": "1" * 40,
             "new_head": OLD_HEAD, "status": "addressed",
             "reason": "Fixed", "evidence": "removed"},
        ]}}
        assessments = {"findings": [
            {"source_job_id": 1, "source_generation": 1,
             "finding_id": "finding:1", "status": "partially_addressed",
             "reason": "Half fixed", "evidence": "new guard added"},
            {"source_job_id": 7, "source_generation": 3,
             "finding_id": "finding:2", "status": "addressed",
             "reason": "Fixed", "evidence": "test now asserts it"},
        ]}
        with patch.object(followup.repository, "has_commit", return_value=True), \
                patch.object(followup.repository, "diff_between_heads", return_value="diff"), \
                patch.object(followup.repository, "diff_path_between_heads",
                             return_value="path diff"), \
                patch.object(followup.repository, "file_excerpt_at", return_value="excerpt"), \
                patch.object(followup.model, "request_response",
                             return_value=(response(json.dumps(assessments)), b"{}", 0.1)):
            result = followup.assess_completed_review(
                "api-key", Path("/checkout"), self.current,
                [previous, source], prior, self.prompt_config, self.budget)
        rows = [(item["source_job_id"], item["source_generation"],
                 item["finding_id"], item["old_head"], item["status"])
                for item in result["findings"]]
        self.assertEqual(rows, [
            (1, 1, "finding:1", SOURCE_HEAD, "partially_addressed"),
            (7, 3, "finding:2", OLD_HEAD, "addressed"),
        ])

    def test_skipped_assessment_does_not_break_unresolved_carry_chain(self):
        source = completed_job(1, 1, SOURCE_HEAD, finding_debug())
        partial = completed_job(2, 2, OLD_HEAD, {})
        forced_same_head = completed_job(3, 3, OLD_HEAD, {})
        prior = {
            2: {"findings": [{
                "source_job_id": 1, "source_generation": 1,
                "finding_id": "finding:1", "title": "Old point",
                "path": "src/a.cpp", "old_head": SOURCE_HEAD,
                "new_head": OLD_HEAD, "status": "partially_addressed",
                "reason": "Only part changed", "evidence": "one branch remains"}]},
            3: {"status": "skipped", "reason": "same_head", "findings": []},
        }
        assessments = {"findings": [{
            "source_job_id": 1, "source_generation": 1,
            "finding_id": "finding:1", "status": "still_present",
            "reason": "Still present", "evidence": "same guard"}]}
        with patch.object(followup.repository, "has_commit", return_value=True), \
                patch.object(followup.repository, "diff_between_heads", return_value="diff"), \
                patch.object(followup.repository, "diff_path_between_heads",
                             return_value="path diff"), \
                patch.object(followup.repository, "file_excerpt_at", return_value="excerpt"), \
                patch.object(followup.model, "request_response",
                             return_value=(response(json.dumps(assessments)), b"{}", 0.1)):
            result = followup.assess_completed_review(
                "api-key", Path("/checkout"), self.current,
                [forced_same_head, partial, source], prior,
                self.prompt_config, self.budget)
        self.assertEqual(result["findings"][0]["source_job_id"], 1)
        self.assertEqual(result["findings"][0]["old_head"], SOURCE_HEAD)

    def test_addressed_fingerprint_is_not_reintroduced_by_later_review(self):
        source = completed_job(1, 1, SOURCE_HEAD, finding_debug())
        repeated = completed_job(2, 2, OLD_HEAD, finding_debug())
        prior = {1: {"findings": [{
            "source_job_id": 1, "source_generation": 1,
            "finding_id": "finding:1", "title": "Old point",
            "path": "src/a.cpp", "old_head": SOURCE_HEAD,
            "new_head": OLD_HEAD, "status": "addressed",
            "reason": "Fixed", "evidence": "removed"}]}}
        with patch.object(followup.model, "request_response") as request:
            result = followup.assess_completed_review(
                "api-key", Path("/checkout"), self.current, [repeated, source],
                prior, self.prompt_config, self.budget)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_previous_findings")
        request.assert_not_called()

    def test_first_assessment_uses_latest_review_without_historical_backfill(self):
        older = completed_job(
            1, 1, SOURCE_HEAD,
            finding_debug("Ancient point", "src/old.cpp", "Ancient body."))
        previous = completed_job(7, 3, OLD_HEAD, finding_debug())
        assessments = {"findings": [{
            "source_job_id": 7, "source_generation": 3,
            "finding_id": "finding:1", "status": "still_present",
            "reason": "Still present", "evidence": "same code"}]}
        with patch.object(followup.repository, "has_commit", return_value=True), \
                patch.object(followup.repository, "diff_between_heads", return_value="diff"), \
                patch.object(followup.repository, "diff_path_between_heads",
                             return_value="path diff"), \
                patch.object(followup.repository, "file_excerpt_at", return_value="excerpt"), \
                patch.object(followup.model, "request_response",
                             return_value=(response(json.dumps(assessments)), b"{}", 0.1)):
            result = followup.assess_completed_review(
                "api-key", Path("/checkout"), self.current, [previous, older],
                {}, self.prompt_config, self.budget)
        self.assertEqual(
            [(item["source_job_id"], item["finding_id"]) for item in result["findings"]],
            [(7, "finding:1")])

    def test_overflow_is_persisted_as_deferred_unclear(self):
        previous = completed_job(7, 3, OLD_HEAD, {
            "finding_attribution": [
                {"finding_id": f"finding:{index}", "title": f"Point {index}",
                 "path": f"src/{index}.cpp", "line": index, "side": "head"}
                for index in range(1, followup.MAX_CANDIDATES + 2)
            ],
            "decisions": [
                {"disposition": "publish", "finding": {
                    "title": f"Point {index}", "path": f"src/{index}.cpp",
                    "line": index, "body": f"Body {index}"}}
                for index in range(1, followup.MAX_CANDIDATES + 2)
            ],
        })
        assessed = {"findings": [
            {"source_job_id": 7, "source_generation": 3,
             "finding_id": f"finding:{index}", "status": "still_present",
             "reason": "Still present", "evidence": "same code"}
            for index in range(1, followup.MAX_CANDIDATES + 1)
        ]}
        with patch.object(followup.repository, "has_commit", return_value=True), \
                patch.object(followup.repository, "diff_between_heads", return_value="diff"), \
                patch.object(followup.repository, "diff_path_between_heads",
                             return_value="path diff"), \
                patch.object(followup.repository, "file_excerpt_at", return_value="excerpt"), \
                patch.object(followup.model, "request_response",
                             return_value=(response(json.dumps(assessed)), b"{}", 0.1)):
            result = followup.assess_completed_review(
                "api-key", Path("/checkout"), self.current, [previous], {},
                self.prompt_config, self.budget)
        self.assertEqual(len(result["findings"]), followup.MAX_CANDIDATES + 1)
        self.assertEqual(result["findings"][-1]["status"], "unclear")
        self.assertEqual(result["findings"][-1]["evidence"], "candidate_limit")


if __name__ == "__main__":
    unittest.main()
