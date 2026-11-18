"""统计周期口径：把报告里的「统计周期」解释成半开时间区间 [start, end)。

周期写法统一成 ISO 周（如 2026-W45），也接受中文日期/时间戳：
- 2026-W45 / 2026-w45：该 ISO 周的周一 00:00 到下一周周一 00:00
- 2026-11-09：单日，当天 00:00 到次日 00:00
- 2026-11-09~2026-11-15：闭区间日期段
- 2026-11：整月

所有周期边界都按半开区间处理，相邻周期的端点不会重复计数，
采集记录只按「发生时间」落到唯一一个周期里。
"""
from __future__ import annotations

import datetime as dt
import re

WEEK_RE = re.compile(r"^(\d{4})-?W(\d{1,2})$", re.IGNORECASE)
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*[~至]\s*(\d{4}-\d{2}-\d{2})$")


class PeriodError(ValueError):
    """统计周期写法无法识别。"""


def _week_bounds(year: int, week: int) -> tuple[dt.date, dt.date]:
    try:
        start = dt.date.fromisocalendar(year, week, 1)
    except ValueError as exc:  # ISO 周号越界
        raise PeriodError(f"统计周期 {year}-W{week} 不是有效的 ISO 周") from exc
    return start, start + dt.timedelta(days=7)


def parse_period(label: str) -> tuple[dt.datetime, dt.datetime]:
    """把周期标签解析成 [开始, 结束) 的 datetime 区间。"""
    text = str(label or "").strip()
    if not text:
        raise PeriodError("统计周期不能为空")

    if match := WEEK_RE.match(text):
        start_date, end_date = _week_bounds(int(match.group(1)), int(match.group(2)))
        return dt.datetime.combine(start_date, dt.time()), dt.datetime.combine(end_date, dt.time())
    if match := MONTH_RE.match(text):
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            raise PeriodError(f"统计周期 {text} 的月份不合法")
        start_date = dt.date(year, month, 1)
        end_date = dt.date(year + (month == 12), month % 12 + 1, 1)
        return dt.datetime.combine(start_date, dt.time()), dt.datetime.combine(end_date, dt.time())
    if match := RANGE_RE.match(text):
        start_date = dt.date.fromisoformat(match.group(1))
        end_date = dt.date.fromisoformat(match.group(2)) + dt.timedelta(days=1)
        if end_date <= start_date:
            raise PeriodError(f"统计周期 {text} 的结束日期早于开始日期")
        return dt.datetime.combine(start_date, dt.time()), dt.datetime.combine(end_date, dt.time())
    if match := DAY_RE.match(text):
        start_date = dt.date.fromisoformat(text)
        return dt.datetime.combine(start_date, dt.time()), dt.datetime.combine(start_date + dt.timedelta(days=1), dt.time())
    raise PeriodError(f"统计周期「{text}」暂不支持，请使用 2026-W45、2026-11 或 2026-11-09 这类写法")


def period_of(moment: dt.datetime) -> str:
    """取某个时刻所属的 ISO 周标签，与 parse_period 的周口径保持一致。"""
    iso = moment.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def normalize_period(label: str) -> str:
    """把可接受的各种写法归一成存储用的标签；周统一成 2026-W45。"""
    parse_period(label)  # 先校验
    text = str(label or "").strip()
    if match := WEEK_RE.match(text):
        return f"{int(match.group(1))}-W{int(match.group(2)):02d}"
    if match := RANGE_RE.match(text):
        return f"{match.group(1)}~{match.group(2)}"
    return text

