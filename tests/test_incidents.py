"""中毒事件域：升级、假愈期、合并回滚、权限与重启还原测试。"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from autumn_poison import domain
from autumn_poison.service import DomainStore, ServiceError

BASE = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = BASE

    def __call__(self):
        return self.now.isoformat()

    def advance(self, hours):
        self.now += timedelta(hours=hours)


def public_report(store, **overrides):
    data = {
        "sample_type": domain.SAMPLE_WILD_MUSHROOM,
        "source": domain.SOURCE_SELF_PICKED,
        "route": domain.ROUTE_INGESTION,
        "location": "中心公园橡树林",
        "reporter": {"name": "张三", "contact": "13800000000"},
        "patient": {"name": "张小孩", "contact": "13900000000"},
        "symptoms": [{"code": "vomiting", "onset_at": BASE.isoformat()}],
        "channel": "hotline",
        "dedupe": {"date": "2026-09-26", "location": "中心公园橡树林",
                   "patient": "张小孩"},
    }
    data.update(overrides)
    return store.report_incident(data, role="public", actor="")


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = DomainStore(clock=self.clock)

    def tearDown(self):
        self.store.close()

    # -------------------------------------------------- 报案与建议
    def test_public_report_starts_moderate_with_advice(self):
        result = public_report(self.store)
        self.assertEqual(result["risk_level"], "moderate")
        self.assertIn("假愈期", result["advice"])
        self.assertIn("自行采摘", result["advice"])
        self.assertTrue(result["incident_id"].startswith("INC-"))

    def test_advice_differs_by_route_and_source(self):
        skin = domain.advice_for(domain.SAMPLE_LYCORIS, domain.ROUTE_SKIN)
        ingest = domain.advice_for(domain.SAMPLE_LYCORIS, domain.ROUTE_INGESTION)
        self.assertIn("冲洗", skin)
        self.assertIn("催吐", ingest)
        ginkgo = domain.advice_for(domain.SAMPLE_GINKGO, domain.ROUTE_INGESTION)
        self.assertIn("抽搐", ginkgo)

    # -------------------------------------------------- 自动升级
    def test_high_risk_symptom_auto_escalates(self):
        result = public_report(self.store)
        updated = self.store.add_symptom(
            result["incident_id"], {"code": "jaundice"},
            role="medical", actor="doc-li")
        self.assertEqual(updated["risk_level"], "high")
        self.assertIn("R2", updated["risk_basis"])
        view = self.store.incident_view(result["incident_id"], role="dispatcher")
        self.assertEqual(view["status"], "escalated")
        kinds = {f["kind"] for f in view["pending_follow_ups"]}
        self.assertIn("urgent_callback", kinds)

    def test_critical_symptom_escalates(self):
        result = public_report(self.store)
        updated = self.store.add_symptom(
            result["incident_id"], {"code": "seizure"},
            role="medical", actor="doc-li")
        self.assertEqual(updated["risk_level"], "critical")

    def test_ginkgo_seizure_rule_r6(self):
        result = public_report(self.store, sample_type=domain.SAMPLE_GINKGO,
                               symptoms=[])
        self.assertEqual(result["risk_level"], "moderate")
        updated = self.store.add_symptom(
            result["incident_id"], {"code": "seizure"}, role="medical")
        self.assertIn("R6", updated["risk_basis"])
        self.assertEqual(updated["risk_level"], "critical")

    # -------------------------------------------------- 假愈期
    def test_false_recovery_escalates_and_schedules_watch(self):
        result = public_report(self.store)  # 呕吐，中风险
        updated = self.store.add_symptom(
            result["incident_id"],
            {"code": "gi_relief", "note": "呕吐停止，要求回家"},
            role="medical", actor="doc-li")
        self.assertEqual(updated["risk_level"], "critical")
        self.assertIn("R4", updated["risk_basis"])
        view = self.store.incident_view(result["incident_id"], role="dispatcher")
        windows = sorted(f["note"] for f in view["pending_follow_ups"]
                         if f["kind"] == "false_recovery_watch")
        self.assertEqual(len(windows), 3)
        self.assertTrue(any("肝功" in f["note"] for f in view["pending_follow_ups"]))
        # 时间线必须留下升级依据
        kinds = [e["kind"] for e in view["timeline"]]
        self.assertIn("risk_escalated", kinds)

    def test_delayed_onset_rule_r5(self):
        ingested = (BASE - timedelta(hours=7)).isoformat()
        result = public_report(self.store, sample_type=domain.SAMPLE_UNKNOWN,
                               ingestion_time=ingested, symptoms=[])
        self.assertEqual(result["risk_level"], "high")
        self.assertIn("R5", result["risk_basis"])

    # -------------------------------------------------- 多渠道重复上报
    def test_duplicate_reports_auto_attach_without_losing_evidence(self):
        first = public_report(self.store)
        second = public_report(
            self.store, channel="walk_in",
            symptoms=[{"code": "abdominal_pain"}],
            reporter={"name": "李四", "contact": "13700000000"})
        self.assertTrue(second["merged_existing"])
        self.assertEqual(first["incident_id"], second["incident_id"])
        view = self.store.incident_view(first["incident_id"], role="dispatcher")
        self.assertEqual(len(view["reports"]), 2)
        self.assertEqual(len(view["samples"]), 2)
        codes = {s["code"] for s in view["symptoms"]}
        self.assertEqual(codes, {"vomiting", "abdominal_pain"})

    def test_idempotent_report_replay(self):
        a = public_report(self.store)
        b = self.store.report_incident(
            {"sample_type": domain.SAMPLE_GINKGO, "route": domain.ROUTE_INGESTION},
            role="public", request_key="req-hotline-001")
        again = self.store.report_incident(
            {"sample_type": domain.SAMPLE_GINKGO, "route": domain.ROUTE_INGESTION},
            role="public", request_key="req-hotline-001")
        self.assertEqual(b, again)
        self.assertNotEqual(a["incident_id"], b["incident_id"])

    # -------------------------------------------------- 医护补充与权限
    def test_medical_supplement_requires_authorization(self):
        result = public_report(self.store)
        with self.assertRaises(ServiceError):
            self.store.report_incident(
                {"incident_id": result["incident_id"],
                 "sample_type": domain.SAMPLE_WILD_MUSHROOM,
                 "route": domain.ROUTE_INGESTION,
                 "symptoms": [{"code": "diarrhea"}]},
                role="public")
        ok = self.store.report_incident(
            {"incident_id": result["incident_id"],
             "sample_type": domain.SAMPLE_WILD_MUSHROOM,
             "route": domain.ROUTE_INGESTION,
             "symptoms": [{"code": "diarrhea"}]},
            role="medical", actor="doc-wang")
        self.assertEqual(ok["incident_id"], result["incident_id"])
        handoff = self.store.record_handoff(
            result["incident_id"],
            {"facility": "市一院急诊", "staff": "王医生",
             "summary": "已洗胃留观"}, role="medical", actor="doc-wang")
        self.assertEqual(handoff["status"], "handoff")

    # -------------------------------------------------- 合并与回滚
    def test_merge_then_undo_restores_evidence(self):
        a = public_report(self.store, location="东山")
        b = public_report(self.store, channel="app", location="西山",
                          symptoms=[{"code": "confusion"}],
                          dedupe={"date": "2026-09-26", "location": "西山",
                                  "patient": "另一人"})
        merge = self.store.merge_incidents(
            a["incident_id"], b["incident_id"], role="dispatcher",
            reason="同一采食群体")
        survivor = self.store.incident_view(a["incident_id"], role="dispatcher")
        self.assertEqual(len(survivor["reports"]), 2)
        self.assertTrue(any(s["code"] == "confusion" for s in survivor["symptoms"]))
        absorbed = self.store.incident_view(b["incident_id"], role="dispatcher")
        self.assertEqual(absorbed["status"], "merged")
        self.assertEqual(absorbed["merged_into"], a["incident_id"])

        self.store.undo_merge(merge["merge_id"], role="dispatcher")
        restored_a = self.store.incident_view(a["incident_id"], role="dispatcher")
        restored_b = self.store.incident_view(b["incident_id"], role="dispatcher")
        # A 仅自身证据（呕吐）-> 回到中风险 open；B 含意识模糊 -> 仍为高危
        self.assertEqual(restored_a["status"], "open")
        self.assertEqual(restored_a["risk_level"], "moderate")
        self.assertEqual(restored_b["status"], "escalated")
        self.assertEqual(restored_b["risk_level"], "high")
        self.assertIsNone(restored_b["merged_into"])
        self.assertEqual(len(restored_a["reports"]), 1)
        self.assertEqual(len(restored_b["reports"]), 1)
        self.assertTrue(any(s["code"] == "confusion"
                            for s in restored_b["symptoms"]))
        self.assertIn("merge_undone",
                      [e["kind"] for e in restored_b["timeline"]])

    def test_merge_requires_operator_role(self):
        a = public_report(self.store)
        b = public_report(self.store, dedupe={"date": "2026-09-26",
                                              "location": "北园",
                                              "patient": "另一人"})
        with self.assertRaises(ServiceError):
            self.store.merge_incidents(a["incident_id"], b["incident_id"],
                                       role="medical")

    def test_relocate_wrong_auto_attach(self):
        first = public_report(self.store)
        second = public_report(self.store, channel="app")
        self.assertTrue(second["merged_existing"])
        moved = self.store.relocate_report(
            second["report_id"], None, role="dispatcher", reason="并非同批")
        self.assertNotEqual(moved["incident_id"], first["incident_id"])
        view = self.store.incident_view(first["incident_id"], role="dispatcher")
        self.assertEqual(len(view["reports"]), 1)

    # -------------------------------------------------- 权限脱敏
    def test_pii_masked_for_public(self):
        result = public_report(self.store)
        view = self.store.incident_view(result["incident_id"], role="public")
        for report in view["reports"]:
            self.assertNotIn("reporter_name", report)
            self.assertNotIn("patient_contact", report)
        staff = self.store.incident_view(result["incident_id"], role="dispatcher")
        self.assertEqual(staff["reports"][0]["reporter_name"], "张三")
        self.assertEqual(staff["reports"][0]["patient_name"], "张小孩")

    def test_manual_downgrade_needs_reason_and_operator(self):
        result = public_report(self.store,
                               sample_type=domain.SAMPLE_UNKNOWN,
                               symptoms=[{"code": "jaundice"}])
        self.assertEqual(result["risk_level"], "high")
        with self.assertRaises(ServiceError):
            self.store.downgrade_risk(result["incident_id"], "low", "",
                                      role="dispatcher")
        with self.assertRaises(ServiceError):
            self.store.downgrade_risk(result["incident_id"], "low", "已好转",
                                      role="medical")
        self.store.downgrade_risk(
            result["incident_id"], "low", "复查肝功正常，患者离院签字",
            role="dispatcher")
        view = self.store.incident_view(result["incident_id"], role="dispatcher")
        self.assertEqual(view["risk_level"], "low")
        self.assertIn("人工降级", view["risk_basis"])

    # -------------------------------------------------- 随访台
    def test_due_follow_up_board_marks_overdue(self):
        result = public_report(self.store)
        self.store.add_symptom(result["incident_id"], {"code": "jaundice"},
                               role="medical")
        self.clock.advance(3)
        groups = self.store.list_due_follow_ups(role="dispatcher")
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0]["overdue"])
        fu = groups[0]["follow_ups"][0]
        done = self.store.complete_follow_up(
            fu["fu_id"], role="dispatcher", outcome="已确认住院")
        self.assertEqual(done["status"], "done")
        self.assertEqual(self.store.list_due_follow_ups(role="dispatcher"), [])

    def test_action_note_records_basis(self):
        result = public_report(self.store, route=domain.ROUTE_SKIN)
        self.store.add_action_note(
            result["incident_id"], "指导清水冲洗15分钟",
            basis="R10 非食入接触处置口径", role="dispatcher")
        view = self.store.incident_view(result["incident_id"], role="dispatcher")
        action = [e for e in view["timeline"] if e["kind"] == "action"][0]
        self.assertEqual(action["basis"], "R10 非食入接触处置口径")

    # -------------------------------------------------- 重启还原
    def test_state_restored_after_restart(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = DomainStore(database=path, clock=self.clock)
            result = public_report(store)
            store.add_symptom(result["incident_id"], {"code": "gi_relief"},
                              role="medical", actor="doc-li")
            store.record_handoff(
                result["incident_id"],
                {"facility": "市一院急诊", "staff": "王医生"},
                role="medical", actor="doc-li")
            expected = store.incident_view(result["incident_id"],
                                           role="dispatcher")
            store.close()

            reopened = DomainStore(database=path, clock=self.clock)
            view = reopened.incident_view(result["incident_id"],
                                          role="dispatcher")
            self.assertEqual(view["risk_level"], "critical")
            self.assertIn("R4", view["risk_basis"])
            self.assertEqual(view["handoffs"][0]["facility"], "市一院急诊")
            self.assertGreaterEqual(len(view["pending_follow_ups"]), 4)
            self.assertTrue(all(f["basis"].startswith("R4")
                                for f in view["pending_follow_ups"]
                                if f["kind"] != "urgent_callback"
                                or f["basis"].startswith("R4")))
            self.assertEqual(view["version"], expected["version"])
            self.assertIn("risk_escalated",
                          [e["kind"] for e in view["timeline"]])
            reopened.close()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
