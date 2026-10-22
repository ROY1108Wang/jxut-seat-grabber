"""选座/选时段策略：把「用户意图」翻译成具体的 API 参数。"""
from __future__ import annotations

import random
from typing import Dict, List

from .models import Room, Seat, TimeSlot
from .config import TargetConfig
from .utils import matches_any, plus_days


def resolve_dates(target: TargetConfig, tz: str, override: Optional[str] = None) -> List[str]:
    """目标日期列表：CLI 覆盖 > dates > date_offset。"""
    if override:
        return [override]
    if target.dates:
        return list(target.dates)
    return [plus_days(tz, target.date_offset)]


def pick_slots(slots: List[TimeSlot], periods: List[str]) -> List[TimeSlot]:
    """按 periods 规则筛时段；规则为空表示全部。规则可用 fnmatch 通配。"""
    if not periods:
        return list(slots)
    picked = [s for s in slots if matches_any(s.name, periods) or matches_any(s.time_range, periods)]
    # 按 slots 原始顺序（服务端已按时段排序）去重
    seen, out = set(), []
    for s in picked:
        if s.id not in seen:
            seen.add(s.id)
            out.append(s)
    return out


def _room_priority(rooms: List[Room], patterns: List[str]) -> Dict[str, int]:
    """按 patterns 的出现顺序给房间定优先级（越小越优先）。"""
    priority: Dict[str, int] = {}
    for idx, room in enumerate(rooms):
        level = len(patterns) + idx
        for i, pattern in enumerate(patterns or []):
            if matches_any(room.name, [pattern]):
                level = i
                break
        priority[room.id] = level
    return priority


def rank_seats(rooms: List[Room], target: TargetConfig, limit: int = 10) -> List[Seat]:
    """把房间里的空闲座位按用户策略排序，返回候选列表。

    order:
      preferred —— 先优先级最高的理想座位，其次按房间优先级 + 座位号升序
      code_asc / code_desc —— 忽略 seat 偏好，按座位号排序
      random —— 随机（用于分散、降低被风控的概率）
    """
    if not target.rooms:
        filtered = list(rooms)
    else:
        filtered = [r for r in rooms if matches_any(r.name, target.rooms)]
    if not filtered:
        return []

    priority = _room_priority(filtered, list(target.rooms))
    scored: List[tuple] = []
    for room in filtered:
        for seat in room.seats:
            if target.avoid_seats and seat.code in target.avoid_seats:
                continue
            try:
                preferred_idx = list(target.seats).index(seat.code) if target.seats else None
            except ValueError:
                preferred_idx = None
            code_num = int(seat.code) if seat.code.isdigit() else 10 ** 6
            scored.append((seat, preferred_idx, priority.get(room.id, 999), code_num, random.random()))

    # require_preferred=True：指定了理想座位时，理想座位全部不可用就一个都不坐
    if getattr(target, "require_preferred", True) and target.seats:
        scored = [s for s in scored if s[1] is not None]

    if target.order == "preferred":
        scored.sort(key=lambda x: (0 if x[1] is not None else 1, x[1] if x[1] is not None else 0,
                                   x[2], x[3]))
    elif target.order == "code_desc":
        scored.sort(key=lambda x: (-x[3], x[2]))
    elif target.order == "random":
        random.shuffle(scored)
    else:  # code_asc
        scored.sort(key=lambda x: (x[3], x[2]))

    return [s[0] for s in scored][:max(1, limit)]
