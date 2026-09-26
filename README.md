# 秋季误采处置协同

这是面向秋季误采处置协同的本地服务基础，负责保存业务记录、状态变化和可追溯事件。核心写入使用 SQLite 事务，服务接口保持无页面依赖，便于运营人员在现场或后台系统中核对状态。

## 目录

- src/autumn_poison/domain.py：领域对象与时间约定。
- src/autumn_poison/service.py：事务、状态迁移、权限和幂等边界。
- src/autumn_poison/incident.py：中毒事件规则（样本、接触途径、高危症状与假愈期判定）。
- src/autumn_poison/incident_service.py：报案受理、症状时间线、医疗交接、合并回滚、自动升级与值班还原。
- src/autumn_poison/api.py：本地 HTTP 接口（角色经 X-Actor-Id / X-Actor-Role 头传入）。
- tests/：状态、版本、权限、重复请求与中毒事件测试。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests
