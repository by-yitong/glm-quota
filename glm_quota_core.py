#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智谱 GLM Coding Plan 用量查询 —— 核心模块。

查询规则复刻自 cc-switch (https://github.com/farion1231/cc-switch) 的
`src-tauri/src/services/coding_plan.rs::query_zhipu`，包括：
  - 端点路由：base_url 含 bigmodel.cn → https://open.bigmodel.cn，否则 → https://api.z.ai
  - 鉴权：Authorization 头直接放 api_key，不加 Bearer 前缀
  - 解析：取 data.limits[] 中 type==TOKENS_LIMIT 的条目，按 unit 字段分类窗口
         (unit 3 → 5小时窗口, unit 6 → 每周窗口)；unit 缺失时用重置时间启发式兜底
  - 等级：data.level 字段

零外部依赖，仅用标准库 urllib。
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


CONFIG_DIR = os.path.expanduser("~/.config/glm-quota")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# 窗口名（与 cc-switch 的 TIER_FIVE_HOUR / TIER_WEEKLY_LIMIT 对齐）
TIER_FIVE_HOUR = "five_hour"
TIER_WEEKLY = "weekly_limit"


@dataclass
class Tier:
    name: str            # "five_hour" 或 "weekly_limit"
    utilization: float   # 已用百分比，0-100（可能为负或超 100，原样透传）
    resets_at: Optional[str] = None  # ISO 8601 字符串


@dataclass
class QuotaResult:
    ok: bool = False
    level: Optional[str] = None      # 套餐等级
    tiers: list = field(default_factory=list)  # list[Tier]
    error: Optional[str] = None      # 错误信息（ok=False 时有值）
    queried_at: Optional[int] = None # 毫秒时间戳
    credential_valid: bool = True    # False = key 失效(401/403)


def _now_millis() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _millis_to_iso(ms: int) -> Optional[str]:
    """毫秒时间戳 → ISO 8601 字符串。"""
    try:
        secs = ms // 1000
        return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def default_config() -> dict:
    return {
        "api_key": "",
        "base_url": "https://api.z.ai",
        "refresh_minutes": 30,       # 托盘自动刷新间隔(0=不自动刷新)
        "notify_threshold": 85,      # 超过该百分比弹桌面通知(0=关闭)
    }


def load_config() -> dict:
    """读取配置；文件不存在则返回默认值。"""
    cfg = default_config()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update(user)
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    """写入配置，权限 600（仅属主可读写）。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def _error(msg: str, credential_valid: bool = True) -> QuotaResult:
    return QuotaResult(ok=False, error=msg, queried_at=_now_millis(),
                       credential_valid=credential_valid)


def _classify_window(item: dict) -> Optional[str]:
    """按 unit 字段判定窗口类型（复刻 classify_zhipu_window）。

    unit 3 → 5小时窗口；unit 6 → 每周窗口；其它 → None(走启发式兜底)。
    """
    unit = item.get("unit")
    if isinstance(unit, bool):  # JSON true/false 在 Python 里也是 int，挡掉
        return None
    if unit == 3:
        return TIER_FIVE_HOUR
    if unit == 6:
        return TIER_WEEKLY
    return None


def _parse_token_tiers(data: dict) -> list:
    """把 data['limits'] 解析成 Tier 列表（复刻 parse_zhipu_token_tiers）。

    分类优先级：
      1. 显式 unit 字段标识窗口类型
      2. unit 缺失/不认识 → 启发式：无 nextResetTime 的优先归 five_hour，
         其余按 reset 升序填入空缺槽位
    最多两条（five_hour / weekly）。
    """
    five_hour = None       # (reset_ms|None, percentage, reset_iso|None)
    weekly = None
    unclassified = []
    limits = data.get("limits")
    if not isinstance(limits, list):
        limits = []

    for item in limits:
        if not isinstance(item, dict):
            continue
        ltype = item.get("type", "")
        # 大小写不敏感比较（上游可能改成小写/驼峰）
        if not isinstance(ltype, str) or ltype.upper() != "TOKENS_LIMIT":
            continue
        pct = item.get("percentage")
        try:
            pct = float(pct) if isinstance(pct, (int, float)) else 0.0
        except (TypeError, ValueError):
            pct = 0.0
        reset_ms = item.get("nextResetTime")
        if isinstance(reset_ms, bool) or not isinstance(reset_ms, int):
            reset_ms = None
        reset_iso = _millis_to_iso(reset_ms) if reset_ms is not None else None
        entry = (reset_ms, pct, reset_iso)

        window = _classify_window(item)
        if window == TIER_FIVE_HOUR and five_hour is None:
            five_hour = entry
        elif window == TIER_WEEKLY and weekly is None:
            weekly = entry
        else:
            unclassified.append(entry)

    # 启发式兜底：无 reset 的优先，再按 reset 升序
    unclassified.sort(key=lambda e: (e[0] is not None, e[0] if e[0] is not None else 0))
    for entry in unclassified:
        if five_hour is None:
            five_hour = entry
        elif weekly is None:
            weekly = entry
        # 多余的忽略

    tiers = []
    for name, slot in ((TIER_FIVE_HOUR, five_hour), (TIER_WEEKLY, weekly)):
        if slot is not None:
            tiers.append(Tier(name=name, utilization=slot[1], resets_at=slot[2]))
    return tiers


def query_quota(api_key: str, base_url: str = "https://api.z.ai",
                timeout: float = 15.0) -> QuotaResult:
    """查询智谱 Coding Plan 用量。api_key 为空时直接报错，不发请求。"""
    if not api_key:
        return _error("未配置 api_key（请编辑 " + CONFIG_PATH + "）")

    # 端点路由：bigmodel.cn → open.bigmodel.cn，否则 → api.z.ai
    host = "https://open.bigmodel.cn" if "bigmodel.cn" in base_url.lower() else "https://api.z.ai"
    url = host + "/api/monitor/usage/quota/limit"

    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", api_key)   # 注意：不加 Bearer 前缀
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept-Language", "en-US,en")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body_bytes = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        if status in (401, 403):
            return _error("鉴权失败 (HTTP %d)：api_key 无效或已过期" % status,
                          credential_valid=False)
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return _error("API 错误 (HTTP %d): %s" % (status, body[:300]))
    except urllib.error.URLError as e:
        return _error("网络错误: %s" % e.reason)
    except Exception as e:
        return _error("请求失败: %s" % e)

    if status < 200 or status >= 300:
        return _error("API 错误 (HTTP %d)" % status)

    try:
        body = json.loads(body_bytes.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return _error("响应解析失败: %s" % e)

    # 业务级错误
    if body.get("success") is False:
        msg = body.get("msg", "未知错误")
        return _error("API 业务错误: %s" % msg)

    data = body.get("data")
    if not isinstance(data, dict):
        return _error("响应缺少 data 字段")

    tiers = _parse_token_tiers(data)
    level = data.get("level")
    if not isinstance(level, str):
        level = None

    return QuotaResult(ok=True, level=level, tiers=tiers,
                       queried_at=_now_millis(), credential_valid=True)


def tier_by_name(result: QuotaResult, name: str) -> Optional[Tier]:
    for t in result.tiers:
        if t.name == name:
            return t
    return None


def primary_percentage(result: QuotaResult) -> Optional[float]:
    """主展示用量：优先 five_hour 窗口，没有则取第一个 tier。"""
    t = tier_by_name(result, TIER_FIVE_HOUR)
    if t is None and result.tiers:
        t = result.tiers[0]
    return t.utilization if t else None
