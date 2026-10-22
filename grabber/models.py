"""接口数据模型。字段名与抓包返回的 JSON 保持一致，方便对照排查。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TimeSlot:
    """`getSjd` 返回的一个预约时段。

    抓包样例：
        {"yyType":"YY","zwCode":"019","pw":null,"totalPw":null,
         "menuName":"4楼紫阳阁-D","id":"B938B85919CE2FA2E0530100007F477B",
         "sjdName":"中午","sjdStart":"12:00","status":null,"sjdEnd":"14:00"}
    """

    id: str
    name: str
    start: str
    end: str
    room: Optional[str] = None       # menuName：已预约时才有值
    seat_code: Optional[str] = None  # zwCode：已预约座位号
    yy_type: str = ""                # "YY" = 已预约
    reserved: Optional[int] = None   # pw（已约人数）
    total: Optional[int] = None      # totalPw（总名额）
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TimeSlot":
        return cls(
            id=str(d.get("id") or ""),
            name=d.get("sjdName") or "",
            start=d.get("sjdStart") or "",
            end=d.get("sjdEnd") or "",
            room=d.get("menuName") or None,
            seat_code=d.get("zwCode") or None,
            yy_type=d.get("yyType") or "",
            reserved=d.get("pw"),
            total=d.get("totalPw"),
            raw=d,
        )

    @property
    def booked(self) -> bool:
        return self.yy_type == "YY"

    @property
    def time_range(self) -> str:
        return f"{self.start}-{self.end}"

    def label(self) -> str:
        return f"{self.name} {self.time_range}"

    def __str__(self) -> str:  # pragma: no cover
        status = f"已约 {self.room}-{self.seat_code}" if self.booked else "未预约"
        return f"<TimeSlot {self.label()} [{status}] id={self.id[:8]}>"


@dataclass
class Seat:
    """`getZws` 返回的空闲座位（zwList 元素）。"""

    code: str          # zwCode，如 "019"
    id: str            # zwId，save 时提交
    room_id: str = ""
    room_name: str = ""
    remark: Optional[str] = None  # zwBz
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any], room_id: str = "", room_name: str = "") -> "Seat":
        return cls(
            code=d.get("zwCode") or "",
            id=str(d.get("id") or ""),
            room_id=room_id,
            room_name=room_name,
            remark=d.get("zwBz"),
            raw=d,
        )

    def label(self) -> str:
        return f"{self.room_name or self.room_id}-{self.code}"

    def __str__(self) -> str:  # pragma: no cover
        return f"<Seat {self.label()} id={self.id[:10]}>"


@dataclass
class Room:
    """`getZws` 返回的一个房间/区域，zwList 只包含当前空闲座位。"""

    id: str
    name: str = ""
    total: Optional[int] = None      # totalCount：座位总数
    available: Optional[int] = None  # kzwCount：可预约数
    seats: List[Seat] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Room":
        rid = str(d.get("id") or "")
        name = d.get("roomName") or ""
        room = cls(
            id=rid,
            name=name,
            total=d.get("totalCount"),
            available=d.get("kzwCount"),
            raw=d,
        )
        room.seats = [Seat.from_dict(s, rid, name) for s in (d.get("zwList") or [])]
        return room

    def __str__(self) -> str:  # pragma: no cover
        return f"<Room {self.name} free={len(self.seats)}/{self.total}>"


@dataclass
class SaveResult:
    """`save` 接口结果。"""

    ok: bool
    message: str = ""
    status: str = ""
    booked: bool = False     # True 表示本接口又帮忙换了座（换座成功）
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SaveResult":
        status = str(d.get("status") or "")
        message = str(d.get("message") or "")
        ok = status == "success"
        return cls(ok=ok, message=message, status=status,
                   booked="换座" in message, raw=d if isinstance(d, dict) else {})

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class GrabResult:
    """一次「日期 + 时段」的最终结果，用于汇总通知。"""

    user: str
    date: str
    slot_label: str
    slot_id: str = ""
    success: bool = False
    skipped: bool = False
    terminal: bool = False    # True = 无需再重试（例如理想座位已被别人占，重试没意义）
    seat: Optional[Seat] = None
    message: str = ""
    attempts: int = 0
    elapsed: float = 0.0
    started_at: str = ""     # 本时段开始处理的时间戳（配置时区）
    finished_at: str = ""    # 本时段结束处理的时间戳

    def line(self) -> str:
        if self.skipped:
            icon = "⏭"
        elif self.success:
            icon = "✅"
        else:
            icon = "❌"
        seat = self.seat.label() if self.seat else (self.message or "-")
        return (f"{icon} {self.date} {self.slot_label} -> {seat} | "
                f"耗时 {self.elapsed:.2f}s | {self.finished_at or '-'}")
