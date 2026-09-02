"""飞书群机器人「热点日报 + 实时提醒」(见 doc/dev.md §5.11)。

**为什么有签名**:飞书自定义机器人若配置了"签名校验",请求体里必须带
`sign`,由 `secret` 求得,否则返回 `sign match fail`。算法是
`sign = base64(HmacSHA256(secret, f"{timestamp}\\n{secret}"))`(timestamp 单位秒)。

**两条推送通道**:
- `run_feishu_daily()`:按 `FEISHU_DAILY_CRON` 每天一次,推送三板块"上一轮 vs
  上上轮"榜单对比 + 整体趋势分析(上升最多/回落最多/新增最多)。
- `run_feishu_realtime(section, user_id)`:每次采集成功后调用;只把**当日新增**、
  **排名跳升 ≥ FEISHU_HOT_RANK_JUMP 名**、或**分值环比 ≥ FEISHU_HOT_RATIO** 的
  话题立即推到群里,并按 `FEISHU_ALERT_COOLDOWN_HOURS` 去重,防刷屏。

未配置 `FEISHU_WEBHOOK` 时全部自动关闭(不影响其他功能)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import DouhotWatchSnap, DouhotWord, FeishuAlert, WeiboHotItem, XianyuItem
from app.utils import get_logger

logger = get_logger(__name__)

SECTIONS = ("weibo", "xianyu", "douhot")
SECTION_LABELS = {"weibo": "微博热搜", "xianyu": "闲鱼热榜", "douhot": "抖音热点"}
# 每个板块如何取"标题"与"排名/分值"
_RANK_FIELD = {"weibo": "rank", "xianyu": "best_rank", "douhot": "score"}
# 每板块的历史表(用于上一轮快照对比)
_TABLES = {"weibo": WeiboHotItem, "xianyu": XianyuItem, "douhot": DouhotWord}

_TIMEOUT = 15


def _sign(secret: str, ts: int) -> str:
    """飞书签名。

    官方算法:`string_to_sign = '{timestamp}\\n{secret}'`,以它为 **HMAC key**,
    消息体为**空字符串**,`sign = base64(HmacSHA256(string_to_sign, ""))`。
    注意不是拿 secret 当 key——两者写反会 `sign match fail`。
    """
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, msg=b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuClient:
    """飞书自定义机器人客户端(单条文本消息)。"""

    def __init__(self, webhook: str, secret: str) -> None:
        self.webhook = webhook
        self.secret = secret or ""

    def send(self, text: str) -> bool:
        """推送一条文本。成功返回 True;网络/签名/风控错误记日志并返回 False。"""
        if len(text) > 18000:
            text = text[:17000] + "\n…(内容过长已截断)"
        ts = int(time.time())
        body: dict = {"msg_type": "text", "content": {"text": text}}
        if self.secret:
            body["timestamp"] = str(ts)
            body["sign"] = _sign(self.secret, ts)
        try:
            resp = requests.post(self.webhook, json=body, timeout=_TIMEOUT)
            obj = resp.json()
            code, msg = obj.get("code"), obj.get("msg")
            if code == 0:
                return True
            logger.warning("飞书推送失败 code=%s msg=%s", code, msg)
            return False
        except (requests.RequestException, ValueError) as exc:  # 网络错误/非 JSON
            logger.error("飞书推送异常:%s", exc)
            return False


# ---------------------------------------------------------------------------
# 数据读取:取该用户该板块最近两次采集批次
# ---------------------------------------------------------------------------

def _batches(db: Session, user_id: int, section: str) -> tuple[dict[str, object], dict[str, object]]:
    """返回 (当前批 {title: row}, 上一批 {title: row})。

    批次 = 该用户该板块**全局最近一次采集时间点**(当前批)与其**前一个时间点**(上一批)。
    同一标题在不同批各自出现 → 才能做"排名涨跌";只在当前批出现 → 判为新增。
    每批内若同标题出现多次(极少数),取该批最后一次。
    """
    import time as _t

    table = _TABLES[section]
    rows = db.scalars(
        select(table).where(table.user_id == user_id).order_by(table.id.asc())
    ).all()
    if not rows:
        return {}, {}

    def key_of(r) -> str:
        return str(getattr(r, "title", "") or getattr(r, "item_id", "") or "").strip()

    def ts_of(r) -> datetime:
        ts = getattr(r, "captured_at", None) or getattr(r, "created_at")
        return ts if isinstance(ts, datetime) else datetime.min

    # 收集全局所有出现过的采集时间点,取最近两个
    all_ts = {ts_of(r) for r in rows}
    sorted_ts = sorted(all_ts)
    cur_ts = sorted_ts[-1]
    prev_ts = sorted_ts[-2] if len(sorted_ts) >= 2 else None

    def build(ts: datetime) -> dict[str, object]:
        out: dict[str, object] = {}
        for r in rows:
            if ts_of(r) == ts:
                out[key_of(r)] = r  # id 升序遍历,同批内后者覆盖 = 取该批最后一条
        return {k: v for k, v in out.items() if k}

    return build(cur_ts), build(prev_ts) if prev_ts is not None else {}


# ---------------------------------------------------------------------------
# 日报
# ---------------------------------------------------------------------------

def _delta(section: str, cur: object, prev_by_title: dict[str, object]) -> tuple[str, str]:
    """返回 (shift_tag, extra):排名/分值相对上一轮的变化标签与补充说明。"""
    title = str(getattr(cur, "title", "") or getattr(cur, "item_id", "")).strip()
    prev = prev_by_title.get(title)
    if prev is None:
        return "new", ""

    if section == "douhot":
        cv = float(getattr(cur, "score", 0) or 0)
        pv = float(getattr(prev, "score", 0) or 0)
        if pv > 0 and cv >= pv * 1.05:
            return "up", f"+{(cv - pv) / pv * 100:.0f}%"
        if pv > 0 and cv <= pv * 0.95:
            return "down", f"-{(pv - cv) / pv * 100:.0f}%"
        return "stay", ""

    cr = int(getattr(cur, _RANK_FIELD[section], 0) or 0)
    pr = int(getattr(prev, _RANK_FIELD[section], 0) or 0)
    delta = pr - cr  # 名次从 pr 变到 cr;变正 = 上升(名次更小)
    if delta > 0:
        return "up", f"+{delta}名"
    if delta < 0:
        return "down", f"-{abs(delta)}名"
    return "stay", ""


def _trend_summary(section: str, cur: dict[str, object], prev: dict[str, object], n: int = 5) -> str:
    """分析一段话:上升最多 / 新增 / 回落最多,用于日报的"整体趋势"。"""
    up, down, new = [], [], []
    for title, c in list(cur.items())[:60]:
        tag, extra = _delta(section, c, prev)
        if tag == "up":
            up.append((title, extra))
        elif tag == "down":
            down.append((title, extra))
        elif tag == "new":
            new.append(title)
    parts = []
    if up:
        parts.append("上升:" + "; ".join(f"{t}({e})" for t, e in up[:n]))
    if new:
        parts.append("新增:" + ", ".join(new[:n]))
    if down:
        parts.append("回落:" + "; ".join(f"{t}({e})" for t, e in down[:n]))
    return " | ".join(parts) if parts else "整体平稳"


def _section_lines(db: Session, user_id: int, section: str, top_n: int = 10) -> list[str]:
    """某板块日报:每话题带排名变化或新增标签,末尾附一段趋势分析。"""
    cur, prev = _batches(db, user_id, section)
    if not cur:
        return [f"  · {SECTION_LABELS[section]}:暂无数据"]

    # douhot 无 rank 列,按 score 逆序得到名次顺序,用于显示与排序
    order_titles = sorted(cur.keys(), key=lambda t: -float(getattr(cur[t], "score", 0) or 0))
    ranked = order_titles[:top_n]
    lines = [f"【{SECTION_LABELS[section]}】"]
    for title in ranked:
        c = cur[title]
        tag, extra = _delta(section, c, prev)
        if tag == "new":
            badge = "✅新增"
        elif tag == "up":
            badge = f"🔥{extra}"
        elif tag == "down":
            badge = f"📉{extra}"
        else:
            badge = "➖持平"
        lines.append(f"  · {title[:24]}  {badge}")
    lines.append(f"  分析:{_trend_summary(section, cur, prev)}")
    return lines


def build_daily(db: Session, user_id: int, settings: Settings) -> str:
    """生成三板块日报文本(供飞书推送与测试)。"""
    today = datetime.now().strftime("%m-%d")
    lines = [f"📊 热点日报 · {today}", "每个话题标注相对上一轮的涨跌或新增。"]
    for section in SECTIONS:
        lines += _section_lines(db, user_id, section)
    return "\n".join(lines)


def run_feishu_daily() -> int:
    """每日定时:给所有启用用户生成并推送日报。返回推送次数。"""
    from app.db import get_session_local

    settings = get_settings()
    if not settings.feishu_webhook:
        return 0
    from app.db.models import User

    client = FeishuClient(settings.feishu_webhook, settings.feishu_secret)
    db = get_session_local()()
    sent = 0
    try:
        for user in db.scalars(select(User).where(User.enabled.is_(True))).all():
            text = build_daily(db, user.id, settings)
            if client.send(text):
                sent += 1
        logger.info("飞书日报推送完成,用户数=%s", sent)
        return sent
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 实时提醒(采集成功后调用)
# ---------------------------------------------------------------------------

def _in_cooldown(db: Session, user_id: int, section: str, title: str, settings: Settings) -> bool:
    now = datetime.now()
    row = db.scalar(
        select(FeishuAlert).where(
            FeishuAlert.user_id == user_id, FeishuAlert.section == section, FeishuAlert.title == title
        )
    )
    return row is not None and (now - row.alerted_at).total_seconds() < settings.feishu_alert_cooldown_hours * 3600


def run_feishu_keyword_alerts(user_id: int, settings: Settings | None = None, db: Session | None = None) -> int:
    """智能体预警:把"预测可能爆发"的关注词推送到群里。

    对用户每个关注词,用 `keyword_agent` 做趋势分析;命中爆发信号(上升期 + 强增长 +
    一定置信度 + 正加速度)且不在冷却期内的,推一条"预测爆发"消息。去重表复用 FeishuAlert。
    返回推送条数;`db` 供测试注入。
    """
    settings = settings or get_settings()
    if not settings.feishu_webhook:
        return 0
    from app.services import keyword_agent
    from app.services import tenant
    from app.db import get_session_local

    own_session = db is None
    db = db or get_session_local()()
    pushed = 0
    try:
        watches = tenant.list_douhot_watch(db, user_id)
        if not watches:
            return 0
        client = FeishuClient(settings.feishu_webhook, settings.feishu_secret)
        hits: list[dict] = []
        for w in watches:
            snaps = db.scalars(
                select(DouhotWatchSnap)
                .where(DouhotWatchSnap.user_id == user_id, DouhotWatchSnap.keyword == w["keyword"])
                .order_by(DouhotWatchSnap.id.asc())
            ).all()
            values = [s.score for s in snaps]
            if not values:
                continue
            agent = keyword_agent.analyze(w["keyword"], values)
            if not agent["burst"]:
                continue
            if _in_cooldown(db, user_id, "keyword_burst", w["keyword"], settings):
                continue
            _mark_alerted(db, user_id, "keyword_burst", w["keyword"], "预测爆发")
            hits.append(agent)
        if hits:
            lines = ["🔮 智能体预测 · 可能爆发"]
            for a in hits:
                lines.append(f"  · {a['keyword']} 预测 {a['forecast_next']:.0f} 环比+{a['growth']*100:.0f}%")
            client.send("\n".join(lines))
            pushed = len(hits)
            logger.info("飞书智能体预警推送 user=%s 条数=%s", user_id, pushed)
        return pushed
    finally:
        if own_session:
            db.close()


def _mark_alerted(db: Session, user_id: int, section: str, title: str, reason: str) -> None:
    row = db.scalar(
        select(FeishuAlert).where(
            FeishuAlert.user_id == user_id, FeishuAlert.section == section, FeishuAlert.title == title
        )
    )
    if row is None:
        db.add(FeishuAlert(user_id=user_id, section=section, title=title, reason=reason))
    else:
        row.reason, row.alerted_at = reason, datetime.now()
    db.commit()


def run_feishu_realtime(
    section: str, user_id: int, settings: Settings | None = None, db: Session | None = None
) -> int:
    """采集成功后调用:把新增/飙升的话题立即推送到群里,并去重。

    返回此次推送条数(0 = 未配置/无触发)。`db` 供测试注入;缺省用全局会话。
    """
    settings = settings or get_settings()
    if not settings.feishu_webhook:
        return 0
    from app.db import get_session_local

    own_session = db is None
    db = db or get_session_local()()
    pushed = 0
    try:
        cur, prev = _batches(db, user_id, section)
        if not cur:
            return 0
        client = FeishuClient(settings.feishu_webhook, settings.feishu_secret)
        need_push: list[str] = []
        for title, c in list(cur.items())[:40]:
            tag, extra = _delta(section, c, prev)
            reason = None
            if tag == "new":
                reason = "新增"
            elif tag == "up":
                if section == "douhot":
                    pct = float(extra.rstrip("%")) if extra.endswith("%") else 0
                    if pct / 100 >= settings.feishu_hot_ratio:
                        reason = f"飙升+{pct:.0f}%"
                else:
                    up = int(extra.rstrip("名").lstrip("+"))
                    if up >= settings.feishu_hot_rank_jump:
                        reason = str(extra)
            if reason and not _in_cooldown(db, user_id, section, title, settings):
                need_push.append(f"  · {title[:24]}  {reason}")
                _mark_alerted(db, user_id, section, title, reason)
        if need_push:
            head = f"⚡ {SECTION_LABELS[section]} 实时热点"
            client.send(head + "\n" + "\n".join(need_push))
            pushed = len(need_push)
            logger.info("飞书实时推送 section=%s user=%s 条数=%s", section, user_id, pushed)
        return pushed
    finally:
        if own_session:
            db.close()
