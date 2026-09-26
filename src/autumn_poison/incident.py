"""中毒事件领域规则：样本、接触途径、风险级别与自动升级依据。"""
SAMPLE_TYPES=("lycoris","ginkgo","mushroom","unknown")# 彼岸花、银杏果、野生蘑菇、不明样本
EXPOSURE_ROUTES=("ingestion","skin_contact","inhalation","eye_contact")# 食入、皮肤接触、吸入、眼部接触
RISK_ORDER=("low","medium","high","critical")
# 初始风险：野生蘑菇毒种不明，先按中等风险对待；其余样本从低风险起步
INITIAL_RISK={"mushroom":"medium","unknown":"medium"}
# 高危症状关键词：命中即升级，命中两项及以上直接升至 critical
HIGH_RISK_KEYWORDS=("意识障碍","昏迷","抽搐","惊厥","呼吸困难","呕血","咯血","黄疸","少尿","无尿","休克","心律失常","谵妄","幻觉")
# 胃肠期关键词：缓解前曾出现剧烈胃肠症状，是判断假愈期的前提
GI_KEYWORDS=("呕吐","腹泻","腹痛","恶心")
# 缓解表述：症状时间线中出现即视为进入缓解期
RELIEF_KEYWORDS=("缓解","好转","减轻")
# 假愈期适用样本：鹅膏毒素类有迟发肝损伤风险，野生蘑菇与不明样本均需警惕
FALSE_RECOVERY_SAMPLES=("mushroom","unknown")
# 可查看报告人敏感身份信息的角色
IDENTITY_ROLES=("medic","duty")
ROLE_RIGHTS={
 "public":{"report"},
 "consultant":{"report","advise"},
 "medic":{"report","advise","symptom","handoff","view_identity"},
 "duty":{"report","advise","symptom","handoff","view_identity","merge","rollback","followup"},
}
