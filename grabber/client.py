"""HTTP 客户端：完全对齐抓包得到的三个接口。

抓包要点（务必保持一致，否则容易被服务端拒绝）：
  * POST + application/x-www-form-urlencoded; charset=UTF-8
  * Referer / Origin = https://jxut.educationgroup.cn/tsg/kzwWx/index
  * X-Requested-With: XMLHttpRequest
  * 返回体 UTF-8 JSON：{"data":..., "message":..., "status":"success"}
"""
from __future__ import annotations

import json
import random
import threading
import time
from http.cookies import SimpleCookie
from typing import Any, Dict, List, Optional, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import Room, SaveResult, TimeSlot


def _build_retry(total: int) -> Retry:
    """兼容 urllib3 v1（method_whitelist）与 v2（allowed_methods）。"""
    common = dict(total=total, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504),
                  raise_on_status=False)
    try:
        return Retry(allowed_methods=frozenset(["GET", "POST"]), **common)
    except TypeError:  # urllib3 < 2
        return Retry(method_whitelist=frozenset(["GET", "POST"]), **common)


class ClientError(Exception):
    """业务/协议层错误。"""


class SessionExpired(ClientError):
    """登录态失效，需要重新获取 cookie。"""


class ApiError(ClientError):
    """接口返回了明确的失败信息（例如座位已被抢走）。"""

    def __init__(self, message: str, raw: Optional[dict] = None):
        super().__init__(message)
        self.raw = raw or {}


DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")

# 会话失效关键字（抓不到真实样本，故宁严勿滥：宁可当成普通失败重抢，也不要误判成重新登录）
EXPIRE_KEYWORDS = ("未登录", "重新登录", "登录失效", "登录已过期", "身份过期", "会话过期",
                   "请先登录", "no login", "unauthorized", "尚未登录")
# 业务失败关键字：这类要多换座位重试，而不是重登录
BUSINESS_KEYWORDS = ("已被", "已约", "占满", "满员", "不可预", "不可选", "超过", "限制",
                     "失败", "不能", "冲突", "已存在", "次数")


class SeatClient:
    """单个用户一个实例，内部持有 requests.Session 与 cookie。"""

    def __init__(self, *, cookie: str = "", base_url: str = "https://jxut.educationgroup.cn",
                 api_prefix: str = "/tsg/kzwWx", index_path: str = "/tsg/kzwWx/index",
                 timeout: float = 10.0, retries: int = 3, min_interval: float = 0.35,
                 jitter: float = 0.25, user_agent: str = "", proxy: Optional[str] = None,
                 logger=None):
        self.base_url = base_url.rstrip("/")
        self.api_prefix = api_prefix
        self.index_url = self.base_url + index_path
        self.timeout = timeout
        self.min_interval = min_interval
        self.jitter = jitter
        self.logger = logger
        self._last_call = 0.0
        self._lock = threading.Lock()

        self.session = requests.Session()
        self.session.trust_env = True
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        self.session.headers.update({
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": self.base_url,
            "Referer": self.index_url,
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": user_agent or DEFAULT_UA,
            "sec-ch-ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "DNT": "1",
        })

        if retries > 0:
            adapter = HTTPAdapter(max_retries=_build_retry(retries),
                                  pool_connections=8, pool_maxsize=16)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

        if cookie:
            self.set_cookie(cookie)

    # ---------------- cookie ----------------
    def set_cookie(self, cookie: Union[str, Dict[str, str]]) -> None:
        """支持 "a=1; b=2" 字符串或 dict。"""
        self.session.cookies.clear()
        if isinstance(cookie, dict):
            for k, v in cookie.items():
                self.session.cookies.set(k, v)
            return
        jar = SimpleCookie()
        try:
            jar.load(cookie or "")
        except Exception as exc:  # 手抄 cookie 常见格式错误
            raise ClientError(f"Cookie 解析失败：{exc}（应形如 `name=value; name2=value2`）") from exc
        for key, morsel in jar.items():
            self.session.cookies.set(key, morsel.value)

    def export_cookie(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.session.cookies)

    # ---------------- 内部请求 ----------------
    def _throttle(self) -> None:
        wait = self.min_interval - (time.perf_counter() - self._last_call)
        if wait > 0:
            wait += random.uniform(0, self.jitter) if self.jitter else 0
            time.sleep(max(0.0, wait))
        self._last_call = time.perf_counter()

    def _post(self, path: str, data: Dict[str, str]) -> Dict[str, Any]:
        url = f"{self.base_url}{self.api_prefix}{path}"
        with self._lock:
            self._throttle()
        try:
            resp = self.session.post(url, data=data, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            raise ClientError(f"网络异常 {path}：{exc}") from exc

        text = resp.content.decode("utf-8", errors="replace").strip()
        if not text:
            raise ClientError(f"{path} 返回空内容，HTTP {resp.status_code}")

        # 被重定向到微信授权页 / 登录页（返回 HTML）
        if text.lstrip().startswith("<"):
            if any(k in text for k in ("OAuth", "weixin", "wxLogin", "oaRedirect", "授权")):
                raise SessionExpired("会话已失效，被重定向到微信授权页面")
            raise ClientError(f"{path} 返回了非 JSON 内容（HTTP {resp.status_code}）")

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ClientError(f"{path} 返回不是合法 JSON：{text[:200]}") from exc

        if self.logger:
            self.logger.debug("%s <- status=%s message=%s", path,
                              payload.get("status"), payload.get("message"))

        message = str(payload.get("message") or "")
        lower = message.lower()
        status = payload.get("status")

        # 1) 明确要求重新登录
        if any(k in lower for k in EXPIRE_KEYWORDS):
            raise SessionExpired(message or "会话失效")
        # 2) status 不是 success：绝大多数是业务失败（座位被抢/规则限制），换座重试即可
        if status not in (None, "", "success"):
            raise ApiError(message or f"服务端返回 status={status}", payload)
        # 3) status 是 success 但文案是失败语义（部分老接口这样返回）
        if any(k in message for k in BUSINESS_KEYWORDS) and "成功" not in message:
            raise ApiError(message, payload)
        return payload

    # ---------------- 三个业务接口 ----------------
    def get_slots(self, date: str) -> List[TimeSlot]:
        """`/tsg/kzwWx/getSjd`：查询某天所有时段及本人预约情况。"""
        payload = self._post("/getSjd", {"rq": date})
        data = payload.get("data") or []
        return [TimeSlot.from_dict(d) for d in data if isinstance(d, dict)]

    def get_rooms(self, date: str, slot_id: str) -> List[Room]:
        """`/tsg/kzwWx/getZws`：查询某时段各房间的空闲座位。"""
        payload = self._post("/getZws", {"rq": date, "sjdId": slot_id})
        data = payload.get("data") or []
        return [Room.from_dict(d) for d in data if isinstance(d, dict)]

    def save_seat(self, date: str, slot_id: str, seat_id: str) -> SaveResult:
        """`/tsg/kzwWx/save`：预约；该时段已有预约时表现为「换座成功」。"""
        payload = self._post("/save", {"rq": date, "sjdId": slot_id, "zwId": seat_id})
        result = SaveResult.from_dict(payload)
        return result

    def check_login(self, date: Optional[str] = None) -> bool:
        """用一个无害请求探测登录态是否有效。"""
        try:
            self.get_slots(date or time.strftime("%Y-%m-%d"))
            return True
        except (SessionExpired, ClientError):
            return False

    # ---------------- 身份信息 ----------------
    @staticmethod
    def is_logged_out_page(html: str) -> bool:
        """未登录时 index 会变成一个跳转页（实测标题为「正在转入统一认证平台」）。"""
        lower = (html or "").lower()
        return any(k in lower for k in ("统一认证平台", "正在转入", "微信授权",
                                        "weixin://", "oauth/authorize"))

    def fetch_profile_html(self) -> str:
        """GET 预约首页，用于从页面里抠出姓名/学号等身份信息。"""
        try:
            resp = self.session.get(self.index_url, timeout=self.timeout)
            resp.encoding = resp.encoding or "utf-8"
            return resp.text
        except requests.RequestException as exc:
            raise ClientError(f"获取首页失败：{exc}") from exc
