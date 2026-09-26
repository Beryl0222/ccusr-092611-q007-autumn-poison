"""秋季误采处置协同的持久化边界与服务。

两类数据并存：
1) 旧的通用记录 records/events/idempotency（create/get/transition），
   保持既有状态机、版本与请求键幂等语义；
2) 中毒事件域：incidents/reports/samples/symptoms/handoffs/
   follow_ups/audit/merges。

关键约定：
- 所有写入走单条 SQLite 事务（BEGIN IMMEDIATE），失败整体回滚；
- 风险等级只由规则引擎自动上调，人工降级必须给出依据；
- 合并只重挂归属、不删除任何证据行，并记录 moved_refs 以便回滚；
- 敏感身份字段按角色脱敏；
- 全部状态落库，重新打开同一数据库文件即可还原值班视图。
"""
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from . import domain
from .domain import (
    RISK_RANK,
    SAMPLE_TYPES,
    CONTACT_ROUTES,
    SYMPTOM_CATALOG,
    advice_for,
    utc_now,
)

# 允许查看敏感身份信息的角色。
PII_ROLES = frozenset({"dispatcher", "medical", "admin"})
# 可补充病情 / 交接的授权角色。
CLINICAL_ROLES = frozenset({"medical", "dispatcher", "admin"})
# 可执行合并、回滚、人工降级的角色。
OPERATOR_ROLES = frozenset({"dispatcher", "admin"})


class ServiceError(Exception):
    """业务错误；code 供 HTTP 边界映射状态码。"""

    def __init__(self, message, code="validation"):
        super().__init__(message)
        self.code = code


def _new_id(prefix):
    return prefix + "-" + uuid.uuid4().hex[:10]


def _dedupe_key(hint):
    """根据报案人/患者/样本/时间/地点等线索生成稳定去重键。"""
    if not hint:
        return None
    canonical = json.dumps(hint, sort_keys=True, ensure_ascii=False)
    return "dk-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS records(
  record_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  state TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(record_id) REFERENCES records(record_id));
CREATE TABLE IF NOT EXISTS idempotency(
  request_key TEXT PRIMARY KEY,
  result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents(
  incident_id TEXT PRIMARY KEY,
  risk_level TEXT NOT NULL,
  status TEXT NOT NULL,
  version INTEGER NOT NULL,
  risk_basis TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  merged_into TEXT);
CREATE TABLE IF NOT EXISTS reports(
  report_id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'walk_in',
  reporter_kind TEXT NOT NULL DEFAULT 'public',
  reporter_name TEXT, reporter_contact TEXT,
  patient_name TEXT, patient_contact TEXT,
  location TEXT, route TEXT, ingestion_time TEXT,
  quantity TEXT, note TEXT,
  request_key TEXT UNIQUE, dedupe_key TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(incident_id) REFERENCES incidents(incident_id));
CREATE INDEX IF NOT EXISTS idx_reports_dedupe ON reports(dedupe_key);
CREATE TABLE IF NOT EXISTS samples(
  sample_id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL,
  report_id TEXT NOT NULL,
  sample_type TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'unknown',
  description TEXT, photo_ref TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(incident_id) REFERENCES incidents(incident_id),
  FOREIGN KEY(report_id) REFERENCES reports(report_id));
CREATE TABLE IF NOT EXISTS symptoms(
  symptom_id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL,
  report_id TEXT,
  code TEXT NOT NULL,
  onset_at TEXT, observed_at TEXT NOT NULL,
  note TEXT, recorded_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(incident_id) REFERENCES incidents(incident_id));
CREATE TABLE IF NOT EXISTS handoffs(
  handoff_id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL,
  facility TEXT NOT NULL,
  staff TEXT, summary TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(incident_id) REFERENCES incidents(incident_id));
CREATE TABLE IF NOT EXISTS follow_ups(
  fu_id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  due_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  basis TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  assignee_role TEXT NOT NULL DEFAULT 'dispatcher',
  created_at TEXT NOT NULL,
  completed_at TEXT, completed_by TEXT,
  UNIQUE(incident_id, dedupe_key),
  FOREIGN KEY(incident_id) REFERENCES incidents(incident_id));
CREATE TABLE IF NOT EXISTS audit_events(
  event_id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  basis TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(incident_id) REFERENCES incidents(incident_id));
CREATE TABLE IF NOT EXISTS merges(
  merge_id TEXT PRIMARY KEY,
  survivor_id TEXT NOT NULL,
  absorbed_id TEXT NOT NULL,
  moved_refs TEXT NOT NULL DEFAULT '{}',
  undone INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  undone_by TEXT, undone_at TEXT);
"""

# 合并时需要重挂归属的子表：(表名, 主键列)。
_MERGE_CHILDREN = (
    ("reports", "report_id"),
    ("samples", "sample_id"),
    ("symptoms", "symptom_id"),
    ("handoffs", "handoff_id"),
    ("follow_ups", "fu_id"),
    ("audit_events", "event_id"),
)


class DomainStore:
    def __init__(self, database=":memory:", clock=utc_now):
        # check_same_thread=False：HTTP 边界为多线程服务，写事务由 self._lock 串行化。
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.clock = clock
        self._lock = threading.RLock()
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    # ------------------------------------------------------------ 事务边界
    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def _audit(self, conn, incident_id, kind, actor="", basis="", body=None):
        conn.execute(
            "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
            (_new_id("EV"), incident_id, kind, actor, basis,
             json.dumps(body or {}, ensure_ascii=False), self.clock()),
        )

    # ------------------------------------------------------------ 旧记录服务
    def create(self, record_id, owner_id, payload=None):
        with self.transaction():
            now = self.clock()
            self.connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?)",
                (record_id, owner_id, "draft", 1,
                 json.dumps(payload or {}, ensure_ascii=False), now))
            self.connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?)",
                (record_id + ":created", record_id, "created", "{}", now))
        return self.get(record_id)

    def get(self, record_id):
        row = self.connection.execute(
            "SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise ServiceError("记录不存在", "not_found")
        from .domain import Record
        return Record(row["record_id"], row["owner_id"], row["state"],
                      row["version"], row["updated_at"])

    def transition(self, record_id, owner_id, target, request_key, expected_version=None):
        with self.transaction():
            conn = self.connection
            old = conn.execute(
                "SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
            if old is None:
                raise ServiceError("记录不存在", "not_found")
            if old["owner_id"] != owner_id:
                raise ServiceError("无权操作", "forbidden")
            cached = conn.execute(
                "SELECT result FROM idempotency WHERE request_key=?",
                (request_key,)).fetchone()
            if cached:
                return json.loads(cached["result"])
            if expected_version is not None and old["version"] != expected_version:
                raise ServiceError("版本冲突", "conflict")
            allowed = {"draft": {"pending"},
                       "pending": {"approved", "cancelled"},
                       "approved": {"closed"},
                       "cancelled": set(), "closed": set()}
            if target not in allowed.get(old["state"], set()):
                raise ServiceError("状态迁移不允许", "conflict")
            version = old["version"] + 1
            now = self.clock()
            conn.execute(
                "UPDATE records SET state=?,version=?,updated_at=? WHERE record_id=?",
                (target, version, now, record_id))
            body = json.dumps({"from": old["state"], "to": target,
                               "version": version}, ensure_ascii=False)
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                         (request_key + ":event", record_id, "transition", body, now))
            result = {"record_id": record_id, "state": target, "version": version}
            conn.execute("INSERT INTO idempotency VALUES(?,?)",
                         (request_key, json.dumps(result, ensure_ascii=False)))
            return result

    # ------------------------------------------------------------ 报案接入
    def report_incident(self, data, role="public", actor="", request_key=None):
        """公众匿名报案 / 医护补充报案的统一入口。

        data 字段：sample_type/source/route/symptoms/ingestion_time/location/
        quantity/note/reporter{name,contact}/patient{name,contact}/channel/
        incident_id（医护对已知事件补充时传入）/dedupe。
        返回 {incident_id, report_id, merged_existing, risk_level, advice}。
        """
        data = dict(data or {})
        request_key = request_key or data.get("request_key")
        with self.transaction():
            conn = self.connection
            if request_key:
                cached = conn.execute(
                    "SELECT result FROM idempotency WHERE request_key=?",
                    (request_key,)).fetchone()
                if cached:
                    return json.loads(cached["result"])

            sample_type = data.get("sample_type", domain.SAMPLE_UNKNOWN)
            route = data.get("route", domain.ROUTE_UNKNOWN)
            source = data.get("source", domain.SOURCE_UNKNOWN)
            self._validate_vocab(sample_type, route, source, data.get("symptoms"))

            now = self.clock()
            target_incident = data.get("incident_id")
            merged_existing = False
            dkey = _dedupe_key(data.get("dedupe"))

            if target_incident:
                # 医护 / 咨询员对既有事件补充。
                if role not in CLINICAL_ROLES:
                    raise ServiceError("无权对既有事件补充", "forbidden")
                inc = self._fetch_incident(conn, target_incident)
                if inc["merged_into"]:
                    raise ServiceError("事件已被合并，请改挂到现存事件", "conflict")
            else:
                inc = self._match_open_incident(conn, dkey) if dkey else None
                if inc is not None:
                    target_incident = inc["incident_id"]
                    merged_existing = True
                else:
                    target_incident = data.get("new_incident_id") or _new_id("INC")
                    conn.execute(
                        "INSERT INTO incidents(incident_id,risk_level,status,version,"
                        "risk_basis,created_by,created_at,updated_at,merged_into) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (target_incident, "low", "open", 1, "", actor or role,
                         now, now, None))
                    self._audit(conn, target_incident, "created", actor or role,
                                body={"channel": data.get("channel", "walk_in")})

            reporter = data.get("reporter") or {}
            patient = data.get("patient") or {}
            report_id = data.get("report_id") or _new_id("RPT")
            conn.execute(
                "INSERT INTO reports(report_id,incident_id,channel,reporter_kind,"
                "reporter_name,reporter_contact,patient_name,patient_contact,"
                "location,route,ingestion_time,quantity,note,request_key,"
                "dedupe_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (report_id, target_incident, data.get("channel", "walk_in"),
                 data.get("reporter_kind", role),
                 reporter.get("name"), reporter.get("contact"),
                 patient.get("name"), patient.get("contact"),
                 data.get("location"), route, data.get("ingestion_time"),
                 data.get("quantity"), data.get("note"),
                 request_key, dkey, now))

            sample_id = _new_id("SMP")
            conn.execute(
                "INSERT INTO samples(sample_id,incident_id,report_id,sample_type,"
                "source,description,photo_ref,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (sample_id, target_incident, report_id, sample_type, source,
                 data.get("sample_description"), data.get("photo_ref"), now))

            symptom_ids = []
            for item in data.get("symptoms") or []:
                sid = self._insert_symptom(conn, target_incident, report_id,
                                           item, role, actor, now)
                symptom_ids.append(sid)

            self._audit(conn, target_incident,
                        "medical_supplement" if role == "medical" else "report_added",
                        actor or role,
                        body={"report_id": report_id,
                              "channel": data.get("channel", "walk_in"),
                              "auto_attached": merged_existing})
            escalation = self._reevaluate(conn, target_incident, actor or role)
            result = {
                "incident_id": target_incident,
                "report_id": report_id,
                "sample_id": sample_id,
                "symptom_ids": symptom_ids,
                "merged_existing": merged_existing,
                "risk_level": escalation["level"],
                "risk_basis": escalation["basis"],
                "advice": advice_for(sample_type, route, source),
            }
            if request_key:
                conn.execute("INSERT OR REPLACE INTO idempotency VALUES(?,?)",
                             (request_key, json.dumps(result, ensure_ascii=False)))
            return result

    def add_symptom(self, incident_id, item, role, actor="", request_key=None):
        """授权医护 / 咨询员追加症状（含“胃肠症状缓解”标记，触发假愈期升级）。"""
        if role not in CLINICAL_ROLES:
            raise ServiceError("无权补充症状", "forbidden")
        with self.transaction():
            conn = self.connection
            inc = self._fetch_incident(conn, incident_id)
            if inc["merged_into"]:
                raise ServiceError("事件已被合并", "conflict")
            if request_key:
                cached = conn.execute(
                    "SELECT result FROM idempotency WHERE request_key=?",
                    (request_key,)).fetchone()
                if cached:
                    return json.loads(cached["result"])
            code = item.get("code")
            if code not in SYMPTOM_CATALOG:
                raise ServiceError("未知症状代码：%s" % code)
            now = self.clock()
            sid = self._insert_symptom(conn, incident_id, None, item, role, actor, now)
            self._audit(conn, incident_id, "symptom_recorded", actor or role,
                        body={"symptom_id": sid, "code": code})
            escalation = self._reevaluate(conn, incident_id, actor or role)
            result = {"symptom_id": sid, "risk_level": escalation["level"],
                      "risk_basis": escalation["basis"]}
            if request_key:
                conn.execute("INSERT OR REPLACE INTO idempotency VALUES(?,?)",
                             (request_key, json.dumps(result, ensure_ascii=False)))
            return result

    def record_handoff(self, incident_id, data, role, actor=""):
        """记录医疗交接（急救/急诊/住院等）。"""
        if role not in CLINICAL_ROLES:
            raise ServiceError("无权记录医疗交接", "forbidden")
        facility = (data or {}).get("facility")
        if not facility:
            raise ServiceError("交接必须注明接收医疗机构")
        with self.transaction():
            conn = self.connection
            inc = self._fetch_incident(conn, incident_id)
            now = self.clock()
            hid = _new_id("HO")
            conn.execute(
                "INSERT INTO handoffs(handoff_id,incident_id,facility,staff,"
                "summary,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (hid, incident_id, facility, data.get("staff"),
                 data.get("summary"), actor or role, now))
            conn.execute(
                "UPDATE incidents SET status='handoff',updated_at=? WHERE incident_id=?",
                (now, incident_id))
            self._audit(conn, incident_id, "handoff", actor or role,
                        basis=data.get("basis", ""),
                        body={"handoff_id": hid, "facility": facility,
                              "staff": data.get("staff")})
            return {"handoff_id": hid, "status": "handoff"}

    # ------------------------------------------------------------ 随访
    def complete_follow_up(self, fu_id, role, actor="", outcome=""):
        if role not in OPERATOR_ROLES and role != "medical":
            raise ServiceError("无权完成随访", "forbidden")
        with self.transaction():
            conn = self.connection
            row = conn.execute(
                "SELECT * FROM follow_ups WHERE fu_id=?", (fu_id,)).fetchone()
            if row is None:
                raise ServiceError("随访不存在", "not_found")
            if row["status"] != "pending":
                raise ServiceError("随访已处理", "conflict")
            now = self.clock()
            conn.execute(
                "UPDATE follow_ups SET status='done',completed_at=?,completed_by=?,"
                "note=? WHERE fu_id=?",
                (now, actor or role,
                 (row["note"] + "｜结果：" + outcome).strip("｜"), fu_id))
            self._audit(conn, row["incident_id"], "followup_done", actor or role,
                        basis=row["basis"], body={"fu_id": fu_id, "outcome": outcome})
            return {"fu_id": fu_id, "status": "done"}

    def cancel_follow_up(self, fu_id, role, actor="", reason=""):
        if role not in OPERATOR_ROLES:
            raise ServiceError("无权取消随访", "forbidden")
        with self.transaction():
            conn = self.connection
            row = conn.execute(
                "SELECT * FROM follow_ups WHERE fu_id=?", (fu_id,)).fetchone()
            if row is None:
                raise ServiceError("随访不存在", "not_found")
            conn.execute(
                "UPDATE follow_ups SET status='cancelled',completed_at=?,"
                "completed_by=? WHERE fu_id=?",
                (self.clock(), actor or role, fu_id))
            self._audit(conn, row["incident_id"], "followup_cancelled",
                        actor or role, body={"fu_id": fu_id, "reason": reason})
            return {"fu_id": fu_id, "status": "cancelled"}

    def list_due_follow_ups(self, role="dispatcher", at=None):
        """值班台全局待办：到期未完成的随访，按事件分组。"""
        if role not in PII_ROLES:
            raise ServiceError("无权查看随访台", "forbidden")
        moment = at or self.clock()
        rows = self.connection.execute(
            "SELECT f.*,i.risk_level FROM follow_ups f JOIN incidents i "
            "ON f.incident_id=i.incident_id WHERE f.status='pending' "
            "AND i.merged_into IS NULL ORDER BY f.due_at", ()).fetchall()
        groups = {}
        for row in rows:
            groups.setdefault(row["incident_id"], {
                "incident_id": row["incident_id"], "risk_level": row["risk_level"],
                "overdue": False, "follow_ups": []})
            entry = {"fu_id": row["fu_id"], "kind": row["kind"],
                     "due_at": row["due_at"], "basis": row["basis"],
                     "note": row["note"], "assignee_role": row["assignee_role"],
                     "overdue": row["due_at"] <= moment}
            groups[row["incident_id"]]["follow_ups"].append(entry)
            groups[row["incident_id"]]["overdue"] |= entry["overdue"]
        return list(groups.values())

    # ------------------------------------------------------------ 人工降级 / 处置记录
    def downgrade_risk(self, incident_id, level, reason, role, actor=""):
        if role not in OPERATOR_ROLES:
            raise ServiceError("无权调整风险级别", "forbidden")
        if level not in RISK_RANK:
            raise ServiceError("未知风险级别")
        if not reason:
            raise ServiceError("人工降级必须写明依据")
        with self.transaction():
            conn = self.connection
            inc = self._fetch_incident(conn, incident_id)
            if RISK_RANK[level] >= RISK_RANK[inc["risk_level"]]:
                raise ServiceError("仅允许向更低级别人工调整", "conflict")
            now = self.clock()
            conn.execute(
                "UPDATE incidents SET risk_level=?,risk_basis=?,version=version+1,"
                "updated_at=? WHERE incident_id=?",
                (level, "人工降级：" + reason, now, incident_id))
            if level not in ("high", "critical"):
                conn.execute(
                    "UPDATE incidents SET status='open',updated_at=? "
                    "WHERE incident_id=? AND status='escalated'",
                    (now, incident_id))
            self._audit(conn, incident_id, "risk_downgraded", actor or role,
                        basis=reason,
                        body={"from": inc["risk_level"], "to": level})
            return {"incident_id": incident_id, "risk_level": level}

    def add_action_note(self, incident_id, note, basis, role, actor=""):
        """记录一次处置动作及其依据（咨询口径、送医建议等）。"""
        if role not in CLINICAL_ROLES:
            raise ServiceError("无权记录处置", "forbidden")
        if not note:
            raise ServiceError("处置内容不能为空")
        with self.transaction():
            conn = self.connection
            self._fetch_incident(conn, incident_id)
            eid = _new_id("EV")
            conn.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (eid, incident_id, "action", actor or role, basis or "",
                 json.dumps({"note": note}, ensure_ascii=False), self.clock()))
            return {"event_id": eid}

    # ------------------------------------------------------------ 合并与回滚
    def merge_incidents(self, survivor_id, absorbed_id, role, actor="", reason=""):
        """把 absorbed 事件并入 survivor；只重挂归属、不删除证据。"""
        if role not in OPERATOR_ROLES:
            raise ServiceError("无权合并事件", "forbidden")
        if survivor_id == absorbed_id:
            raise ServiceError("不能合并事件自身")
        with self.transaction():
            conn = self.connection
            survivor = self._fetch_incident(conn, survivor_id)
            absorbed = self._fetch_incident(conn, absorbed_id)
            if survivor["merged_into"] or absorbed["merged_into"]:
                raise ServiceError("已合并事件不能再次合并", "conflict")
            now = self.clock()
            moved = {}
            for table, key in _MERGE_CHILDREN:
                if table == "follow_ups":
                    # 随访在同事件内按 dedupe_key 唯一；与存留事件撞键的随访
                    # 保留在被合并事件下（证据不丢，值班台不再展示），不强行重挂。
                    existing = {r["dedupe_key"] for r in conn.execute(
                        "SELECT dedupe_key FROM follow_ups WHERE incident_id=?",
                        (survivor_id,)).fetchall()}
                    rows = conn.execute(
                        "SELECT %s,dedupe_key FROM %s WHERE incident_id=?"
                        % (key, table), (absorbed_id,)).fetchall()
                    ids = [r[key] for r in rows if r["dedupe_key"] not in existing]
                else:
                    ids = [r[key] for r in conn.execute(
                        "SELECT %s FROM %s WHERE incident_id=?" % (key, table),
                        (absorbed_id,)).fetchall()]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        "UPDATE %s SET incident_id=? WHERE %s IN (%s)"
                        % (table, key, placeholders),
                        [survivor_id, *ids])
                    moved[table] = ids
            merge_id = _new_id("MRG")
            conn.execute(
                "INSERT INTO merges(merge_id,survivor_id,absorbed_id,moved_refs,"
                "undone,created_by,created_at) VALUES(?,?,?,?,0,?,?)",
                (merge_id, survivor_id, absorbed_id,
                 json.dumps(moved, ensure_ascii=False), actor or role, now))
            conn.execute(
                "UPDATE incidents SET merged_into=?,status='merged',version=version+1,"
                "updated_at=? WHERE incident_id=?",
                (survivor_id, now, absorbed_id))
            conn.execute(
                "UPDATE incidents SET version=version+1,updated_at=? WHERE incident_id=?",
                (now, survivor_id))
            self._audit(conn, survivor_id, "merged", actor or role,
                        basis=reason,
                        body={"merge_id": merge_id, "absorbed_id": absorbed_id})
            self._reevaluate(conn, survivor_id, actor or role)
            return {"merge_id": merge_id, "survivor_id": survivor_id,
                    "absorbed_id": absorbed_id}

    def undo_merge(self, merge_id, role, actor=""):
        """按 merges.moved_refs 精确回滚一次错误合并不影响合并后新增的数据。"""
        if role not in OPERATOR_ROLES:
            raise ServiceError("无权回滚合并", "forbidden")
        with self.transaction():
            conn = self.connection
            row = conn.execute(
                "SELECT * FROM merges WHERE merge_id=?", (merge_id,)).fetchone()
            if row is None:
                raise ServiceError("合并记录不存在", "not_found")
            if row["undone"]:
                raise ServiceError("该合并已回滚", "conflict")
            now = self.clock()
            moved = json.loads(row["moved_refs"] or "{}")
            for table, key in _MERGE_CHILDREN:
                ids = moved.get(table, [])
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        "UPDATE %s SET incident_id=? WHERE %s IN (%s)"
                        % (table, key, placeholders),
                        [row["absorbed_id"], *ids])
            conn.execute(
                "UPDATE merges SET undone=1,undone_by=?,undone_at=? WHERE merge_id=?",
                (actor or role, now, merge_id))
            conn.execute(
                "UPDATE incidents SET merged_into=NULL,status='open',"
                "version=version+1,updated_at=? WHERE incident_id=?",
                (now, row["absorbed_id"]))
            conn.execute(
                "UPDATE incidents SET version=version+1,updated_at=? WHERE incident_id=?",
                (now, row["survivor_id"]))
            self._audit(conn, row["survivor_id"], "merge_undone", actor or role,
                        body={"merge_id": merge_id,
                              "absorbed_id": row["absorbed_id"]})
            self._audit(conn, row["absorbed_id"], "merge_undone", actor or role,
                        body={"merge_id": merge_id,
                              "survivor_id": row["survivor_id"]})
            # 双方各自按现存证据重新评估风险（允许回落，纠正错误合并）。
            self._reevaluate(conn, row["absorbed_id"], actor or role,
                             recompute=True)
            self._reevaluate(conn, row["survivor_id"], actor or role,
                             recompute=True)
            return {"merge_id": merge_id, "undone": True}

    def relocate_report(self, report_id, target_incident_id, role, actor="",
                        reason=""):
        """纠正自动挂接：把单个报告及其样本/症状拆到另一事件（target 为 None 则新建）。"""
        if role not in OPERATOR_ROLES:
            raise ServiceError("无权迁移报案", "forbidden")
        with self.transaction():
            conn = self.connection
            report = conn.execute(
                "SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
            if report is None:
                raise ServiceError("报案不存在", "not_found")
            source_id = report["incident_id"]
            now = self.clock()
            if target_incident_id is None:
                target_incident_id = _new_id("INC")
                conn.execute(
                    "INSERT INTO incidents(incident_id,risk_level,status,version,"
                    "risk_basis,created_by,created_at,updated_at,merged_into) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (target_incident_id, "low", "open", 1, "", actor or role,
                     now, now, None))
                self._audit(conn, target_incident_id, "created", actor or role,
                            body={"split_from": source_id})
            else:
                self._fetch_incident(conn, target_incident_id)
            conn.execute("UPDATE reports SET incident_id=? WHERE report_id=?",
                         (target_incident_id, report_id))
            conn.execute("UPDATE samples SET incident_id=? WHERE report_id=?",
                         (target_incident_id, report_id))
            conn.execute("UPDATE symptoms SET incident_id=? WHERE report_id=?",
                         (target_incident_id, report_id))
            self._audit(conn, source_id, "report_relocated", actor or role,
                        basis=reason,
                        body={"report_id": report_id, "to": target_incident_id})
            self._audit(conn, target_incident_id, "report_relocated", actor or role,
                        basis=reason,
                        body={"report_id": report_id, "from": source_id})
            self._reevaluate(conn, source_id, actor or role, recompute=True)
            self._reevaluate(conn, target_incident_id, actor or role)
            return {"report_id": report_id,
                    "incident_id": target_incident_id}

    # ------------------------------------------------------------ 查询视图
    def incident_view(self, incident_id, role="dispatcher", at=None):
        """值班还原视图：当前风险级别与依据、待办随访、完整处置时间线。

        role 不在 PII_ROLES 时（匿名公众）敏感身份字段一律脱敏。
        """
        conn = self.connection
        inc = conn.execute(
            "SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if inc is None:
            raise ServiceError("事件不存在", "not_found")
        can_see_pii = role in PII_ROLES
        moment = at or self.clock()

        reports = []
        for row in conn.execute(
                "SELECT * FROM reports WHERE incident_id=? ORDER BY created_at",
                (incident_id,)).fetchall():
            item = {
                "report_id": row["report_id"], "channel": row["channel"],
                "reporter_kind": row["reporter_kind"],
                "location": row["location"], "route": row["route"],
                "ingestion_time": row["ingestion_time"],
                "quantity": row["quantity"], "note": row["note"],
                "created_at": row["created_at"],
            }
            if can_see_pii:
                item.update({
                    "reporter_name": row["reporter_name"],
                    "reporter_contact": row["reporter_contact"],
                    "patient_name": row["patient_name"],
                    "patient_contact": row["patient_contact"],
                })
            reports.append(item)

        samples = [dict(sample_id=r["sample_id"], report_id=r["report_id"],
                        sample_type=r["sample_type"], source=r["source"],
                        description=r["description"], photo_ref=r["photo_ref"],
                        created_at=r["created_at"])
                   for r in conn.execute(
                       "SELECT * FROM samples WHERE incident_id=? ORDER BY created_at",
                       (incident_id,))]
        symptoms = [dict(symptom_id=r["symptom_id"], report_id=r["report_id"],
                         code=r["code"], label=SYMPTOM_CATALOG.get(
                             r["code"], (r["code"],))[0],
                         onset_at=r["onset_at"], observed_at=r["observed_at"],
                         note=r["note"], recorded_by=r["recorded_by"])
                    for r in conn.execute(
                        "SELECT * FROM symptoms WHERE incident_id=? ORDER BY observed_at",
                        (incident_id,))]
        handoffs = [dict(handoff_id=r["handoff_id"], facility=r["facility"],
                         staff=r["staff"], summary=r["summary"],
                         created_by=r["created_by"], created_at=r["created_at"])
                    for r in conn.execute(
                        "SELECT * FROM handoffs WHERE incident_id=? ORDER BY created_at",
                        (incident_id,))]
        follow_ups = [dict(fu_id=r["fu_id"], kind=r["kind"], due_at=r["due_at"],
                           status=r["status"], basis=r["basis"], note=r["note"],
                           assignee_role=r["assignee_role"],
                           overdue=(r["status"] == "pending" and r["due_at"] <= moment),
                           completed_at=r["completed_at"])
                      for r in conn.execute(
                          "SELECT * FROM follow_ups WHERE incident_id=? ORDER BY due_at",
                          (incident_id,))]
        timeline = [dict(event_id=r["event_id"], kind=r["kind"], actor=r["actor"],
                         basis=r["basis"], body=json.loads(r["body"] or "{}"),
                         created_at=r["created_at"])
                    for r in conn.execute(
                        "SELECT * FROM audit_events WHERE incident_id=? ORDER BY created_at",
                        (incident_id,))]

        primary = self._primary_factors(samples, reports)
        view = {
            "incident_id": inc["incident_id"],
            "risk_level": inc["risk_level"],
            "risk_basis": inc["risk_basis"],
            "status": inc["status"],
            "version": inc["version"],
            "merged_into": inc["merged_into"],
            "updated_at": inc["updated_at"],
            "primary_sample": primary[0],
            "primary_route": primary[1],
            "primary_source": primary[2],
            "advice": advice_for(*primary),
            "reports": reports,
            "samples": samples,
            "symptoms": symptoms,
            "handoffs": handoffs,
            "follow_ups": follow_ups,
            "pending_follow_ups": [f for f in follow_ups if f["status"] == "pending"],
            "timeline": timeline,
        }
        if not can_see_pii:
            # 公众视图只保留状态与建议，时间线隐去处置人。
            for entry in view["timeline"]:
                entry["actor"] = ""
        return view

    def public_status(self, request_key):
        """匿名公众凭报案回执键查询脱敏状态。"""
        row = self.connection.execute(
            "SELECT incident_id FROM reports WHERE request_key=?",
            (request_key,)).fetchone()
        if row is None:
            raise ServiceError("回执不存在", "not_found")
        return self.incident_view(row["incident_id"], role="public")

    # ------------------------------------------------------------ 内部规则
    def _validate_vocab(self, sample_type, route, source, symptoms):
        if sample_type not in SAMPLE_TYPES:
            raise ServiceError("未知样本类型：%s" % sample_type)
        if route not in CONTACT_ROUTES:
            raise ServiceError("未知接触途径：%s" % route)
        if source not in domain.SAMPLE_SOURCES:
            raise ServiceError("未知样本来源：%s" % source)
        for item in symptoms or []:
            code = item.get("code")
            if code not in SYMPTOM_CATALOG:
                raise ServiceError("未知症状代码：%s" % code)

    def _insert_symptom(self, conn, incident_id, report_id, item, role, actor, now):
        sid = _new_id("SYM")
        observed = item.get("observed_at") or now
        conn.execute(
            "INSERT INTO symptoms(symptom_id,incident_id,report_id,code,onset_at,"
            "observed_at,note,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, incident_id, report_id, item["code"], item.get("onset_at"),
             observed, item.get("note"), actor or role, now))
        return sid

    def _fetch_incident(self, conn, incident_id):
        row = conn.execute(
            "SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if row is None:
            raise ServiceError("事件不存在", "not_found")
        return row

    def _match_open_incident(self, conn, dedupe_key):
        """同一去重键命中未合并事件时，视为多渠道重复上报，自动挂接。"""
        if not dedupe_key:
            return None
        row = conn.execute(
            "SELECT i.* FROM incidents i JOIN reports r ON r.incident_id=i.incident_id "
            "WHERE r.dedupe_key=? AND i.merged_into IS NULL "
            "ORDER BY i.created_at LIMIT 1", (dedupe_key,)).fetchone()
        return row

    def _primary_factors(self, sample_rows, report_rows):
        """选取主样本/途径/来源：风险高者优先，其次取最早记录。"""
        priority = {domain.SAMPLE_UNKNOWN: 4, domain.SAMPLE_WILD_MUSHROOM: 3,
                    domain.SAMPLE_GINKGO: 2, domain.SAMPLE_LYCORIS: 1,
                    domain.SAMPLE_OTHER: 0}
        sample_type = domain.SAMPLE_UNKNOWN
        source = domain.SOURCE_UNKNOWN
        if sample_rows:
            first = sorted(sample_rows, key=lambda r: r["created_at"])[0]
            sample_type = first["sample_type"]
            source = first["source"]
            for row in sample_rows:
                if priority.get(row["sample_type"], 0) > priority.get(sample_type, 0):
                    sample_type = row["sample_type"]
                    source = row["source"]
        route = domain.ROUTE_UNKNOWN
        for row in sorted(report_rows, key=lambda r: r["created_at"]):
            if row["route"]:
                route = row["route"]
                if row["route"] == domain.ROUTE_INGESTION:
                    break
        return sample_type, route, source

    def _reevaluate(self, conn, incident_id, actor, recompute=False):
        """汇总事件全部证据调用规则引擎：升级风险、补排随访，写审计。

        recompute=False（默认）：风险只升不降，用于新增证据后的常规评估；
        recompute=True：按现存证据从零重算，仅用于合并回滚 / 报案迁出这类
        “系统纠正自身错误”的场景，降级的依据与前后级别同样写入审计。
        """
        inc = conn.execute(
            "SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        now = self.clock()
        sample_rows = conn.execute(
            "SELECT * FROM samples WHERE incident_id=?", (incident_id,)).fetchall()
        report_rows = conn.execute(
            "SELECT * FROM reports WHERE incident_id=?", (incident_id,)).fetchall()
        symptom_rows = conn.execute(
            "SELECT * FROM symptoms WHERE incident_id=? ORDER BY observed_at",
            (incident_id,)).fetchall()
        if not sample_rows:
            return {"level": inc["risk_level"], "basis": inc["risk_basis"]}

        symptoms = [{"code": r["code"], "onset_at": r["onset_at"]}
                    for r in symptom_rows]
        types = sorted({r["sample_type"] for r in sample_rows})
        routes = sorted({r["route"] for r in report_rows if r["route"]}
                        ) or [domain.ROUTE_UNKNOWN]
        ingestion_times = [r["ingestion_time"] for r in report_rows
                           if r["ingestion_time"]]
        ingested_at = min(ingestion_times) if ingestion_times else None

        floor = "low" if recompute else inc["risk_level"]
        level = floor
        all_rules, all_basis, specs = set(), [], {}
        for sample_type in types:
            for route in routes:
                result = domain.evaluate_risk(
                    sample_type, route, symptoms,
                    current_level=level, ingested_at=ingested_at, now=now)
                if RISK_RANK[result["level"]] > RISK_RANK[level]:
                    level = result["level"]
                all_rules.update(result["fired_rules"])
                if result["basis"]:
                    all_basis.append(result["basis"])
                for spec in result["follow_ups"]:
                    specs[spec.dedupe_key] = spec

        if level != inc["risk_level"]:
            decreasing = RISK_RANK[level] < RISK_RANK[inc["risk_level"]]
            basis = "；".join(dict.fromkeys(all_basis)) or "无升级规则命中"
            if decreasing:
                basis = "按现存证据重算：" + basis
            conn.execute(
                "UPDATE incidents SET risk_level=?,risk_basis=?,version=version+1,"
                "updated_at=? WHERE incident_id=?",
                (level, basis, now, incident_id))
            self._audit(conn, incident_id,
                        "risk_recomputed" if decreasing else "risk_escalated",
                        actor, basis=basis,
                        body={"from": inc["risk_level"], "to": level,
                              "rules": sorted(all_rules)})
            current_basis = basis
        else:
            conn.execute("UPDATE incidents SET updated_at=? WHERE incident_id=?",
                         (now, incident_id))
            current_basis = inc["risk_basis"]

        # 状态与级别对齐：仅在 open/escalated 之间自动切换，
        # handoff / merged 等人工状态不受影响。
        if level in ("high", "critical"):
            conn.execute(
                "UPDATE incidents SET status='escalated',updated_at=? "
                "WHERE incident_id=? AND status='open'", (now, incident_id))
        else:
            conn.execute(
                "UPDATE incidents SET status='open',updated_at=? "
                "WHERE incident_id=? AND status='escalated'", (now, incident_id))

        scheduled = []
        for spec in specs.values():
            cursor = conn.execute(
                "INSERT OR IGNORE INTO follow_ups(incident_id,dedupe_key,kind,"
                "due_at,status,basis,note,assignee_role,created_at) "
                "VALUES(?,?,?,?,'pending',?,?,?,?)",
                (incident_id, spec.dedupe_key, spec.kind,
                 domain.iso_after(domain.parse_ts(now), spec.due_in_hours),
                 spec.basis, spec.note, spec.assignee_role, now))
            if cursor.rowcount:
                scheduled.append(spec.kind)
                self._audit(conn, incident_id, "followup_scheduled", actor,
                            basis=spec.basis,
                            body={"kind": spec.kind, "due_in_hours": spec.due_in_hours})
        return {"level": level, "basis": current_basis,
                "scheduled": scheduled}

    def close(self):
        with self._lock:
            self.connection.close()
