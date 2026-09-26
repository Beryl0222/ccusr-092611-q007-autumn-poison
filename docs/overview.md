# 秋季误采处置协同说明

秋季误采处置协同需要在多方参与、事件顺序不稳定和服务重启的情况下保持记录一致。

## 数据与一致性

- 每个事件（incidents）有风险级别、状态、递增版本与风险依据；报案、样本、症状、交接、随访、审计事件分别落表，全部写操作包在单条 SQLite 事务（BEGIN IMMEDIATE）中，失败整体回滚。
- 请求键（X-Request-Key / request_key）用于抵御重复提交：命中即返回首次结果，不重复写入。
- 所有状态变化与处置动作写入 audit_events，kind 区分 created / report_added / medical_supplement / symptom_recorded / risk_escalated / risk_recomputed / risk_downgraded / followup_scheduled / followup_done / handoff / merged / merge_undone / report_relocated / action，每条都带 actor 与 basis（依据）。

## 风险规则

规则引擎为纯函数（domain.evaluate_risk），编号 R1–R10：症状级别定级（R2 高危 / R3 危重）、假愈期（R4，迟发毒性样本胃肠症状缓解即升 critical 并排 24/48/72h 复查）、迟发毒性（R5，食后 6h 无症状）、银杏果惊厥（R6）、基线定级（R7–R10，按样本与接触途径）。风险只自动上调；人工降级须值班角色并写明依据；合并回滚/报案迁出属系统自我纠正，按现存证据重算并留痕。

## 合并与回滚

- 自动：报案带去重线索，命中未合并事件即挂接为同一事件的新报案，证据各自成行不丢失。
- 显式：merge_incidents 把被吸收事件的全部子记录重挂到存留事件，moved_refs 记录每一行的移动；undo_merge 按 moved_refs 精确还原，不影响合并期间新产生的数据。
- 随访在同事件内按 dedupe_key 唯一；合并时撞键的随访保留在被合并事件下，不强行重挂。

## 权限

角色：public（匿名公众）/ dispatcher（值班咨询）/ medical（授权医护）/ admin。敏感身份字段（报案人、患者姓名与联系方式）仅 dispatcher、medical、admin 可见；补充症状、医疗交接限 medical 及以上；合并、回滚、人工降级限 dispatcher 及以上。

## 重启还原

服务重启后按事件编号查询 incident_view 即可还原：当前风险级别与依据、待办随访（含逾期标记）、症状时间线、医疗交接与每次处置的审计依据；值班台用 list_due_follow_ups 恢复全局待办。
