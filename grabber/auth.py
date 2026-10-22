"""登录态管理：获取 cookie、缓存 cookie、失效时重新登录，并抓取身份信息。

三种凭据来源，按优先级：
  1. 配置里内联的 `cookie`（仅限本地调试，切勿提交仓库）
  2. 环境变量（GitHub Actions 里走 Secrets）
  3. cookie 文件（纯文本一行 / playwright 导出的 JSON）
  4. `python main.py login` 用 playwright 交互扫码抓取并落盘到 cookie_file
"""
from __future__ import annotations

import datetime
import json
import os
import re
from typing import Dict, List, Optional

from .client import SeatClient, SessionExpired
from .config import GlobalConfig, UserConfig
from .utils import get_logger, mask

# 从预约首页 HTML 里抠身份信息的常见写法（命中即返回）
PROFILE_PATTERNS: Dict[str, List[str]] = {
    "姓名": [r"姓\s*名[：:\s]*([一-龥]{2,4})",
             r'"(?:userName|readerName|xm|name)"\s*[:=]\s*"([^"]{2,20})"'],
    "学号": [r"学\s*号[：:\s]*([A-Za-z0-9]{6,20})",
             r'"(?:userCode|readerNo|xh|studentNo|account)"\s*[:=]\s*"([^"]{4,20})"'],
    "证件号": [r"(?:读者证|借阅证|证\s*件)\s*号[：:\s]*([A-Za-z0-9\-]{4,30})"],
    "单位": [r"(?:院系|单位|部门)[：:\s]*([^\s<>\"'　]{2,30})"],
}


class AuthError(Exception):
    pass


class AuthManager:
    def __init__(self, user: UserConfig, glob: GlobalConfig, logger=None):
        self.user = user
        self.glob = glob
        self.logger = logger or get_logger()

    # ---------------- cookie 读取 ----------------
    def load_cookie(self) -> Optional[str]:
        """按优先级取出一条 cookie 字符串。"""
        if self.user.cookie:
            return self._normalize(self.user.cookie)

        if self.user.cookie_env:
            value = os.environ.get(self.user.cookie_env)
            if value:
                return self._normalize(value)
            # GitHub Actions 常见写法：JXUT_COOKIE_<NAME 大写>
            alt = os.environ.get(f"JXUT_COOKIE_{self.user.name.upper()}")
            if alt:
                return self._normalize(alt)
            # 兜底：矩阵只注入了通用变量时也能用
            generic = os.environ.get("JXUT_COOKIE")
            if generic:
                return self._normalize(generic)

        for path in filter(None, [self.user.cookie_file]):
            cookie = self._from_file(path)
            if cookie:
                return cookie
        return None

    @staticmethod
    def _normalize(value: str) -> str:
        value = value.strip().strip('"').strip("'")
        # 允许直接粘贴 "Cookie: a=1; b=2"
        if value.lower().startswith("cookie:"):
            value = value.split(":", 1)[1]
        return value.strip()

    def _from_file(self, path: str) -> Optional[str]:
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError as exc:
            self.logger.warning("读取 cookie 文件失败 %s：%s", path, exc)
            return None
        if not content:
            return None
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return self._normalize(content.splitlines()[0])

        # playwright storage_state / 扩展导出格式
        if isinstance(data, dict):
            if isinstance(data.get("cookies"), str):
                return self._normalize(data["cookies"])
            items = data.get("cookies") or []
            return "; ".join(f"{c['name']}={c['value']}" for c in items
                             if c.get("name") and c.get("value") is not None)
        if isinstance(data, list):
            return "; ".join(f"{c['name']}={c['value']}" for c in data
                             if c.get("name") and c.get("value") is not None)
        return None

    def save_cookie(self, cookie: str, path: Optional[str] = None) -> Optional[str]:
        path = path or self.user.cookie_cache or self.user.cookie_file
        if not path:
            return None
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(cookie)
        try:
            os.chmod(path, 0o600)
        except OSError:  # Windows 上无所谓
            pass
        self.logger.info("[%s] cookie 已写入 %s（%s）", self.user.name, path, mask(cookie))
        return path

    # ---------------- 登录 ----------------
    def interactive_login(self, timeout: int = 180) -> str:
        """打开浏览器让用户完成微信授权/扫码，然后导出 cookie。需要 playwright。"""
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AuthError("缺少 playwright：pip install playwright && playwright install chromium") from exc

        url = self.user.login_url or (self.glob.base_url + self.glob.index_path)
        self.logger.warning("[%s] 即将打开浏览器，请在 %ds 内完成登录/扫码：%s",
                            self.user.name, timeout, url)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.user.headless)
            context = browser.new_context(locale="zh-CN")
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            # 等待页面里出现座位预约的标志（出现 getSjd 请求 / 标题变化）
            deadline = timeout
            while deadline > 0:
                page.wait_for_timeout(2000)
                deadline -= 2
                cookies = {c["name"]: c["value"] for c in context.cookies()}
                if cookies and self._cookie_works(cookies):
                    break
            cookies = context.cookies()
            browser.close()

        cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
        if not cookie:
            raise AuthError("未能获取到 cookie，请检查页面是否完成登录")
        return cookie

    def _cookie_works(self, cookies: Dict[str, str]) -> bool:
        client = self.build_client(cookies)
        try:
            client.get_slots(datetime.date.today().isoformat())
            return True
        except Exception:
            return False

    # ---------------- 组装 / 校验 ----------------
    def build_client(self, cookie: Optional[str] = None) -> SeatClient:
        cookie = cookie or self.load_cookie() or ""
        return SeatClient(
            cookie=cookie,
            base_url=self.glob.base_url,
            api_prefix=self.glob.api_prefix,
            index_path=self.glob.index_path,
            timeout=self.glob.timeout,
            retries=self.glob.http_retries,
            min_interval=self.glob.min_interval,
            jitter=self.glob.jitter,
            user_agent=self.glob.user_agent,
            proxy=self.glob.proxy,
            logger=self.logger,
        )

    def ensure_client(self, *, allow_login: bool = False) -> SeatClient:
        """拿到一个登录态可用的 client；不可用时尝试刷新。"""
        cookie = self.load_cookie()
        if not cookie:
            if not allow_login:
                raise AuthError(
                    f"[{self.user.name}] 未配置 cookie：请在环境变量 {self.user.cookie_env} "
                    f"或 {self.user.cookie_file} 中提供，或执行 `python main.py login -u {self.user.name}`")
            cookie = self.interactive_login()
            self.save_cookie(cookie)

        client = self.build_client(cookie)
        if self.verify(client):
            return client

        if not allow_login:
            raise SessionExpired(f"[{self.user.name}] cookie 已失效，请更新后重试")
        self.logger.warning("[%s] cookie 失效，尝试重新登录", self.user.name)
        cookie = self.interactive_login()
        client = self.build_client(cookie)
        if not self.verify(client):
            raise AuthError(f"[{self.user.name}] 重新登录后仍无法通过校验")
        self.save_cookie(cookie)
        return client

    def verify(self, client: SeatClient) -> bool:
        """双重判据：接口可用 + 首页不是登录跳转页。

        未登录时部分接口仍会返回 status=success（data 为空），只看接口会误判。
        """
        try:
            client.get_slots(datetime.date.today().isoformat())
        except SessionExpired:
            return False
        except Exception:
            pass  # 网络抖动不算失效，交给上层重试
        try:
            return not client.is_logged_out_page(client.fetch_profile_html())
        except Exception:
            return True

    # ---------------- 身份信息 ----------------
    def profile(self, client: SeatClient) -> Dict[str, str]:
        """尽力从首页 HTML 中提取身份信息，失败返回空 dict。"""
        try:
            html = client.fetch_profile_html()
        except Exception as exc:
            self.logger.debug("[%s] 获取首页失败：%s", self.user.name, exc)
            return {}
        info: Dict[str, str] = {}
        for key, patterns in PROFILE_PATTERNS.items():
            for pattern in patterns:
                m = re.search(pattern, html)
                if m:
                    info[key] = m.group(1).strip()
                    break
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        if m:
            info.setdefault("页面标题", re.sub(r"\s+", " ", m.group(1)).strip())
        return info
