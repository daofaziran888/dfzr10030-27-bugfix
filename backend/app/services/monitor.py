"""采集监控业务规则：消费周期采集报告、同步趋势、回写核对结果与待办工作台。

- 周期报告是唯一数据源：监控不再随机生成指标，面板与趋势都来自报告快照。
- 同一条报告的同一个版本只消费一次；重复消费返回已有结果，重复待办会被拦住。
- 修订版报告的失败增量写入趋势，趋势点与报告版本一一对应。
- 核对的差异（未归因失败、状态冲突、总量不一致等）回写报告，并进入待办工作台。
"""
from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from app.services.periods import PeriodError, normalize_period
from app.services.report import ReportService
from app.store import store

MODULE = "monitor"
PANEL_TABLE = "monitor"
REPORT_MIRROR_TABLE = "_monitor_reports"
TREND_TABLE = "_monitor_trends"
TODO_TABLE = "_workbench_todos"

LIST_FIELDS = ["面板编号", "采集源总数", "今日采集量", "成功率", "平均延迟", "失败重试", "异常告警", "更新时间"]
REQUIRED_FIELDS = ["面板编号", "采集源总数", "今日采集量"]
STATUS_ORDER = ["正常", "预警", "告警"]
ACTION_RULES = {"查看详情": "正常"}
NEGATIVE_ACTIONS: list[str] = []

TODO_STATUSES = ["待处理", "处理中", "已处理"]
TODO_ACTIONS = {"开始处理": "处理中", "标记已处理": "已处理"}

_CONSUME_LOCK = threading.Lock()


def _now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def _iso(value: dt.datetime | None) -> str:
    return value.replace(microsecond=0).isoformat() if value else ""


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def _diff(report_version: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, int]:
    """修订版相对上一版的失败/成功增量；首版相对零基线。"""
    previous = previous or {}
    return {
        "失败增量": int(report_version.get("失败量", 0)) - int(previous.get("失败量", 0)),
        "成功增量": int(report_version.get("成功量", 0)) - int(previous.get("成功量", 0)),
        "总量增量": int(report_version.get("总采集量", 0)) - int(previous.get("总采集量", 0)),
    }


class MonitorService:
    def __init__(self) -> None:
        self.reports = ReportService()

    # ---------- 既有面板 ----------

    def list_results(self) -> list[dict[str, Any]]:
        """面板列表把每个面板指向最新周期的报告快照，避免与报告页分叉。"""
        rows = store.rows(PANEL_TABLE)
        latest = self._latest_consumed()
        results: list[dict] = []
        for row in rows:
            d = dict(row)
            d["指标值"] = self._panel_rate(latest)
            d["对比基准"] = 0.0
            d["差异"] = round(d["指标值"], 3)
            d["关联报告编号"] = latest.get("报告编号") if latest else None
            d["统计周期"] = latest.get("统计周期") if latest else None
            results.append(d)
        return results

    def _latest_consumed(self) -> dict[str, Any] | None:
        reports = [dict(row) for row in store.rows(REPORT_MIRROR_TABLE)]
        if not reports:
            return None
        return max(reports, key=lambda row: (str(row.get("统计周期", "")), str(row.get("报告编号", ""))))

    def _panel_rate(self, latest: dict[str, Any] | None) -> float:
        if latest is None:
            return 0.0
        total = int(latest.get("总采集量", 0))
        return round(int(latest.get("成功量", 0)) / total, 3) if total else 0.0

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(PANEL_TABLE, entry_id)

    # ---------- 报告消费 ----------

    def _trend_for(self, report_no: str) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(TREND_TABLE) if str(row.get("报告编号")) == report_no]
        return sorted(rows, key=lambda row: int(row.get("版本", 0)))

    def _mirror_for(self, report_no: str) -> dict[str, Any] | None:
        for row in store.rows(REPORT_MIRROR_TABLE):
            if str(row.get("报告编号")) == report_no:
                return row
        return None

    def _find_todo(self, report_no: str, version: int, reason: str) -> dict[str, Any] | None:
        for row in store.rows(TODO_TABLE):
            if (str(row.get("报告编号")) == report_no
                    and int(row.get("版本", 0)) == version
                    and str(row.get("待办原因")) == reason):
                return row
        return None

    def _ensure_todo(self, report: dict[str, Any], reason: str, detail: str, version: int | None = None) -> dict[str, Any]:
        """同一报告版本 + 同一原因只建一条待办，后到的重复诉求直接返回原记录。"""
        report_no = str(report.get("报告编号"))
        version = int(version if version is not None else report.get("版本", 1))
        existing = self._find_todo(report_no, version, reason)
        if existing is not None:
            return existing
        todo = {
            "id": _next_id(TODO_TABLE),
            "待办编号": f"TODO-{report_no}-{version}-{_next_id(TODO_TABLE):03d}",
            "报告编号": report_no,
            "统计周期": report.get("统计周期"),
            "采集源": report.get("采集源"),
            "版本": version,
            "待办原因": reason,
            "待办说明": detail,
            "来源": "采集监控",
            "状态": TODO_STATUSES[0],
            "创建时间": _iso(_now()),
            "处理人": "",
            "处理时间": "",
        }
        store.rows(TODO_TABLE).append(todo)
        return todo

    def consume(self, period: str | None = None, *, force: bool = False) -> dict[str, Any]:
        """消费周期报告：持久化报告结果、同步失败增量到趋势、差异回写报告与待办工作台。"""
        with _CONSUME_LOCK:
            normalized = normalize_period(period) if period else None
            try:
                reports = self.reports.list_reports(period=normalized)
            except PeriodError as exc:
                raise ValueError(str(exc)) from exc
            consumed: list[dict[str, Any]] = []
            todos: list[dict[str, Any]] = []
            for report in reports:
                detail = self.reports.get_entry(int(report.get("id", 0)))
                if detail is None:
                    continue
                report_no = str(detail.get("报告编号"))
                versions = self.reports.version_summaries(report_no)
                if not versions:
                    continue
                trend = self._trend_for(report_no)
                known = {int(row.get("版本", 0)) for row in trend}
                latest_version = int(versions[-1]["版本"])
                added = 0
                if latest_version in known and not force:
                    mirror = self._mirror_for(report_no)
                    if mirror is not None:
                        consumed.append({
                            "报告编号": report_no,
                            "统计周期": mirror.get("统计周期"),
                            "已消费版本": mirror.get("已消费版本"),
                            "最新版本": latest_version,
                            "本次新增趋势点": 0,
                            "失败增量": mirror.get("失败增量", 0),
                            "核对结论": mirror.get("核对结论"),
                            "差异": mirror.get("差异", []),
                            "状态": "已是最新",
                        })
                        continue

                for index, version in enumerate(versions):
                    version_no = int(version["版本"])
                    if version_no in known:
                        continue
                    previous = versions[index - 1] if index > 0 else None
                    delta = _diff(version, previous)
                    snapshot = self.reports.get_version(report_no, version_no) or {}
                    point = {
                        "id": _next_id(TREND_TABLE),
                        "报告id": int(detail.get("id", 0)),
                        "报告编号": report_no,
                        "统计周期": version.get("统计周期"),
                        "采集源": version.get("采集源"),
                        "版本": version_no,
                        "总采集量": version.get("总采集量", 0),
                        "成功量": version.get("成功量", 0),
                        "失败量": version.get("失败量", 0),
                        "平均耗秒": version.get("平均耗秒", 0),
                        "未结算量": version.get("未结算量", 0),
                        "失败增量": delta["失败增量"],
                        "成功增量": delta["成功增量"],
                        "总量增量": delta["总量增量"],
                        "生成时间": version.get("生成时间"),
                        "修订原因": version.get("修订原因", ""),
                        "异常项数": len(snapshot.get("异常项") or []),
                        "未归因失败量": snapshot.get("未归因失败量", 0),
                        "消费时间": _iso(_now()),
                    }
                    store.rows(TREND_TABLE).append(point)
                    known.add(version_no)
                    added += 1
                    # 首版是首次生成，只入趋势；修订版才需要人工复核。
                    if version_no > 1:
                        todos.append(self._ensure_todo(
                            detail,
                            f"报告修订复核（版本 {version_no}）",
                            f"{version.get('统计周期')} 报告 {report_no} 修订到版本 {version_no}，"
                            f"失败增量 {point['失败增量']:+d}，修订原因：{version.get('修订原因', '')}",
                            version_no,
                        ))

                trend = self._trend_for(report_no)
                # 每个版本一个趋势点：失败增量逐版累加应等于最新版本快照的失败量。
                delta_total = sum(int(point.get("失败增量", 0)) for point in trend)
                trend_failure = int(trend[-1].get("失败量", 0)) if trend else 0
                total_failure = int(detail.get("失败量", 0))
                differences: list[str] = []
                if delta_total != total_failure:
                    differences.append(f"趋势失败增量累计 {delta_total} 与报告失败量 {total_failure} 不一致")
                if int(detail.get("未归因失败量", 0)):
                    differences.append(f"存在 {detail.get('未归因失败量')} 条未归因失败事件，只能计入失败量")
                reconcile = detail.get("核对") or {}
                if str(reconcile.get("核对结论")) != "一致":
                    differences.append("报告自身成功量 + 失败量无法回到采集记录")
                for anomaly in detail.get("异常项") or []:
                    if anomaly.get("类型") in {"状态冲突", "记录迁移", "记录迁入", "重复失败事件", "记录错位"}:
                        differences.append(f"{anomaly.get('类型')}：{anomaly.get('说明')}")
                conclusion = "一致" if not differences else "存在差异"
                if differences:
                    todos.append(self._ensure_todo(
                        detail,
                        "监控核对差异",
                        f"{detail.get('统计周期')} 报告 {report_no} 版本 {detail.get('版本')}：" + "；".join(differences),
                    ))

                mirror = self._mirror_for(report_no)
                payload = {
                    "报告编号": report_no,
                    "统计周期": detail.get("统计周期"),
                    "采集源": detail.get("采集源"),
                    "已消费版本": int(trend[-1].get("版本", 1)) if trend else 0,
                    "总采集量": detail.get("总采集量", 0),
                    "成功量": detail.get("成功量", 0),
                    "失败量": detail.get("失败量", 0),
                    "平均耗秒": detail.get("平均耗秒", 0),
                    "失败增量": delta_total,
                    "最新失败增量": int(trend[-1].get("失败增量", 0)) if trend else 0,
                    "趋势点数": len(trend),
                    "差异": differences,
                    "核对结论": conclusion,
                    "消费时间": _iso(_now()),
                }
                if mirror is None:
                    mirror = {"id": _next_id(REPORT_MIRROR_TABLE), **payload}
                    store.rows(REPORT_MIRROR_TABLE).append(mirror)
                else:
                    mirror.update(payload)

                # 差异与核对结论回写采集报告；只写监控核对字段，不改统计口径。
                check = {
                    "趋势版本": payload["已消费版本"],
                    "趋势失败量": trend_failure,
                    "趋势失败增量": delta_total,
                    "核对结论": conclusion,
                    "差异": differences,
                    "核对时间": payload["消费时间"],
                }
                self.reports.record_monitor_check(report_no, check)

                consumed.append({
                    "报告编号": report_no,
                    "统计周期": detail.get("统计周期"),
                    "已消费版本": payload["已消费版本"],
                    "最新版本": latest_version,
                    "本次新增趋势点": added,
                    "失败增量": delta_total,
                    "核对结论": conclusion,
                    "差异": differences,
                    "状态": "已消费" if not differences else "存在差异",
                })

            created: list[dict[str, Any]] = []
            for todo in todos:
                if all(int(item.get("id", 0)) != int(todo.get("id", 0)) for item in created):
                    created.append(todo)
            return {
                "ok": True,
                "consumed": consumed,
                "todos": created,
                "trend": self.trend(),
                "summary": self.summary(),
            }

    def ensure_consumed(self) -> None:
        """面板打开前先把报告的新版本消费掉，避免面板停留在旧结果。"""
        reports = self.reports.list_reports()
        pending = False
        for report in reports:
            report_no = str(report.get("报告编号"))
            versions = self.reports.version_summaries(report_no)
            if not versions:
                continue
            mirror = self._mirror_for(report_no)
            if mirror is None or int(mirror.get("已消费版本", 0)) != int(versions[-1]["版本"]):
                pending = True
                break
        if pending:
            self.consume()

    # ---------- 查询 ----------

    def list_consumed_reports(self, period: str | None = None) -> list[dict[str, Any]]:
        if period:
            normalized = normalize_period(period)
            return [dict(row) for row in store.rows(REPORT_MIRROR_TABLE) if str(row.get("统计周期")) == normalized]
        return [dict(row) for row in store.rows(REPORT_MIRROR_TABLE)]

    def trend(self, report_no: str | None = None, period: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(TREND_TABLE)]
        if report_no:
            rows = [row for row in rows if str(row.get("报告编号")) == report_no]
        if period:
            normalized = normalize_period(period)
            rows = [row for row in rows if str(row.get("统计周期")) == normalized]
        return sorted(rows, key=lambda row: (str(row.get("统计周期", "")), str(row.get("报告编号", "")), int(row.get("版本", 0))))

    def list_todos(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(TODO_TABLE)]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def run_todo_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(TODO_TABLE, entry_id)
        if entry is None:
            return None, f"待办 {entry_id} 不存在或已归档"
        if action not in TODO_ACTIONS:
            return None, f"动作「{action}」不属于待办工作台可执行范围"
        target = TODO_ACTIONS[action]
        entry["状态"] = target
        entry["处理时间"] = _iso(_now())
        return entry, f"待办已{action}"

    def summary(self) -> dict[str, Any]:
        """面板指标来自已持久化的周期报告消费结果，与报告页同源，避免两处状态分叉。"""
        reports = [dict(row) for row in store.rows(REPORT_MIRROR_TABLE)]
        latest = max(reports, key=lambda row: str(row.get("统计周期", "")), default=None)
        todos = [row for row in store.rows(TODO_TABLE) if row.get("状态") != "已处理"]
        latest_no = str(latest.get("报告编号", "")) if latest else ""
        # 版本号跟随最新一份报告的趋势点，避免把别的报告的高版本算进来。
        trend = max((int(row.get("版本", 0)) for row in store.rows(TREND_TABLE)
                     if not latest_no or str(row.get("报告编号")) == latest_no), default=0)
        if latest is None:
            return {"metrics": [
                {"label": "周期采集量", "value": 0, "trend": 0},
                {"label": "成功率", "value": 0, "trend": 0},
                {"label": "监控趋势版本", "value": 0, "trend": 0},
                {"label": "待办", "value": len(todos), "trend": 0},
            ], "period": None, "report": None}
        total = int(latest.get("总采集量", 0))
        rate = round(int(latest.get("成功量", 0)) / total, 3) if total else 0.0
        return {"metrics": [
            {"label": "周期采集量", "value": total, "trend": int(latest.get("最新失败增量", 0))},
            {"label": "成功率", "value": rate, "trend": 0},
            {"label": "监控趋势版本", "value": trend, "trend": int(latest.get("失败增量", 0))},
            {"label": "待办", "value": len(todos), "trend": len(todos)},
        ], "period": latest.get("统计周期"), "report": latest}

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """分析结果来自持久化趋势，同一报告版本只对应一个趋势点。"""
        period = params.get("统计周期") or params.get("period")
        normalized = normalize_period(str(period)) if period else None
        trend = self.trend(period=normalized)
        if not trend:
            self.consume(normalized)
            trend = self.trend(period=normalized)
        return [
            {
                "id": point.get("id", index),
                "报告编号": point.get("报告编号"),
                "采集源": point.get("采集源"),
                "统计周期": point.get("统计周期"),
                "版本": point.get("版本"),
                "总采集量": point.get("总采集量", 0),
                "成功量": point.get("成功量", 0),
                "失败量": point.get("失败量", 0),
                "失败增量": point.get("失败增量", 0),
                "平均耗秒": point.get("平均耗秒", 0),
                "生成时间": point.get("生成时间"),
            }
            for index, point in enumerate(trend, start=1)
        ]

    def analyze(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self.run_analysis(params)
