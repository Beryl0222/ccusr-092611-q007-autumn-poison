"""中毒事件服务：报案受理、症状时间线、医疗交接、合并回滚与自动升级。

所有写入在 SQLite 事务内完成，每次处置连同依据写入事件表；
服务重启后值班人员可按事件编号还原风险级别、待办随访与处置历史。
"""
import json,sqlite3,uuid
from contextlib import contextmanager
from datetime import datetime,timedelta
from .domain import utc_now
from .service import ServiceError
from .incident import(SAMPLE_TYPES,EXPOSURE_ROUTES,RISK_ORDER,INITIAL_RISK,HIGH_RISK_KEYWORDS,GI_KEYWORDS,RELIEF_KEYWORDS,FALSE_RECOVERY_SAMPLES,IDENTITY_ROLES,ROLE_RIGHTS)
SCHEMA="""PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS incidents(incident_no TEXT PRIMARY KEY,status TEXT NOT NULL,risk_level TEXT NOT NULL,sample_type TEXT NOT NULL,exposure_route TEXT NOT NULL,merged_into TEXT,version INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports(report_id TEXT PRIMARY KEY,incident_no TEXT NOT NULL,channel TEXT NOT NULL,anonymous INTEGER NOT NULL,reporter_name TEXT,reporter_phone TEXT,sample_type TEXT NOT NULL,exposure_route TEXT NOT NULL,narrative TEXT NOT NULL,actor_role TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(incident_no) REFERENCES incidents(incident_no));
CREATE TABLE IF NOT EXISTS symptoms(entry_id TEXT PRIMARY KEY,incident_no TEXT NOT NULL,observed_at TEXT NOT NULL,description TEXT NOT NULL,improving INTEGER NOT NULL,actor TEXT NOT NULL,actor_role TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(incident_no) REFERENCES incidents(incident_no));
CREATE TABLE IF NOT EXISTS handoffs(handoff_id TEXT PRIMARY KEY,incident_no TEXT NOT NULL,from_party TEXT NOT NULL,to_party TEXT NOT NULL,note TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(incident_no) REFERENCES incidents(incident_no));
CREATE TABLE IF NOT EXISTS followups(followup_id TEXT PRIMARY KEY,incident_no TEXT NOT NULL,kind TEXT NOT NULL,due_at TEXT NOT NULL,status TEXT NOT NULL,rationale TEXT NOT NULL,created_at TEXT NOT NULL,done_at TEXT,FOREIGN KEY(incident_no) REFERENCES incidents(incident_no));
CREATE TABLE IF NOT EXISTS merges(merge_id TEXT PRIMARY KEY,primary_no TEXT NOT NULL,secondary_no TEXT NOT NULL,prev_status TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,rolled_back_at TEXT,rollback_reason TEXT);
CREATE TABLE IF NOT EXISTS incident_events(event_id TEXT PRIMARY KEY,incident_no TEXT NOT NULL,kind TEXT NOT NULL,actor TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(incident_no) REFERENCES incidents(incident_no));
CREATE TABLE IF NOT EXISTS incident_idempotency(request_key TEXT PRIMARY KEY,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters(key TEXT PRIMARY KEY,value INTEGER NOT NULL);"""
def _new_id(prefix):return prefix+"-"+uuid.uuid4().hex[:12]
class IncidentService:
 def __init__(self,database=":memory:",clock=utc_now):
  self.connection=sqlite3.connect(database);self.connection.row_factory=sqlite3.Row;self.clock=clock;self.connection.executescript(SCHEMA);self.connection.commit()
 def close(self):self.connection.close()
 @contextmanager
 def transaction(self):
  try:self.connection.execute("BEGIN IMMEDIATE");yield;self.connection.commit()
  except Exception:self.connection.rollback();raise
 # --- 权限与幂等 ---
 def _require(self,role,right):
  if right not in ROLE_RIGHTS.get(role,set()):raise ServiceError("角色 %s 无权执行该操作"%role)
 def _cached(self,request_key):
  if not request_key:return None
  row=self.connection.execute("SELECT result FROM incident_idempotency WHERE request_key=?",(request_key,)).fetchone()
  return json.loads(row["result"]) if row else None
 def _remember(self,request_key,result):
  if request_key:self.connection.execute("INSERT OR IGNORE INTO incident_idempotency VALUES(?,?)",(request_key,json.dumps(result)))
 # --- 基础读写 ---
 def _row(self,incident_no):
  row=self.connection.execute("SELECT * FROM incidents WHERE incident_no=?",(incident_no,)).fetchone()
  if row is None:raise ServiceError("事件不存在")
  return row
 def _active(self,incident_no):
  row=self._row(incident_no)
  if row["merged_into"]:raise ServiceError("事件已合并至 %s，请在主事件上操作"%row["merged_into"])
  return row
 def _event(self,incident_no,kind,actor,body):
  self.connection.execute("INSERT INTO incident_events VALUES(?,?,?,?,?,?)",(_new_id("evt"),incident_no,kind,actor,json.dumps(body,ensure_ascii=False),self.clock()))
 def _next_incident_no(self):
  day=self.clock()[:10].replace("-","");key="incident:"+day
  row=self.connection.execute("SELECT value FROM counters WHERE key=?",(key,)).fetchone()
  value=(row["value"] if row else 0)+1
  self.connection.execute("INSERT INTO counters(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
  return "INC-%s-%04d"%(day,value)
 def _after(self,hours):return (datetime.fromisoformat(self.clock())+timedelta(hours=hours)).isoformat()
 def _bump(self,incident,**fields):
  sets="version=?,updated_at=?"+"".join(",%s=?"%k for k in fields)
  self.connection.execute("UPDATE incidents SET %s WHERE incident_no=?"%sets,(incident["version"]+1,self.clock(),*fields.values(),incident["incident_no"]))
 # --- 报案 ---
 def file_report(self,channel,sample_type,exposure_route,narrative,reporter_name=None,reporter_phone=None,actor="anonymous",role="public",request_key=None):
  """受理报案：公众可匿名，身份信息仅登记不公开；返回新事件编号。"""
  self._require(role,"report")
  if sample_type not in SAMPLE_TYPES:raise ServiceError("未知样本类型")
  if exposure_route not in EXPOSURE_ROUTES:raise ServiceError("未知接触途径")
  cached=self._cached(request_key)
  if cached:return cached
  with self.transaction():
   incident_no=self._next_incident_no();now=self.clock();risk=INITIAL_RISK.get(sample_type,"low")
   self.connection.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?)",(incident_no,"reported",risk,sample_type,exposure_route,None,1,now,now))
   report_id=_new_id("rep");anonymous=0 if (reporter_name or reporter_phone) else 1
   self.connection.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?)",(report_id,incident_no,channel,anonymous,reporter_name,reporter_phone,sample_type,exposure_route,narrative,role,now))
   self._event(incident_no,"report_filed",actor,{"report_id":report_id,"channel":channel,"rationale":"初始风险 %s：样本 %s，接触途径 %s"%(risk,sample_type,exposure_route)})
   result={"incident_no":incident_no,"report_id":report_id,"risk_level":risk}
   self._remember(request_key,result);return result
 # --- 症状时间线与自动升级 ---
 def add_symptom(self,incident_no,observed_at,description,improving=False,actor=None,role="medic",request_key=None):
  """追加症状记录；命中高危症状或假愈期特征时自动升级并生成随访。"""
  self._require(role,"symptom")
  cached=self._cached(request_key)
  if cached:return cached
  with self.transaction():
   incident=self._active(incident_no);entry_id=_new_id("sym");now=self.clock()
   improving=bool(improving) or any(k in description for k in RELIEF_KEYWORDS)
   self.connection.execute("INSERT INTO symptoms VALUES(?,?,?,?,?,?,?,?)",(entry_id,incident_no,observed_at,description,1 if improving else 0,actor,role,now))
   self._event(incident_no,"symptom_added",actor,{"entry_id":entry_id,"description":description,"improving":improving})
   escalations=self._evaluate(incident,description,improving,actor)
   result={"entry_id":entry_id,"incident_no":incident_no,"escalations":escalations}
   self._remember(request_key,result);return result
 def _evaluate(self,incident,description,improving,actor):
  actions=[]
  hits=[k for k in HIGH_RISK_KEYWORDS if k in description]
  if hits:
   target="critical" if len(hits)>=2 else "high"
   rationale="出现高危症状："+"、".join(hits)
   if self._escalate(incident,target,rationale,actor):actions.append({"risk_level":target,"rationale":rationale})
   self._ensure_followup(incident["incident_no"],"confirm_handoff",self._after(2),"高危症状需在两小时内确认医疗交接")
   incident=self._row(incident["incident_no"])
  if improving and incident["sample_type"] in FALSE_RECOVERY_SAMPLES:
   prior="".join(r["description"] for r in self.connection.execute("SELECT description FROM symptoms WHERE incident_no=?",(incident["incident_no"],)))
   if any(k in prior for k in GI_KEYWORDS):
    rationale="剧烈胃肠症状后暂时缓解，符合假愈期特征，须继续追踪迟发肝损伤"
    if self._escalate(incident,"high",rationale,actor):actions.append({"risk_level":"high","rationale":rationale})
    for hours in (24,48,72):self._ensure_followup(incident["incident_no"],"liver_recheck",self._after(hours),"假愈期监测：+%d小时复查肝功能与凝血"%hours)
  return actions
 def _escalate(self,incident,target,rationale,actor):
  """风险级别只升不降；返回是否发生变化。"""
  if RISK_ORDER.index(target)<=RISK_ORDER.index(incident["risk_level"]):return False
  status="monitoring" if incident["status"]=="reported" else incident["status"]
  self._bump(incident,risk_level=target,status=status)
  self._event(incident["incident_no"],"escalated",actor,{"from":incident["risk_level"],"to":target,"rationale":rationale})
  return True
 def _ensure_followup(self,incident_no,kind,due_at,rationale):
  row=self.connection.execute("SELECT 1 FROM followups WHERE incident_no=? AND kind=? AND due_at=? AND status='pending'",(incident_no,kind,due_at)).fetchone()
  if row:return
  self.connection.execute("INSERT INTO followups VALUES(?,?,?,?,?,?,?,?)",(_new_id("fu"),incident_no,kind,due_at,"pending",rationale,self.clock(),None))
 # --- 医疗交接与咨询建议 ---
 def record_handoff(self,incident_no,from_party,to_party,note,actor,role="medic",request_key=None):
  self._require(role,"handoff")
  cached=self._cached(request_key)
  if cached:return cached
  with self.transaction():
   incident=self._active(incident_no);handoff_id=_new_id("ho")
   self.connection.execute("INSERT INTO handoffs VALUES(?,?,?,?,?,?,?)",(handoff_id,incident_no,from_party,to_party,note,actor,self.clock()))
   self._bump(incident,status="handed_off")
   self._event(incident_no,"handoff",actor,{"handoff_id":handoff_id,"from":from_party,"to":to_party,"rationale":note})
   result={"handoff_id":handoff_id,"incident_no":incident_no}
   self._remember(request_key,result);return result
 def record_advice(self,incident_no,advice,actor,role="consultant",request_key=None):
  """咨询人员按样本来源与接触途径给出处置建议，依据留痕。"""
  self._require(role,"advise")
  cached=self._cached(request_key)
  if cached:return cached
  with self.transaction():
   incident=self._active(incident_no)
   self._event(incident_no,"advice",actor,{"advice":advice,"rationale":"依据样本 %s 与接触途径 %s 给出"%(incident["sample_type"],incident["exposure_route"])})
   result={"incident_no":incident_no,"advice":advice}
   self._remember(request_key,result);return result
 # --- 合并与回滚 ---
 def merge_incidents(self,primary_no,secondary_no,actor,reason,role="duty",request_key=None):
  """多渠道重复上报合并：证据保留在原事件上，主事件视图聚合展示。"""
  self._require(role,"merge")
  cached=self._cached(request_key)
  if cached:return cached
  with self.transaction():
   primary=self._active(primary_no);secondary=self._active(secondary_no)
   if primary_no==secondary_no:raise ServiceError("不能与自身合并")
   merge_id=_new_id("mrg")
   self.connection.execute("INSERT INTO merges VALUES(?,?,?,?,?,?,?,?,?)",(merge_id,primary_no,secondary_no,secondary["status"],actor,reason,self.clock(),None,None))
   self._bump(secondary,status="merged",merged_into=primary_no)
   self._event(primary_no,"merged",actor,{"merge_id":merge_id,"secondary_no":secondary_no,"rationale":reason})
   self._event(secondary_no,"merged_away",actor,{"merge_id":merge_id,"primary_no":primary_no,"rationale":reason})
   result={"merge_id":merge_id,"primary_no":primary_no,"secondary_no":secondary_no}
   self._remember(request_key,result);return result
 def rollback_merge(self,merge_id,actor,reason,role="duty"):
  """回滚错误合并：恢复被合并事件状态，合并记录保留备查。"""
  self._require(role,"rollback")
  with self.transaction():
   merge=self.connection.execute("SELECT * FROM merges WHERE merge_id=?",(merge_id,)).fetchone()
   if merge is None:raise ServiceError("合并记录不存在")
   if merge["rolled_back_at"]:raise ServiceError("该合并已回滚")
   secondary=self._row(merge["secondary_no"])
   self.connection.execute("UPDATE merges SET rolled_back_at=?,rollback_reason=? WHERE merge_id=?",(self.clock(),reason,merge_id))
   self._bump(secondary,status=merge["prev_status"],merged_into=None)
   self._event(merge["primary_no"],"merge_rolled_back",actor,{"merge_id":merge_id,"secondary_no":merge["secondary_no"],"rationale":reason})
   self._event(merge["secondary_no"],"restored",actor,{"merge_id":merge_id,"rationale":reason})
   return {"merge_id":merge_id,"restored":merge["secondary_no"]}
 # --- 随访 ---
 def complete_followup(self,followup_id,actor,role="duty"):
  self._require(role,"followup")
  with self.transaction():
   row=self.connection.execute("SELECT * FROM followups WHERE followup_id=?",(followup_id,)).fetchone()
   if row is None:raise ServiceError("随访不存在")
   if row["status"]!="pending":raise ServiceError("随访已办结")
   self.connection.execute("UPDATE followups SET status='done',done_at=? WHERE followup_id=?",(self.clock(),followup_id))
   self._event(row["incident_no"],"followup_done",actor,{"followup_id":followup_id,"kind":row["kind"],"rationale":row["rationale"]})
   return {"followup_id":followup_id,"status":"done"}
 # --- 查询与还原 ---
 def _evidence_nos(self,incident_no):
  rows=self.connection.execute("SELECT secondary_no FROM merges WHERE primary_no=? AND rolled_back_at IS NULL",(incident_no,)).fetchall()
  return [incident_no]+[r["secondary_no"] for r in rows]
 def _mask(self,report,role):
  d=dict(report)
  if role not in IDENTITY_ROLES:
   d["reporter_name"]="***" if d["reporter_name"] else None
   d["reporter_phone"]="***" if d["reporter_phone"] else None
  return d
 def get_incident(self,incident_no,role="public"):
  """事件详情；敏感身份信息仅获准角色可见，合并进来的证据一并展示。"""
  incident=self._row(incident_no);nos=self._evidence_nos(incident_no)
  marks=",".join("?"*len(nos))
  reports=[self._mask(r,role) for r in self.connection.execute("SELECT * FROM reports WHERE incident_no IN (%s) ORDER BY created_at"%marks,nos)]
  symptoms=[dict(r) for r in self.connection.execute("SELECT * FROM symptoms WHERE incident_no IN (%s) ORDER BY observed_at"%marks,nos)]
  handoffs=[dict(r) for r in self.connection.execute("SELECT * FROM handoffs WHERE incident_no IN (%s) ORDER BY created_at"%marks,nos)]
  return {"incident_no":incident_no,"status":incident["status"],"risk_level":incident["risk_level"],"sample_type":incident["sample_type"],"exposure_route":incident["exposure_route"],"merged_into":incident["merged_into"],"version":incident["version"],"reports":reports,"symptoms":symptoms,"handoffs":handoffs}
 def briefing(self,incident_no):
  """值班还原：当前风险级别、待办随访与每次处置依据，重启后同样可用。"""
  incident=self._row(incident_no);nos=self._evidence_nos(incident_no)
  marks=",".join("?"*len(nos))
  followups=[dict(r) for r in self.connection.execute("SELECT followup_id,kind,due_at,rationale FROM followups WHERE incident_no IN (%s) AND status='pending' ORDER BY due_at"%marks,nos)]
  events=[{"created_at":r["created_at"],"kind":r["kind"],"actor":r["actor"],**json.loads(r["body"])} for r in self.connection.execute("SELECT * FROM incident_events WHERE incident_no IN (%s) ORDER BY created_at"%marks,nos)]
  return {"incident_no":incident_no,"status":incident["status"],"risk_level":incident["risk_level"],"pending_followups":followups,"dispositions":events}
