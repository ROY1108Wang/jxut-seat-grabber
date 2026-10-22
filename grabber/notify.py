"""通知推送：成功/失败/汇总一键推送到多种渠道。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import smtplib
import time
import urllib.parse
from email.header import Header
from email.mime.text import MIMEText
from typing import Dict, List

import requests

from .config import NotifyConfig
from .utils import get_logger


class Notifier:
    def __init__(self, cfg: NotifyConfig, logger=None, timeout: int = 10):
        self.cfg = cfg
        self.logger = logger or get_logger()
        self.timeout = timeout
        self.failed: List[str] = []

    # ------------------------------------------------------------------
    def send(self, title: str, content: str) -> None:
        title = f"{self.cfg.title_prefix}{title}"
        handlers = {
            "serverchan": self._serverchan,
            "pushplus": self._pushplus,
            "telegram": self._telegram,
            "dingtalk": self._dingtalk,
            "wecom_bot": self._wecom_bot,
            "bark": self._bark,
            "smtp": self._smtp,
            "webhook": self._webhook,
        }
        for name, handler in handlers.items():
            conf = self.cfg.channel(name)
            if not self._enabled(conf):
                continue
            try:
                handler(conf, title, content)
                self.logger.info("通知已发送：%s", name)
            except Exception as exc:
                self.failed.append(name)
                self.logger.warning("通知 %s 发送失败：%s", name, exc)

    @staticmethod
    def _enabled(conf: Dict) -> bool:
        if not isinstance(conf, dict):
            return False
        flag = conf.get("enabled", False)
        if isinstance(flag, str):
            return flag.strip().lower() in {"1", "true", "yes", "on"}
        return bool(flag)

    # ------------------------------------------------------------------
    def _serverchan(self, conf, title, content):
        key = (conf.get("send_key") or "").strip()
        if not key:
            return
        api = "https://sctapi.ftqq.com" if key.startswith("sctp") else "https://sc.ftqq.com"
        requests.post(f"{api}/{key}.send", data={"title": title, "desp": content},
                      timeout=self.timeout)

    def _pushplus(self, conf, title, content):
        token = (conf.get("token") or "").strip()
        if not token:
            return
        requests.post("https://www.pushplus.plus/send", timeout=self.timeout,
                      json={"token": token, "title": title, "content": content, "template": "txt"})

    def _telegram(self, conf, title, content):
        token = (conf.get("bot_token") or "").strip()
        chat_id = str(conf.get("chat_id") or "").strip()
        if not (token and chat_id):
            return
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=self.timeout,
                      json={"chat_id": chat_id, "text": f"{title}\n\n{content}"})

    def _dingtalk(self, conf, title, content):
        webhook = (conf.get("webhook") or "").strip()
        if not webhook:
            return
        url = webhook
        secret = (conf.get("secret") or "").strip()
        if secret:
            ts = str(round(time.time() * 1000))
            sign = base64.b64encode(
                hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
            ).decode()
            url = f"{webhook}&timestamp={ts}&sign={urllib.parse.quote(sign)}"
        requests.post(url, timeout=self.timeout,
                      json={"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}})

    def _wecom_bot(self, conf, title, content):
        webhook = (conf.get("webhook") or "").strip()
        if not webhook:
            return
        requests.post(webhook, timeout=self.timeout,
                      json={"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}})

    def _bark(self, conf, title, content):
        key = (conf.get("device_key") or "").strip()
        if not key:
            return
        server = (conf.get("server") or "https://api.day.app").rstrip("/")
        requests.post(f"{server}/{key}", timeout=self.timeout,
                      json={"title": title, "body": content, "group": "抢座"})

    def _smtp(self, conf, title, content):
        host, port = conf.get("host"), int(conf.get("port") or 465)
        user, pwd, to = conf.get("username"), conf.get("password"), conf.get("to")
        if not (host and user and to):
            return
        msg = MIMEText(content, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = user
        msg["To"] = to
        if conf.get("ssl", True):
            server = smtplib.SMTP_SSL(host, port, timeout=self.timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=self.timeout)
        with server:
            server.login(user, pwd or "")
            server.sendmail(user, [a.strip() for a in str(to).split(",")], msg.as_string())

    def _webhook(self, conf, title, content):
        url = (conf.get("url") or "").strip()
        if not url:
            return
        requests.post(url, timeout=self.timeout,
                      json={"title": title, "content": content, "source": "jxut-seat-grabber"})

    # ------------------------------------------------------------------
    @staticmethod
    def summary(results) -> str:
        ok = sum(1 for r in results if r.success or r.skipped)
        return "\n".join(["结果汇总：%d/%d" % (ok, len(results))] + [r.line() for r in results])
