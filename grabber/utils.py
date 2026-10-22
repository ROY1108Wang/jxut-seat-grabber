"""通用工具：日志、环境变量展开、时间/等待、字符串脱敏。"""
from __future__ import annotations

import logging
import os
import random
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from fnmatch import fnmatch
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo

_ENV_PATTERN = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """配置根 logger。Windows 控制台强制 UTF-8，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass

    fmt = "%(asctime)s | %(levelname)-7s | %(threadName)-10s | %(message)s"
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format=fmt, datefmt="%H:%M:%S", handlers=handlers, force=True)
    return logging.getLogger("jxut")


def get_logger(name: str = "jxut") -> logging.Logger:
    return logging.getLogger(name)


def env_expand(value, default: Optional[str] = None):
    """递归展开 `${ENV:VAR}`，缺失时返回 default（None 则保留原串由调用方处理）。"""
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: env_expand(v, default) for k, v in value.items()}
    if isinstance(value, list):
        return [env_expand(v, default) for v in value]
    if not isinstance(value, str):
        return value

    missing = []

    def _sub(m: re.Match) -> str:
        val = os.environ.get(m.group(1))
        if val is None:
            missing.append(m.group(1))
            return default if default is not None else ""
        return val

    out = _ENV_PATTERN.sub(_sub, value)
    if missing and default is None:
        # 只有整串就是一个变量且没取到时才判定为“未配置”
        if _ENV_PATTERN.fullmatch(value.strip()):
            return None
    return out


def mask(text: Optional[str], keep: int = 6) -> str:
    """脱敏展示 cookie / token。"""
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= keep else f"{text[:keep]}***({len(text)})"


def now(tz: str = "Asia/Shanghai") -> datetime:
    return datetime.now(ZoneInfo(tz))


def parse_time(value: str, tz: str, base: Optional[datetime] = None) -> datetime:
    """把 "07:00:00" / "07:00" / "2026-10-22 07:00:00" 解析成 tz 时区的时间。"""
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt.startswith("%Y"):
            return dt.replace(tzinfo=ZoneInfo(tz))
        base = base or now(tz)
        return base.replace(hour=dt.hour, minute=dt.minute, second=dt.second, microsecond=0)
    raise ValueError(f"无法解析时间：{value!r}")


def sleep_until(target: datetime, tz: str, stop_event: Optional[threading.Event] = None,
                logger: Optional[logging.Logger] = None, poll: float = 0.2) -> bool:
    """等待到目标时刻。粗略阶段用 sleep，最后 2 秒忙等以提高精度。

    返回 True 表示正常到达目标时刻，False 表示被 stop_event 打断。
    """
    zone = ZoneInfo(tz)
    while True:
        remain = (target - datetime.now(zone)).total_seconds()
        if remain <= 0:
            return True
        if stop_event is not None and stop_event.is_set():
            return False
        if logger and remain > 60 and int(remain) % 60 == 0:
            logger.info("距离开抢还有 %.0f 分钟（目标 %s）", remain / 60, target.strftime("%F %T"))
        time.sleep(min(poll, remain) if remain > 2 else 0.005)


def jittered(base: float, jitter: float = 0.0) -> float:
    """带随机抖动的正数睡眠时长。"""
    if base <= 0:
        return 0.0
    if jitter <= 0:
        return base
    return max(0.0, base + random.uniform(-jitter, jitter))


def matches_any(value: Optional[str], patterns: Iterable[str]) -> bool:
    """fnmatch 匹配；patterns 为空表示“不限制”。"""
    if not patterns:
        return True
    if value is None:
        return False
    return any(fnmatch(value, p) for p in patterns)


def date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def plus_days(tz: str, offset: int) -> str:
    return date_str(now(tz) + timedelta(days=offset))
