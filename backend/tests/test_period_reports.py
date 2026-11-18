"""采集报告与采集监控的周期口径回归测试。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import threading
import unittest

from app.services import report as report_module
from app.services.monitor import MonitorService
from app.services.periods import PeriodError, normalize_period, parse_period, period_of
from app.services.report import ReportConflict, ReportService
from app.store import store

SEED_RECORDS = [
    {"id": 1, "采集源": "官网抓取", "任务": "列表页", "状态": "成功",
     "发生时间": "2026-11-02T09:00:00", "耗时秒": 1.0, "失败原因": ""},
    {"id": 2, "采集源": "官网抓取", "任务": "详情页", "状态": "失败",
     "发生时间": "2026-11-03T09:00:00", "耗时秒": 3.0, "失败原因": "选择器超时"},
    {"id": 3, "采集源": "开放API", "任务": "订单接口", "状态": "成功",
     "发生时间": "2026-11-09T09:00:00", "耗时秒": 2.0, "失败原因": ""},
    {"id": 4, "采集源": "开放API", "任务": "库存接口", "状态": "运行中",
     "发生时间": "2026-11-10T09:00:00", "耗时秒": None, "失败原因": ""},
    {"id": 5, "采集源": "官网抓取", "任务": "分类页", "状态": "成功",
     "发生时间": "2026-11-04T09:00:00", "耗时秒": 1.5, "失败原因": ""},
]
SEED_EVENTS = [
    {"id": 1, "事件编号": "FAIL-A", "采集源": "官网抓取", "任务": "详情页",
     "事件时间": "2026-11-03T09:00:00", "上报时间": "2026-11-03T09:05:00",
     "失败原因": "选择器超时", "归属记录": 2, "归属策略": "event_time",
     "归属周期": "2026-W45", "处理状态": "已归集"},
    {"id": 2, "事件编号": "FAIL-B", "采集源": "官网抓取", "任务": "列表页",
     "事件时间": "2026-11-02T09:00:00", "上报时间": "2026-11-18T09:00:00",
     "失败原因": "迟到补报", "归属记录": 1, "归属策略": "event_time",
     "归属周期": "2026-W45", "处理状态": "已归集"},
]


class PeriodTest(unittest.TestCase):
    def test_week_bounds_are_half_open(self) -> None:
        start, end = parse_period("2026-W45")
        self.assertEqual(start.isoformat(), "2026-11-02T00:00:00")
        self.assertEqual(end.isoformat(), "2026-11-09T00:00:00")
        self.assertEqual(period_of(end), "2026-W46")

    def test_aliases_normalize(self) -> None:
        self.assertEqual(normalize_period("2026-w5"), "2026-W05")
        self.assertEqual(normalize_period("2026-11"), "2026-11")
        self.assertEqual(normalize_period("2026-11-09"), "2026-11-09")
        with self.assertRaises(PeriodError):
            normalize_period("上周")


class IsolatedCase(unittest.TestCase):
    """每个用例使用独立的采集记录与失败事件，不污染示例数据。"""

    def setUp(self) -> None:
        self._backup = {
            name: [dict(row) for row in store.rows(name)]
            for name in (report_module.RECORD_TABLE, report_module.EVENT_TABLE,
                         report_module.REPORT_TABLE, report_module.VERSION_TABLE)
        }
        for name in self._backup:
            store.rows(name).clear()
        store.rows(report_module.RECORD_TABLE).extend(dict(row) for row in SEED_RECORDS)
        store.rows(report_module.EVENT_TABLE).extend(dict(row) for row in SEED_EVENTS)

    def tearDown(self) -> None:
        for name, rows in self._backup.items():
            store.rows(name)[:] = rows

    def build(self) -> tuple[ReportService, dict[str, dict]]:
        service = ReportService()
        service.ensure_reports()
        return service, {row["统计周期"]: row for row in service.list_reports()}


class AggregateTest(IsolatedCase):
    def test_success_and_failure_derive_from_records(self) -> None:
        service, reports = self.build()
        w45 = reports["2026-W45"]
        # 记录 1 被迟到失败事件改判为失败，记录 2 本身失败，记录 5 仍是成功。
        self.assertEqual((w45["总采集量"], w45["成功量"], w45["失败量"]), (3, 1, 2))
        self.assertEqual(w45["平均耗秒"], round((1.0 + 3.0 + 1.5) / 3, 3))
        detail = service.get_by_period("2026-W45")
        self.assertEqual(detail["核对"]["核对结论"], "一致")
        self.assertEqual({item["记录编号"] for item in detail["失败明细"]}, {"1", "2"})

    def test_unattributed_event_counts_as_failure_never_success(self) -> None:
        service = ReportService()
        event, message, reports = service.add_failure_event({
            "事件编号": "FAIL-ORPHAN", "采集源": "官网抓取", "任务": "不存在",
            "事件时间": "2026-11-04T09:00:00", "上报时间": "2026-11-04T09:01:00",
        })
        self.assertIsNotNone(event, message)
        detail = service.get_by_period("2026-W45")
        self.assertEqual(detail["总采集量"], 4)
        self.assertEqual(detail["成功量"], 1)
        self.assertEqual(detail["失败量"], 3)
        self.assertEqual(detail["未归因失败量"], 1)
        self.assertIn("未归因失败", {item["类型"] for item in detail["异常项"]})

    def test_same_period_results_are_stable(self) -> None:
        service, reports = self.build()
        w46 = reports["2026-W46"]
        first = (w46["总采集量"], w46["成功量"], w46["失败量"], w46["平均耗秒"])
        for _ in range(3):
            service.regenerate(int(w46["id"]))
        detail = service.get_by_period("2026-W46")
        self.assertEqual((detail["总采集量"], detail["成功量"], detail["失败量"], detail["平均耗秒"]), first)
        self.assertEqual(detail["版本"], 1)

    def test_regeneration_creates_new_version_and_keeps_old(self) -> None:
        service, reports = self.build()
        w45 = reports["2026-W45"]
        self.assertEqual(w45["版本"], 1)
        service.add_failure_event({
            "事件编号": "FAIL-LATE", "采集源": "官网抓取", "任务": "分类页",
            "事件时间": "2026-11-04T09:00:00", "上报时间": "2026-11-19T09:00:00",
            "失败原因": "迟到失败", "归属记录": 5, "归属策略": "event_time",
        })
        detail = service.get_by_period("2026-W45")
        self.assertEqual(detail["版本"], 2)
        self.assertEqual(detail["失败量"], 3)
        old = service.get_version(detail["报告编号"], 1)
        self.assertEqual((old["成功量"], old["失败量"]), (1, 2))
        self.assertEqual(len(detail["修订记录"]), 2)


class ConflictTest(IsolatedCase):
    def test_concurrent_create_allows_only_one(self) -> None:
        service = ReportService()
        results: list[str] = []
        lock = threading.Lock()

        def create() -> None:
            try:
                service.create_report("2026-W46")
                with lock:
                    results.append("ok")
            except ReportConflict:
                with lock:
                    results.append("conflict")

        threads = [threading.Thread(target=create) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("ok"), 1)
        matching = [row for row in store.rows(report_module.REPORT_TABLE) if row["统计周期"] == "2026-W46"]
        self.assertEqual(len(matching), 1)

    def test_duplicate_event_rejected(self) -> None:
        service = ReportService()
        event, message, _reports = service.add_failure_event({
            "事件编号": "FAIL-A", "采集源": "官网抓取", "任务": "详情页",
            "事件时间": "2026-11-03T09:00:00",
        })
        self.assertIsNone(event)
        self.assertIn("已存在", message)


class LateEventTest(IsolatedCase):
    def test_event_time_policy_revises_original_period(self) -> None:
        service, reports = self.build()
        service.add_failure_event({
            "事件编号": "FAIL-EVENT-TIME", "采集源": "官网抓取", "任务": "分类页",
            "事件时间": "2026-11-04T09:00:00", "上报时间": "2026-11-20T09:00:00",
            "失败原因": "接口超时", "归属记录": 5, "归属策略": "event_time",
        })
        w45 = service.get_by_period("2026-W45")
        w46 = service.get_by_period("2026-W46")
        self.assertEqual(w45["版本"], 2)
        self.assertEqual(w46["版本"], 1)
        self.assertEqual(w45["失败量"], 3)
        self.assertEqual(w46["成功量"], 1)

    def test_report_time_policy_moves_record_to_next_period(self) -> None:
        service, reports = self.build()
        service.add_failure_event({
            "事件编号": "FAIL-REPORT-TIME", "采集源": "官网抓取", "任务": "分类页",
            "事件时间": "2026-11-04T09:00:00", "上报时间": "2026-11-18T09:00:00",
            "失败原因": "补报", "归属记录": 5, "归属策略": "report_time",
        })
        w45 = service.get_by_period("2026-W45")
        w47 = service.get_by_period("2026-W47")
        # 记录 5 整体迁出：原周期只统计记录 1、2。
        self.assertEqual(w45["采集记录数"], 3)
        self.assertEqual(w45["总采集量"], 2)
        self.assertEqual(w45["依据"]["迁移出记录"], [5])
        self.assertEqual(w47["失败量"], 1)
        self.assertEqual(w47["核对"]["迁移入记录数"], 1)
        self.assertEqual(w47["依据"]["迁移入记录"], [5])
        # 原周期排除迁移记录后仍能回到采集记录。
        self.assertEqual(w45["核对"]["核对结论"], "一致")
        self.assertEqual(w47["核对"]["核对结论"], "一致")

    def test_success_plus_failure_returns_to_records_for_every_period(self) -> None:
        service = ReportService()
        service.ensure_reports()
        service.add_failure_event({
            "事件编号": "FAIL-MIGRATE", "采集源": "官网抓取", "任务": "列表页",
            "事件时间": "2026-11-02T09:00:00", "上报时间": "2026-11-18T10:00:00",
            "失败原因": "迟到补报", "归属记录": 1, "归属策略": "report_time",
        })
        for period in ("2026-W45", "2026-W46", "2026-W47"):
            detail = service.get_by_period(period)
            reconciled = detail["核对"]
            self.assertEqual(reconciled["核对结论"], "一致", period)
            # 周期内每条采集记录要么成功、要么失败、要么未结算、要么迁出，四者不重不漏。
            self.assertEqual(
                int(reconciled["成功量"]) + int(reconciled["失败量"]) - int(reconciled["迁移入记录数"])
                - int(reconciled["未归因失败事件数"]) + int(reconciled["未结算量"])
                + int(reconciled["迁移出记录数"]),
                int(reconciled["采集记录数"]),
                period,
            )
            self.assertEqual(int(detail["总采集量"]), int(detail["成功量"]) + int(detail["失败量"]))
            self.assertEqual(int(detail["失败量"]), len(detail["失败明细"]))


class MonitorTest(IsolatedCase):
    def setUp(self) -> None:
        super().setUp()
        self._monitor_backup = {
            name: [dict(row) for row in store.rows(name)]
            for name in ("_monitor_reports", "_monitor_trends", "_workbench_todos")
        }
        for name in self._monitor_backup:
            store.rows(name).clear()

    def tearDown(self) -> None:
        for name, rows in self._monitor_backup.items():
            store.rows(name)[:] = rows
        super().tearDown()

    def test_consume_is_idempotent(self) -> None:
        service = ReportService()
        service.ensure_reports()
        monitor = MonitorService()
        first = monitor.consume()
        trend_count = len(first["trend"])
        second = monitor.consume()
        self.assertEqual(len(second["trend"]), trend_count)
        self.assertTrue(all(item["状态"] == "已是最新" for item in second["consumed"]))

    def test_trend_point_per_report_version(self) -> None:
        service = ReportService()
        service.ensure_reports()
        monitor = MonitorService()
        monitor.consume()
        service.add_failure_event({
            "事件编号": "FAIL-TREND", "采集源": "官网抓取", "任务": "分类页",
            "事件时间": "2026-11-04T09:00:00", "上报时间": "2026-11-20T09:00:00",
            "失败原因": "接口超时", "归属记录": 5, "归属策略": "event_time",
        })
        result = monitor.consume()
        w45_report = next(row for row in service.list_reports() if row["统计周期"] == "2026-W45")
        trend = [point for point in result["trend"] if point["报告编号"] == w45_report["报告编号"]]
        self.assertEqual([point["版本"] for point in trend], [1, 2])
        self.assertEqual(trend[-1]["失败增量"], 1)
        summary = service.version_summaries(w45_report["报告编号"])
        latest = summary[-1]
        self.assertEqual(sum(point["失败增量"] for point in trend), latest["失败量"])

    def test_differences_written_back_and_todo_created(self) -> None:
        service = ReportService()
        service.ensure_reports()
        monitor = MonitorService()
        monitor.consume()
        service.add_failure_event({
            "事件编号": "FAIL-WRITEBACK", "采集源": "官网抓取", "任务": "分类页",
            "事件时间": "2026-11-04T09:00:00", "上报时间": "2026-11-18T10:00:00",
            "失败原因": "迟到补报", "归属记录": 5, "归属策略": "event_time",
        })
        result = monitor.consume()
        detail = service.get_by_period("2026-W45")
        self.assertIn("监控核对", {key for key in detail if key == "监控核对"})
        self.assertEqual(detail["监控核对"]["趋势版本"], detail["版本"])
        self.assertTrue(detail["监控核对"]["趋势失败量"] >= 0)
        todos = monitor.list_todos()
        self.assertTrue(todos)
        self.assertEqual(result["consumed"][0]["报告编号"], detail["报告编号"])

    def test_duplicate_todo_is_prevented(self) -> None:
        service = ReportService()
        service.ensure_reports()
        monitor = MonitorService()
        monitor.consume()
        count_before = len(monitor.list_todos())
        monitor.consume(force=True)
        self.assertEqual(len(monitor.list_todos()), count_before)


if __name__ == "__main__":
    unittest.main()
