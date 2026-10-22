"""配置加载：YAML -> 数据类，并展开 `${ENV:VAR}`。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from .utils import env_expand


class ConfigError(Exception):
    pass


@dataclass
class LockConfig:
    enabled: bool = True
    backend: str = "none"       # none | redis | gist
    ttl: int = 900
    redis_url: Optional[str] = None
    gist_id: Optional[str] = None
    gist_token: Optional[str] = None
    wait: bool = True


@dataclass
class GlobalConfig:
    base_url: str = "https://jxut.educationgroup.cn"
    api_prefix: str = "/tsg/kzwWx"
    index_path: str = "/tsg/kzwWx/index"
    timezone: str = "Asia/Shanghai"
    timeout: float = 10.0
    http_retries: int = 3
    min_interval: float = 0.35
    jitter: float = 0.25
    user_agent: str = ""
    proxy: Optional[str] = None
    max_workers: int = 4
    burst_workers: int = 1
    burst_delay: float = 0.0
    open_time: Optional[str] = None
    wait_timeout: int = 1800
    history_file: str = "logs/history.log"
    max_retry: int = 5
    retry_backoff: float = 1.5
    retry_max_backoff: float = 30.0   # 退避上限，配合大 max_retry 可实现“轮询等放号”
    fallback_seats: int = 8
    lock: LockConfig = field(default_factory=LockConfig)


@dataclass
class TargetConfig:
    """一个「日期 + 时段 + 房间 + 座位」的抢座目标。"""

    date_offset: int = 1
    dates: List[str] = field(default_factory=list)
    periods: List[str] = field(default_factory=list)
    rooms: List[str] = field(default_factory=list)
    seats: List[str] = field(default_factory=list)
    avoid_seats: List[str] = field(default_factory=list)
    order: str = "preferred"        # preferred | code_asc | code_desc | random
    allow_change: bool = True       # 已有预约但座位不理想时是否换座
    allow_skip_if_booked: bool = True
    require_preferred: bool = True  # True：理想座位 seats 抢不到就不占其它座
    fallback_seats: Optional[int] = None
    max_retry: Optional[int] = None


@dataclass
class UserConfig:
    name: str
    enabled: bool = True
    cookie_env: Optional[str] = None
    cookie_file: Optional[str] = None
    cookie: Optional[str] = None
    login_url: Optional[str] = None
    cookie_cache: Optional[str] = None
    headless: bool = False
    max_retry: Optional[int] = None
    notify_extra: str = ""
    targets: List[TargetConfig] = field(default_factory=list)


@dataclass
class NotifyConfig:
    raw: Dict[str, Any] = field(default_factory=dict)
    title_prefix: str = "[抢座]"

    def channel(self, name: str) -> Dict[str, Any]:
        return self.raw.get(name) or {}


@dataclass
class AppConfig:
    global_: GlobalConfig
    notify: NotifyConfig
    users: List[UserConfig]
    path: str = ""


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: str) -> AppConfig:
    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在：{path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件根节点必须是字典")

    root = os.path.dirname(os.path.abspath(path))

    g_raw: Dict[str, Any] = env_expand(raw.get("global") or {}, default="") or {}
    l_raw: Dict[str, Any] = env_expand(g_raw.get("lock") or {}, default="") or {}
    lock = LockConfig(
        enabled=_as_bool(l_raw.get("enabled"), True),
        backend=str(l_raw.get("backend") or "none").lower(),
        ttl=int(l_raw.get("ttl") or 900),
        redis_url=l_raw.get("redis_url") or None,
        gist_id=l_raw.get("gist_id") or None,
        gist_token=l_raw.get("gist_token") or None,
        wait=_as_bool(l_raw.get("wait"), True),
    )

    glob = GlobalConfig(
        base_url=str(g_raw.get("base_url") or GlobalConfig.base_url).rstrip("/"),
        api_prefix=str(g_raw.get("api_prefix") or "/tsg/kzwWx"),
        index_path=str(g_raw.get("index_path") or "/tsg/kzwWx/index"),
        timezone=str(g_raw.get("timezone") or "Asia/Shanghai"),
        timeout=float(g_raw.get("timeout") or 10),
        http_retries=int(g_raw.get("http_retries") or 3),
        min_interval=float(g_raw.get("min_interval") or 0.35),
        jitter=float(g_raw.get("jitter") or 0.25),
        user_agent=str(g_raw.get("user_agent") or ""),
        proxy=g_raw.get("proxy") or None,
        max_workers=max(1, int(g_raw.get("max_workers") or 4)),
        burst_workers=max(1, int(g_raw.get("burst_workers") or 1)),
        burst_delay=float(g_raw.get("burst_delay") or 0.0),
        open_time=(g_raw.get("open_time") or None) or None,
        wait_timeout=int(g_raw.get("wait_timeout") or 1800),
        history_file=str(g_raw.get("history_file") or "logs/history.log"),
        max_retry=max(0, int(g_raw.get("max_retry") or 5)),
        retry_backoff=float(g_raw.get("retry_backoff") or 1.5),
        retry_max_backoff=float(g_raw.get("retry_max_backoff") or 30.0),
        fallback_seats=max(1, int(g_raw.get("fallback_seats") or 8)),
        lock=lock,
    )

    n_raw: Dict[str, Any] = env_expand(raw.get("notify") or {}, default="") or {}
    notify = NotifyConfig(raw=n_raw, title_prefix=str(n_raw.pop("title_prefix", "[抢座]") or "[抢座]"))

    users: List[UserConfig] = []
    for item in raw.get("users") or []:
        item = env_expand(item or {}, default="") or {}
        if not item.get("name"):
            raise ConfigError(f"用户缺少 name 字段：{item}")
        targets = []
        for t in item.get("targets") or []:
            t = env_expand(t or {}, default="") or {}
            targets.append(TargetConfig(
                date_offset=int(t.get("date_offset") if t.get("date_offset") is not None else 1),
                dates=list(t.get("dates") or []),
                periods=list(t.get("periods") or []),
                rooms=list(t.get("rooms") or []),
                seats=[str(s).zfill(3) if str(s).isdigit() else str(s) for s in (t.get("seats") or [])],
                avoid_seats=[str(s).zfill(3) if str(s).isdigit() else str(s) for s in (t.get("avoid_seats") or [])],
                order=str(t.get("order") or "preferred"),
                allow_change=_as_bool(t.get("allow_change"), True),
                allow_skip_if_booked=_as_bool(t.get("allow_skip_if_booked"), True),
                require_preferred=_as_bool(t.get("require_preferred"), True),
                fallback_seats=t.get("fallback_seats"),
                max_retry=t.get("max_retry"),
            ))
        if not targets:
            raise ConfigError(f"用户 {item['name']} 没有配置任何 targets")

        cache = item.get("cookie_cache")
        users.append(UserConfig(
            name=str(item["name"]),
            enabled=_as_bool(item.get("enabled"), True),
            cookie_env=item.get("cookie_env") or None,
            cookie_file=_abs(item.get("cookie_file"), root),
            cookie=item.get("cookie") or None,
            login_url=item.get("login_url") or None,
            cookie_cache=_abs(cache, root) if cache else None,
            headless=_as_bool(item.get("headless"), False),
            max_retry=item.get("max_retry"),
            notify_extra=str(item.get("notify_extra") or ""),
            targets=targets,
        ))

    if not users:
        raise ConfigError("配置文件中没有任何用户")

    return AppConfig(global_=glob, notify=notify, users=users, path=path)


def _abs(p: Optional[str], root: str) -> Optional[str]:
    if not p:
        return None
    return p if os.path.isabs(p) else os.path.join(root, "..", p)
