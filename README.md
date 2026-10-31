# jxut-seat-grabber

江西科技学院图书馆座位预约自动化脚本：配置驱动、**多用户并发**、**结果校验 + 换座重试**、**云端定时 / 手动_trigger**、**分布式去重**三者合一。

> 接口逆向自 `jxut.*.c*.har`，三个接口即可完成整个流程（见下）。

---

## 1. 接口协议（抓包逆向结果）

Base：`https://jxut.educationgroup.cn`，全部 `POST` + `application/x-www-form-urlencoded; charset=UTF-8`，需带 `X-Requested-With: XMLHttpRequest` 与 `Referer: .../tsg/kzwWx/index`。

| 接口 | 入参 | 返回要点 |
| --- | --- | --- |
| `/tsg/kzwWx/getSjd` | `rq=2026-10-21` | 当天全部时段：`id`、`sjdName`（早上/中午/下午2…）、`sjdStart`、`sjdEnd`、`menuName` + `zwCode`（已预约的房间与座位号）、`yyType=="YY"` 表示已预约 |
| `/tsg/kzwWx/getZws` | `rq`、`sjdId` | 房间列表：`id`、`roomName`（如 `图书馆4F_4楼紫阳阁-C`）、`totalCount`、`kzwCount`、`zwList[{zwCode, id, zwBz}]`，**只返回空闲座位** |
| `/tsg/kzwWx/save` | `rq`、`sjdId`、`zwId` | `{"status":"success","message":"预约成功" / "换座成功"}`，`zwId` 必须取自 `getZws` 的空闲列表 |

关键点：**`save` 一个接口同时承担"预约"和"换座"**——该时段已有预约时服务端返回"换座成功"，所以重试/换座不需要额外的取消接口。

统一响应外壳：`{"data": ..., "message": "...", "status": "success"}`，脚本据此判定成败。

---

## 2. 安装

```bash
pip install -r requirements.txt
# 可选：浏览器扫码登录
pip install playwright && playwright install chromium
```

---

## 3. 获取 Cookie（登录态）

系统走的是微信授权，**推荐**直接从浏览器拿 cookie 喂给脚本，最稳：

1. 微信里打开图书馆预约页（或用浏览器打开 `https://jxut.educationgroup.cn/tsg/kzwWx/index` 完成授权）
2. 用 `Cookie-Editor` / `EditThisCookie` 扩展导出浏览器里的 cookie，形如：`name=value; name2=value2`
3. 二选一写进去：
   - 环境变量：`set JXUT_COOKIE_ZHANGSAN=xxx`（Windows PowerShell 用 `$env:JXUT_COOKIE_ZHANGSAN="xxx"`）
   - 文件：`secrets/zhangsan.cookie`，配置里 `cookie_file: secrets/zhangsan.cookie`

也可以让脚本自己开浏览器扫码：

```bash
python main.py login -u zhangsan     # 需要 playwright，headless 必须 false
```

验证一下是否通了：

```bash
python main.py probe -u zhangsan     # 打印脱敏 cookie / 身份信息 / 当天时段
```

---

## 4. 配置

复制模板后按需修改：

```bash
cp config/config.example.yaml config/config.yaml
```

一个目标（target）的完整语义：

```yaml
users:
  - name: zhangsan
    cookie_env: JXUT_COOKIE_ZHANGSAN
    targets:
      - date_offset: 1                      # 明天
        periods: ["中午", "12:00-14:00"]    # 匹配 sjdName 或 sjdStart-sjdEnd，支持 fnmatch 通配（"下午*"）
        rooms: ["*4楼紫阳阁-C*"]            # 匹配 roomName，出现顺序 = 优先级
        seats: ["019", "002"]               # 理想座位号，出现顺序 = 优先级
        avoid_seats: ["047"]                # 不想要的座位
        order: preferred                    # preferred | code_asc | code_desc | random
        allow_change: true                  # 已预约但不理想 -> 自动换座
        allow_skip_if_booked: true          # 已坐到理想座位 -> 跳过
```

### 常用命令

```bash
python main.py run                                  # 立即按配置抢
python main.py run --dry-run                        # 只选座不下单，先验证策略
python main.py run --at 07:00:00                    # 阻塞等到 07:00:00 再开抢（抢点模式）
python main.py run --no-wait                        # 忽略 open_time，立即抢（手动补抢用）
python main.py run -u zhangsan -u lisi              # 只跑部分用户
python main.py run --date 2026-10-22                # 覆盖全部目标的日期
python main.py rooms --date 2026-10-22 --period 中午  # 看看某时段还剩哪些座位、房间名怎么写
```

### 参数速查

| 配置项 | 含义 |
| --- | --- |
| `global.max_workers` | 多用户并发线程数 |
| `global.burst_workers` | 单时段同时发起的 `save` 数量；**>1 可能同时占多个座位**，默认 1 |
| `global.open_time` | 开抢时刻，脚本先等待、到点再打（配合 Actions cron 提前启动） |
| `global.min_interval` / `jitter` | 请求最小间隔与抖动，**别调太小**，避免给服务器添麻烦 |
| `global.max_retry` / `retry_backoff` | 失败重试轮数与指数退避基数 |
| `global.retry_max_backoff` | 退避上限秒数；把 `max_retry` 调大 + 它调小 = 「放号晚了就一直轮询等」 |
| `global.fallback_seats` | 单时段最多尝试的候选座位数（换座次数上限） |
| `target.require_preferred` | `true`：理想座位 seats 一个都没有就直接放弃，不占别的座；`false`：退而求其次坐房间里的空位 |
| `global.history_file` | 每次执行的明细留痕（含起止时间戳、每个时段耗时），默认 `logs/history.log` |
| `global.lock.backend` | `none` / `file` / `redis` / `gist`，多实例互斥去重 |

---

## 5. 失败重试与结果校验

单个「日期 + 时段」的执行链路：

```
getSjd -> 匹配时段 -> 已预约？(满意则跳过 / 允许则换座)
        -> getZws 拿空闲座位 -> 策略排序 -> save
        -> getSjd 回查校验 zwCode 是否落位
        -> 失败则指数退避后重来，换下一个候选座位
```

- 座位被抢走（并发冲突）→ 换下一个候选，最多 `fallback_seats` 个
- 整轮都没成 → 退避 `retry_backoff * 2^n` 秒后重跑，最多 `max_retry` 轮
- cookie 失效 → 自动触发 `_relogin()`（本地可弹浏览器，云端给通知）
- 提交成功但回查不到 → 判定失败并记入汇总，**以回查结果为准**，避免假成功

---

## 6. GitHub Actions：云端 + 离线

两个工作流已经写好，推送后即可用：

| 文件 | 触发 | 作用 |
| --- | --- | --- |
| `.github/workflows/grab.yml` | cron（每天 UTC 21:55 = 北京 05:55，脚本自己等到 **06:05**）+ 手动 `workflow_dispatch` | 每天 06:05 抢**下一天**的座位；手动触发时默认立即抢，可传 `user` / `at` / `date` / `dry_run` |
| `.github/workflows/watchdog.yml` | cron（北京 05:30 体检；06:00/06:30/07:00/07:30 补抢） | 主任务漏抢的时段再补三次、cookie 提前体检 |

手动触发：仓库 → Actions → `图书馆自动抢座` → `Run workflow`（手机 GitHub App 也能点），无需本地开机。

### Secrets 配置

Settings → Secrets and variables → Actions：

- Secrets：`JXUT_COOKIE_<大写用户名>`（如 `JXUT_COOKIE_ZHANGSAN`）、通知渠道 key、`LOCK_GIST_TOKEN`
- Variables：`GRAB_USERS`，JSON 数组，**元素必须大写**，例如 `["ZHANGSAN","LISI"]`

> 手动触发时 `user` 填 `all` 或留空 = 按 `GRAB_USERS` 展开成多个独立矩阵任务；只有填具体用户名才单跑一个。若 `GRAB_USERS` 未配置且不填，默认按 `["ZHANGSAN"]` 处理。

矩阵 = 真正的分布式：每个用户一个独立 runner 并行执行，`concurrency` + 分布式锁双保险防止重复下单。

> ⚠️ GitHub 对**连续 60 天无任何提交**的仓库会停用定时任务。长期放着用，建议 `watchdog.yml` 里再挂一个 `repository_dispatch`，或不定期 push 一次。

---

## 7. 通知

配置 `notify` 段，可同时启用多个渠道：`serverchan`、`pushplus`、`telegram`、`dingtalk`（支持加签）、`wecom_bot`、`bark`、`smtp`、`webhook`（自定义 POST JSON）。

- 单个时段成功 → 立即推送「用户 / 日期 / 时段 / 座位」
- 整轮结束 → 推送汇总（含失败项），并写入 Actions 的 Step Summary

---

## 8. 目录结构

```
jxut-seat-grabber/
├── main.py                  # CLI：run / login / probe / rooms
├── grabber/
│   ├── client.py            # 三个接口的 HTTP 客户端（完全对齐抓包）
│   ├── auth.py              # cookie 获取/缓存/刷新 + 身份信息解析
│   ├── models.py            # TimeSlot / Room / Seat / SaveResult / GrabResult
│   ├── config.py            # YAML -> 数据类，${ENV:VAR} 展开
│   ├── strategy.py          # 日期/时段/房间/座位的选择策略
│   ├── grabber.py           # 抢座引擎（重试、换座、回查、突发并发）
│   ├── runner.py            # 多用户线程池调度 + 汇总
│   ├── lock.py              # 分布式锁：none/file/redis/gist
│   ├── notify.py            # 8 种通知渠道
│   └── utils.py             # 日志/等待/抖动/匹配
├── config/config.example.yaml
└── .github/workflows/       # grab.yml（定时+手动） / watchdog.yml（捡漏+体检）
```

---

## 9. 使用建议

- 先用 `--dry-run` 跑一遍，确认时段名、房间名、座位号都解析正确，再正式下单
- `min_interval` 保持默认值，别高频刷接口；一个账号同时只允许一个任务在跑
- 本项目仅用于学习 HTTP 协议与自动化工程实践，请遵守学校图书馆的使用规定，不要用于批量占座等影响他人的行为
- 本项目仅作为工程项目，仅供参考
