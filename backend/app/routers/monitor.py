"""采集监控接口：消费周期采集报告、展示趋势与报告版本对照、维护待办工作台。

监控不自己造指标：看板、趋势、分析都读「周期采集报告」的持久化结果，
报告修订后只消费新版本，失败增量同步进趋势，核对差异回写报告。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionResult
from app.services.monitor import TODO_STATUSES, MonitorService
from app.services.periods import PeriodError, normalize_period

router = APIRouter(prefix="/api/monitor", tags=["采集监控"])
service = MonitorService()

LIST_FIELDS = ["面板编号", "采集源总数", "今日采集量", "成功率", "平均延迟", "失败重试", "异常告警", "更新时间"]
PERIOD_HELP = "统计周期：2026-W45（ISO 周）、2026-11（整月）、2026-11-09（单日）"


def _period_or_400(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_period(value)
    except PeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    """面板 KPI 与报告页同源：打开面板先消费最新报告版本，避免显示旧结果。"""
    service.ensure_consumed()
    return service.summary()


@router.get("")
def list_results() -> dict[str, Any]:
    """监控面板列表：指标值来自已消费的周期报告，不再是随机数。"""
    return {"items": service.list_results()}


@router.get("/reports")
def list_reports(period: str | None = Query(default=None, description=PERIOD_HELP)) -> dict[str, Any]:
    """已消费的周期报告（持久化结果），含已消费版本与核对结论。"""
    normalized = _period_or_400(period)
    items = service.list_consumed_reports(normalized)
    return {"items": items, "total": len(items)}


@router.post("/reports/consume")
def consume_reports(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """消费周期报告：同一版本只消费一次，重复调用不会重复生成趋势点与待办。"""
    payload = payload or {}
    normalized = _period_or_400(payload.get("统计周期") or payload.get("period"))
    try:
        return service.consume(normalized, force=bool(payload.get("force")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/trend")
def get_trend(
    report: str | None = Query(default=None, description="报告编号"),
    period: str | None = Query(default=None, description=PERIOD_HELP),
) -> dict[str, Any]:
    """监控趋势：每个点对应一个报告版本，失败增量来自修订版报告。"""
    normalized = _period_or_400(period)
    return {"items": service.trend(report_no=report, period=normalized)}


@router.get("/todos")
def list_todos(status: str | None = Query(default=None, description="待处理、处理中、已处理")) -> dict[str, Any]:
    items = service.list_todos(status)
    return {"items": items, "total": len(items), "statuses": TODO_STATUSES}


@router.post("/todos/{entry_id}/actions", response_model=ActionResult)
def run_todo_action(entry_id: int, payload: dict[str, Any]) -> ActionResult:
    action = str(payload.get("action") or "").strip()
    entry, message = service.run_todo_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries(request: Request) -> StreamingResponse:
    items = service.list_results()
    columns = ["id", *LIST_FIELDS, "status"]
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    for item in items:
        row = []
        for key in columns:
            text = "" if item.get(key) is None else str(item.get(key))
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="monitor.csv"'})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"监控面板 {entry_id} 不存在")
    return entry


@router.post("/run")
def run_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    return {"items": service.run_analysis(values or {})}
