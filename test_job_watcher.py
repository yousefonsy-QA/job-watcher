"""Offline checks for job_watcher filters. Run: python test_job_watcher.py"""
import json
import unittest
from pathlib import Path

import job_watcher as w

CFG = json.loads((Path(__file__).parent / "watcher_config.json").read_text(encoding="utf-8"))
F = w.build_filters(CFG)


def job(**kw):
    j = w.new_job(**{"title": "QA Engineer", "company": "Acme", "location": "Cairo, Egypt", "text": "", **kw})
    j["years"] = w.min_required_years(j["text"] or "")
    return j


def reason(**kw):
    return w.skip_reason(job(**kw), CFG, F)


class Filters(unittest.TestCase):
    def test_years(self):
        self.assertIsNone(reason(text="2-4 years of experience in software testing."))
        self.assertEqual(reason(text="Minimum 5 years of QA experience."), "JD min 5 years")
        self.assertEqual(w.min_required_years("Ideally 7+ years in software QA"), 7)

    def test_nationals(self):
        self.assertEqual(reason(title="QA Analyst 1 (UAE Nationals)"), "nationals only")
        self.assertEqual(reason(text="This role is open to Saudi nationals only."), "nationals only")
        self.assertIsNone(reason(text="UAE nationals are preferred."))
        self.assertEqual(reason(title="Test Engineer (Emiratisation)"), "nationals only")

    def test_foreign_language(self):
        self.assertTrue(reason(text="Fluent German is required.").startswith("foreign language"))
        self.assertTrue(reason(text="Must be fluent in English and French.").startswith("foreign language"))
        self.assertIsNone(reason(text="Fluent English is required. French is a plus."))
        self.assertIsNone(reason(text="Arabic native speaker required."))

    def test_rater_titles_excluded(self):
        for t in ["Quality Assurance Rater - Arabic speakers", "Data Annotation QA", "AI Code Trainer"]:
            self.assertTrue(F["title_exclude"].search(t), t)
        self.assertFalse(F["title_exclude"].search("QA Automation Engineer"))

    def test_gulf_remote_only(self):
        self.assertEqual(reason(location="Riyadh", remote_only=True, remote=False),
                         "not remote (remote-only country)")
        self.assertIsNone(reason(location="Riyadh", remote_only=True, remote=True))
        self.assertIsNone(reason(location="Cairo", remote_only=False, remote=False))

    def test_remote_board_regions(self):
        ok = dict(remote=True, region_check=True)
        self.assertIsNone(reason(location="Anywhere in the World", **ok))
        self.assertIsNone(reason(location="", **ok))
        self.assertIsNone(reason(location="EMEA", **ok))
        self.assertTrue(reason(location="USA Only", **ok).startswith("remote only for"))
        self.assertTrue(reason(location="Remote - Americas", **ok).startswith("remote only for"))
        self.assertTrue(reason(location="Worldwide", text="Applicants must be authorized to work in the US.",
                               **ok).startswith("region lock"))


class Health(unittest.TestCase):
    def setUp(self):
        self.sent, self.state = [], {}
        self.t0 = w.datetime(2026, 10, 14, 1, 0)

    def step(self, problems, minutes):
        w.update_health(self.state, problems, CFG, self.t0 + w.timedelta(minutes=minutes), self.sent.append)

    def test_alert_after_three_bad_runs_then_quiet_then_recovery(self):
        block = ["LinkedIn answered only 1/5 searches (rate limit or block)"]
        self.step(block, 0); self.step(block, 30)
        self.assertEqual(self.sent, [])                      # 2 bad runs: no alert yet
        self.step(block, 60)
        self.assertEqual(len(self.sent), 1)                  # 3rd bad run: alert
        self.assertIn("3 runs in a row", self.sent[0])
        self.step(block, 90); self.step(block, 120)
        self.assertEqual(len(self.sent), 1)                  # no spam every run
        self.step(block, 60 + 12 * 60)
        self.assertEqual(len(self.sent), 2)                  # reminder after 12 hours
        self.step([], 60 + 12 * 60 + 30)
        self.assertIn("back to normal", self.sent[-1])       # recovery message
        self.step([], 60 + 12 * 60 + 60)
        self.assertEqual(len(self.sent), 3)                  # only once

    def test_one_bad_run_is_ignored(self):
        self.step(["LinkedIn answered only 0/5 searches (rate limit or block)"], 0)
        self.step([], 30)
        self.assertEqual(self.sent, [])

    def test_new_kind_of_problem_alerts_immediately(self):
        block = ["LinkedIn answered only 1/5 searches (rate limit or block)"]
        for m in (0, 30, 60):
            self.step(block, m)
        self.step(block + ["Could not read remote board feed: wwr"], 90)
        self.assertEqual(len(self.sent), 2)

    def test_run_problems(self):
        w.SEARCHES_MADE[0], w.FAILED_SEARCHES[:] = 5, ["a", "b", "c"]
        w.PARSE_ISSUES[:], w.BOARD_ISSUES[:] = [], []
        self.assertTrue(w.run_problems(CFG)[0].startswith("LinkedIn answered only 2/5"))
        w.FAILED_SEARCHES[:] = ["a"]
        self.assertEqual(w.run_problems(CFG), [])
        w.PARSE_ISSUES.append("LinkedIn job page")
        self.assertIn("layout may have changed", w.run_problems(CFG)[0])
        w.SEARCHES_MADE[0], w.FAILED_SEARCHES[:], w.PARSE_ISSUES[:] = 0, [], []


class DualRunner(unittest.TestCase):
    def test_laptop_active_window(self):
        now = w.datetime.now(w.timezone.utc)
        recent = {"local_heartbeat_utc": (now - w.timedelta(minutes=10)).isoformat(timespec="minutes")}
        stale = {"local_heartbeat_utc": (now - w.timedelta(minutes=40)).isoformat(timespec="minutes")}
        self.assertTrue(w.laptop_active(recent, CFG))
        self.assertFalse(w.laptop_active(stale, CFG))
        self.assertFalse(w.laptop_active({}, CFG))

    def test_merge_keeps_both_copies_work(self):
        import push_state
        ours = {"watcher_seen.json": ["1", "2", "3"],
                "jobs.json": [{"watcher_id": "2"}, {"watcher_id": "3"}],
                "watcher_state.json": {"local_heartbeat_utc": "2026-10-14T11:00+00:00",
                                       "boards_last_fetch": "2026-10-14T13:00", "health": {"bad_streak": 0}}}
        theirs = {"watcher_seen.json": ["1", "2", "9"],
                  "jobs.json": [{"watcher_id": "2"}, {"watcher_id": "9"}],
                  "watcher_state.json": {"local_heartbeat_utc": "2026-10-14T10:30+00:00",
                                         "boards_last_fetch": "2026-10-14T13:30", "health": {"bad_streak": 2}}}
        m = push_state.merge(ours, theirs)
        self.assertEqual(m["watcher_seen.json"], ["1", "2", "3", "9"])
        self.assertEqual([j["watcher_id"] for j in m["jobs.json"]], ["2", "9", "3"])
        self.assertEqual(m["watcher_state.json"]["local_heartbeat_utc"], "2026-10-14T11:00+00:00")
        self.assertEqual(m["watcher_state.json"]["boards_last_fetch"], "2026-10-14T13:30")
        self.assertEqual(m["watcher_state.json"]["health"], {"bad_streak": 0})


if __name__ == "__main__":
    unittest.main(verbosity=1)
