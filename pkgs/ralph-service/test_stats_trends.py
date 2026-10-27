import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ralph_service import stats


class ProductionTrendTests(unittest.TestCase):
    def test_daily_events_use_utc_fill_gaps_and_preserve_unknowns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            traces = root / "review-traces"
            traces.mkdir()
            checkout = root / "followup" / "checkout"
            checkout.mkdir(parents=True)

            def git(*args, **kwargs):
                return (
                    subprocess.check_output(
                        ["git", "-C", str(checkout), *args], **kwargs
                    )
                    .decode()
                    .strip()
                )

            git("init", "--quiet")
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
                "GIT_AUTHOR_DATE": "2026-09-01T12:00:00+00:00",
                # October 3 in UTC, despite the local date being October 2.
                "GIT_COMMITTER_DATE": "2026-10-02T23:30:00-02:00",
            }
            git("commit", "--quiet", "--allow-empty", "-m", "Assessment head", env=env)
            head = git("rev-parse", "HEAD")
            records = []
            for number, dates, verified, findings in (
                (1, ["2026-10-01T00:30:00+02:00"], True, 2),
                (2, ["2026-10-01T00:00:00+00:00"], True, 1),
                (3, ["2026-10-02T12:00:00+00:00"], None, None),
                (4, [], True, 99),
                (
                    5,
                    ["2026-10-01T12:00:00+00:00", "2026-10-01T13:00:00+00:00"],
                    True,
                    99,
                ),
            ):
                records.append(
                    {
                        "job_id": number,
                        "generation": 0,
                        "number": number,
                        "head": head,
                        "verified": verified,
                        "published_findings": findings,
                    }
                )
                for date in dates:
                    timestamp = (
                        int(datetime.fromisoformat(date).timestamp()) * 1_000_000_000
                    )
                    if number == 2:
                        timestamp -= 1  # Last nanosecond of September 30.
                    (traces / f"{number}-{head}-{timestamp}.json").write_text("{}")
            series, undated = stats._time_series_summary(
                root,
                records,
                [
                    {
                        "status": "addressed",
                        "new_head": head,
                        "assessed_at": "2026-10-10",
                    },
                    {"status": "partially_addressed", "new_head": head},
                    {"status": "addressed", "new_head": "f" * 40},
                    {"status": "unaddressed", "new_head": head},
                ],
            )

        self.assertEqual(
            series,
            [
                {
                    "period": "2026-09-30",
                    "reviews": 2,
                    "verified_reviews": 2,
                    "findings": 3,
                    "addressed": 0,
                },
                {
                    "period": "2026-10-01",
                    "reviews": 0,
                    "verified_reviews": 0,
                    "findings": 0,
                    "addressed": 0,
                },
                {
                    "period": "2026-10-02",
                    "reviews": 1,
                    "verified_reviews": None,
                    "findings": None,
                    "addressed": 0,
                },
                {
                    "period": "2026-10-03",
                    "reviews": 0,
                    "verified_reviews": 0,
                    "findings": 0,
                    "addressed": 2,
                },
            ],
        )
        self.assertEqual(undated["reviews"], 2)
        self.assertEqual(undated["review_missing_trace_groups"], 1)
        self.assertEqual(undated["review_ambiguous_trace_groups"], 1)
        self.assertEqual(undated["addressed_missing_commit"], 1)


if __name__ == "__main__":
    unittest.main()
