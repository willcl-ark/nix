import unittest

from ralph import coverage


def completed(status="complete", limitations=()):
    return {"status": "completed", "coverage": {
        "status": status, "limitations": list(limitations)}}


class CoverageTests(unittest.TestCase):
    def summarize(self, **stages):
        return coverage.summarize({"stages": {"independent": completed(),
                                              "verifier": completed(), **stages}})

    def test_advisory_history_does_not_reduce_code_coverage(self):
        result = self.summarize(archaeologist=completed("partial", ["Linked discussion was unavailable."]))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["context"]["status"], "partial")
        self.assertEqual(result["limitations"], [])
        self.assertIn("Linked discussion", result["context"]["limitations"][0])

    def test_failure_identifies_stage_and_reason(self):
        result = self.summarize(adversarial_glm={"status": "failed", "error_type": "HTTPError"})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["verification"]["status"], "complete")
        self.assertEqual(result["limitations"], ["adversarial_glm: Model API request failed."])
        result = self.summarize(adversarial_glm={
            "status": "failed", "error_type": "ValueError",
            "max_output_tokens": 25_000,
            "turns": [{"status": "incomplete",
                       "incomplete_reason": "max_output_tokens"}],
        })
        self.assertEqual(result["limitations"], [
            "adversarial_glm: Model response reached its 25,000-token output limit."])
        result = self.summarize(verifier={"status": "invalid", "validation_error": "Unknown candidate ID"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verification"]["status"], "failed")
        self.assertIn("Unknown candidate ID", result["limitations"][0])

    def test_static_caveat_only_is_scope_note(self):
        for reason in ["Static review only; no builds, tests, or sanitizers were run.",
                       "No builds, tests, or sanitizers were run; this is a static review.",
                       "Static review only; no builds or test runs were performed, which is expected for this review."]:
            with self.subTest(reason=reason):
                result = self.summarize(adversarial_glm=completed("partial", [reason]))
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["notes"], [reason])

    def test_mixed_or_specific_unanswered_evidence_is_preserved(self):
        for reasons in [["Static review only; no builds or tests were run.", "Caller was not inspected."],
                        ["Static review only; no builds or tests were run. Caller was not inspected."],
                        ["Compiler behavior of the new flag was not verified."]]:
            with self.subTest(reasons=reasons):
                result = self.summarize(adversarial_glm=completed("partial", reasons))
                self.assertEqual(result["status"], "partial")
                self.assertIn(reasons[-1], result["limitations"][-1])

    def test_undocumented_partial_and_missing_stages_are_not_complete(self):
        result = self.summarize(adversarial_glm=completed("partial"))
        self.assertEqual(result["status"], "partial")
        self.assertIn("without identifying", result["limitations"][0])
        self.assertEqual(coverage.summarize({})["status"], "unknown")

    def test_unresolved_concept_is_context_and_unresolved_candidate_is_verification(self):
        debug = {"stages": {"independent": completed(), "archaeologist": completed(),
                            "verifier": completed("partial", ["Prior objection unavailable."])},
                 "concept_assessment": {"verification": {"disposition": "unresolved",
                                                           "reason": "Prior objection unavailable."}}}
        result = coverage.summarize(debug)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["context"]["status"], "partial")
        debug["decisions"] = [{"disposition": "unresolved", "reason": "Caller guard was not checked."}]
        result = coverage.summarize(debug)
        self.assertEqual(result["status"], "partial")
        self.assertIn("Caller guard", result["verification"]["limitations"][0])

    def test_skipped_stages_do_not_reduce_coverage(self):
        result = self.summarize(adversarial_glm={"status": "skipped"})
        self.assertEqual(result["status"], "complete")
