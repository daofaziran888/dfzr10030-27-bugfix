"""采集报告接口：按统计周期汇总采集记录，生成可复核的周期采集报告。

报告不再是随机指标：列表、详情、导出、看板都读同一份周期快照，
同一周期反复生成或下午再打开，总量与平均耗时都保持不变。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult
from app.services.periods import PeriodError, normalize_period
from app.services.report import POLICY_LABELS, REQUIRED_FIELDS, ReportConflict, ReportService

router = APIRouter(prefix="/api/report", tags=["采集报告"])
service = ReportService()

LIST_FIELDS = ["报告编号", "采集源", "统计周期", "总采集量", "成功量", "失败量", "平均耗秒", "生成时间"]
PERIOD_HELP = "统计周期：2026-W45（ISO 周）、2026-11（整月）、2026-11-09（单日）或 2026-11-09~2026-11-15"


def _period_or_400(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_period(value)
    except PeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    """看板指标与列表同源：取最新一个周期报告的快照，不再随机取值。"""
    return service.summary()


@router.get("/events")
def list_events(period: str | None = Query(default=None, description=PERIOD_HELP)) -> dict[str, Any]:
    """失败事件清单，含归属周期与归属策略，便于核对迟到事件落在哪一版报告。"""
    normalized = _period_or_400(period)
    return {
        "items": service.list_events(normalized),
        "policies": [{"value": key, "label": label} for key, label in POLICY_LABELS.items()],
    }


@router.post("/events")
def create_event(payload: dict[str, Any]) -> ActionResult:
    """登记失败事件：事件时间口径修订原周期报告，报告生成时间口径计入新周期。"""
    event, message, reports = service.add_failure_event(payload)
    if event is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True,
        message=message,
        entry={
            "event": event,
            "reports": [
                {
                    "报告编号": report.get("报告编号"),
                    "统计周期": report.get("统计周期"),
                    "版本": report.get("版本"),
                    "总采集量": report.get("总采集量"),
                    "成功量": report.get("成功量"),
                    "失败量": report.get("失败量"),
                    "平均耗秒": report.get("平均耗秒"),
                    "生成时间": report.get("生成时间"),
                    "修订原因": report.get("修订原因"),
                }
                for report in reports
            ],
        },
    )


@router.get("")
def list_results(
    period: str | None = Query(default=None, description=PERIOD_HELP),
    status: str | None = Query(default=None, description="生成中、已生成、已发送"),
    keyword: str | None = Query(default=None, description="按报告编号或采集源检索"),
) -> dict[str, Any]:
    normalized = _period_or_400(period)
    items = service.list_reports(period=normalized, status=status, keyword=keyword)
    return {"items": items, "total": len(items), "page": 1, "size": len(items)}


@router.post("/period")
def create_period_report(payload: dict[str, Any]) -> ActionResult:
    """按统计周期生成报告；同一周期并发创建只有一条成功，后到请求被拦住。"""
    period = str(payload.get("统计周期") or payload.get("period") or "").strip()
    missing = [field for field in REQUIRED_FIELDS if not str(payload.get(field) or "").strip()]
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    try:
        report = service.create_report(period, payload.get("采集源"))
    except ReportConflict as exc:
        return ActionResult(ok=False, message=str(exc))
    except PeriodError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"统计周期 {report['统计周期']} 的采集报告已生成", entry=report)


@router.get("/export")
def export_entries(period: str | None = Query(default=None)) -> StreamingResponse:
    """下载当前筛选条件下的周期报告 CSV；先写 BOM，Excel 打开中文不乱码。"""
    normalized = _period_or_400(period)
    items = service.list_reports(period=normalized)
    columns = LIST_FIELDS + ["版本", "未结算量"]
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for item in items:
        row = []
        for key in columns:
            text = "" if item.get(key) is None else str(item.get(key))
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=report.csv"})


@router.post("/run")
def run_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """运行分析只回放周期报告快照，同一周期多次运行结果一致。"""
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    return {"items": service.run_analysis(values or {})}


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集报告 {entry_id} 不存在")
    return entry


@router.get("/{entry_id}/versions/{version}")
def get_version(entry_id: int, version: int) -> dict:
    """回查历史修订版摘要与当时的失败明细，修复后原依据仍可追溯。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集报告 {entry_id} 不存在")
    snapshot = service.get_version(str(entry.get("报告编号")), version)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"报告 {entry.get('报告编号')} 没有版本 {version}")
    return snapshot


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """重新生成：按当前采集记录与失败事件重算；来源未变化时保持原版本。"""
    action = payload.action.strip()
    if action != "重新生成":
        return ActionResult(ok=False, message=f"动作「{action}」不属于采集报告可执行范围")
    try:
        report, message, _snapshot = service.regenerate(entry_id, reason=payload.remark)
    except ReportConflict as exc:
        return ActionResult(ok=False, message=str(exc))
    if report is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=report)


@router.post("/{entry_id}/sync")
def sync_report(entry_id: int, payload: dict[str, Any] | None = None) -> ActionResult:
    """供采集监控回写核对结果：只改监控核对状态，不改统计口径。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=f"采集报告 {entry_id} 不存在")
    payload = payload or {}
    check = {
        "趋势版本": payload.get("趋势版本", entry.get("版本")),
        "趋势失败量": payload.get("趋势失败量", entry.get("失败量")),
        "核对结论": payload.get("核对结论", "一致"),
        "核对时间": payload.get("核对时间"),
    }
    service.record_monitor_check(str(entry.get("报告编号")), check)
    entry["监控核对"] = check
    entry["pending"] = False
    return ActionResult(ok=True, message="采集监控核对结果已回写", entry=entry)


