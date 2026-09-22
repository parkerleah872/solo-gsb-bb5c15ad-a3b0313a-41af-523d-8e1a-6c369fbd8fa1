"""ISO-8601 UTC 时间工具：固定 6 位微秒，字符串字典序与时间顺序一致。"""
from __future__ import annotations

from datetime import datetime, timezone

# 可直接用于 SQL 字符串比较
ISO = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(ISO)


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, ISO).replace(tzinfo=timezone.utc)
