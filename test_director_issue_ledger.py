# -*- coding: utf-8 -*-
import json
import unittest

from director_issue_ledger import DirectorIssueLedger


def audio(anchor="edge-a", track=1, side="start", **changes):
    row = {"type": "speech_cut_" + side, "modality": "audio",
           "anchor_key": anchor, "track_id": track, "review_kind": "cut_" + side,
           "severity": "critical", "veto": True, "confidence": 0.9,
           "action": "expand_" + side, "id": "unstable-finding",
           "suggested_interval": [1.0, 1.3], "evidence_refs": ["audio:%s" % track]}
    row.update(changes)
    return row


class DirectorIssueLedgerTests(unittest.TestCase):
    def test_twenty_rounds_keep_one_issue_and_resolve_only_after_recheck(self):
        ledger = DirectorIssueLedger()
        issue_id = None
        for index in range(20):
            rows = ledger.observe("audio", [audio(
                id="finding-%s" % index, suggested_interval=[1.0 - index / 20, 1.3],
                evidence_refs=["audio:1", "review:review_%03d" % index])], "r%s" % index)
            issue_id = issue_id or rows[0]["issue_id"]
            self.assertEqual(issue_id, rows[0]["issue_id"])
            self.assertEqual({"total": 1, "resolved": 0, "remaining": 1, "reopened": 0},
                             ledger.counts())
        ledger.observe("audio", [], "clean")
        self.assertEqual(1, ledger.counts()["resolved"])
        self.assertEqual([], ledger.pending())
        self.assertEqual("clean", ledger.snapshot()["entries"][0]["resolution"]["revision"])

    def test_local_coverage_leaves_other_edges_unresolved(self):
        ledger = DirectorIssueLedger()
        ledger.observe("audio", [audio("a"), audio("b")], "r1")
        ledger.observe("audio", [], "r2", coverage={"anchor_keys": ["a"]})
        self.assertEqual((2, 1, 1), tuple(ledger.counts()[key]
                                        for key in ("total", "resolved", "remaining")))
        self.assertEqual("b", ledger.pending()[0]["anchor_key"])

    def test_coverage_can_be_track_and_side_specific(self):
        ledger = DirectorIssueLedger()
        ledger.observe("audio", [audio("a", 1), audio("a", 2), audio("a", 1, "end")], "r1")
        ledger.observe("audio", [], "r2", coverage={"anchors": [
            {"anchor_key": "a", "track_id": 1, "side": "start"}]})
        self.assertEqual(1, ledger.counts()["resolved"])
        self.assertEqual({("2", "start"), ("1", "end")}, {
            (str(row["track_id"]), row["review_kind"].replace("cut_", ""))
            for row in ledger.pending()})

    def test_incomplete_review_never_closes_absent_or_minor_findings(self):
        ledger = DirectorIssueLedger()
        ledger.observe("audio", [audio()], "r1")
        ledger.observe("audio", [], "r2", complete=False)
        ledger.observe("audio", [audio(severity="minor", veto=False)], "r3", complete=False)
        self.assertEqual(1, ledger.counts()["remaining"])
        self.assertTrue(ledger.pending()[0]["veto"])

    def test_minor_handles_old_issue_but_veto_stays_open(self):
        ledger = DirectorIssueLedger()
        ledger.observe("audio", [audio()], "r1")
        ledger.observe("audio", [audio(type="unconfirmed_boundary_activity", severity="minor",
                                       veto=False, action="transition_guard")], "r2")
        entry = ledger.snapshot()["entries"][0]
        self.assertEqual("resolved", entry["status"])
        self.assertEqual("handled", entry["scopes"]["audio"]["status"])
        self.assertEqual("non_blocking_evidence", entry["resolution"]["basis"])
        ledger.observe("audio", [audio(severity="minor", veto=True)], "r3")
        self.assertEqual(1, ledger.counts()["remaining"])
        self.assertEqual(1, ledger.counts()["reopened"])

    def test_recurrence_reopens_same_issue_without_increasing_total(self):
        ledger = DirectorIssueLedger()
        first = ledger.observe("audio", [audio()], "r1")[0]["issue_id"]
        ledger.observe("audio", [], "r2")
        returned = ledger.observe("audio", [audio(id="different")], "r3")[0]["issue_id"]
        self.assertEqual(first, returned)
        self.assertEqual({"total": 1, "resolved": 0, "remaining": 1, "reopened": 1}, ledger.counts())

    def test_different_scopes_do_not_clear_each_other(self):
        ledger = DirectorIssueLedger()
        ledger.observe("audio", [audio()], "r1")
        ledger.observe("text", [], "r2")
        ledger.observe("multimodal", [], "r3")
        self.assertEqual(1, ledger.counts()["remaining"])
        self.assertEqual(1, len(ledger.pending("audio")))
        self.assertEqual([], ledger.pending("text"))

    def test_shared_explicit_issue_still_requires_both_scopes_to_close(self):
        ledger = DirectorIssueLedger()
        ledger.observe("a", [audio(issue_key="shared")], "r1")
        ledger.observe("b", [audio(issue_key="shared")], "r1")
        ledger.observe("a", [], "r2")
        self.assertEqual(1, ledger.counts()["remaining"])
        ledger.observe("b", [], "r2")
        self.assertEqual({"total": 1, "resolved": 1, "remaining": 0, "reopened": 0}, ledger.counts())

    def test_explicit_identity_has_priority_and_deduplicates_same_snapshot(self):
        ledger = DirectorIssueLedger()
        rows = ledger.observe("audio", [audio(issue_key="stable"), audio(
            "different-anchor", 3, "end", issue_key="stable", severity="major", veto=False)], "r1")
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["veto"])
        self.assertEqual(1, ledger.counts()["total"])

    def test_text_identity_ignores_report_enumeration_and_timeline_revision(self):
        ledger = DirectorIssueLedger()
        row = {"type": "missing_context", "modality": "text", "action": "add_previous",
               "indices": [7], "event_ids": ["ev4"], "severity": "major",
               "evidence_refs": ["text_issue:5", "source_timeline:old", "segment:7"]}
        first = ledger.observe("text", [row], "r1")[0]["issue_id"]
        row.update(id="new-id", message="different words", timeline_revision="new",
                   suggested_interval=[100.0, 102.0], evidence_refs=[
                       "text_issue:1", "source_timeline:new", "segment:7"])
        self.assertEqual(first, ledger.observe("text", [row], "r2")[0]["issue_id"])
        row["evidence_refs"].append("additional_context:later-evidence")
        self.assertEqual(first, ledger.observe("text", [row], "r3")[0]["issue_id"])
        self.assertEqual(1, ledger.counts()["total"])

    def test_stale_revision_cannot_undo_newer_review(self):
        ledger = DirectorIssueLedger()
        ledger.observe("audio", [audio()], "r1")
        ledger.observe("audio", [], "r2")
        before = ledger.snapshot()
        self.assertEqual([], ledger.observe("audio", [audio()], "r1"))
        self.assertEqual(before, ledger.snapshot())

    def test_explicit_fresh_reinspection_can_revisit_an_older_timeline_hash(self):
        ledger = DirectorIssueLedger()
        issue_id = ledger.observe("audio", [audio()], "r1")[0]["issue_id"]
        ledger.observe("audio", [], "r2")
        fresh = ledger.observe("audio", [audio(id="new-evidence")], "r1", allow_revisit=True)
        self.assertEqual(issue_id, fresh[0]["issue_id"])
        self.assertEqual(1, ledger.counts()["total"])
        self.assertEqual(1, ledger.counts()["reopened"])
        self.assertEqual(1, ledger.counts()["remaining"])

    def test_new_minor_is_not_counted_and_noncovered_minor_cannot_close(self):
        ledger = DirectorIssueLedger()
        ledger.observe("audio", [audio(severity="minor", veto=False)], "r1")
        self.assertEqual(0, ledger.counts()["total"])
        ledger.observe("audio", [audio()], "r2")
        ledger.observe("audio", [audio(severity="minor", veto=False)], "r3", coverage=[])
        self.assertEqual(1, ledger.counts()["remaining"])

    def test_snapshot_roundtrip_preserves_resolution_reopening_and_pending(self):
        ledger = DirectorIssueLedger()
        ledger.observe("audio", [audio(), audio("b")], "r1")
        ledger.observe("audio", [], "r2", coverage={"edge-a"})
        encoded = json.dumps(ledger.snapshot(), ensure_ascii=False)
        restored = DirectorIssueLedger(json.loads(encoded))
        self.assertEqual(ledger.snapshot(), restored.snapshot())
        self.assertEqual(ledger.pending(), restored.pending())
        restored.observe("audio", [audio()], "r3", coverage={"edge-a"})
        self.assertEqual(2, restored.counts()["remaining"])
        self.assertEqual(1, restored.counts()["reopened"])

    def test_empty_coverage_does_not_accidentally_mean_full_scope(self):
        ledger = DirectorIssueLedger()
        ledger.observe("audio", [audio()], "r1")
        ledger.observe("audio", [], "r2", coverage={})
        ledger.observe("audio", [], "r3", coverage=[])
        self.assertEqual(1, ledger.counts()["remaining"])

    def test_resolution_can_cover_the_stable_issue_id(self):
        ledger = DirectorIssueLedger()
        issue_id = ledger.observe("audio", [audio()], "r1")[0]["issue_id"]
        ledger.observe("audio", [], "r2", coverage={"issue_ids": [issue_id]})
        entry = ledger.snapshot()["entries"][0]
        self.assertEqual("resolved", entry["status"])
        self.assertEqual("audio", entry["scope"])
        self.assertEqual(issue_id, entry["finding"]["issue_id"])

    def test_pure_minor_observation_is_returned_for_current_review_only(self):
        ledger = DirectorIssueLedger()
        rows = ledger.observe("visual", [{"type": "scene_jump", "severity": "minor",
                                          "issue_key": "node-a", "modality": "visual"}], "r1")
        self.assertIn("issue_id", rows[0])
        self.assertEqual([], ledger.snapshot()["entries"])
        self.assertEqual([], ledger.pending())


if __name__ == "__main__":
    unittest.main()
