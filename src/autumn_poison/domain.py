"""秋季误采处置协同的领域对象、时间约定与风险规则。

本模块只放纯数据与纯函数：
- 样本类型、接触途径、症状目录等受控词表；
- 风险等级与自动升级规则（高危症状、假愈期、迟发毒性）；
- 随访规格与按“样本来源 + 接触途径”的处置建议。

规则全部以 R-xxx 编号，写入审计与随访的 basis 字段，
保证每次处置都能回答“依据是什么”。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class Record:
    """旧通用记录（records 表）的只读视图。"""
    record_id: str
    owner_id: str
    state: str
    version: int
    updated_at: str

# ---------------------------------------------------------------- 时间约定

def utc_now():
    """当前 UTC 时间，ISO8601 字符串；所有存储统一使用。"""
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value):
    """解析 ISO8601 时间；允许 None/空串，返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def iso_after(moment, hours):
    """moment 之后 hours 小时的 ISO8601 字符串。"""
    return (moment + timedelta(hours=hours)).isoformat()

# ---------------------------------------------------------------- 受控词表

# 样本类型：秋季公园三类高风险物 + 兜底。
SAMPLE_LYCORIS = "lycoris_bulb"        # 彼岸花（石蒜）鳞茎
SAMPLE_GINKGO = "ginkgo_seed"          # 银杏果（白果）
SAMPLE_WILD_MUSHROOM = "wild_mushroom"  # 野生蘑菇
SAMPLE_UNKNOWN = "unknown"             # 不明样本
SAMPLE_OTHER = "other"                 # 其他（已确认低风险物）

SAMPLE_TYPES = (
    SAMPLE_LYCORIS, SAMPLE_GINKGO, SAMPLE_WILD_MUSHROOM,
    SAMPLE_UNKNOWN, SAMPLE_OTHER,
)

# 迟发毒性样本：症状缓解不代表脱险，存在假愈期，缓解后反而要升级追踪。
DELAYED_TOXIN_SAMPLES = frozenset({SAMPLE_WILD_MUSHROOM, SAMPLE_UNKNOWN})

# 样本来源：影响建议口径（自行采食风险高于市场购买）。
SOURCE_SELF_PICKED = "self_picked"   # 园内/野外自行采摘
SOURCE_MARKET = "market"             # 市场/商超购买
SOURCE_GIFT = "gift"                 # 他人赠送
SOURCE_UNKNOWN = "unknown"
SAMPLE_SOURCES = (SOURCE_SELF_PICKED, SOURCE_MARKET, SOURCE_GIFT, SOURCE_UNKNOWN)

# 接触途径。
ROUTE_INGESTION = "ingestion"    # 误食
ROUTE_SKIN = "skin_contact"      # 皮肤接触（汁液）
ROUTE_EYE = "eye_contact"        # 溅入眼睛
ROUTE_INHALATION = "inhalation"  # 吸入粉尘/孢子
ROUTE_UNKNOWN = "unknown"
CONTACT_ROUTES = (
    ROUTE_INGESTION, ROUTE_SKIN, ROUTE_EYE, ROUTE_INHALATION, ROUTE_UNKNOWN,
)

# ---------------------------------------------------------------- 症状目录

# code -> (名称, 级别)。级别：mild 一般 / high 高危 / critical 危重。
SYMPTOM_CATALOG = {
    "nausea": ("恶心呕吐", "mild"),
    "vomiting": ("呕吐", "mild"),
    "diarrhea": ("腹泻", "mild"),
    "abdominal_pain": ("腹痛", "mild"),
    "dizziness": ("头晕乏力", "mild"),
    "oral_irritation": ("口腔咽喉灼痛", "mild"),
    "skin_irritation": ("皮肤红肿瘙痒", "mild"),
    "excessive_sweating": ("大量出汗流涎", "high"),
    "confusion": ("意识模糊", "high"),
    "jaundice": ("黄疸", "high"),
    "oliguria": ("少尿无尿", "high"),
    "seizure": ("抽搐惊厥", "critical"),
    "coma": ("昏迷", "critical"),
    "shock": ("休克", "critical"),
    "dyspnea": ("呼吸困难", "critical"),
    # 特殊标记：胃肠症状明显缓解。它不是病情，而是假愈期规则的触发信号。
    "gi_relief": ("胃肠症状明显缓解", "marker"),
}

# 进入假愈期判定所需的“前期胃肠症状”集合。
GI_SYMPTOMS = frozenset({"nausea", "vomiting", "diarrhea", "abdominal_pain"})

SEVERITY_RANK = {"mild": 1, "high": 2, "critical": 3, "marker": 0}

# ---------------------------------------------------------------- 风险等级

RISK_LEVELS = ("low", "moderate", "high", "critical")
RISK_RANK = {name: index for index, name in enumerate(RISK_LEVELS)}

# ---------------------------------------------------------------- 规则与随访

@dataclass(frozen=True)
class FollowUpSpec:
    """一次随访的调度规格。dedupe_key 用于避免重复排程。"""
    kind: str
    due_in_hours: float
    note: str
    basis: str
    assignee_role: str = "dispatcher"

    @property
    def dedupe_key(self):
        return "%s@%s" % (self.kind, self.due_in_hours)


# 假愈期观察窗：胃肠症状缓解后仍需密集复查的小时数。
FALSE_RECOVERY_WINDOWS = (24, 48, 72)
# 迟发毒性：食后无症状超过该小时数即按迟发处理。
DELAYED_ONSET_HOURS = 6


def _relief_specs(relief_at):
    specs = [
        FollowUpSpec(
            kind="liver_panel_recheck",
            due_in_hours=1,
            note="假愈期预警：立即复查肝功能与凝血，勿因症状缓解放行",
            basis="R4 假愈期：立即复查肝功",
        )
    ]
    for hours in FALSE_RECOVERY_WINDOWS:
        specs.append(FollowUpSpec(
            kind="false_recovery_watch",
            due_in_hours=hours,
            note="假愈期观察窗内复查肝肾功能并电话随访",
            basis="R4 假愈期：%d小时复查" % hours,
        ))
    return specs


def evaluate_risk(
    sample_type,
    route,
    symptoms,
    *,
    current_level="low",
    ingested_at=None,
    now=None,
):
    """根据样本、途径与症状时间线评估目标风险等级与应排随访。

    symptoms: [{"code": str, "onset_at": str|None}, ...]，按时间线累计传入。
    返回 {"level", "fired_rules", "basis", "follow_ups"}；level 只升不降
    （不低于 current_level），降级只能由人工带依据执行。
    """
    now = now or utc_now()
    now_dt = parse_ts(now)
    codes = [s["code"] for s in symptoms]
    code_set = set(codes)
    max_severity = "mild"
    for code in codes:
        severity = SYMPTOM_CATALOG.get(code, ("", "mild"))[1]
        if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(max_severity, 0):
            max_severity = severity

    fired = []          # [(rule_id, level, basis)]
    follow_ups = []
    delayed = sample_type in DELAYED_TOXIN_SAMPLES

    # R2/R3：症状级别直接定级，任何样本、任何途径都可能触发。
    if max_severity == "critical":
        fired.append(("R3", "critical",
                      "R3 危重症状：出现抽搐/昏迷/休克/呼吸困难，按危重处置"))
        follow_ups.append(FollowUpSpec(
            kind="urgent_callback", due_in_hours=1,
            note="危重症状：1小时内回访确认已入院抢救",
            basis="R3 危重症状：1小时回访"))
    elif max_severity == "high":
        fired.append(("R2", "high",
                      "R2 高危症状：出现意识模糊/黄疸/少尿等，需立即就医"))
        follow_ups.append(FollowUpSpec(
            kind="urgent_callback", due_in_hours=2,
            note="高危症状：2小时内回访确认就医情况",
            basis="R2 高危症状：2小时回访"))

    # R4：假愈期。迟发毒性样本 + 曾出现胃肠症状 + 已标记缓解。
    if delayed and code_set & GI_SYMPTOMS and "gi_relief" in code_set:
        relief_at = None
        for entry in symptoms:
            if entry["code"] == "gi_relief":
                relief_at = parse_ts(entry.get("onset_at")) or relief_at
        fired.append(("R4", "critical",
                      "R4 假愈期：迟发毒性样本胃肠症状缓解，警惕肝衰竭假愈，禁止放行"))
        follow_ups.extend(_relief_specs(relief_at or now_dt))

    # R5：迟发表现。食后 DELAYED_ONSET_HOURS 小时仍无危重症状，按迟发毒性追踪。
    if delayed and route == ROUTE_INGESTION and max_severity in ("mild", "marker"):
        ingested_dt = parse_ts(ingested_at)
        if ingested_dt is not None and now_dt - ingested_dt >= timedelta(hours=DELAYED_ONSET_HOURS):
            fired.append(("R5", "high",
                          "R5 迟发毒性：食后超过%d小时，可能处于潜伏/假愈期" % DELAYED_ONSET_HOURS))
            follow_ups.append(FollowUpSpec(
                kind="delayed_onset_callback", due_in_hours=12,
                note="迟发毒性风险：12小时内回访并复查肝功",
                basis="R5 迟发毒性：12小时回访"))

    # R6：银杏果 + 抽搐（儿童常见重症表现）。
    if sample_type == SAMPLE_GINKGO and "seizure" in code_set:
        fired.append(("R6", "critical",
                      "R6 银杏果惊厥：白果中毒出现抽搐，按危重处置"))
        follow_ups.append(FollowUpSpec(
            kind="urgent_callback", due_in_hours=1,
            note="银杏果惊厥：1小时内回访确认抢救与止痉情况",
            basis="R6 银杏果惊厥：1小时回访"))

    # ---- 基线定级（无症状或一般症状时的初始风险）----
    baseline = "low"
    baseline_basis = None
    if route == ROUTE_INGESTION:
        if sample_type == SAMPLE_UNKNOWN:
            baseline, baseline_basis = "moderate", "R7 不明样本误食：按中风险起步追踪"
        elif sample_type == SAMPLE_WILD_MUSHROOM:
            baseline, baseline_basis = "moderate", "R8 野生蘑菇误食：按中风险起步追踪"
        elif sample_type in (SAMPLE_LYCORIS, SAMPLE_GINKGO) or code_set & GI_SYMPTOMS:
            baseline, baseline_basis = "moderate", "R9 误食有毒样本或已出现胃肠症状：按中风险追踪"
    elif route in (ROUTE_SKIN, ROUTE_EYE, ROUTE_INHALATION):
        baseline, baseline_basis = "low", "R10 非食入接触：按低风险处置并指导清洗"
    if baseline_basis:
        fired.append((baseline_basis.split(" ", 1)[0], baseline, baseline_basis))

    # ---- 汇总：只升不降 ----
    target = current_level if current_level in RISK_RANK else "low"
    for _rule, level, _basis in fired:
        if RISK_RANK[level] > RISK_RANK[target]:
            target = level
    basis = "；".join(b for _r, _l, b in fired if b) or "人工评估"
    return {
        "level": target,
        "fired_rules": sorted({r for r, _l, _b in fired if r}),
        "basis": basis,
        "follow_ups": follow_ups,
    }

# ---------------------------------------------------------------- 处置建议

# (样本, 途径) 精确建议；缺失时退回按途径的通用建议。
_ADVICE_BY_PAIR = {
    (SAMPLE_LYCORIS, ROUTE_INGESTION):
        "立即催吐并口服活性炭，携带剩余鳞茎样本尽快就医；石蒜碱可致剧烈呕吐腹泻，注意补液防脱水。",
    (SAMPLE_GINKGO, ROUTE_INGESTION):
        "白果含银杏毒素，儿童尤为敏感；控制食用量并立即就医观察，若出现抽搐按惊厥急救，保持呼吸道通畅。",
    (SAMPLE_WILD_MUSHROOM, ROUTE_INGESTION):
        "保留剩余蘑菇与呕吐物样本供鉴定；切勿因症状缓解而离院，假愈期后可能爆发肝衰竭，建议留观至少72小时。",
    (SAMPLE_UNKNOWN, ROUTE_INGESTION):
        "样本不明按高风险处理：保留样本与呕吐物，尽快就医并告知进食时间与数量，症状缓解后仍需复查。",
}

_ADVICE_BY_ROUTE = {
    ROUTE_INGESTION: "停止进食可疑物，保留样本，尽快就医评估。",
    ROUTE_SKIN: "脱去污染衣物，用大量清水冲洗接触部位至少15分钟；若出现皮疹或全身症状需就医。",
    ROUTE_EYE: "立即用流动清水或生理盐水冲洗眼睛至少15分钟，随后眼科就诊。",
    ROUTE_INHALATION: "迅速离开现场至空气新鲜处，保持呼吸道通畅，出现咳嗽胸闷需就医。",
}

_SOURCE_EXTRA = {
    SOURCE_SELF_PICKED: "样本为自行采摘，来源与鉴定不确定，按高风险口径告知。",
    SOURCE_MARKET: "样本为市场购买，建议保留购买凭证与剩余样本以便溯源。",
}


def advice_for(sample_type, route, source=SOURCE_UNKNOWN):
    """咨询口径：按样本来源与接触途径给出差异化处置建议。"""
    text = _ADVICE_BY_PAIR.get((sample_type, route)) or _ADVICE_BY_ROUTE.get(route) \
        or "保持观察，出现任何不适立即就医。"
    extra = _SOURCE_EXTRA.get(source)
    if extra:
        text = text + " " + extra
    return text
