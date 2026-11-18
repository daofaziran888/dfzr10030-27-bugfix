"""采集报告业务规则：按统计周期汇总采集记录与失败事件，生成可复核的周期快照。

设计要点：
- 报告只读「采集记录 + 失败事件」，不再用随机数填充成功量/失败量/平均耗时。
- 同一 (统计周期, 采集源) 只允许有一条报告；并发创建时只放行一个，后到请求直接拦下。
- 每次来源数据变化都会留下版本快照，旧摘要与当时的失败明细仍可按版本回查。
- 失败事件按「事件时间」归属时修订原周期；按「报告生成时间」归属时计入下一周期。
  两种口径都会把失败事件与它指向的采集记录绑定，并把记录整体迁到归属周期，
  因此每个周期的成功量 + 失败量都能回到采集记录，不会重复计数。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
from typing import Any

from app.services.periods import PeriodError, normalize_period, parse_period, period_of
from app.store import store

MODULE = "report"
REPORT_TABLE = "report"
RECORD_TABLE = "_collect_records"
EVENT_TABLE = "_failure_events"
VERSION_TABLE = "_report_versions"

LIST_FIELDS = ["报告编号", "采集源", "统计周期", "总采集量", "成功量", "失败量", "平均耗秒", "生成时间"]
REQUIRED_FIELDS = ["统计周期"]
STATUS_ORDER = ["生成中", "已生成", "已发送"]
ACTION_RULES = {"重新生成": "已生成"}
NEGATIVE_ACTIONS: list[str] = []

ALL_SOURCES = "全部采集源"
SUCCESS = "成功"
FAILED = "失败"
RUNNING = "运行中"
SETTLED = {SUCCESS, FAILED}
POLICY_EVENT_TIME = "event_time"
POLICY_REPORT_TIME = "report_time"
POLICY_LABELS = {
    POLICY_EVENT_TIME: "按事件发生时间",
    POLICY_REPORT_TIME: "按报告生成时间",
}


class ReportConflict(RuntimeError):
    """同一周期报告正在生成或已经存在。"""


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[tuple[str, str], threading.Lock] = {}


def _lock_for(period: str, scope: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault((period, scope), threading.Lock())


def _now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def _parse_time(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip().replace("Z", "").replace("T", " ")
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _iso(value: dt.datetime | None) -> str:
    return value.replace(microsecond=0).isoformat() if value else ""


def _scope_of(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text in {"全部", "全部采集源", "ALL", "all"}:
        return ALL_SOURCES
    return text


def _compact_period(period: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", period)


def _slug(value: str) -> str:
    """采集源转成报告编号后缀；中文等非 ASCII 名称用短哈希，避免不同来源撞号。"""
    ascii_part = re.sub(r"[^0-9A-Za-z]+", "-", value).strip("-")
    if ascii_part:
        return ascii_part
    return f"S{hashlib.sha1(value.encode('utf-8')).hexdigest()[:6].upper()}"


def _in_scope(row: dict[str, Any], scope: str) -> bool:
    return scope == ALL_SOURCES or str(row.get("采集源", "")) == scope


def _record_time(record: dict[str, Any]) -> dt.datetime | None:
    return _parse_time(record.get("发生时间"))


def _event_time(event: dict[str, Any]) -> dt.datetime | None:
    return _parse_time(event.get("事件时间"))


def _event_reported_time(event: dict[str, Any]) -> dt.datetime | None:
    return _parse_time(event.get("上报时间")) or _event_time(event)


def _event_policy(event: dict[str, Any]) -> str:
    policy = str(event.get("归属策略") or POLICY_EVENT_TIME).strip()
    return POLICY_REPORT_TIME if policy == POLICY_REPORT_TIME else POLICY_EVENT_TIME


def _event_period(event: dict[str, Any]) -> str | None:
    """失败事件归属的统计周期；口径决定用事件时间还是上报（报告生成）时间。"""
    explicit = str(event.get("归属周期") or "").strip()
    if explicit:
        return normalize_period(explicit)
    moment = _event_time(event)
    if _event_policy(event) == POLICY_REPORT_TIME:
        moment = _event_reported_time(event)
    if moment is None:
        return None
    return period_of(moment)


def _match_record(event: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """失败事件对应的采集记录：优先用显式归属记录，否则按来源+任务+事件时间匹配。"""
    explicit = event.get("归属记录")
    if explicit not in (None, "", 0, "0"):
        try:
            target_id = int(explicit)
        except (TypeError, ValueError):
            return None
        for record in records:
            if int(record.get("id", 0)) == target_id:
                return record
        return None
    moment = _event_time(event)
    if moment is None:
        return None
    source = str(event.get("采集源", ""))
    task = str(event.get("任务", ""))
    for record in records:
        if str(record.get("采集源", "")) != source:
            continue
        if task and str(record.get("任务", "")) != task:
            continue
        if _record_time(record) == moment:
            return record
    return None


def _duration(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status_of(record: dict[str, Any]) -> str:
    status = str(record.get("状态", "")).strip()
    return status if status in SETTLED else RUNNING


class ReportService:
    # ---------- 汇总 ----------

    def _migration_index(self) -> dict[int, tuple[str, dict[str, Any]]]:
        """记录被失败事件迁离原周期：record_id -> (原周期, 事件)。

        事件时间/报告生成时间只要与记录自身发生时间落在不同周期，就把整条记录
        迁到事件归属周期，原周期不能再按原状态统计，否则同一条记录会在两个周期
        里各算一次。
        """
        index: dict[int, tuple[str, dict[str, Any]]] = {}
        records = list(store.rows(RECORD_TABLE))
        for event in store.rows(EVENT_TABLE):
            target = _match_record(event, records)
            if target is None:
                continue
            target_moment = _record_time(target)
            attributed = _event_period(event)
            if target_moment is None or attributed is None:
                continue
            source_period = period_of(target_moment)
            if source_period != attributed:
                index[int(target.get("id", 0))] = (source_period, event)
        return index

    def _aggregate(self, period: str, scope: str) -> dict[str, Any]:
        """按周期汇总采集记录与失败事件，返回可落盘的快照。"""
        start, end = parse_period(period)
        records = list(store.rows(RECORD_TABLE))
        migrations = self._migration_index()

        period_records = [
            row for row in records
            if _in_scope(row, scope)
            and (moment := _record_time(row)) is not None
            and start <= moment < end
        ]
        migrated_out = {
            int(row.get("id", 0)): migrations[int(row.get("id", 0))]
            for row in period_records
            if int(row.get("id", 0)) in migrations
        }

        # 周期内失败事件：按生效口径归属到本周期。
        period_events: list[dict[str, Any]] = []
        for event in store.rows(EVENT_TABLE):
            attributed = _event_period(event)
            if attributed == period and _in_scope(event, scope):
                period_events.append(event)

        state = {
            int(row.get("id", 0)): _status_of(row)
            for row in period_records
            if int(row.get("id", 0)) not in migrated_out
        }
        records_by_id = {int(row.get("id", 0)): row for row in records}
        events_by_record: dict[int, list[dict[str, Any]]] = {}
        failures: list[dict[str, Any]] = []
        anomalies: list[dict[str, Any]] = []
        migrated_in_ids: set[int] = set()

        for record in period_records:
            record_id = int(record.get("id", 0))
            if record_id not in migrated_out:
                continue
            source_period, event = migrated_out[record_id]
            target_period = _event_period(event) or ""
            anomalies.append({
                "类型": "记录迁移",
                "对象": str(record.get("任务", "")),
                "说明": f"采集记录 {record_id} 按{POLICY_LABELS[_event_policy(event)]}归属到 {target_period}，本周期不再统计",
                "依据": str(event.get("事件编号", "")),
            })

        period_events.sort(key=lambda event: (_event_time(event) or dt.datetime.min, str(event.get("事件编号", ""))))
        for event in period_events:
            event_no = str(event.get("事件编号", ""))
            moment = _event_time(event)
            reported = _event_reported_time(event)
            target = _match_record(event, records)
            base = {
                "任务": str(event.get("任务", "")),
                "发生时间": _iso(moment),
                "失败原因": str(event.get("失败原因", "")),
                "事件编号": event_no,
                "耗时秒": None,
            }
            if target is None:
                failures.append({
                    **base,
                    "记录编号": "",
                    "来源": "失败事件",
                    "归因": "未归因失败事件：没有对应采集记录，单列失败，不计成功",
                })
                anomalies.append({
                    "类型": "未归因失败",
                    "对象": event_no,
                    "说明": "失败事件没有对应的采集记录，已单独计入失败量，不会被算作成功",
                    "依据": event_no,
                })
                continue

            target_id = int(target.get("id", 0))
            target_moment = _record_time(target)
            target_period = period_of(target_moment) if target_moment else ""
            if target_id in migrated_out:
                # 记录已按另一条失败事件的生效口径迁走，本周期不再统计这条记录，
                # 也不重复统计它的事件；否则成功+失败会超出采集记录数。
                moved_period, moved_event = migrated_out[target_id]
                anomalies.append({
                    "类型": "事件随记录迁移",
                    "对象": event_no,
                    "说明": (
                        f"采集记录 {target_id} 已随 {moved_event.get('事件编号')} 按"
                        f"{POLICY_LABELS[_event_policy(moved_event)]}迁到 {moved_period}，"
                        "本事件不再在本周期重复计失败"
                    ),
                    "依据": event_no,
                })
                continue
            already = events_by_record.setdefault(target_id, [])
            if already:
                anomalies.append({
                    "类型": "重复失败事件",
                    "对象": event_no,
                    "说明": f"采集记录 {target_id} 已由 {already[0].get('事件编号')} 归因，同一记录只计一次失败",
                    "依据": event_no,
                })
                continue
            already.append(event)

            if target_period != period:
                # 记录按事件口径迁入本周期；原周期已通过 migrated_out 排除该记录。
                migrated_in_ids.add(target_id)
                failures.append({
                    **base,
                    "记录编号": str(target_id),
                    "来源": "失败事件",
                    "归因": f"迟到失败事件按{POLICY_LABELS[_event_policy(event)]}归入本周期（原周期 {target_period}）",
                    "耗时秒": _duration(target.get("耗时秒")),
                })
                anomalies.append({
                    "类型": "记录迁入",
                    "对象": event_no,
                    "说明": f"采集记录 {target_id} 由 {target_period} 迁移到 {period}，原周期已排除，失败量只在本周期计一次",
                    "依据": event_no,
                })
                continue

            original = state.get(target_id, RUNNING)
            if original != FAILED:
                state[target_id] = FAILED
                if original == SUCCESS:
                    anomalies.append({
                        "类型": "状态冲突",
                        "对象": str(target.get("任务", "")),
                        "说明": f"采集记录 {target_id} 原状态为成功，按失败事件 {event_no} 的生效口径改判为失败，不再计入成功",
                        "依据": event_no,
                    })
                else:
                    anomalies.append({
                        "类型": "状态冲突",
                        "对象": str(target.get("任务", "")),
                        "说明": f"采集记录 {target_id} 仍在运行，按失败事件 {event_no} 结算为失败",
                        "依据": event_no,
                    })

            if reported is not None and reported >= end and moment is not None and moment < end:
                anomalies.append({
                    "类型": "迟到失败事件",
                    "对象": event_no,
                    "说明": (
                        f"事件时间 {_iso(moment)} 落在 {period}，上报时间 {_iso(reported)} 晚于周期结束，"
                        "按事件发生时间修订原周期报告"
                    ),
                    "依据": event_no,
                })

        # 失败明细：周期内被判为失败的记录 + 迁入记录 + 未归因事件。
        for record_id, status in sorted(state.items()):
            if status != FAILED:
                continue
            record = records_by_id[record_id]
            matched = events_by_record.get(record_id, [])
            event_nos = [str(event.get("事件编号", "")) for event in matched]
            if event_nos:
                attribution = f"失败事件 {event_nos[0]} 归集"
                if str(record.get("状态", "")) != FAILED:
                    attribution += "（原状态成功，已改判）"
            else:
                attribution = "采集记录状态为失败"
            moment = _record_time(record)
            event_reason = str(matched[0].get("失败原因", "")) if matched else ""
            failures.append({
                "记录编号": str(record_id),
                "任务": str(record.get("任务", "")),
                "发生时间": _iso(moment),
                "失败原因": str(record.get("失败原因", "")) or event_reason or "采集记录状态为失败",
                "事件编号": event_nos[0] if event_nos else "",
                "来源": "采集记录",
                "归因": attribution,
                "耗时秒": _duration(record.get("耗时秒")),
            })

        failures.sort(key=lambda item: (
            str(item.get("发生时间", "")), str(item.get("记录编号", "")), str(item.get("事件编号", "")),
        ))

        success = sum(1 for status in state.values() if status == SUCCESS)
        failed_records = sum(1 for status in state.values() if status == FAILED)
        unsettled = sum(1 for status in state.values() if status == RUNNING)
        unattributed = sum(1 for item in failures if not item.get("记录编号"))
        migrated_in = sum(1 for item in failures if item.get("记录编号") and int(item["记录编号"]) not in state)
        total = success + len(failures)

        counted_durations = [
            _duration(records_by_id[record_id].get("耗时秒"))
            for record_id, status in state.items()
            if status in SETTLED
        ]
        counted_durations += [
            _duration(records_by_id[record_id].get("耗时秒"))
            for record_id in migrated_in_ids
        ]
        durations = [value for value in counted_durations if value is not None]
        average = round(sum(durations) / len(durations), 3) if durations else 0.0

        if unsettled:
            anomalies.append({
                "类型": "未结算记录",
                "对象": period,
                "说明": f"本周期有 {unsettled} 条采集记录仍在运行，未计入成功量或失败量",
                "依据": "",
            })

        record_accounted = success + failed_records + unsettled + len(migrated_out)
        failures_accounted = success + len(failures)
        reconcile_ok = record_accounted == len(period_records) and failures_accounted == total
        if not reconcile_ok:
            anomalies.append({
                "类型": "总量核对",
                "对象": period,
                "说明": "成功量 + 失败量无法回到采集记录，报告已标记异常",
                "依据": "",
            })
        reconciliation = {
            "采集记录数": len(period_records),
            "成功量": success,
            "失败量": len(failures),
            "未结算量": unsettled,
            "迁移出记录数": len(migrated_out),
            "迁移入记录数": migrated_in,
            "未归因失败事件数": unattributed,
            "记录核对": f"{success}+{failed_records}+{unsettled}+{len(migrated_out)}={record_accounted}/{len(period_records)}",
            "失败核对": f"{len(failures)} 条失败明细（记录失败 {failed_records}、迁入 {migrated_in}、未归因 {unattributed}）",
            "核对结论": "一致" if reconcile_ok else "不一致",
        }

        return {
            "统计周期": period,
            "采集源": scope,
            "总采集量": total,
            "成功量": success,
            "失败量": len(failures),
            "平均耗秒": average,
            "未结算量": unsettled,
            "采集记录数": len(period_records),
            "失败事件数": len(period_events),
            "未归因失败量": unattributed,
            "失败明细": failures,
            "异常项": anomalies,
            "核对": reconciliation,
            "依据": {
                "采集记录": sorted(int(row.get("id", 0)) for row in period_records),
                "迁移出记录": sorted(migrated_out),
                "迁移入记录": sorted(migrated_in_ids),
                "失败事件": sorted(str(event.get("事件编号", "")) for event in period_events),
                "平均耗时样本数": len(durations),
                "耗时合计": round(sum(durations), 3),
            },
        }

    # ---------- 版本 ----------

    def _versions(self, report_no: str) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(VERSION_TABLE) if str(row.get("报告编号")) == report_no]
        return sorted(rows, key=lambda row: int(row.get("版本", 0)))

    def _snapshot_key(self, snapshot: dict[str, Any]) -> tuple[Any, ...]:
        """用于判断来源数据是否变化；只比对报告内容，异常说明文字变化不算新版。"""
        # 只比对数据本身，归因说明这类文案变化不触发新版。
        failure_keys = sorted(
            (str(item.get("记录编号", "")), str(item.get("事件编号", "")), str(item.get("失败原因", "")))
            for item in snapshot.get("失败明细") or []
        )
        return (
            int(snapshot.get("总采集量", 0)),
            int(snapshot.get("成功量", 0)),
            int(snapshot.get("失败量", 0)),
            round(float(snapshot.get("平均耗秒") or 0), 3),
            int(snapshot.get("未结算量", 0)),
            int(snapshot.get("未归因失败量", 0)),
            tuple(failure_keys),
        )

    def _version_snapshot(self, report: dict[str, Any], aggregate: dict[str, Any], reason: str, generated_at: dt.datetime) -> dict[str, Any]:
        versions = self._versions(str(report["报告编号"]))
        version = max((int(row.get("版本", 0)) for row in versions), default=0) + 1
        version_id = max((int(row.get("id", 0)) for row in store.rows(VERSION_TABLE)), default=0) + 1
        snapshot = {
            "id": version_id,
            "报告id": int(report.get("id", 0)),
            "报告编号": str(report["报告编号"]),
            "统计周期": str(report["统计周期"]),
            "采集源": str(report["采集源"]),
            "版本": version,
            "总采集量": aggregate["总采集量"],
            "成功量": aggregate["成功量"],
            "失败量": aggregate["失败量"],
            "平均耗秒": aggregate["平均耗秒"],
            "未结算量": aggregate["未结算量"],
            "采集记录数": aggregate["采集记录数"],
            "失败事件数": aggregate["失败事件数"],
            "未归因失败量": aggregate["未归因失败量"],
            "生成时间": _iso(generated_at),
            "修订原因": reason,
            "失败明细": aggregate["失败明细"],
            "异常项": aggregate["异常项"],
            "核对": aggregate["核对"],
            "依据": aggregate["依据"],
            "指标摘要": (
                f"总采集量 {aggregate['总采集量']}，成功 {aggregate['成功量']}，失败 {aggregate['失败量']}，"
                f"未结算 {aggregate['未结算量']}，平均耗秒 {aggregate['平均耗秒']}"
            ),
        }
        store.rows(VERSION_TABLE).append(snapshot)
        return snapshot

    def _apply_snapshot(self, report: dict[str, Any], snapshot: dict[str, Any]) -> None:
        for field in ("总采集量", "成功量", "失败量", "平均耗秒", "未结算量",
                      "采集记录数", "失败事件数", "未归因失败量"):
            report[field] = snapshot[field]
        report["版本"] = snapshot["版本"]
        report["生成时间"] = snapshot["生成时间"]
        report["修订原因"] = snapshot["修订原因"]
        report["失败明细"] = snapshot["失败明细"]
        report["异常项"] = snapshot["异常项"]
        report["核对"] = snapshot["核对"]
        report["依据"] = snapshot["依据"]
        report["status"] = "已生成"
        report["pending"] = bool(snapshot["版本"] > 1 or snapshot["失败明细"])
        report["abnormal"] = bool(snapshot["异常项"])

    def _new_report_number(self, period: str, scope: str) -> str:
        base = f"REPO-{_compact_period(period)}"
        if scope != ALL_SOURCES:
            base = f"{base}-{_slug(scope)}"
        existing = {str(row.get("报告编号")) for row in store.rows(REPORT_TABLE)}
        if base not in existing:
            return base
        index = 2
        while f"{base}-{index}" in existing:
            index += 1
        return f"{base}-{index}"

    def _find_report(self, period: str, scope: str) -> dict[str, Any] | None:
        for row in store.rows(REPORT_TABLE):
            if str(row.get("统计周期")) == period and _scope_of(row.get("采集源")) == scope:
                return row
        return None

    def create_report(self, period_label: str, scope_label: str | None = None) -> dict[str, Any]:
        """创建周期报告；同一周期并发创建只放行一个，后到请求收到冲突。"""
        period = normalize_period(period_label)
        scope = _scope_of(scope_label)
        lock = _lock_for(period, scope)
        if not lock.acquire(blocking=False):
            raise ReportConflict(f"统计周期 {period} 的报告正在生成，请稍后重试")
        try:
            existing = self._find_report(period, scope)
            if existing is not None:
                raise ReportConflict(f"统计周期 {period} 已存在报告 {existing.get('报告编号')}，不能重复创建")
            aggregate = self._aggregate(period, scope)
            generated_at = _now()
            report = {
                "id": max((int(row.get("id", 0)) for row in store.rows(REPORT_TABLE)), default=0) + 1,
                "status": "已生成",
                "pending": True,
                "abnormal": bool(aggregate["异常项"]),
                "报告编号": self._new_report_number(period, scope),
                "采集源": scope,
                "统计周期": period,
            }
            snapshot = self._version_snapshot(report, aggregate, "首次生成：按统计周期汇总采集记录与失败事件", generated_at)
            self._apply_snapshot(report, snapshot)
            store.rows(REPORT_TABLE).append(report)
            return report
        finally:
            lock.release()

    def regenerate(self, entry_id: int, *, reason: str | None = None) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """按当前采集记录与失败事件重算报告；来源未变化时保持原版本。"""
        report = store.find(REPORT_TABLE, entry_id)
        if report is None:
            return None, f"采集报告 {entry_id} 不存在或已归档", None
        period = str(report.get("统计周期"))
        scope = _scope_of(report.get("采集源"))
        lock = _lock_for(period, scope)
        if not lock.acquire(blocking=False):
            raise ReportConflict(f"统计周期 {period} 的报告正在修订，请稍后重试")
        try:
            aggregate = self._aggregate(period, scope)
            versions = self._versions(str(report["报告编号"]))
            latest = versions[-1] if versions else None
            if latest is not None and self._snapshot_key(latest) == self._snapshot_key(aggregate):
                return report, f"来源数据没有变化，保持版本 {report.get('版本')}（{report.get('报告编号')}）", None
            default_reason = "按生效口径重算：采集记录或失败事件发生变化"
            if aggregate["未归因失败量"] or aggregate["异常项"]:
                default_reason = "按生效口径重算：失败事件与采集记录重新核对"
            snapshot = self._version_snapshot(report, aggregate, reason or default_reason, _now())
            self._apply_snapshot(report, snapshot)
            return report, f"采集报告 {report['报告编号']} 已重算并生成版本 {snapshot['版本']}", snapshot
        finally:
            lock.release()

    def regenerate_period(self, period_label: str, scope_label: str | None = None, *, reason: str | None = None) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        period = normalize_period(period_label)
        scope = _scope_of(scope_label)
        report = self._find_report(period, scope)
        if report is None:
            created = self.create_report(period, scope)
            return created, f"统计周期 {period} 的报告已生成（版本 {created.get('版本')}）", None
        return self.regenerate(int(report.get("id", 0)), reason=reason)

    # ---------- 失败事件 ----------

    def list_events(self, period: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(EVENT_TABLE)]
        if period:
            normalized = normalize_period(period)
            rows = [row for row in rows if _event_period(row) == normalized]
        return sorted(rows, key=lambda row: (str(row.get("事件时间", "")), int(row.get("id", 0))))

    def _revise_reports(self, periods: list[tuple[str, str]], event_no: str, policy: str, moment: str) -> tuple[list[dict[str, Any]], list[str]]:
        reports: list[dict[str, Any]] = []
        messages: list[str] = []
        seen: set[int] = set()
        for period, scope in periods:
            # 事件通常按采集源上报，报告通常按全部采集源生成：优先修订已有的全量报告，
            # 没有全量报告时才落到该采集源的口径，避免同一周期出现两份互相矛盾的报告。
            report = self._find_report(period, ALL_SOURCES) or self._find_report(period, scope)
            if report is None:
                report = self.create_report(period, ALL_SOURCES)
                messages.append(f"{period} 报告已生成，当前失败量 {report.get('失败量')}")
                reports.append(report)
                continue
            if int(report.get("id", 0)) in seen:
                continue
            seen.add(int(report.get("id", 0)))
            reason = (
                f"迟到失败事件 {event_no} 按事件发生时间 {moment} 归入 {period}，原周期报告修订"
                if policy == POLICY_EVENT_TIME
                else f"迟到失败事件 {event_no} 按报告生成时间归入 {period}，失败记录从原周期迁出"
            )
            updated, message, _snapshot = self.regenerate(int(report.get("id", 0)), reason=reason)
            if updated is not None:
                reports.append(updated)
                messages.append(message)
        return reports, messages

    def add_failure_event(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        """登记失败事件并按归属口径触发周期报告修订。

        - 归属策略 event_time：事件时间决定修订哪个原周期报告。
        - 归属策略 report_time：上报（报告生成）时间决定进入哪个周期；
          若事件指向的记录属于别的周期，该记录随之迁出原周期，避免两个周期重复统计。
        """
        event_no = str(values.get("事件编号") or "").strip()
        if not event_no:
            return None, "缺少必填字段：事件编号", []
        if any(str(row.get("事件编号")) == event_no for row in store.rows(EVENT_TABLE)):
            return None, f"失败事件 {event_no} 已存在，不能重复登记", []
        moment = _parse_time(values.get("事件时间"))
        if moment is None:
            return None, "缺少可识别的失败事件时间", []
        policy = str(values.get("归属策略") or POLICY_EVENT_TIME).strip()
        if policy not in POLICY_LABELS:
            return None, f"归属策略「{policy}」不支持，请使用 event_time 或 report_time", []
        reported = _parse_time(values.get("上报时间")) or _now()
        attributed = period_of(moment if policy == POLICY_EVENT_TIME else reported)
        event = {
            "id": max((int(row.get("id", 0)) for row in store.rows(EVENT_TABLE)), default=0) + 1,
            "事件编号": event_no,
            "采集源": str(values.get("采集源") or "").strip(),
            "任务": str(values.get("任务") or "").strip(),
            "事件时间": _iso(moment),
            "上报时间": _iso(reported),
            "失败原因": str(values.get("失败原因") or "").strip(),
            "归属记录": values.get("归属记录"),
            "归属策略": policy,
            "归属周期": attributed,
            "处理状态": "已归集",
        }
        store.rows(EVENT_TABLE).append(event)

        scopes: list[tuple[str, str]] = [(attributed, _scope_of(values.get("采集源")))]
        if policy == POLICY_REPORT_TIME:
            target = _match_record(event, store.rows(RECORD_TABLE))
            target_moment = _record_time(target) if target else None
            if target_moment is not None:
                source_period = period_of(target_moment)
                if source_period != attributed:
                    scopes.append((source_period, _scope_of(target.get("采集源"))))
        reports, messages = self._revise_reports(scopes, event_no, policy, event["事件时间"])
        return event, "；".join(messages) or "失败事件已登记", reports

    # ---------- 查询 ----------

    def ensure_reports(self) -> None:
        """为已有采集数据补建缺失的周期报告；已存在的周期原样保留，不产生新版本。"""
        periods: set[str] = set()
        for record in store.rows(RECORD_TABLE):
            moment = _record_time(record)
            if moment is not None:
                periods.add(period_of(moment))
        for event in store.rows(EVENT_TABLE):
            try:
                attributed = _event_period(event)
            except PeriodError:
                continue
            if attributed:
                periods.add(attributed)
        for period in sorted(periods):
            if self._find_report(period, ALL_SOURCES) is not None:
                continue
            try:
                self.create_report(period, ALL_SOURCES)
            except (ReportConflict, PeriodError):
                continue

    def list_reports(self, *, period: str | None = None, status: str | None = None, keyword: str | None = None) -> list[dict[str, Any]]:
        self.ensure_reports()
        rows = [dict(row) for row in store.rows(REPORT_TABLE)]
        if period:
            normalized = normalize_period(period)
            rows = [row for row in rows if str(row.get("统计周期")) == normalized]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("报告编号", "")).casefold()
                or needle in str(row.get("采集源", "")).casefold()
            ]
        return sorted(rows, key=lambda row: (str(row.get("统计周期", "")), int(row.get("id", 0))))

    def record_monitor_check(self, report_no: str, check: dict[str, Any]) -> None:
        """采集监控回写核对结果：只记录监控侧结论，不改报告的统计快照。"""
        for row in store.rows(REPORT_TABLE):
            if str(row.get("报告编号")) == report_no:
                row["监控核对"] = check
                row["pending"] = False
                return

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(REPORT_TABLE, entry_id)
        if row is None:
            return None
        data = dict(row)
        data["修订记录"] = self.version_summaries(str(row.get("报告编号")))
        data["监控核对"] = row.get("监控核对")
        return data

    def get_by_period(self, period_label: str, scope_label: str | None = None) -> dict[str, Any] | None:
        self.ensure_reports()
        period = normalize_period(period_label)
        report = self._find_report(period, _scope_of(scope_label))
        if report is None:
            return None
        return self.get_entry(int(report.get("id", 0)))

    def version_summaries(self, report_no: str) -> list[dict[str, Any]]:
        return [
            {
                "版本": row["版本"],
                "统计周期": row["统计周期"],
                "采集源": row.get("采集源"),
                "总采集量": row["总采集量"],
                "成功量": row["成功量"],
                "失败量": row["失败量"],
                "平均耗秒": row["平均耗秒"],
                "未结算量": row.get("未结算量", 0),
                "未归因失败量": row.get("未归因失败量", 0),
                "生成时间": row["生成时间"],
                "修订原因": row.get("修订原因", ""),
                "指标摘要": row.get("指标摘要", ""),
            }
            for row in self._versions(report_no)
        ]

    def get_version(self, report_no: str, version: int) -> dict[str, Any] | None:
        for row in self._versions(report_no):
            if int(row.get("版本", 0)) == version:
                return dict(row)
        return None

    def summary(self) -> dict[str, Any]:
        self.ensure_reports()
        rows = sorted(store.rows(REPORT_TABLE), key=lambda row: str(row.get("统计周期", "")))
        if not rows:
            return {"metrics": [
                {"label": "周期采集量", "value": 0, "trend": 0},
                {"label": "成功率", "value": 0, "trend": 0},
                {"label": "平均耗时", "value": 0, "trend": 0},
            ], "period": None, "report": None}
        latest, previous = rows[-1], rows[-2] if len(rows) > 1 else None
        total = int(latest.get("总采集量", 0))
        success = int(latest.get("成功量", 0))
        rate = round(success / total, 3) if total else 0.0
        prev_total = int(previous.get("总采集量", 0)) if previous else 0
        prev_success = int(previous.get("成功量", 0)) if previous else 0
        prev_rate = round(prev_success / prev_total, 3) if prev_total else 0.0
        revisions = sum(max(0, int(row.get("版本", 1)) - 1) for row in rows)
        return {"metrics": [
            {"label": "周期采集量", "value": total, "trend": total - prev_total},
            {"label": "成功率", "value": rate, "trend": round((rate - prev_rate) * 100, 1)},
            {"label": "平均耗时", "value": float(latest.get("平均耗秒", 0)), "trend": 0},
            {"label": "报告版本数", "value": int(latest.get("版本", 1)), "trend": revisions},
        ], "period": latest.get("统计周期"), "report": latest}

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """分析结果固定取自周期报告快照，同一周期反复运行结果一致。"""
        period = params.get("统计周期") or params.get("period")
        reports = self.list_reports(period=str(period) if period else None)
        return [
            {
                "id": report.get("id", index),
                "报告编号": report.get("报告编号"),
                "采集源": report.get("采集源"),
                "统计周期": report.get("统计周期"),
                "版本": report.get("版本", 1),
                "总采集量": report.get("总采集量", 0),
                "成功量": report.get("成功量", 0),
                "失败量": report.get("失败量", 0),
                "平均耗秒": report.get("平均耗秒", 0),
                "生成时间": report.get("生成时间"),
            }
            for index, report in enumerate(reports, start=1)
        ]
