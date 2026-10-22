"""分布式锁：避免同一个用户在多个 runner（GitHub Actions 并发任务 / 多台机器）上重复抢。

backend:
  none  —— 不做任何互斥（单实例时可接受）
  file  —— 本地文件锁，适合同一台机器上多进程
  redis —— SET NX PX，适合自建多节点；配 global.lock.redis_url
  gist  —— 用 GitHub Gist 做一个轻量「云锁」，无服务器成本，适合 Actions 多任务互斥
"""
from __future__ import annotations

import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from typing import Optional

from .config import GlobalConfig
from .utils import get_logger, mask

OWNER = uuid.uuid4().hex[:8]


class BaseLock(ABC):
    @abstractmethod
    def acquire(self, key: str, ttl: int = 900) -> Optional[str]:
        """抢到返回 token，抢不到返回 None。"""

    @abstractmethod
    def release(self, key: str, token: str) -> None:
        """释放自己持有的锁。"""


class NullLock(BaseLock):
    def acquire(self, key: str, ttl: int = 900) -> Optional[str]:
        return f"none-{OWNER}"

    def release(self, key: str, token: str) -> None:
        pass


class FileLock(BaseLock):
    def __init__(self, directory: str = ".locks", logger=None):
        self.directory = directory
        self.logger = logger or get_logger()
        os.makedirs(directory, exist_ok=True)

    def _path(self, key: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return os.path.join(self.directory, f"{safe}.lock")

    def acquire(self, key: str, ttl: int = 900) -> Optional[str]:
        path = self._path(key)
        token = f"{OWNER}-{uuid.uuid4().hex[:6]}"
        now = time.time()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if now - float(data.get("ts", 0)) < data.get("ttl", ttl):
                    return None
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"token": token, "ts": now, "ttl": ttl}, f)
            return token
        except OSError:
            return None
        except json.JSONDecodeError:
            return None

    def release(self, key: str, token: str) -> None:
        path = self._path(key)
        try:
            with open(path, "r", encoding="utf-8") as f:
                if json.load(f).get("token") != token:
                    return
            os.remove(path)
        except OSError:
            pass


class RedisLock(BaseLock):
    def __init__(self, url: str, logger=None):
        import redis  # type: ignore
        self.client = redis.Redis.from_url(url, decode_responses=True)
        self.prefix = "jxut-grab:"
        self.logger = logger or get_logger()

    def acquire(self, key: str, ttl: int = 900) -> Optional[str]:
        token = f"{OWNER}-{uuid.uuid4().hex[:6]}"
        try:
            ok = self.client.set(self.prefix + key, token, nx=True, ex=ttl)
        except Exception as exc:
            self.logger.warning("Redis 不可用，降级为无锁：%s", exc)
            return token
        return token if ok else None

    def release(self, key: str, token: str) -> None:
        script = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
        try:
            self.client.eval(script, 1, self.prefix + key, token)
        except Exception:
            pass


class GistLock(BaseLock):
    """Gist 作为共享存储，乐观读写做 CAS；非强一致，但足以去重绝大多数重复执行。"""

    FILE = "grab-locks.json"

    def __init__(self, gist_id: str, token: str, logger=None):
        self.gist_id = gist_id
        self.token = token
        self.logger = logger or get_logger()
        if not gist_id or not token:
            raise ValueError("GistLock 需要 gist_id 与 gist_token")

    def _headers(self):
        return {"Authorization": f"token {self.token}", "Accept": "application/vnd.github+json"}

    def _read(self):
        import requests
        resp = requests.get(f"https://api.github.com/gists/{self.gist_id}",
                            headers=self._headers(), timeout=10)
        if resp.status_code != 200:
            self.logger.warning("读取 gist 失败 %s（key=%s）", resp.status_code, mask(self.token))
            return {}
        try:
            return json.loads(resp.json()["files"][self.FILE]["content"])
        except Exception:
            return {}

    def _write(self, data) -> None:
        import requests
        requests.patch(f"https://api.github.com/gists/{self.gist_id}",
                       headers=self._headers(), timeout=10,
                       json={"files": {self.FILE: {"content": json.dumps(data, ensure_ascii=False)}}})

    def acquire(self, key: str, ttl: int = 900) -> Optional[str]:
        data = self._read()
        now = time.time()
        current = data.get(key)
        if current and now - float(current.get("ts", 0)) < float(current.get("ttl", ttl)):
            return None
        my_token = f"{OWNER}-{uuid.uuid4().hex[:6]}"
        data[key] = {"token": my_token, "ts": now, "ttl": ttl}
        self._write(data)
        time.sleep(0.6)  # 给其它并发实例留出写入时间
        confirmed = self._read().get(key) or {}
        return my_token if confirmed.get("token") == my_token else None

    def release(self, key: str, token: str) -> None:
        data = self._read()
        if (data.get(key) or {}).get("token") == token:
            data.pop(key, None)
            self._write(data)


def build_lock(glob: GlobalConfig, logger=None):
    cfg = glob.lock
    if not cfg.enabled or cfg.backend == "none":
        return NullLock()
    try:
        if cfg.backend == "redis":
            return RedisLock(cfg.redis_url or os.environ.get("REDIS_URL", ""), logger)
        if cfg.backend == "gist":
            return GistLock(cfg.gist_id or "", cfg.gist_token or "", logger)
        if cfg.backend == "file":
            return FileLock(".locks", logger)
    except Exception as exc:
        logger.warning("初始化锁 %s 失败，降级为无锁：%s", cfg.backend, exc)
        return NullLock()
    logger.warning("未知锁后端 %s，使用无锁模式", cfg.backend)
    return NullLock()
