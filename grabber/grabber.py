"""抢座引擎：拉取时段 -> 选座 -> 提交 -> 校验 -> 失败重试/换座。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from .auth import AuthManager
from .client import ApiError, ClientError, SeatClient, SessionExpired
from .config import GlobalConfig, TargetConfig, UserConfig
from .models import GrabResult, Seat, SaveResult, TimeSlot
from .strategy import pick_slots, rank_seats, resolve_dates
from .utils import get_logger, jittered, now, sleep_until


class Grabber:
    """单个用户的抢座执行器（线程安全：一个用户一把锁顺序处理目标）。"""

    def __init__(self, user: UserConfig, glob: GlobalConfig, client: SeatClient,
                 auth: Optional[AuthManager] = None, notifier=None, lock=None,
                 logger=None, dry_run: bool = False):
        self.user = user
        self.glob = glob
        self.client = client
        self.auth = auth
        self.notifier = notifier
        self.lock = lock
        self.logger = logger or get_logger()
        self.dry_run = dry_run
        self.profile: dict = {}

    # ------------------------------------------------------------------
    def run(self, *, open_at=None, date_override: Optional[str] = None,
            stop_event: Optional[threading.Event] = None) -> List[GrabResult]:
        results: List[GrabResult] = []
        if open_at is not None:
            self.logger.info("[%s] 等待开抢时刻 %s", self.user.name, open_at.strftime("%F %T"))
            if not sleep_until(open_at, self.glob.timezone, stop_event, self.logger):
                self.logger.warning("[%s] 等待被中断", self.user.name)
                return results

        if self.auth is not None:
            self.profile = self.auth.profile(self.client) or {}
            if self.profile:
                self.logger.info("[%s] 身份信息：%s", self.user.name,
                                 " ".join(f"{k}={v}" for k, v in self.profile.items()))

        for target in self.user.targets:
            if stop_event is not None and stop_event.is_set():
                break
            for date in resolve_dates(target, self.glob.timezone, date_override):
                for result in self.grab_date(date, target, stop_event):
                    results.append(result)
        return results

    # ------------------------------------------------------------------
    def grab_date(self, date: str, target: TargetConfig,
                  stop_event: Optional[threading.Event] = None) -> List[GrabResult]:
        results: List[GrabResult] = []
        try:
            slots = self.client.get_slots(date)
        except SessionExpired:
            if not self._relogin():
                return [GrabResult(self.user.name, date, "-", message="登录失效且无法刷新",
                                   success=False)]
            slots = self.client.get_slots(date)
        except ClientError as exc:
            self.logger.error("[%s] 查询 %s 时段失败：%s", self.user.name, date, exc)
            return [GrabResult(self.user.name, date, "-", message=f"查询时段失败：{exc}")]

        chosen = pick_slots(slots, target.periods)
        if not chosen:
            self.logger.warning("[%s] %s 没有匹配 %s 的时段，可用：%s", self.user.name, date,
                                target.periods, [s.label() for s in slots])
            return [GrabResult(self.user.name, date, "-",
                               message=f"无匹配时段（可用 {len(slots)} 个）")]

        for slot in chosen:
            if stop_event is not None and stop_event.is_set():
                break
            results.append(self.grab_slot(date, slot, target))
        return results

    # ------------------------------------------------------------------
    def grab_slot(self, date: str, slot: TimeSlot, target: TargetConfig) -> GrabResult:
        started = time.perf_counter()
        max_retry = target.max_retry or self.user.max_retry or self.glob.max_retry
        fallback = target.fallback_seats or self.glob.fallback_seats
        key = f"{self.user.name}:{date}:{slot.id}"

        token = None
        if self.lock is not None:
            token = self.lock.acquire(key, ttl=self.glob.lock.ttl)
            if not token:
                msg = f"未能获取执行锁（可能已有实例在跑）"
                self.logger.warning("[%s] %s %s：%s", self.user.name, date, slot.label(), msg)
                return GrabResult(self.user.name, date, slot.label(), slot_id=slot.id,
                                  skipped=True, message=msg)

        started_at = now(self.glob.timezone)

        def _stamp(res: GrabResult) -> GrabResult:
            """补记起止时间戳与耗时，便于事后对账。"""
            res.elapsed = res.elapsed or (time.perf_counter() - started)
            res.started_at = started_at.strftime("%Y-%m-%d %H:%M:%S")
            res.finished_at = now(self.glob.timezone).strftime("%Y-%m-%d %H:%M:%S")
            return res

        last_message = ""
        try:
            for attempt in range(1, max_retry + 1):
                result = self._attempt(date, slot, target, fallback, attempt)
                last_message = result.message or last_message
                if result.success or result.skipped or result.terminal:
                    return _stamp(result)
                # 指数退避但加上限：这样可以把 max_retry 调大去「等放号」而不会被拖到天荒地老
                raw_backoff = self.glob.retry_backoff * (2 ** (attempt - 1))
                backoff = min(raw_backoff, max(self.glob.retry_max_backoff, self.glob.retry_backoff))
                backoff = jittered(backoff, self.glob.retry_backoff * 0.3)
                self.logger.info("[%s] %s %s 第 %d 次未成功（%s），%.1fs 后重试",
                                 self.user.name, date, slot.label(), attempt,
                                 result.message, backoff)
                time.sleep(backoff)
            return _stamp(GrabResult(self.user.name, date, slot.label(), slot_id=slot.id,
                                     success=False,
                                     message=f"重试 {max_retry} 次未成功：{last_message}",
                                     attempts=max_retry,
                                     elapsed=time.perf_counter() - started))
        finally:
            if self.lock is not None and token:
                self.lock.release(key, token)

    # ------------------------------------------------------------------
    def _attempt(self, date: str, slot: TimeSlot, target: TargetConfig,
                 fallback: int, attempt: int) -> GrabResult:
        base = dict(user=self.user.name, date=date, slot_label=slot.label(),
                    slot_id=slot.id, attempts=attempt)
        try:
            rooms = self.client.get_rooms(date, slot.id)
        except SessionExpired:
            if self._relogin():
                rooms = self.client.get_rooms(date, slot.id)
            else:
                return GrabResult(**base, message="登录失效")
        except ClientError as exc:
            return GrabResult(**base, message=f"查询座位失败：{exc}")

        # 已预约：判断是否满意 / 是否换座
        if slot.booked:
            satisfied = (not target.seats) or (slot.seat_code in target.seats)
            if satisfied and target.allow_skip_if_booked:
                self.logger.info("[%s] %s %s 已在 %s-%s，跳过", self.user.name, date,
                                 slot.label(), slot.room, slot.seat_code)
                return GrabResult(**base, success=True, skipped=True,
                                  message=f"已预约 {slot.room}-{slot.seat_code}")
            if not target.allow_change:
                return GrabResult(**base, skipped=True,
                                  message=f"已预约 {slot.room}-{slot.seat_code}，未开启换座")

        candidates = rank_seats(rooms, target, max(fallback, 1))
        if not candidates:
            if target.seats and target.require_preferred:
                return GrabResult(**base, terminal=True,
                                  message=f"理想座位 {'/'.join(target.seats)} 已被占用，"
                                          f"按 require_preferred 不占其它座")
            return GrabResult(**base, message="目标房间暂无空闲座位")

        if self.glob.burst_workers > 1:
            outcome = self._burst_save(date, slot, candidates)
        else:
            outcome = self._sequential_save(date, slot, candidates)

        return self._finalize(outcome, _slot=slot, **base)

    # ------------------------------------------------------------------
    def _sequential_save(self, date: str, slot: TimeSlot, candidates: List[Seat]):
        """串行尝试候选座位，成功即止。"""
        for seat in candidates:
            saved = self._try_save(date, slot, seat)
            if saved is not None:
                return saved, seat
        return None, None

    def _burst_save(self, date: str, slot: TimeSlot, candidates: List[Seat]):
        """并发尝试多个候选座位，任一成功即发出停止信号。

        注意：服务端并发写可能同时成功并占用多个座位，请谨慎设置 burst_workers。
        """
        stop = threading.Event()
        result_box: dict = {"saved": None, "seat": None}
        box_lock = threading.Lock()
        workers = min(self.glob.burst_workers, len(candidates))

        def worker(seat: Seat, delay: float):
            if delay:
                time.sleep(delay)
            if stop.is_set():
                return
            saved = self._try_save(date, slot, seat)
            if saved is not None:
                with box_lock:
                    if result_box["saved"] is None:
                        result_box["saved"] = saved
                        result_box["seat"] = seat
                stop.set()

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="burst") as pool:
            futures = []
            for i, seat in enumerate(candidates[:workers]):
                futures.append(pool.submit(worker, seat, i * self.glob.burst_delay))
            for f in futures:
                try:
                    f.result()
                except Exception as exc:  # pragma: no cover
                    self.logger.debug("burst worker 异常：%s", exc)
        return result_box["saved"], result_box["seat"]

    # ------------------------------------------------------------------
    def _try_save(self, date: str, slot: TimeSlot, seat: Seat):
        """提交一次预约；成功返回 SaveResult，可继续重试返回 None。"""
        try:
            if self.dry_run:
                self.logger.warning("[%s] [试运行] 将预约 %s %s %s", self.user.name,
                                    date, slot.label(), seat.label())
                return SaveResult(ok=True, message="试运行，未实际提交", status="dry-run")
            result = self.client.save_seat(date, slot.id, seat.id)
        except SessionExpired:
            self.logger.warning("[%s] 登录失效，尝试刷新后重试", self.user.name)
            if self._relogin():
                try:
                    result = self.client.save_seat(date, slot.id, seat.id)
                except (ClientError, SessionExpired) as exc:
                    self.logger.debug("刷新后重试失败：%s", exc)
                    return None
            else:
                return None
        except ClientError as exc:
            if isinstance(exc, ApiError):
                self.logger.info("[%s] %s：%s，换下一个座位", self.user.name,
                                 getattr(seat, "label", lambda: "?")(), exc)
            else:
                self.logger.debug("[%s] save 异常 %s", self.user.name, exc)
            return None

        if result.ok:
            self.logger.info("[%s] 预约成功 %s %s -> %s（%s）", self.user.name, date,
                             slot.label(), seat.label(), result.message or "OK")
            return result
        # status == success 但 message 不是成功文案时也按失败处理
        self.logger.debug("[%s] save 返回 %s / %s", self.user.name, result.status, result.message)
        return None

    # ------------------------------------------------------------------
    def _finalize(self, outcome, **base) -> GrabResult:
        slot: TimeSlot = base.pop("_slot")
        date = base["date"]
        saved, seat = outcome
        if saved is None:
            return GrabResult(**base, success=False, message="候选座位均预约失败（可能已被抢占）")

        if self.dry_run:
            return GrabResult(**base, success=True, seat=seat, message=saved.message)

        verified = None
        try:
            for s in self.client.get_slots(date):
                if s.id == slot.id:
                    verified = s
                    break
        except ClientError:
            pass

        conflict = False
        if verified is not None and verified.booked and verified.seat_code and \
                seat is not None and verified.seat_code != seat.code:
            # 服务端保留了原座位（例如重复请求落在旧的预约上）
            conflict = True

        if verified is not None and verified.booked and not conflict:
            self.logger.info("[%s] 结果校验通过：%s %s -> %s %s", self.user.name, date,
                             slot.label(), verified.room, verified.seat_code)
            self._notify_success(date, slot, verified, seat)
            return GrabResult(**base, success=True, seat=seat or Seat(code=verified.seat_code or "",
                                                                     id="", room_name=verified.room or ""),
                              message=saved.message or "预约成功")
        if verified is None:
            return GrabResult(**base, success=False, seat=seat, message="提交成功但无法回查校验")
        return GrabResult(**base, success=False, seat=seat,
                          message=f"回查状态异常：{verified.room}-{verified.seat_code}")

    # ------------------------------------------------------------------
    def _notify_success(self, date: str, slot: TimeSlot, verified: TimeSlot, seat: Optional[Seat]) -> None:
        if not self.notifier:
            return
        who = self.profile.get("姓名") or self.user.name
        extra = f"\n备注：{self.user.notify_extra}" if self.user.notify_extra else ""
        self.notifier.send(
            title=f"抢座成功 · {who} · {date} {slot.label()}",
            content=(f"用户：{who}\n日期：{date}\n时段：{slot.label()}\n"
                     f"座位：{verified.room}-{verified.seat_code}\n"
                     f"结果：预约成功{extra}"),
        )

    # ------------------------------------------------------------------
    def _relogin(self) -> bool:
        if self.auth is None:
            return False
        try:
            self.client = self.auth.ensure_client(allow_login=True)
            return True
        except Exception as exc:
            self.logger.error("[%s] 重新登录失败：%s", self.user.name, exc)
            return False
