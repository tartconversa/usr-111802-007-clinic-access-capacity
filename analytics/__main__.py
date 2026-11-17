"""本地演练入口：导入样本，查看原始摘要与排班分析、评估快照复核流程。"""
import argparse
import json
from pathlib import Path
from .service import AnalyticsService


def main():
    parser = argparse.ArgumentParser(description="运营分析服务")
    parser.add_argument("--database", required=True)
    parser.add_argument("--fixture", default="fixtures/sample.json")
    args = parser.parse_args()
    sample = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    service = AnalyticsService(args.database)
    try:
        for subject in sample["entities"]:
            service.register(**subject)
        for alias in sample["aliases"]:
            service.link(**alias)
        for slot in sample.get("slots", []):
            service.schedule_slot(slot["site"], slot["room"], slot["day"],
                                  slot["seats"], slot.get("session", "allday"))
        for event in sample["events"]:
            service.ingest(event)

        print("== 原始事件摘要（既有口径） ==")
        print(json.dumps(service.summary(), ensure_ascii=False, sort_keys=True))

        scope = {"site": "site-1", "day": "2026-11-10", "session": "morning"}
        snapshot_cutoff = "2026-11-09T18:00:00+08:00"
        print("\n== 管理者确认的排班评估（截至 %s，冻结数据与口径） ==" % snapshot_cutoff)
        assessment = service.publish_assessment(
            "assess-demo", "2026-11-09T18:05:00+08:00", "11月10日上午可及性评估",
            scope=scope, as_of=snapshot_cutoff)
        print(json.dumps(assessment["body"]["analysis"], ensure_ascii=False,
                         sort_keys=True, indent=2))

        print("\n== 补录取消与临时停诊后，当前评估 ==")
        current = service.scheduling_analysis(scope=scope)
        print(json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2))

        print("\n== 旧评估适用性复核（标明何处已不适合继续用于排班） ==")
        review = service.reassess("assess-demo")
        print(json.dumps({key: review[key] for key in
                          ("fit_for_scheduling", "stale_slots", "added_slots",
                           "removed_slots", "scheduling_impact")},
                         ensure_ascii=False, sort_keys=True, indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    main()
