"""多用户并发调度：线程池 + 分布式锁 + 通知汇总。"""
from __future__ import annotations

import datetime
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from .auth import AuthManager
from .config import AppConfig
from .grabber import Grabber
from .lock import build_lock
from .models import GrabResult
from .notify import Notifier
from .utils import get_logger, now, parse_time


class Runner:
    def __init__(self, cfg: AppConfig, logger=None, dry_run: bool = False,
                 user_filter: Optional[List[str]] = None, notify: bool = True,
                 wait: bool = True):
        self.cfg = cfg
        self.glob = cfg.global_
        self.logger = logger or get_logger()
        self.dry_run = dry_run
        self.notify_on = notify
        self.wait = wait
        self.started_at: Optional[datetime.datetime] = None
        self.finished_at: Optional[datetime.datetime] = None
        self.total_elapsed: float = 0.0
        self.results: List[GrabResult] = []
        self.stop_event = threading.Event()

        # GitHub Actions matrix 里变量名只能大写，这里做大小写不敏感匹配
        wanted = None if not user_filter else {u.strip().lower() for u in user_filter}
        self.users = [u for u in cfg.users
                      if u.enabled and (wanted is None or u.name.lower() in wanted
                                        or "all" in wanted)]
        if not self.users:
            raise SystemExit("没有可运行的用户（检查 enabled / -u 参数）")
        self.notifier = Notifier(cfg.notify, self.logger) if notify else None
        self.lock = build_lock(self.glob, self.logger)

    # ------------------------------------------------------------------
    def run(self, *, open_at: Optional[str] = None, date: Optional[str] = None) -> List[GrabResult]:
        self.started_at = now(self.glob.timezone)
        started_perf = time.perf_counter()

        target_time = None
        if self.wait:
            if open_at:
                target_time = parse_time(open_at, self.glob.timezone)
            elif self.glob.open_time:
                target_time = parse_time(self.glob.open_time, self.glob.timezone)

        workers = min(self.glob.max_workers, len(self.users))
        self.logger.info("开始执行：%d 个用户，%d 线程，dry_run=%s%s",
                         len(self.users), workers, self.dry_run,
                         f"，开抢时刻 {target_time:%F %T}" if target_time else "")

        if workers <= 1:
            for user in self.users:
                self.results.extend(self._run_user(user, target_time, date))
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="user") as pool:
                futures = {pool.submit(self._run_user, u, target_time, date): u for u in self.users}
                for future in as_completed(futures):
                    user = futures[future]
                    try:
                        self.results.extend(future.result())
                    except Exception as exc:
                        self.logger.error("[%s] 任务异常：%s\n%s", user.name, exc,
                                          traceback.format_exc(limit=3))
                        self.results.append(GrabResult(user.name, "-", "-",
                                                       message=f"执行异常：{exc}"))

        self.finished_at = now(self.glob.timezone)
        self.total_elapsed = time.perf_counter() - started_perf
        self._report()
        return self.results

    # ------------------------------------------------------------------
    def _run_user(self, user, target_time, date: Optional[str]) -> List[GrabResult]:
        auth = AuthManager(user, self.glob, self.logger)
        try:
            client = auth.ensure_client(allow_login=False)
        except Exception as exc:
            self.logger.error("[%s] 登录失败：%s", user.name, exc)
            return [GrabResult(user.name, "-", "-", message=f"登录失败：{exc}")]

        grabber = Grabber(user, self.glob, client, auth=auth, notifier=self.notifier,
                          lock=self.lock, logger=self.logger, dry_run=self.dry_run)
        try:
            return grabber.run(open_at=target_time, date_override=date,
                               stop_event=self.stop_event)
        except Exception as exc:
            self.logger.error("[%s] 抢座流程异常：%s\n%s", user.name, exc,
                              traceback.format_exc(limit=3))
            return [GrabResult(user.name, "-", "-", message=f"执行异常：{exc}")]

    # ------------------------------------------------------------------
    def _report(self) -> None:
        lines = [r.line() for r in self.results]
        total = len(self.results)
        success = sum(1 for r in self.results if r.success or r.skipped)
        started = self.started_at.strftime("%F %T") if self.started_at else "-"
        finished = self.finished_at.strftime("%F %T") if self.finished_at else "-"
        headline = f"抢座完成：{success}/{total} 个目标达成，总耗时 {self.total_elapsed:.2f}s"

        self.logger.info("=" * 72)
        self.logger.info("开始时间：%s   结束时间：%s   总耗时：%.2fs",
                         started, finished, self.total_elapsed)
        self.logger.info(headline)
        for line in lines:
            self.logger.info("  %s", line)
        self.logger.info("=" * 72)

        rows = [(r.user, r.date, r.slot_label,
                 r.seat.label() if r.seat else (r.message or "-"),
                 f"{r.elapsed:.2f}s", r.finished_at or "-", r.started_at or "-")
                for r in self.results]

        self._append_history(rows, started, finished)

        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                with open(summary_path, "a", encoding="utf-8") as f:
                    f.write(f"## {headline}\n\n")
                    f.write(f"- 开始：`{started}`\n- 结束：`{finished}`\n"
                            f"- 总耗时：**{self.total_elapsed:.2f}s**\n\n")
                    f.write("| 用户 | 日期 | 时段 | 座位 | 耗时 | 完成时间戳 | 开始时间戳 |\n")
                    f.write("|---|---|---|---|---|---|---|\n")
                    for row in rows:
                        f.write("| " + " | ".join(row) + " |\n")
                    f.write("\n")
            except OSError:
                pass

        if self.notifier and total:
            failures = [r for r in self.results if not (r.success or r.skipped)]
            body = Notifier.summary(self.results)
            body += (f"\n\n开始：{started}\n结束：{finished}\n总耗时：{self.total_elapsed:.2f}s")
            if failures:
                self.notifier.send(f"有 {len(failures)} 个目标未达成", body)
            else:
                self.notifier.send(f"全部 {total} 个目标达成", body)

    def _append_history(self, rows, started: str, finished: str) -> None:
        """把每次执行的明细追加到历史日志，便于长期核对成功率。"""
        path = self.glob.history_file
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"# 开始={started} 结束={finished} 总耗时={self.total_elapsed:.2f}s "
                        f"成功={sum(1 for r in self.results if r.success or r.skipped)}"
                        f"/{len(self.results)}\n")
                for row in rows:
                    f.write(" | ".join(row) + "\n")
        except OSError as exc:
            self.logger.warning("写入历史日志失败 %s：%s", path, exc)
