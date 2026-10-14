"""Offline checks for job_watcher filters and Telegram button handling. Run: python test_job_watcher.py"""
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


class Buttons(unittest.TestCase):
    def test_applied_then_undo(self):
        calls = []
        updates = [
            {"update_id": 10, "callback_query": {"id": "c1", "data": "a|123",
                                                 "message": {"message_id": 5, "chat": {"id": 42}}}},
            {"update_id": 11, "callback_query": {"id": "c2", "data": "u|456",
                                                 "message": {"message_id": 6, "chat": {"id": 42}}}},
            {"update_id": 12, "callback_query": {"id": "c3", "data": "a|123",
                                                 "message": {"message_id": 5, "chat": {"id": 999}}}},
        ]

        def fake(token, method, params):
            calls.append((method, params))
            return {"ok": True, "result": updates} if method == "getUpdates" else {"ok": True}

        jobs = [{"watcher_id": "123", "status": "NEW (watcher)"}, {"watcher_id": "456", "status": "APPLIED"}]
        state = {}
        original, w.tg_call = w.tg_call, fake
        try:
            handled = w.process_button_presses("t", "42", state, jobs)
        finally:
            w.tg_call = original
        self.assertEqual(handled, 2)                      # update from another chat ignored
        self.assertEqual(jobs[0]["status"], "APPLIED")
        self.assertEqual(jobs[1]["status"], "NEW (watcher)")
        self.assertEqual(state["telegram_offset"], 13)
        edits = [p for m, p in calls if m == "editMessageReplyMarkup"]
        self.assertIn("u|123", json.dumps(edits[0]["reply_markup"]))
        self.assertIn("a|456", json.dumps(edits[1]["reply_markup"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
