"""链路全检:每条采集/转存链到底通不通(2026-10-04)。

用户口径:「**链路都跑通之后来完善 agent**」—— 所以先要有一个**可重复跑**的体检,
而不是靠人回忆。每条链给出 绿/黄/红 + **判据**(判据要能自己复核)。

运行:`python scripts/chain_health.py`(`--json` 出机器可读)

判据分三层,缺一层都不算"通":
  ① **依赖在不在**(容器/库/venv/登录档案)—— "没装"和"装坏了"要分开;
  ② **凭证活不活**(各平台 Cookie 有没有、最近更新);
  ③ **最近有没有真产出**(运行记录 + 落库数据)—— ⚠️ **这一层最容易被漏掉**:
     前三层全绿而产出恒 0,那才是最难查的("静默失败=假成功")。

⚠️ 本脚本**只读**:不打任何写接口、不改库。探测用的网络请求都是 GET/只读。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

GREEN, YELLOW, RED = "🟢", "🟡", "🔴"

# 每条链:`(名称, 运行记录 kind, 期望产出的字段, 说明, 侧)`
# 「期望产出字段」取运行记录 detail 里的 `key=数字`;`None` = 该链没有产出计数
# 「侧」:`"local"`(默认,跑在本机)/ **`"remote"`(跑在远程,本机库看不到它的运行记录)**
#   ⚠️ **远程侧不能按本地标准判红** —— 本机与远程**库是独立的**,本机查不到它的记录是**正常的**,
#   报红就是误报。而"一条永远红的项会训练人忽略整份报告"(与"闸门失准"同一条教训)。
CHAINS: tuple[tuple[str, str, str | None, str, str], ...] = (
    ("公众号·发现候选号", "wechat_candidates", "new", "搜狗微信搜索(免费免账号)"),
    ("公众号·收录对标号", "wechat_candidate_import", "listenable", "WeRSS search_mp → MP_WXS_*"),
    ("公众号·监听采文", "wechat_listen", "new", "微信读书 cover + 列表轮转"),
    ("抖音·口令线索", "douyin_leads", "线索", "MediaCrawler 搜索 → 《口令》→ 转存"),
    ("知乎/贴吧·盘链发现", "pan_discovery", None, "自研搜知乎 + MediaCrawler 搜贴吧"),
    ("小红书/贴吧·名字型热度", "resource_presence", "命中", "按资源名探平台 → 库内配对 → 转存"),
    ("迅雷·群转存", "xunlei_group", None, "群消息 → 转存 → 推卡"),
    ("迅雷·盘同步", "xunlei_sync", None, "扫盘入库"),
    ("闲鱼·采集", "xianyu", None, "Playwright 页面内调 mtop", "local"),
    ("本机→远程·公众号数据同步", "remote_sync", "文章", "SSH 隧道推到远程(补 Agent 的输入)"),
    # ⚠️ 这一条重点看 detail 里的「证据触发」:**恒为 0 的那几档**是"接了但没生效"的信号
    # (数据源没接上 / 阈值太高),不是"今天恰好没有" —— 那是本仓反复出现的"废弃链只摘了一半"。
    ("热点选题 Agent(远程)", "hotspot_agent", "热点",
     "detail 尾部带各档证据的触发次数,恒 0 的档要查;⚠️ 它跑在远程,本机库看不到", "remote"),
)


def _rows(db, kind: str, n: int = 5):
    from sqlalchemy import desc, select

    from app.db.models import RunRecord

    return db.scalars(select(RunRecord).where(RunRecord.kind == kind)
                      .order_by(desc(RunRecord.id)).limit(n)).all()


def _latest_map(db, days: int = 7) -> dict[str, datetime]:
    """各 kind 最近一次成功的时间(用于判"多久没动静了")。"""
    from sqlalchemy import func, select

    from app.db.models import RunRecord

    since = datetime.now() - timedelta(days=days)
    rows = db.execute(
        select(RunRecord.kind, func.max(RunRecord.started_at))
        .where(RunRecord.started_at >= since).group_by(RunRecord.kind)).all()
    return {str(k): v for k, v in rows}


def check_dependencies() -> list[dict]:
    """① 依赖层:容器 / 库 / venv / 登录档案。**只读探测**。"""
    import importlib
    import os
    import subprocess

    out: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append({"name": name, "level": GREEN if ok else RED, "detail": detail})

    # WeRSS 容器(免费列表源;它同时是"号名→fakeid"的解析器)
    try:
        import requests

        r = requests.get("http://127.0.0.1:8001/", timeout=6)
        add("WeRSS 容器(列表源 + 号名解析)", r.status_code < 500,
            f"HTTP {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        add("WeRSS 容器(列表源 + 号名解析)", False, f"连不上:{type(exc).__name__}")

    # aiotieba(贴吧点赞)
    try:
        importlib.import_module("aiotieba")
        add("aiotieba(贴吧点赞 agree)", True, "已安装")
    except ImportError:
        add("aiotieba(贴吧点赞 agree)", False, "未安装 —— pip install aiotieba")

    # MediaCrawler 独立 venv
    mc_py = os.path.join("tools", "MediaCrawler", ".venv", "Scripts", "python.exe")
    add("MediaCrawler venv", os.path.exists(mc_py),
        "存在" if os.path.exists(mc_py) else f"缺 {mc_py}")

    # 贴吧登录档案(MediaCrawler 需要先扫码一次)
    prof = os.path.join("tools", "MediaCrawler", "browser_data")
    n = len([d for d in os.listdir(prof) if "tieba" in d.lower()]) if os.path.isdir(prof) else 0
    add("贴吧登录档案", n > 0, f"{prof} 下 {n} 个贴吧档案")

    # newsnow(热榜长尾)—— ⚠️ **按实例角色判断**:它只服务于 `hot_source`(role=hotspot),
    # 而本机是 role=wechat ⇒ **本来就不该有它**,报红是**误报**。
    # (一条永远红的项会训练人忽略整份报告 —— 与"闸门失准"告警同一条教训。)
    from config.settings import get_settings

    role = str(getattr(get_settings(), "scheduler_role", "all") or "all")
    if role not in ("all", "hotspot"):
        out.append({"name": "newsnow 容器(热榜长尾)", "level": GREEN,
                    "detail": f"本机 role={role} **不该跑热榜**,不需要它"
                              f"(远程 hotspot 侧才要,且需配 HOT_NEWSNOW_URL)"})
        return out
    try:
        r = subprocess.run(["docker", "ps", "-a", "--filter", "name=newsnow",
                            "--format", "{{.Status}}"],
                           capture_output=True, text=True, timeout=15)
        st = (r.stdout or "").strip()
        add("newsnow 容器(热榜长尾)", bool(st) and st.startswith("Up"),
            st or "容器不存在 —— 本机 role=hotspot 需要它,且 .env 要配 HOT_NEWSNOW_URL")
    except Exception as exc:  # noqa: BLE001
        out.append({"name": "newsnow 容器(热榜长尾)", "level": YELLOW,
                    "detail": f"探不到 docker:{type(exc).__name__}"})
    return out


def check_credentials(db) -> list[dict]:
    """② 凭证层:各平台 Cookie 在不在、最后一次更新是什么时候。

    ⚠️ **2026-10-05 补:微信读书要"验活",不能只看更新时间**。
    当天实测踩到:这里显示 `🟢 微信读书 —— 更新于 07:50(0 天前)`,
    **而那个 cookie 其实已经 `-2012 登录态失效`** —— 因为 08:02~12:57 之间死的。
    **只看"多久前更新过"是假绿**;一个不验证的指标,绿灯毫无意义
    (与"静默失败=假成功"同一条线)。
    微信读书正好有一个**只读且便宜**的验活口:`/web/shelf/sync`。
    """
    from sqlalchemy import select

    from app.db.models import UserCookie
    from config.settings import get_settings

    out: list[dict] = []
    rows = db.scalars(select(UserCookie).where(UserCookie.user_id == 1)).all()
    have = {str(r.platform): r.updated_at for r in rows}
    # 只需要凭证的平台(公众号/抖音/贴吧走匿名或档案,不在这张表里)
    for plat, label in (("weread", "微信读书(公众号阅读数)"),
                        ("zhihu", "知乎(盘链搜索)"),
                        ("xunlei", "迅雷(转存)"),
                        ("quark", "夸克(转存)"),
                        ("baidupan", "百度网盘(转存)"),
                        ("goofish", "闲鱼(采集)")):
        at = have.get(plat)
        if at is None:
            # 夸克/百度缺失时转存会降级,不一定是红
            out.append({"name": label, "level": YELLOW if plat in ("quark", "baidupan") else RED,
                        "detail": "库里没有该平台 Cookie"})
            continue
        age = (datetime.now() - at).days
        level = GREEN if age <= 7 else YELLOW
        detail = f"更新于 {str(at)[:16]}({age} 天前)"
        # **验活**(只对微信读书做:它有一个只读且便宜的探针)
        if plat == "weread":
            alive, why = _weread_alive(db)
            nxt = _next_renewal(get_settings())
            if alive is True:
                detail += f" · 验活 ✓({why})"
            elif alive is False:
                # ⚠️ **失效 ≠ 故障**:`wr_skey` 短效,而续期每 6 小时一次 ——
                # 两次续期之间它本来就可能过期,**那个空窗里没有任何作业要用它**,
                # 下一轮续期会在监听前 10 分钟接上。所以判 **🟡 而不是 🔴**。
                # (我第一次就是在这里误报的:12:57 测到 -2012 就喊"监听要挂了",
                #  而 13:50 的续期会接上 —— 假红比漏报更糟,它会训练人忽略整份报告。)
                # ⚠️ **真正的告警在续期作业那边**:`weread_refresh_tick` 失败时会直接推飞书
                # "请重新复制 Cookie" —— 那个才是"凭据真死了"的信号,不靠这里。
                when = f"下一次续期 {nxt:%H:%M}" if nxt else "续期时间未知"
                level = YELLOW
                detail = (f"当前未通过验活({why}),但**这多半是正常空窗** —— "
                          f"{when},会接在监听之前。若那次续期也失败,"
                          f"续期作业会单独推飞书告警。更新于 {str(at)[:16]}")
            else:
                detail += f" · 验活跳过({why})"
        out.append({"name": label, "level": level, "detail": detail})
    return out


def _next_renewal(settings) -> datetime | None:
    """下一次微信读书续期的时间(从 `weread_refresh_cron` 算)。算不出返回 None。"""
    try:
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(str(settings.weread_refresh_cron)).get_next_fire_time(
            None, datetime.now()).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - 算不出就当不知道
        return None


def _weread_alive(db) -> tuple[bool | None, str]:
    """微信读书凭据**验活**(只读):读一次书架。

    返回 `(True/False/None, 原因)`;`None` = **验不了**(别把"不知道"当成"活着")。

    ⚠️ **返回 False 不等于"故障"**(2026-10-05 血的教训):
    `wr_skey` 是**短效**的,而续期是**每 6 小时一次**(03:50/07:50/13:50/19:50,对齐四个监听定点)。
    所以**两次续期之间它本来就可能已经过期** —— 那个空窗里没有任何作业要用它,
    下一轮续期会在监听前 10 分钟接上。**只看"此刻活不活"就会把正常空窗报成故障**
    (我第一次就是这么误报的:12:57 测到 `-2012`,就喊"监听要挂了",而 13:50 的续期会接上)。
    ⇒ 调用方要**结合下次续期时间**判断,别把空窗当故障。
    """
    try:
        import requests

        from app.services.cookie_store import get_cookie
        # ⚠️ `get_cookie` **返回的已经是明文**(它在内部解密)—— 别再 `decrypt_cookie` 一次,
        # 那会双重解密直接 `InvalidToken`(我第一版就这么写的)。
        ck = get_cookie(db, 1, "weread") or ""
        if not ck:
            return False, "库里没有 Cookie"
        r = requests.get("https://weread.qq.com/web/shelf/sync",
                         params={"userVid": "", "synckey": "0", "lectureSynckey": "0"},
                         headers={"User-Agent": "Mozilla/5.0", "Cookie": ck,
                                  "Accept": "application/json"}, timeout=15)
        data = r.json()
        # ⚠️ 微信读书**失败也回 HTTP 200**,判据只能是 `errCode` ——
        # 光看状态码会把它当成功(本仓的"HTTP 200 ≠ 成功",B站那处也栽过)
        code = data.get("errCode")
        if code in (None, 0) and data.get("books"):
            return True, f"{len(data['books'])} 个号"
        return False, f"errCode={code}"
    except Exception as exc:  # noqa: BLE001 - 探针拿不到就说不知道,别报成"死了"
        return None, f"{type(exc).__name__}"


def check_chains(db, days: int = 3) -> list[dict]:
    """③ 产出层:每条链最近几次运行的产出(判据取自运行记录的 detail)。"""
    import re

    out: list[dict] = []
    for entry in CHAINS:
        label, kind, metric = entry[0], entry[1], entry[2]
        side = entry[4] if len(entry) > 4 else "local"
        rows = _rows(db, kind)
        if not rows:
            # ⚠️ **远程侧的链不能按本地标准判红**:两库独立,本机查不到它的运行记录是**正常的** ——
            # 报红就是误报,而"一条永远红的项会训练人忽略整份报告"(与"闸门失准"同一条教训)。
            if side == "remote":
                out.append({"name": label, "level": YELLOW,
                            "detail": "跑在**远程**,本机库看不到它的运行记录(属正常;"
                                      "要查它得登录远程)"})
            else:
                out.append({"name": label, "level": RED, "detail": "没有任何运行记录"})
            continue
        last = rows[0]
        age_h = (datetime.now() - last.started_at).total_seconds() / 3600
        detail = str(last.detail or "")[:70]
        # 产出值:能从 detail 里解析出就用它,解析不出**不下结论**(可能只是格式变了)
        val = None
        if metric:
            for r in rows:
                m = re.search(rf"(?:^|\s){re.escape(metric)}=(\d+)", str(r.detail or ""))
                if m:
                    val = int(m.group(1))
                    break
        stale_h = age_h > 48
        if last.status not in ("success", "partial"):
            lvl = RED
        elif stale_h:
            lvl = YELLOW
        elif metric and val == 0 and all(
                re.search(rf"(?:^|\s){re.escape(metric)}=0", str(r.detail or "")) for r in rows):
            lvl = YELLOW          # 连着几次都是 0
        else:
            lvl = GREEN
        bits = [f"最近 {str(last.started_at)[:16]}({age_h:.0f}h 前)", last.status]
        if metric:
            bits.append(f"{metric}={val if val is not None else '解析不出'}")
        out.append({"name": label, "level": lvl, "detail": " · ".join(bits) + f" · {detail}"})
    return out


def render(sections: list[tuple[str, list[dict]]]) -> str:
    lines: list[str] = [f"🔗 链路全检(只读 · {datetime.now():%m-%d %H:%M})", ""]
    for title, rows in sections:
        lines.append(f"【{title}】")
        for r in rows:
            lines.append(f"  {r['level']} {r['name']} —— {r['detail']}")
        lines.append("")
    bad = [r for _t, rs in sections for r in rs if r["level"] == RED]
    warn = [r for _t, rs in sections for r in rs if r["level"] == YELLOW]
    lines.append(f"小结:🔴 {len(bad)} · 🟡 {len(warn)} · "
                 f"🟢 {sum(len(rs) for _t, rs in sections) - len(bad) - len(warn)}")
    if bad:
        lines.append("先修红的:" + "、".join(r["name"] for r in bad))
    return chr(10).join(lines)


def check_read_num_coverage(db, days: int = 3) -> list[dict]:
    """④ **阅读数覆盖** —— 只看**库里的真值**,不看运行记录字符串。

    ⚠️ **为什么必须单列这一条**(2026-10-05 的真实事故):`check_chains` 判"监听采文"
    看的是 detail 里的 `new=N` —— **采到文章就绿**。而精确阅读数在 detail 里只是个 `ok=0`。
    于是 2026-09-29 ~ 10-05 网页路被账号级拦了**整整一周**:近 3 天入库 162 篇、
    **有阅读数的 0 篇**,而每一层监控都在报绿/黄。

    ⇒ **"采到文章"与"采到阅读数"是两件事,得分开盯。** 这条是对着"它会再坏一次"加的,
    不是对着"这次坏了"。

    判据用**落库数据**而不是 detail 格式:格式会飘(这仓飘过),而库里的 `read_num` 是事实。
    """
    from sqlalchemy import func, select

    from app.db.models import WechatArticle

    since = datetime.now() - timedelta(days=days)
    try:
        total = db.scalar(select(func.count(WechatArticle.id))
                          .where(WechatArticle.created_at >= since)) or 0
        withnum = db.scalar(select(func.count(WechatArticle.id))
                            .where(WechatArticle.created_at >= since,
                                   WechatArticle.read_num > 0)) or 0
    except Exception as exc:  # noqa: BLE001 - 查不到就说不知道,别报成"死了"
        return [{"name": "公众号·精确阅读数", "level": YELLOW,
                 "detail": f"查不到({type(exc).__name__})"}]

    if total == 0:
        return [{"name": "公众号·精确阅读数", "level": YELLOW,
                 "detail": f"近 {days} 天没有新文,无从判断"}]
    ratio = withnum / total
    detail = f"近 {days} 天入库 {total} 篇,其中有阅读数的 **{withnum}** 篇"
    if withnum == 0:
        # 这正是 2026-10-05 那次的形态 —— 采文一切正常,阅读数全无
        return [{"name": "公众号·精确阅读数", "level": RED,
                 "detail": detail + " ⇒ **精确阅读数全丢**。查两条路:网页 "
                           "`/web/mp/articles`(可能 `-2041` 被拦)、App "
                           "`i.weread.qq.com/book/articles`(token 可能过期,跑 "
                           "`scripts/weread_app_login.py` 重取)"}]
    if ratio < 0.2:
        return [{"name": "公众号·精确阅读数", "level": YELLOW,
                 "detail": detail + f"(覆盖率 {ratio:.0%},偏低 —— 通常说明列表额度/兜底有问题)"}]
    return [{"name": "公众号·精确阅读数", "level": GREEN,
             "detail": detail + f"(覆盖率 {ratio:.0%})"}]


def collect_sections(db) -> list[tuple[str, list[dict]]]:
    """跑完三层检查,返回 [(小标题, 结果列表)]。**只读**。调用方负责渲染/推送。"""
    return [("依赖(容器/库/venv/档案)", check_dependencies()),
            ("凭证(Cookie 在不在)", check_credentials(db)),
            ("产出(最近几次真跑出来的东西)", check_chains(db) + check_read_num_coverage(db))]


def chain_report_tick(settings=None) -> int:
    """**把链路体检推到管理群**(2026-10-05 用户口径:「没有推送小红书、B站、知乎、贴吧、
    多平台的运行情况啊,管理器里面」)。

    ⚠️ 之前只有**公众号**有周期性的总结(`wechat_digest`),**其余几条链(小红书/B站/知乎/贴吧/
    抖音/迅雷/闲鱼)全靠人手动跑脚本看** —— 等于**没有主动通报**。
    这里把体检结果推给**管理群**(不是客户群):它是"要人干活的",与 `wechat_digest` 同一去向。

    ⚠️ **只在有红项时才推**? —— 不。**每天固定推一次**,哪怕是全绿:
    "今天全绿"本身就是运维要知道的信息,而且**只有每天都来,人才会注意到"今天没来"**
    (与 `health_push` 的"该来没来本身就是信号"同一条)。
    """
    from config.settings import get_settings
    from app.db import get_session_local

    settings = settings or get_settings()
    from app.services.feishu_client import FeishuClient, webhook_for
    hook = webhook_for(settings, "admin")
    if not hook:
        return 0
    db = get_session_local()()
    try:
        text = render(collect_sections(db))
    finally:
        db.close()
    ok = FeishuClient(hook, settings.feishu_secret).send(
        "🔗 链路体检(多平台)\n" + text[:3000])
    return 1 if ok else 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口(脚本侧 `scripts/chain_health.py` 只是薄壳)。"""
    args = argv if argv is not None else sys.argv[1:]
    from app.db.database import get_session_local

    db = get_session_local()()
    try:
        sections = collect_sections(db)
    finally:
        db.close()
    if "--json" in args:
        print(json.dumps({t: rs for t, rs in sections}, ensure_ascii=False, indent=2,
                         default=str))
    elif "--push" in args:
        print("已推送:", chain_report_tick())
    else:
        print(render(sections))
    return 0


if __name__ == "__main__":       # 直接 `python -m app.services.chain_health` 也能跑
    raise SystemExit(main())
