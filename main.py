#!/usr/bin/env python
"""jxut-seat-grabber —— 图书馆座位自动预约 CLI。

    python main.py run                    # 按配置文件抢座
    python main.py run -u zhangsan --at 07:00:00
    python main.py login -u zhangsan      # 浏览器扫码获取并保存 cookie
    python main.py probe                  # 校验 cookie、打印身份信息与当日时段
    python main.py rooms --date 2026-10-22 --period 中午
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from grabber.auth import AuthManager          # noqa: E402
from grabber.client import ClientError        # noqa: E402
from grabber.config import ConfigError, load_config  # noqa: E402
from grabber.runner import Runner             # noqa: E402
from grabber.strategy import pick_slots       # noqa: E402
from grabber.utils import get_logger, mask, now, parse_time, plus_days, setup_logging  # noqa: E402

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "config.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py", description="图书馆座位自动预约（配置驱动 / 多用户并发 / 可上云）")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    parser.add_argument("--log-file", default=None, help="日志同时输出到文件")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="执行抢座")
    run_p.add_argument("-u", "--user", action="append", default=None, help="只跑指定用户，可重复")
    run_p.add_argument("--dry-run", action="store_true", help="只选座不下单")
    run_p.add_argument("--at", default=None, help="等到该时刻再开抢，如 07:00:00")
    run_p.add_argument("--date", default=None, help="覆盖所有目标的日期 YYYY-MM-DD")
    run_p.add_argument("--no-notify", action="store_true", help="不发送通知")
    run_p.add_argument("--no-wait", action="store_true",
                       help="忽略配置的 open_time，立即执行（补漏任务用）")

    login_p = sub.add_parser("login", help="交互式登录并保存 cookie（需 playwright）")
    login_p.add_argument("-u", "--user", action="append", default=None)
    login_p.add_argument("--timeout", type=int, default=180, help="等待扫码秒数")

    probe_p = sub.add_parser("probe", help="校验 cookie 并打印身份/时段信息")
    probe_p.add_argument("-u", "--user", action="append", default=None)
    probe_p.add_argument("--date", default=None)

    rooms_p = sub.add_parser("rooms", help="列出某天某时段的房间与空闲座位")
    rooms_p.add_argument("-u", "--user", action="append", default=None)
    rooms_p.add_argument("--date", default=None)
    rooms_p.add_argument("--period", default=None, help="时段名或 12:00-14:00")
    rooms_p.add_argument("--limit", type=int, default=20, help="每个房间最多打印多少个座位号")
    return parser


def pick_users(cfg, names: Optional[List[str]]):
    wanted = None if not names else {n.strip().lower() for n in names}
    return [u for u in cfg.users if u.enabled and (wanted is None or u.name.lower() in wanted
                                                   or "all" in wanted)]


def cmd_run(cfg, args, logger) -> int:
    runner = Runner(cfg, logger, dry_run=args.dry_run,
                    user_filter=args.user, notify=not args.no_notify,
                    wait=not args.no_wait)
    results = runner.run(open_at=args.at, date=args.date)
    failures = [r for r in results if not (r.success or r.skipped)]
    return 1 if failures else 0


def cmd_login(cfg, args, logger) -> int:
    code = 0
    for user in pick_users(cfg, args.user):
        auth = AuthManager(user, cfg.global_, logger)
        try:
            cookie = auth.interactive_login(timeout=args.timeout)
            path = auth.save_cookie(cookie)
            client = auth.build_client(cookie)
            profile = auth.profile(client)
            logger.info("[%s] 登录成功 cookie=%s 保存于 %s", user.name, mask(cookie), path)
            logger.info("[%s] 身份信息：%s", user.name, profile or "（页面未包含可识别字段）")
        except Exception as exc:
            code = 1
            logger.error("[%s] 登录失败：%s", user.name, exc)
    return code


def cmd_probe(cfg, args, logger) -> int:
    code = 0
    date = args.date or plus_days(cfg.global_.timezone, 0)
    for user in pick_users(cfg, args.user):
        auth = AuthManager(user, cfg.global_, logger)
        try:
            client = auth.ensure_client(allow_login=False)
        except Exception as exc:
            code = 1
            logger.error("[%s] 登录态不可用：%s", user.name, exc)
            continue
        profile = auth.profile(client)
        print(f"\n=== {user.name} ===")
        print(f"cookie : {mask(client.export_cookie())}")
        print(f"身份   : {profile or '未识别到（不影响抢座）'}")
        try:
            slots = client.get_slots(date)
        except ClientError as exc:
            code = 1
            logger.error("[%s] 查询 %s 失败：%s", user.name, date, exc)
            continue
        print(f"{date} 时段（共 {len(slots)}）：")
        for slot in slots:
            state = f"已约 {slot.room}-{slot.seat_code}" if slot.booked else "未预约"
            print(f"  {slot.name:<6} {slot.time_range}  {state:<28} id={slot.id}")
    return code


def cmd_rooms(cfg, args, logger) -> int:
    date = args.date or plus_days(cfg.global_.timezone, 1)
    code = 0
    for user in pick_users(cfg, args.user):
        auth = AuthManager(user, cfg.global_, logger)
        try:
            client = auth.ensure_client(allow_login=False)
            slots = client.get_slots(date)
        except Exception as exc:
            code = 1
            logger.error("[%s] 初始化失败：%s", user.name, exc)
            continue
        chosen = pick_slots(slots, [args.period] if args.period else [])
        print(f"\n=== {user.name} / {date} ===")
        for slot in chosen or slots:
            try:
                rooms = client.get_rooms(date, slot.id)
            except ClientError as exc:
                code = 1
                logger.error("[%s] 查询座位失败：%s", user.name, exc)
                continue
            print(f"[{slot.label()}] id={slot.id}")
            for room in rooms:
                codes = ", ".join(s.code for s in room.seats[:args.limit])
                more = " ..." if len(room.seats) > args.limit else ""
                print(f"  {room.name:<22} 空闲 {len(room.seats):>4}/{room.total:<4} "
                      f"id={room.id}  {codes}{more}")
        break  # 房间结构对所有用户一致，只看第一个
    return code


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging("DEBUG" if args.verbose else "INFO", args.log_file)
    logger = get_logger()

    path = args.config
    example = os.path.join(os.path.dirname(DEFAULT_CONFIG), "config.example.yaml")
    if path == DEFAULT_CONFIG and not os.path.isfile(path) and os.path.isfile(example):
        logger.warning("未找到 config/config.yaml，回退使用 config.example.yaml")
        path = example

    try:
        cfg = load_config(path)
    except ConfigError as exc:
        logger.error("配置错误：%s", exc)
        return 2

    logger.debug("配置加载完成：%s（%d 个用户）", cfg.path, len(cfg.users))
    handlers = {"run": cmd_run, "login": cmd_login, "probe": cmd_probe, "rooms": cmd_rooms}
    try:
        return handlers[args.command](cfg, args, logger)
    except KeyboardInterrupt:
        logger.warning("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
