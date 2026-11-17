# 门诊预约运营数据服务

服务登记多来源事件、业务主体及现有摘要，并保存发布报告的原始内容。示例数据为虚构记录。

需要 Python 3.10 或更新版本；仅依赖标准库。

运行测试：python -m unittest discover -s tests -v
编译检查：python -m compileall -q analytics tests
本地演练：python -m analytics --database demo.sqlite3 --fixture fixtures/sample.json

analytics/store.py 保存来源事件和修订；identity.py 管理外部身份映射；metrics.py 汇总已映射主体的事件计数与通用金额；decisions.py 保存已发布报告；domain.py 维护按院区、日期、时段的号源目录（旧版无时段表会在启动时迁移到默认时段 allday）；service.py 协调这些接口。

## 排班分析（运营可及性）

analytics/scheduling.py 在原始事件之上提供排班口径，回答新增号源应安排在哪里：

- 等待时间：预约就诊时刻与最早请求时刻之差（中位数、P90、样本数）。
- 未满足需求：等待名单或被拒预约链的去重患者数；另给出号源溢出量。
- 实际可用号源：计划号源扣除临时停诊净调减；停诊只影响供给，永远不计入需求。
- 改约去重：修订链与 `replaces` 链折叠为同一条预约链，同一次改约不会算成两位患者的需求。
- 评估快照：`publish_assessment` 冻结确认时的数据与口径版本；`reassess` 用补录后的当前数据逐槽位复核，标明旧评估哪些字段已不适合继续用于排班。

事件 payload 契约与完整口径定义见 docs/domain.md。本服务只分析运营可及性，不提供临床诊断或治疗建议。
