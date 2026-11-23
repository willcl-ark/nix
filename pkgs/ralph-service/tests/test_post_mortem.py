import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ralph import usage

from ralph_service import commands, monitor, post_mortem

NOW = 1_790_000_000
HEAD, FINAL = "a" * 40, "b" * 40
PROMPTS = {"triage": "triage", "confirm": "confirm", "lessons": "lessons"}


def metadata(number, *, comments=(), merged_at="2026-09-20T00:00:00Z"):
    return {
        "pull": {
            "number": number,
            "title": "Change",
            "body": "",
            "user": {"login": "author"},
            "merged_at": merged_at,
            "merge_commit_sha": f"{number:040x}",
            "head": {"sha": FINAL},
            "html_url": f"https://example/{number}",
        },
        "comments": list(comments),
        "events": [],
    }


def comment(identifier, login="reviewer", head=HEAD, reply_to=None):
    return {
        "id": identifier,
        "user": {"login": login},
        "original_commit_id": head,
        "in_reply_to_id": reply_to,
        "path": None,
        "body": "This is unused",
        "html_url": f"https://example/c{identifier}",
    }


class PostMortemTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.pulls = self.root / "pulls"
        self.pulls.mkdir()
        self.directory = self.root / post_mortem.DIRECTORY
        self.path = self.directory / post_mortem.DATABASE
        self.jobs = self.root / "jobs.sqlite3"
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute(
                "CREATE TABLE jobs (id INTEGER PRIMARY KEY, repo_id, number, generation,"
                " base_ref, head, action, force, status, attempts, review_result)"
            )
        self.client = post_mortem.Client(
            "key",
            usage.Ledger(self.directory / "usage.sqlite3"),
            500_000,
            api_base="https://api",
            clock=lambda: NOW,
        )
        self.asked = []
        self.answers = {}
        patch.object(self.client, "ask", self.ask).start()
        self.addCleanup(patch.stopall)

    def ask(self, stage, prompt, value, schema, **_):
        self.asked.append(stage)
        answer = self.answers[stage]
        return answer(value) if callable(answer) else answer

    def review(self, job_id, number, decisions=()):
        result = {
            "head_sha": HEAD,
            "output": {"data": {"decisions": list(decisions), "stages": {}}},
        }
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute(
                "INSERT INTO jobs VALUES (?, 'core', ?, 1, 'master', ?, 'opened', 0,"
                " 'complete', 1, ?)",
                (job_id, number, HEAD, json.dumps(result)),
            )

    def merge(self, number, **fields):
        path = self.pulls / f"{number}.json"
        path.write_text(json.dumps(metadata(number, **fields)))

    def run_once(self, **fields):
        with patch.object(post_mortem, "LOOKBACK", 10**10):
            return post_mortem.run(
                self.directory,
                self.jobs,
                self.pulls,
                "core",
                "origin",
                self.client,
                prompts=PROMPTS,
                **fields,
            )

    @staticmethod
    def triage(relation="missed", decision_id="", outcome="fixed"):
        return lambda value: [
            {
                "item_id": item["item_id"],
                "relation": relation,
                "decision_id": decision_id,
                "outcome": outcome,
                "impact": "medium",
                "point": "Unused helper",
            }
            for item in value["human_items"]
        ]

    def confirmed(self):
        self.answers["post_mortem_confirm"] = [
            {
                "verdict": "confirmed",
                "reason": "x:1",
                "cause": "not_raised",
                "agent": "design",
                "failure": "Uncalled code",
            }
        ]

    def rows(self, query):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(query).fetchall()

    def test_only_independent_points_are_compared(self):
        entry = metadata(
            1,
            comments=[
                comment(1),
                comment(2, login="author"),
                comment(3, head=FINAL),
                comment(4, reply_to=1, login="author"),
                comment(5, login="DrahtBot"),
            ],
        )
        items = post_mortem.human_items(entry, {HEAD})
        self.assertEqual([item["item_id"] for item in items], ["c1"])
        self.assertEqual(items[0]["replies"], ["author: This is unused"])

    def test_unreviewed_commits_use_the_latest_earlier_reviewed_push(self):
        pushes = [("2026-09-01", "x"), ("2026-09-02", "y"), ("2026-09-03", "z")]
        for commit, at, expected in (
            ("y", "2026-09-04", "y"),
            ("series", "2026-09-04", "z"),
            ("series", "2026-09-02", "y"),
            ("series", "2026-08-01", None),
        ):
            self.assertEqual(
                post_mortem._reviewed_head(commit, at, pushes, {"y", "z"}),
                expected,
            )

    def test_one_pull_request_cannot_use_every_source_check(self):
        self.review(1, 1)
        self.merge(1, comments=[comment(number) for number in range(1, 8)])
        self.answers["post_mortem_triage"] = self.triage()
        self.confirmed()
        summary = self.run_once()
        self.assertEqual((summary["confirmed"], summary["unchecked"]), (4, 3))

    def test_unanswered_points_still_reach_the_source_check(self):
        self.review(1, 1)
        self.merge(1, comments=[comment(1)])
        self.answers["post_mortem_triage"] = self.triage(outcome="unknown")
        self.confirmed()
        self.assertEqual(self.run_once()["confirmed"], 1)

    def test_source_evidence_reports_symbols_without_callers(self):
        checkout = self.root / "repo"
        checkout.mkdir()

        def git(*args):
            return subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "-c",
                    "user.name=t",
                    "-c",
                    "user.email=t@t",
                    *args,
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("init", "-q")
        (checkout / "a.py").write_text("def scan_tx(): pass\n")
        git("add", "a.py")
        git("commit", "-qm", "a")
        head = git("rev-parse", "HEAD")
        git("remote", "add", "origin", str(checkout))
        evidence = post_mortem.source_evidence(
            checkout,
            str(checkout),
            {"head": head, "point": "scan_tx is unused, missing_name too"},
            {"path": None, "body": "in a.py"},
            head,
        )
        self.assertEqual(evidence["symbol_uses"]["scan_tx"]["count"], 1)
        self.assertEqual(evidence["symbol_uses"]["missing_name"]["count"], 0)
        self.assertIn("def scan_tx", evidence["files"][0]["reviewed_source"])

    def test_quiet_merges_use_no_model_and_merges_are_processed_once(self):
        self.review(1, 1)
        self.merge(1)
        self.assertEqual(self.run_once()["quiet"], 1)
        self.assertEqual(self.run_once()["merged"], 0)
        self.assertEqual(self.asked, [])

    def test_invented_matches_do_not_count_as_caught(self):
        self.review(1, 1)
        self.merge(1, comments=[comment(1)])
        self.answers["post_mortem_triage"] = self.triage("published", "9.9")
        self.confirmed()
        self.run_once()
        self.assertEqual(self.rows("SELECT relation FROM cases"), [("missed",)])

    def test_disposition_comes_from_the_saved_decision(self):
        self.review(1, 1, [{"disposition": "drop", "reason": "no benefit"}])
        self.merge(1, comments=[comment(1)])
        self.answers["post_mortem_triage"] = self.triage("published", "1.0")
        self.confirmed()
        self.run_once()
        self.assertEqual(
            self.rows("SELECT relation, status FROM cases"), [("withheld", "confirmed")]
        )

    def test_incomplete_triage_is_retried_without_losing_the_merge(self):
        self.review(1, 1)
        self.merge(1, comments=[comment(1)])
        self.answers["post_mortem_triage"] = []
        self.assertEqual(self.run_once()["failed"], 1)
        self.answers["post_mortem_triage"] = self.triage("excluded")
        self.assertEqual(self.run_once()["triaged"], 1)

    def test_budget_defers_merges_to_a_later_run(self):
        for number in (1, 2):
            self.review(number, number)
            self.merge(number, comments=[comment(number)])

        def refuse(_):
            raise post_mortem.BudgetReached

        self.answers["post_mortem_triage"] = refuse
        summary = self.run_once()
        self.assertEqual((summary["deferred"], summary["budget_reached"]), (2, True))
        self.assertEqual(self.asked, ["post_mortem_triage"])
        self.assertEqual(self.rows("SELECT * FROM merges"), [])

    def test_lessons_need_two_pull_requests_outside_the_held_out_cases(self):
        numbers = [n for n in range(1, 40) if not post_mortem._held_out(n)][:2]
        held = next(n for n in range(1, 40) if post_mortem._held_out(n))
        for number in [*numbers, held]:
            self.review(number, number)
            self.merge(number, comments=[comment(number)])
        self.answers["post_mortem_triage"] = self.triage()
        self.confirmed()
        seen = []

        def lessons(value):
            ids = [case["case_id"] for case in value["cases"]]
            seen.extend(case["pull_request"] for case in value["cases"])
            row = {"agent": "design", "wording": "Check callers.", "risk": "noise"}
            return [
                {**row, "case_ids": ids},
                {**row, "wording": "One source.", "case_ids": ids[:1]},
                {**row, "wording": "Invented.", "case_ids": [*ids, 999]},
            ]

        self.answers["post_mortem_lessons"] = lessons
        self.assertEqual(self.run_once()["lessons"], 1)
        self.assertEqual(sorted(seen), sorted(numbers))
        self.assertEqual(
            self.rows("SELECT wording, status FROM lessons"),
            [("Check callers.", "pending")],
        )

    def test_budget_counts_unreported_usage_and_stops_before_a_request(self):
        patch.stopall()
        self.client.ledger.start("stage", "model", "review")
        self.client.clock = __import__("time").time
        self.assertGreater(self.client.spent(), 0)
        self.client.budget = 1
        with patch.object(post_mortem.model, "request_response") as request:
            with self.assertRaises(post_mortem.BudgetReached):
                self.client.ask(
                    "stage",
                    "p",
                    {},
                    post_mortem.TRIAGE_SCHEMA,
                    model_name="m",
                    effort="low",
                    review_id="r",
                )
        request.assert_not_called()

    def lesson(self, created_at=NOW):
        post_mortem.init(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO lessons (agent, wording, risk, case_ids, created_at) "
                "VALUES ('design', 'Check callers.', 'noise', '[]', ?)",
                (created_at,),
            )

    def poll(self, text, sender, update_id):
        store = monitor.NotificationStore(self.root / "telegram.sqlite3")
        update = {
            "update_id": update_id,
            "channel_post": {
                "chat": {"type": "channel", "id": -100},
                "from": {"id": sender},
                "text": text,
            },
        }
        admins = [{"user": {"id": 7}}, {"user": {"id": 9}, "can_post_messages": True}]

        def request(_token, method, _data, **_):
            return [update] if method == "getUpdates" else admins

        with patch.object(commands.telegram, "api_request", request):
            commands.poll_commands(
                store,
                "token",
                "-100",
                {"id": 9, "username": "Bot"},
                self.root,
                self.root,
                "https://reports",
                repositories={},
            )
        with closing(store._connect()) as db:
            row = db.execute(
                "SELECT text FROM outbox WHERE key = ?", (f"command:{update_id}",)
            ).fetchone()
        return row and row[0]

    def test_only_channel_administrators_decide_and_decisions_stand(self):
        self.lesson()
        self.assertIsNone(self.poll("/approve 1", 8, 1))
        self.assertEqual(self.rows("SELECT status FROM lessons"), [("pending",)])
        self.assertIn("Check callers.", self.poll("/lessons", 7, 2))
        self.assertIn("Usage", self.poll("/approve one", 7, 3))
        self.assertEqual(self.poll("/reject 1 too broad", 7, 4), "Lesson 1 rejected.")
        self.assertEqual(self.poll("/approve 1", 7, 5), "Lesson 1 is not pending.")
        self.assertEqual(
            self.rows("SELECT status, note FROM lessons"), [("rejected", "too broad")]
        )

    def test_pending_lessons_are_reminded_weekly_until_decided(self):
        self.lesson(NOW - post_mortem.WEEK)
        store = monitor.NotificationStore(self.root / "telegram.sqlite3")
        store.observe_lessons(self.root, now=NOW)
        store.observe_lessons(self.root, now=NOW)
        store.observe_lessons(self.root, now=NOW + post_mortem.WEEK)
        with closing(store._connect()) as db:
            rows = db.execute("SELECT key FROM outbox").fetchall()
            [text] = db.execute(
                "SELECT text FROM outbox WHERE key = 'lesson:1'"
            ).fetchone()
        for command in ("/approve 1", "/reject 1", "/veto", "/lessons"):
            self.assertIn(command, text)
        self.assertEqual(sum(key.startswith("lesson-reminder") for (key,) in rows), 2)
        post_mortem.decide(self.path, 1, "approved")
        store.observe_lessons(self.root, now=NOW + 2 * post_mortem.WEEK)
        with closing(store._connect()) as db:
            count = db.execute("SELECT count(*) FROM outbox").fetchone()[0]
        self.assertEqual(count, len(rows))
