import os,tempfile,unittest
from autumn_poison.service import ServiceError
from autumn_poison.incident_service import IncidentService
class IncidentTests(unittest.TestCase):
 def setUp(self):self.svc=IncidentService()
 def tearDown(self):self.svc.close()
 def _report(self,sample="mushroom",route="ingestion",**kw):
  args=dict(channel="hotline",sample_type=sample,exposure_route=route,narrative="公园误食");args.update(kw)
  return self.svc.file_report(**args)
 def test_anonymous_and_identity_permission(self):
  anon=self._report()
  named=self._report(reporter_name="张三",reporter_phone="13800000000",role="medic",actor="m1")
  public_view=self.svc.get_incident(named["incident_no"],"consultant")
  self.assertEqual(public_view["reports"][0]["reporter_name"],"***")
  medic_view=self.svc.get_incident(named["incident_no"],"medic")
  self.assertEqual(medic_view["reports"][0]["reporter_phone"],"13800000000")
  self.assertEqual(self.svc.get_incident(anon["incident_no"],"public")["reports"][0]["anonymous"],1)
 def test_role_rights(self):
  no=self._report()["incident_no"]
  with self.assertRaises(ServiceError):self.svc.add_symptom(no,"2026-09-26T10:00:00+00:00","呕吐",role="public")
  with self.assertRaises(ServiceError):self.svc.merge_incidents(no,no,"u","重复",role="medic")
  with self.assertRaises(ServiceError):self.svc.record_handoff(no,"现场","医院","",actor="c1",role="consultant")
 def test_high_risk_symptom_escalates(self):
  no=self._report()["incident_no"]
  r=self.svc.add_symptom(no,"2026-09-26T10:00:00+00:00","出现抽搐与意识障碍",actor="m1")
  self.assertEqual(r["escalations"][0]["risk_level"],"critical")
  brief=self.svc.briefing(no)
  self.assertEqual(brief["risk_level"],"critical")
  self.assertTrue(any(f["kind"]=="confirm_handoff" for f in brief["pending_followups"]))
  self.assertTrue(any("高危症状" in d.get("rationale","") for d in brief["dispositions"]))
 def test_false_recovery_escalates_and_schedules(self):
  no=self._report()["incident_no"]
  self.svc.add_symptom(no,"2026-09-26T08:00:00+00:00","剧烈呕吐腹泻",actor="m1")
  r=self.svc.add_symptom(no,"2026-09-27T08:00:00+00:00","症状缓解，精神好转",actor="m1")
  self.assertEqual(r["escalations"][0]["risk_level"],"high")
  brief=self.svc.briefing(no)
  self.assertEqual([f["kind"] for f in brief["pending_followups"]],["liver_recheck"]*3)
  self.assertTrue(any("假愈期" in d.get("rationale","") for d in brief["dispositions"]))
 def test_no_false_recovery_without_gi_history(self):
  no=self._report(sample="ginkgo")["incident_no"]
  r=self.svc.add_symptom(no,"2026-09-26T10:00:00+00:00","症状好转",actor="m1")
  self.assertEqual(r["escalations"],[])
  self.assertEqual(self.svc.briefing(no)["risk_level"],"low")
 def test_merge_keeps_evidence_and_rollback(self):
  a=self._report(channel="hotline")["incident_no"]
  b=self._report(channel="ranger",reporter_name="李四",reporter_phone="13900000000",role="medic",actor="m1")["incident_no"]
  self.svc.add_symptom(b,"2026-09-26T09:00:00+00:00","呕吐",actor="m1")
  merge=self.svc.merge_incidents(a,b,"duty1","同一游客多渠道上报")
  view=self.svc.get_incident(a,"duty")
  self.assertEqual(len(view["reports"]),2)
  self.assertEqual(len(view["symptoms"]),1)
  with self.assertRaises(ServiceError):self.svc.add_symptom(b,"2026-09-26T10:00:00+00:00","腹痛",actor="m1")
  self.svc.rollback_merge(merge["merge_id"],"duty1","核实为不同游客")
  restored=self.svc.get_incident(b,"medic")
  self.assertEqual(restored["status"],"reported")
  self.assertEqual(len(self.svc.get_incident(a,"duty")["reports"]),1)
  with self.assertRaises(ServiceError):self.svc.rollback_merge(merge["merge_id"],"duty1","重复回滚")
 def test_handoff_and_followup_completion(self):
  no=self._report()["incident_no"]
  self.svc.add_symptom(no,"2026-09-26T10:00:00+00:00","抽搐",actor="m1")
  self.svc.record_handoff(no,"公园医务点","市中毒救治中心","已交接洗胃记录",actor="m1")
  self.assertEqual(self.svc.get_incident(no)["status"],"handed_off")
  fu=self.svc.briefing(no)["pending_followups"][0]
  self.svc.complete_followup(fu["followup_id"],"duty1")
  self.assertEqual(self.svc.briefing(no)["pending_followups"],[])
 def test_idempotent_requests(self):
  a=self.svc.file_report("hotline","lycoris","ingestion","误食鳞茎",request_key="req-1")
  b=self.svc.file_report("hotline","lycoris","ingestion","误食鳞茎",request_key="req-1")
  self.assertEqual(a,b)
  self.assertEqual(len(self.svc.get_incident(a["incident_no"])["reports"]),1)
 def test_restart_briefing(self):
  fd,path=tempfile.mkstemp(suffix=".db");os.close(fd)
  try:
   svc=IncidentService(path)
   no=svc.file_report("hotline","mushroom","ingestion","误食野生蘑菇")["incident_no"]
   svc.add_symptom(no,"2026-09-26T08:00:00+00:00","呕吐腹泻",actor="m1")
   svc.add_symptom(no,"2026-09-27T08:00:00+00:00","症状缓解",actor="m1")
   svc.close()
   reopened=IncidentService(path)
   brief=reopened.briefing(no)
   self.assertEqual(brief["risk_level"],"high")
   self.assertEqual(len(brief["pending_followups"]),3)
   self.assertTrue(any(d["kind"]=="escalated" for d in brief["dispositions"]))
   reopened.close()
  finally:os.unlink(path)
if __name__=="__main__":unittest.main()
