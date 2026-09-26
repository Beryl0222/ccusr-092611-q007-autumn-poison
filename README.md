# 秋季误采处置协同

面向公园秋季误食（彼岸花鳞茎、银杏果、野生蘑菇）的中毒事件协同服务。本地 SQLite 持久化，核心写入走单事务，接口无页面依赖，便于现场与后台系统核对状态。

## 能力

- **报案接入**：公众可匿名报案（`POST /incidents`，无需身份头）；授权医护/咨询员可对既有事件补充症状、记录医疗交接。
- **重复上报合并**：报案可带去重线索（患者/时间/地点等），命中未合并事件时自动挂接而不新建事件；也支持值班员显式合并（`POST /merges`）。合并只重挂归属、不删除任何证据行。
- **错误合并回滚**：`POST /merges/{id}/undo` 按合并时记录的 moved_refs 精确还原；自动挂接错误可用 `POST /reports/{id}/relocate` 拆分。回滚后双方按各自现存证据重算风险并留痕。
- **自动升级**：高危症状（意识模糊/黄疸/少尿等）升至 high，危重症状（抽搐/昏迷/休克/呼吸困难）升至 critical；迟发毒性样本（野生蘑菇、不明样本）出现“胃肠症状缓解”即判定假愈期升至 critical 并自动排 24/48/72 小时复查随访；食后超 6 小时无症状按迟发追踪。风险只自动上调，人工降级须值班角色并写明依据。
- **处置建议**：按样本来源（自采/市场/赠送）与接触途径（误食/皮肤/眼睛/吸入）给出差异化咨询口径。
- **权限脱敏**：报案人/患者身份信息仅 dispatcher、medical、admin 可见；公众查询（含凭回执 `GET /public/{request_key}`）自动脱敏。
- **重启还原**：全部状态落库，`GET /incidents/{id}` 还原当前风险级别与依据、待办随访、症状时间线、交接与每次处置的审计依据；`GET /follow-ups` 给出值班台全局待办（含逾期标记）。

## 目录

- src/autumn_poison/domain.py：受控词表、风险规则引擎（R1–R10）、随访规格、处置建议、时间约定。
- src/autumn_poison/service.py：SQLite 事务、事件/报案/症状/随访/交接/合并服务、权限与幂等边界；同时保留旧的通用记录状态机（create/get/transition）。
- src/autumn_poison/api.py：本地 HTTP 接口（身份头 X-Actor-Id / X-Actor-Role，幂等头 X-Request-Key）。
- tests/：规则、升级、假愈期、合并回滚、权限脱敏、重启还原与 HTTP 冒烟测试。

## 运行

    PYTHONPATH=src python3 -m autumn_poison.api   # 监听 127.0.0.1:8080，数据文件 autumn_poison.db

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests
