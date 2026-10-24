from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
REPORT_DATE_FORMAT = "%d-%m-%Y"


def report_date(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return _local_datetime(value).strftime(REPORT_DATE_FORMAT)
    if isinstance(value, date):
        return value.strftime(REPORT_DATE_FORMAT)
    if isinstance(value, str):
        parsed = _parse_date_text(value)
        if parsed:
            return report_date(parsed)
    return str(value)


def report_datetime(value: object, fmt: str = "%d %b %Y %H:%M") -> str:
    """Show a stored UTC timestamp in the business time zone."""
    if isinstance(value, str):
        parsed = _parse_date_text(value)
        value = parsed if isinstance(parsed, datetime) else value
    if not isinstance(value, datetime):
        return report_date(value)
    return _local_datetime(value).strftime(fmt)


def _local_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def _parse_date_text(value: str) -> date | datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
