import unittest

from ralph_service import stats, tokens
from ralph_service.stats_cache import USAGE_SUMS


def request(job_id, start, end, stage="tests", **totals):
    return {
        **dict.fromkeys(USAGE_SUMS),
        "job_id": job_id,
        "day": "2026-11-01",
        "stage": stage,
        "model": "test-model",
        "requested_at": start,
        "completed_at": end,
        **totals,
    }


def job(
    job_id,
    repository="core",
    code="code",
    config="config",
    effort="low",
    runs=(),
    stages=("tests",),
):
    models = {"default": {"tests": ["test-model", effort]}}
    return {
        "row": {"id": job_id, "repo_id": repository, "number": 1, "status": "complete"},
        "reviews": [{"findings": {"published": 2}}],
        "stage_runs": list(runs),
        "engine": {
            "code_sha256": code,
            "config_sha256": config,
            "models": models,
            "stages": list(stages),
        },
    }


class TokenTests(unittest.TestCase):
    def test_runs_merge_days_and_keep_times_from_the_start_of_the_review(self):
        router = {
            "stage": "router",
            "model": "test-model",
            "status": "failed",
            "refused": True,
            "turns": 1,
            "tools": 3,
            "candidates": 0,
            "accepted": 0,
            "sole": 0,
            "input_bytes": 1234,
        }
        month = tokens.months(
            [job(1, runs=[router])],
            [
                request(1, 100, 130, "router", seconds=30, timed=1),
                request(1, 120, 150, seconds=30, timed=1),
                request(1, 160, None),
                request(1, 170, 190, seconds=20, timed=1),
            ],
        )["2026-11"]
        rows = {
            month["names"][run[1]]: dict(zip(month["run_fields"], run))
            for run in month["runs"]
        }
        self.assertEqual((rows["router"]["start"], rows["router"]["end"]), (0, 30))
        self.assertEqual((rows["tests"]["start"], rows["tests"]["end"]), (20, 90))
        self.assertEqual((rows["tests"]["seconds"], rows["tests"]["timed"]), (50, 2))
        (review,) = month["reviews"]
        review = dict(zip(month["review_fields"], review))
        self.assertEqual((review["findings"], review["size"]), (2, 1234))
        (stage,) = month["stages"]
        stage = dict(zip(month["stage_fields"], stage))
        self.assertEqual(month["names"][stage["status"]], "failed")
        self.assertEqual((stage["refused"], stage["tools"]), (1, 3))

    def test_changes_follow_start_order_and_ignore_other_repositories(self):
        jobs = [
            job(1),
            job(2, "gui", config="gui"),
            job(3),
            job(4, effort="high", config="new", stages=("judge", "tests")),
            job(5, "gui", effort="high", config="gui-new"),
        ]
        requests = [
            request(number, 100 * number, 100 * number + 1) for number in range(1, 6)
        ]
        self.assertEqual(
            tokens.changes(jobs, requests),
            [
                {
                    "time": 400,
                    "models": [
                        [
                            "default",
                            "tests",
                            ["test-model", "low"],
                            ["test-model", "high"],
                        ]
                    ],
                    "stages": ["judge"],
                },
                {"time": 500, "models": [], "stages": []},
            ],
        )

    def test_stage_runs_count_findings_and_skip_stages_that_did_not_run(self):
        debug = {
            "stages": {
                "tests": {
                    "status": "completed",
                    "model": "test-model",
                    "turns": [{}, {}],
                    "tools": [{}],
                    "review_input_bytes": 10,
                },
                "state": {"status": "failed", "failure_kind": "cyber_refusal"},
                "build": {"status": "skipped"},
            },
            "candidate_sources": {"tests:1": "tests", "state:1": "state"},
            "decisions": [
                {"candidate_ids": ["tests:1"], "disposition": "publish"},
            ],
            "finding_attribution": [
                {"finding_id": "finding:1", "raised_by": ["tests"]},
                {"finding_id": "finding:2", "raised_by": ["tests", "state"]},
            ],
        }
        runs = {run["stage"]: run for run in stats._stage_runs(debug)}
        self.assertEqual(set(runs), {"tests", "state"})
        self.assertEqual(runs["tests"]["model"], "test-model")
        self.assertEqual((runs["tests"]["turns"], runs["tests"]["tools"]), (2, 1))
        self.assertEqual((runs["tests"]["accepted"], runs["tests"]["sole"]), (2, 1))
        self.assertEqual((runs["state"]["accepted"], runs["state"]["sole"]), (1, 0))
        self.assertTrue(runs["state"]["refused"])
        self.assertIsNone(runs["state"]["input_bytes"])


if __name__ == "__main__":
    unittest.main()
