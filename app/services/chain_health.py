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
import re          # ⚠️ 模块级(2026-10-08):`_PARTIAL_FAIL_RE` 是模块级常量,
                   # 而这里原先只在函数里局部 import re,加模块级常量就会 NameError
import sys
from datetime import datetime, timedelta

from app.utils import get_logger

logger = get_logger(__name__)

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
    # ⚠️⚠️ 下面三条**以前根本不在体检里**(2026-10-08 首次做覆盖面审计时发现):
    # 它们有运行记录、也有失败,但**没有任何一行报告会读到** —— 属于"埋了但没人看"。
    # - 夸克口令:`quark_kouling` 的记录**状态是 success**,而 detail 里写着
    #   `试8 成功0 失败2;主因:雷电窗口不在前台` —— **作业没崩 ≠ 事情做成了**,
    #   实测白跑一整天(23 轮)没人知道。指标取 `成功`(解析要容忍 `成功0` 这种无等号写法)。
    ("网盘·夸克口令转存", "quark_kouling", "成功", "雷电里读剪贴板口令 → 转存(前置检查不过则本轮不做)"),
    # - B站对标号扫描:今天撞过 `-352` 限流;指标取 `标题`(扫到多少有资源标题的号)。
    ("B站·对标号扫描", "bili_account_scan", "标题", "B站 API 搜账号 → 抽网盘号"),
    ("闲鱼·深采", "xianyu_deep", "items", "闲鱼商品深采(详情页)"),
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


def check_leaked_browsers() -> list[dict]:
    """①b **残留的采集浏览器** —— 它们占住 profile,会让下一轮同平台**撞锁**。

    ⚠️ **为什么必须单独看**(2026-10-05 实测):小红书连挂几天,**根因不是登录过期**
    —— 档案里 `web_session` 到期 **2027-10-05**,登录一直是好的;
    真正的原因是**上一轮超时留下的 10 个 Edge 一直占着那个 profile**
    (`subprocess.run(timeout=)` 只杀直接子进程,而 Edge 是 `main.py` **另起**的),
    于是失败**自我延续**。

    这类东西**不看进程就发现不了**:运行记录里只有一行"xiaohongshu 抓取失败",
    与"要重新扫码"长得**一模一样**,而处置完全相反(一个是清理残留,一个是人工扫码)。

    ⚠️ 判据只看**命令行里带 MediaCrawler 路径的**:机器上还有用户自己的浏览器,
    数错了会得出"采个集开 36 个浏览器"这种离谱结论。
    """
    import sys

    if sys.platform != "win32":
        return [{"name": "采集浏览器残留", "level": GREEN, "detail": "非 Windows,不适用"}]
    try:
        import subprocess

        ps = ("(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              "Where-Object { $_.CommandLine -like '*MediaCrawler*browser_data*' } | "
              "Measure-Object).Count")
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60)
        n = int((p.stdout or b"0").decode("utf-8", "ignore").strip() or 0)
    except Exception as exc:  # noqa: BLE001 - 查不到就说不知道,别报成"有残留"
        return [{"name": "采集浏览器残留", "level": YELLOW,
                 "detail": f"查不到({type(exc).__name__})"}]
    if n == 0:
        return [{"name": "采集浏览器残留", "level": GREEN, "detail": "0 个(干净)"}]
    return [{"name": "采集浏览器残留", "level": YELLOW,
             "detail": f"**{n} 个** MediaCrawler 的 Edge 没退,正占着 profile ⇒ "
                       f"该平台下一轮会**撞锁**。现在采集前会自动清(`mediacrawler_source."
                       f"kill_stale_browsers`),下一轮跑到就会好;若持续不为 0 说明"
                       f"**有进程逃出了清理**"}]


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
    for plat, label in (("weread", "微信读书(Cookie/书架)"),
                        ("zhihu", "知乎(盘链搜索)"),
                        ("xunlei", "迅雷(转存)"),
                        ("quark", "夸克(转存)"),
                        ("baidupan", "百度网盘(转存)"),
                        ("goofish", "闲鱼(采集)"),
                        # ★ 2026-10-08 补:这两家**凭据一直在库里,体检却从来没看过它**。
                        #   旧注释写着"抖音/贴吧走匿名或档案,不在这张表里"——那句**早过期了**。
                        ("douyin", "抖音(线索搜索)"),
                        ("weibo", "微博(名字型热度)"),
                        # ★ 2026-10-09 补:今天实测它的 cookie **已经死了**、我们连撞 14 次
                        #   `-352`,而**没有任何一行报告看得见这件事** —— 是手工查才发现的。
                        #   这条链的失败模式正好是"凭据死了 ⇒ 匿名扫 space ⇒ 必然 -352",
                        #   所以凭据那一行必须单列(而不是等链失败反推)。
                        ("bilibili", "B站(对标号扫描)")):
        at = have.get(plat)
        # ⚠️⚠️ **这一行只验 `shelf`,根本不碰列表接口** —— 而"公众号阅读数"走的正是列表接口
        # (`/web/mp/articles` 或 App `book/articles`)。所以这句说明要**跟着这一行一直挂着**,
        # 不管这个 Cookie 在不在。2026-10-06 实测的假绿:这里报 **🟢「验活 ✓(142 个号)」**
        # 的同时,阅读数**已经断了 8 天**(覆盖率 1%)。**一个绿灯去管它管不着的事,就是假绿**;
        # 用户正是被这一行骗过去的(他来问"为什么不带阅读数了",而报告上这里是绿的)。
        # 处置:标签改名为它**实际验的东西**,并把判阅读数的责任明确推给
        # `check_read_num_coverage`(产出层那条,判据取库里的真值)。
        note = ("(⚠️ 本条**只验书架**,不含列表接口 —— 阅读数看「产出」层的「精确阅读数」)"
                if plat == "weread" else "")
        if at is None:
            # 夸克/百度缺失时转存会降级,不一定是红
            out.append({"name": label, "level": YELLOW if plat in ("quark", "baidupan") else RED,
                        "detail": "库里没有该平台 Cookie" + note})
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
        elif plat in ("douyin", "weibo", "zhihu", "bilibili"):
            # ★ **协议验活**(2026-10-08):三家都做得到 —— 各发一次搜索即可。
            # ⚠️ 但**只有抖音能自动接回**(它有浏览器档案);微博/知乎的 Cookie 是人粘的,
            #    **没有可导的来源** ⇒ 只能报红并说清修法。别给它们假装一条不存在的自动化。
            from app.services.cookie_store import get_cookie

            ck = get_cookie(db, 1, plat) or ""
            alive, why = _cred_alive(plat, ck, db)
            if alive is True:
                detail += f" · 验活 ✓({why})"
            elif alive is False:
                level = RED
                detail = f"**验活失败**:{why}"
                if plat == "douyin" and "过验证" in why:
                    # ★ 这一档**只报红 + 说清修法**,不重导、不复验(理由见下)
                    detail += " · **不自动重导**:实测重导消不掉滑块,而它会解掉验证冷却"
                if plat == "douyin" and "过验证" not in why:
                    # ⚠️ **「要求过验证」这一档不许自动重导**(2026-10-08 实测):
                    # ① 实测补全 jar 消不掉滑块 —— 重导拿回来的还是同一个"未被验证"的会话;
                    # ② 更糟的是,**重导现在会解掉验证冷却**(重导 = "人动过了"的信号),
                    #    于是"失败了→自动重导→解冻→再撞一次"会**自己把闸打开**,
                    #    正是冷却闸要防的那件事。
                    healed = _douyin_reheal(db)
                    alive2, why2 = _cred_alive(plat, get_cookie(db, 1, plat) or "", db)
                    if alive2 is True:
                        # 自动接回 ⇒ **降级为黄**:它已经不挡路了,但值得知道发生过
                        level = YELLOW
                        detail += f" · {healed} ⇒ **复验通过,已自动接回**"
                    elif "过验证" in why2:
                        # ⚠️ 这一档**不许**给"重导 cookie"的修法:重导只是复制 cookie,
                        # **过不了验证** —— 那是要人在浏览器里点一下的。
                        # 给错的修法 = 让下一个人再把同样无效的动作试一遍(本仓最恨的"假装有自动化")。
                        detail += (f" · {healed} ⇒ 复验**仍是「要求过验证」**:"
                                   "要人在浏览器里打开抖音过一次验证;重导 cookie 解决不了")
                    else:
                        detail += f" · {healed} ⇒ 复验仍失败({why2});人工重导见 tools/douyin_export_cookie.py"
                else:
                    detail += (" · ⚠️ 这一家**没有可自动重导的来源**(Cookie 是人粘进"
                               "「Cookie 管理」页的)⇒ **要人工重新复制**")
            else:
                detail += f" · 验活跳过({why})"
        out.append({"name": label, "level": level, "detail": detail + note})
    out.extend(check_xhs_accounts())
    out.append(_xhs_protocol_row(db))
    out.append(_kuaishou_row(db))
    out.append(_weread_app_row())
    return out


#: 体检里 App 凭据那一行的名字
_WEREAD_APP_LABEL = "微信读书 · App 凭据(精确阅读数的唯一来源)"


def _weread_app_row() -> dict:
    """**微信读书 App 凭据单独一行**(2026-10-08 补)。

    ⚠️⚠️ **为什么必须单独一行**:上面那条「微信读书(Cookie/书架)」看的是**网页 Cookie**,
    而**精确阅读数走的是 App 接口** —— 两件事。实测 2026-10-07~10-08 阅读数**整片丢了两天**
    (卡片「阅读数」全是 `—`),而那份体检报告里那一行**一直是 🟢**。
    **指标没覆盖到的地方,绿得再亮也不算数。**

    判据只用**凭据年龄**,不打接口(便宜、也不占额度):
    App token 按 `weread_refresh_cron` 的节奏续,所以
      · 超过 **2 个周期**没续上 ⇒ 🟡(续期没接上 / 一直失败,阅读数会慢慢变 `—`)
      · 超过 **4 个周期** ⇒ 🔴
    """
    from config.settings import get_settings

    settings = get_settings()
    try:
        from app.db import get_session_local

        db = get_session_local()()
    except Exception:  # noqa: BLE001 - 体检本身不该因为读不到库而挂
        return {"name": _WEREAD_APP_LABEL, "level": YELLOW, "detail": "读不到数据库"}
    try:
        from app.services import weread_app_token as wat

        blob = wat.load(db, 1)
    except Exception as exc:  # noqa: BLE001
        return {"name": _WEREAD_APP_LABEL, "level": YELLOW,
                "detail": f"读取失败:{type(exc).__name__}"}
    finally:
        db.close()
    if not blob:
        return {"name": _WEREAD_APP_LABEL, "level": YELLOW,
                "detail": "未配置 —— 阅读数会一直是 `—`(配法:`scripts/weread_app_login.py`)"}
    # 续期周期:★ **用 `job_liveness.trigger_interval_seconds`**(它取的是**最大间隔**),
    # 不自己算"接下来两次之差" —— 那个会**随时辰变**。
    # ⚠️ 实测(2026-10-08):真实 cron 是 `52 3,7,13,19`(四个定点),间隔是 **4/6/6/8 小时**;
    #    我第一版取"接下来两次的差",于是 19 点后算出 8h、凌晨算出 4h
    #    ⇒ **同一个凭据在不同时辰被判成黄/红的标准都不一样**。
    #    这正是 `job_liveness` 那条注释里写过的坑("拿平均间隔当基准会把每天都要发生的
    #    夜间空档算成超期"),那份实现就在本仓,直接复用。
    gap_h = 8.0
    try:
        from apscheduler.triggers.cron import CronTrigger

        from app.services.job_liveness import trigger_interval_seconds

        secs = trigger_interval_seconds(CronTrigger.from_crontab(str(settings.weread_refresh_cron)))
        if secs:
            gap_h = max(0.5, secs / 3600)
    except Exception:  # noqa: BLE001 - 算不出就用默认 8h,不让体检变成错误源
        pass
    try:
        pulled = datetime.fromisoformat(str(blob.get("pulled_at") or ""))
    except ValueError:
        return {"name": _WEREAD_APP_LABEL, "level": YELLOW,
                "detail": "凭据里的 `pulled_at` 读不出来 —— 重跑一次取凭据脚本"}
    age_h = (datetime.now() - pulled).total_seconds() / 3600
    if age_h > gap_h * 4:
        level = RED
    elif age_h > gap_h * 2:
        level = YELLOW
    else:
        level = GREEN
    detail = (f"{age_h:.1f} 小时前续过(续期周期 {gap_h:g}h;超过 "
              f"{gap_h * 2:g}h 就该黄)")
    if level != GREEN:
        detail += (" —— **续期可能没接上**:查 `weread_refresh_tick` 的日志里有没有"
                   "「App 凭据已定时续期」;没有就是没接上或一直失败")
    return {"name": _WEREAD_APP_LABEL, "level": level, "detail": detail}


def check_xhs_accounts() -> list[dict]:
    """**小红书账号档位**:几个、有没有需要人工处理的(过验证 / 重新登录)。

    ⚠️ **为什么单列这一条**(2026-10-07 实测):小红书走**页面渲染**之后,
    它最大的失败模式是"**某个号被踢下线**"——实测 `给号2登录之后号1变成
    「电脑设备登录超限,请重新登录」`(它按**设备/IP**限制同时登录数,与档案隔离无关)。
    而这件事**不会自己好**:得人去重登。
    不主动报的话,表现是"小红书这条链悄悄不产出了",要等到有人问才发现。
    ⇒ 把"账号可用性"做成**每天能看见**的一行,而不是等轮次失败反推。
    """
    try:
        from config.settings import get_settings

        from app.services import xhs_page_source as xp

        profs = xp._profiles(get_settings())
        bad = xp.need_verify_profiles()
        names = [p.name for p in profs]
        need = [p.name for p in profs if str(p) in bad]
        if not names:
            return [{"name": "小红书账号", "level": YELLOW,
                     "detail": "**一个档位都没配**(`XHS_BROWSER_PROFILES`)—— "
                               "这条链不会产出"}]
        if need:
            return [{"name": "小红书账号", "level": RED,
                     "detail": f"{len(need)}/{len(names)} 个号**需要人工处理**"
                               f"(被踢下线或要过安全验证):{'、'.join(need)} —— "
                               f"修法:`python tools/xhs_pass_verify.py <档案目录>`"}]
        return [{"name": "小红书账号", "level": GREEN,
                 "detail": f"{len(names)} 个号({','.join(names)}),没有待处理的"}]
    except Exception as exc:  # noqa: BLE001 - 体检自己挂了要说出来,别静默
        logger.exception("小红书账号体检失败")
        return [{"name": "小红书账号", "level": YELLOW,
                 "detail": f"检查失败:{type(exc).__name__}: {str(exc)[:100]}"}]


#: 协议凭据那一行的名字
_XHS_PROTO_LABEL = "小红书 · 协议凭据(纯协议采集用的那份)"


def _xhs_reheal(db) -> str:
    """小红书凭据失效时,**从浏览器档案自动重导**一次。返回一句人话。"""
    try:
        from app.services.xhs_cookie_export import export_from_profile

        return f"已从档案自动重导({export_from_profile(db, user_id=1)})"
    except Exception as exc:  # noqa: BLE001 - 自动补救失败不该让体检本身挂掉
        logger.warning("小红书凭据自动重导失败", exc_info=True)
        return f"自动重导失败:{type(exc).__name__}"


def _xhs_protocol_row(db) -> dict:
    """小红书**协议凭据**验活 + 失效自动重导(2026-10-08 补)。

    ⚠️ 与上面那行「小红书账号」**不是一回事**:那行看的是**页面渲染的档位**
    (几个号、哪个要过验证),这行看的是**纯协议采集用的那份凭据**还活不活。

    ★ **按失败类型分流 —— 这是拿真账号换来的教训**(2026-10-08 压测):
      · `need_login`(被踢/凭据掉)⇒ **自动重导**能修,重导后**复验**;
      · `restricted`(-104 账号被限制)⇒ **重导没有任何用**(凭据能认证,是账号没权限),
        而且**反复重试/重登只会延长封锁** ⇒ 只报红、写明"**停手等解封**"。
    把这两件混成一句"小红书挂了",下一个人就会去重登 —— 而那正是最不该做的事。
    """
    from app.services import xhs_protocol_source as xp

    try:
        rows = xp.search(["网盘资源"], session=db)
        if rows:
            return {"name": _XHS_PROTO_LABEL, "level": GREEN,
                    "detail": f"验活 ✓(搜到 {len(rows)} 条)"}
        # ⚠️ 零结果**不判绿也不判红** —— 与"被风控成空"形状相同,不硬下结论
        return {"name": _XHS_PROTO_LABEL, "level": YELLOW,
                "detail": "协议通但零结果(不作判定)"}
    except Exception as exc:  # noqa: BLE001
        kind = str(getattr(exc, "kind", "?") or "?")
        if kind == "need_login":
            healed = _xhs_reheal(db)
            try:
                again = xp.search(["网盘资源"], session=db)
            except Exception as exc2:  # noqa: BLE001
                return {"name": _XHS_PROTO_LABEL, "level": RED,
                        "detail": f"凭据失效:{str(exc)[:90]} · {healed} ⇒ "
                                  f"复验仍失败({str(exc2)[:70]})"}
            if again:
                return {"name": _XHS_PROTO_LABEL, "level": YELLOW,
                        "detail": f"凭据曾失效,**{healed} ⇒ 复验通过,已自动接回**"
                                  f"(搜到 {len(again)} 条)"}
            return {"name": _XHS_PROTO_LABEL, "level": RED,
                    "detail": f"{healed} ⇒ 复验仍零结果"}
        if kind == "restricted":
            # ⚠️ 异常文案里已经写了"停手等解封",**别再拼一遍**(重复的告警会让人跳读)。
            #    这行只需补上**异常里没有**的那条信息:**重导没用**。
            head = str(exc).split("——")[0].strip()
            return {"name": _XHS_PROTO_LABEL, "level": RED,
                    "detail": f"**账号级限制**:{head[:110]} —— ⚠️ **重导凭据通常没用**"
                              f"(凭据能认证,是账号没权限),**重登/重试也不该反复做** ⇒ 先停手等解封。⚠️ **2026-10-09 订正**:原来写的是「重导**没有任何用**」「重登/重试**只会延长**封锁」—— 那两句**我没有控制变量**就写死了;当天实测「解封后**重导 + 打一枪**就通了」(协议返回 16 条),而「解封」与「重导」这两个变量**我分不开** ⇒ 说成绝对话是**过度归因**。只保留站得住的那部分:重导**本身**过不了一道**还在生效**的账号级限制"}
        return {"name": _XHS_PROTO_LABEL, "level": YELLOW,
                "detail": f"验活没做成({kind}):{str(exc)[:100]}"}


def _kuaishou_row(db) -> dict:
    """快手**专属体检行**(2026-10-08 补)。

    以前它的失败只埋在「名字型热度」那行的 `失败:kuaishou` 里 —— **一眼扫过去看不见**,
    而这些埋着的失败正是本仓反复栽的那一类。

    ⚠️ **为什么快手做不了协议验活**:它走 MediaCrawler 浏览器,而"验活"就等于
    **跑一轮采集**(开浏览器、几十秒)—— 不适合塞进每天的体检。
    所以这行的判据是**最近一轮 `resource_presence` 的结果**,不是现发探针。

    ⚠️ **也别指望能自动接回**:快手的登录态在 MediaCrawler 的浏览器档案里
    (`cdp_ks_user_data_dir`),而我们**没有存一份到加密库**(没有协议路要它)⇒
    失效了只能**人扫码重登**。这行能做的就是把"该去重登了"摆到眼前。
    """
    from sqlalchemy import select

    from app.db.models import RunRecord

    name = "快手(跨平台热度)"
    try:
        row = db.scalars(select(RunRecord).where(RunRecord.kind == "resource_presence")
                         .order_by(RunRecord.started_at.desc()).limit(1)).first()
    except Exception as exc:  # noqa: BLE001 - 体检自己不该因为读不到而挂
        return {"name": name, "level": YELLOW, "detail": f"读不到运行记录:{type(exc).__name__}"}
    if row is None:
        return {"name": name, "level": YELLOW, "detail": "还没有跑过跨平台热度那一轮"}
    detail = str(row.detail or "")
    failed = [x.strip() for x in detail.split("失败:")[1:]] if "失败:" in detail else []
    # ⚠️ 失败记录的 detail 里可能带着**整段异常与 SQL 语句** —— 只取第一行,
    #    否则体检报告会被一坨 SQL 撑爆,人就不看了(那是告警变噪音的第一步)。
    one_line = detail.splitlines()[0] if detail else ""
    head = f"最近一轮 {str(row.started_at)[5:16]} [{row.status}] {one_line[:70]}"
    if any("kuaishou" in f for f in failed):
        return {"name": name, "level": RED,
                "detail": f"{head} —— ⚠️ **快手这一轮被抓取失败**,多半是登录态失效。"
                          f"要**人扫码重登**(`cdp_ks_user_data_dir`,MediaCrawler 那条链),"
                          f"快手没有可自动重导的凭据来源"}
    if row.status != "success":
        return {"name": name, "level": YELLOW, "detail": head}
    return {"name": name, "level": GREEN,
            "detail": head + (f"(其他平台失败:{','.join(failed)})" if failed else "")}


def _next_renewal(settings) -> datetime | None:
    """下一次微信读书续期的时间(从 `weread_refresh_cron` 算)。算不出返回 None。"""
    try:
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(str(settings.weread_refresh_cron)).get_next_fire_time(
            None, datetime.now()).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - 算不出就当不知道
        return None


#: 抖音凭据里**该有**的风控/设备项(缺了不一定立刻坏,但**该重导一次**)。
#: ⚠️ 这个表不是"必需项",是"**完整性探针**":2026-10-08 实测我们的 jar 比浏览器档案
#: **少 4 项**(`__ac_nonce` / `x_tt_token` / `is_dbsc` + 一个空名),其中
#: **`__ac_nonce` 与 `__ac_signature` 是一次会话生成的一对** —— 而我们发了签名却没有配对的 nonce。
#: 补全后**滑块依旧**(见 `doc/抖音纯协议-链路拆解.md` §5.x)⇒ **它不解释这次的症状**,
#: 所以只做**提示**,不判红(给一个解释不了的项判红,就是造一个假的红灯)。
_DOUYIN_RISK_KEYS = ("s_v_web_id", "ttwid", "sessionid", "__ac_nonce", "x_tt_token", "UIFID")


def _douyin_jar_note(cookie: str) -> str:
    """一句话说明抖音 jar 与「该有的项」差在哪。**只提示,不影响判级。**"""
    if not cookie:
        return "jar 空"
    from app.services.douyin_protocol_source import _cookie_dict

    ck = _cookie_dict(cookie)
    miss = [k for k in _DOUYIN_RISK_KEYS if k not in ck]
    if not miss:
        return f"jar {len(ck)} 项(风控项齐全)"
    return (f"jar {len(ck)} 项,**缺 {'/'.join(miss)}** ⇒ 该重导一次"
            f"(重导只补档案里现有的项,补不上就只能人去过验证)")


def _cred_alive(platform: str, cookie: str, db) -> tuple[bool | None, str]:
    """抖音 / 微博 / 知乎 的凭据**验活**(2026-10-08 补)。

    ## 为什么这三家非加不可
    它们的 Cookie 都是**人工粘进「Cookie 管理」页**的(没有浏览器档案来源),
    而体检此前**连它们这一行都没有** —— 那行注释还写着"抖音/贴吧走匿名或档案,
    不在这张表里",**那句早就过期了**(抖音的凭据 10-07 已进加密库,微博的 10-05 就在)。
    ⇒ **凭据在库里,体检却不看它:死了没人知道。** 这正是本仓最恨的那种假绿。

    ## ⚠️ 只能"验活",做不到"自动续期"
    这三家是**长期登录态**(`sessionid` 这类),服务端**没有轮换接口** ——
    与迅雷 `refresh_token`、微信读书 `wr_skey` 那种"短期令牌"**不是一类**。
    所以它们的自动化上限就是:**① 能发现死了 ② 告警里把修法说清**。
    1. 抖音额外还能**自动重导**(它有浏览器档案,见 `_douyin_reheal`);微博/知乎**没有**,
       只能提示人去复制(别给它们假装一条不存在自动化 —— 那是骗下一个人)。

    判据一律看**协议返回值**,不看"多久前写进去的":本仓在微信读书那条上已经吃过
    "只看更新时间的假绿"。

    返回 `(True/False/None, 说明)`;`None` = 验不了(网络抖动等),**别当成活着**。
    """
    try:
        if platform == "douyin":
            from app.services import douyin_protocol_source as dps

            rows = dps.search(["网盘资源"], session=db)
            if rows:
                return True, f"搜到 {len(rows)} 条 · {_douyin_jar_note(cookie)}"
            # ⚠️ **"零结果"不能判成绿,也不能判成红** —— 本仓实测过抖音会回
            # `status_code=0 + data:[]`(连发被限流时,与"真没结果"形状**完全一样**)。
            # 但**"没抛 `need_login`"这件事本身是有信息的**:抖音对未登录**一律**回 2483,
            # 它没出现 ⇒ 凭据**被接受**了。所以这里如实说清"证明了什么、没证明什么",
            # 而不是含混地报绿(那正是本仓最恨的假绿)。
            return None, ("2483「请先登录」未出现 ⇒ 凭据**被接受**;但本次零结果"
                          "(限流与真没结果形状相同),**不作判定**")
        if platform == "bilibili":
            # B站 `space` 端点**风控比普通搜索严得多**:登录态失效后匿名去扫
            # **直接回 `-352 风控校验失败`**(本模块与该文件文档都记着这条实测)。
            # ⇒ 判据用 `nav` 接口的 `data.isLogin`(**B站失败也回 HTTP 200**,按 HTTP 判是假绿)。
            from app.services import bili_account_scan as bas

            st = bas.verify_login(cookie)
            if st.get("is_login"):
                return True, f"已登录({st.get('uname') or st.get('mid')})"
            return False, (f"**不是登录态**({st.get('reason') or 'SESSDATA 未被接受'})—— "
                           f"此后每一轮扫描都会匿名去撞 `space` 端点、**必然 -352**、零产出;"
                           f"修法:`python scripts/bili_login.py` 扫码重登")
        if platform == "weibo":
            from app.services import weibo_search

            hits = weibo_search.search(["网盘资源"], session=db)
            return (True, f"搜到 {len(hits)} 条") if hits else (None, "协议通但零结果")
        if platform == "zhihu":
            from app.services.cross_accounts import _search_zhihu

            hits = _search_zhihu(cookie, "网盘资源", limit=5)
            return (True, f"搜到 {len(hits)} 条") if hits else (None, "协议通但零结果")
    except Exception as exc:  # noqa: BLE001
        # ⚠️ **登录态类错误要说出来,网络类错误别喊"Cookie 失效"** ——
        # 两者修法完全不同:前者要人重新复制,后者等一会儿就好。
        kind = str(getattr(exc, "kind", "") or "")
        msg = f"{type(exc).__name__}: {str(exc)[:90]}"
        if kind == "verify":
            # ★ 2026-10-08 新增这一档:抖音可以把搜索结果**整页换成验证页**
            # (`search_nil_info.search_nil_type=verify_check`),而它在响应里是明说的。
            # 在此之前这层只看 status_code/data ⇒ 报成"零结果,不作判定",
            # 于是报告上既不红也不黄,而实际上**整条链是断的**(实测连空 8 小时)。
            return False, ("**抖音要求过验证**(不是凭据失效、也不是限流):"
                           "要在浏览器里打开抖音**过一次验证**(滑块/验证码)。"
                           "⚠️ 措辞要准:重导 cookie **本身过不了验证**(它只是复制登录态),"
                           "所以自动化在这里到顶了 —— 但**先让自动重导试一次**是值得的"
                           "(换一个会话有可能不再被拦)。在此之前每个关键词都返回空。" + msg)
        if kind in ("need_login", "restricted", "argus") or "登录" in str(exc):
            return False, msg
        return None, msg
    return None, "没有这一家的探针"


def _douyin_reheal(db) -> str:
    """抖音凭据失效时,**从浏览器档案自动重导**一次。返回结果说明(空串=没做)。

    ⚠️ **这是这三家里唯一能做到"自动接回"的** —— 因为抖音的登录态活在
    MediaCrawler 的 Chrome 档案里(`cdp_dy_user_data_dir`),档案只要还登着,
    重新导一次就能接上,**不用人去点**。微博/知乎没有档案来源,做不到。
    """
    try:
        from app.services.douyin_cookie_export import export_from_profile

        out = export_from_profile(db, user_id=1)
        return f"已从浏览器档案自动重导({out})" if out else "档案里也没读到有效凭据"
    except Exception as exc:  # noqa: BLE001 - 自动补救失败不该让体检本身挂掉
        logger.warning("抖音凭据自动重导失败", exc_info=True)
        return f"自动重导失败:{type(exc).__name__}"


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


#: 「这个源的失效**等多久都不会自己好**」—— 只有这类才判 🔴。判据宁可漏、不可假红:
#: 假红会训练人忽略整份报告(2026-10-05 我给微信读书误报 🔴 那次就是这么栽的)。
_AUTH_MARKERS = ("200003", "会话失效", "重新扫码", "invalid session", "登录态失效",
                 "授权失效", "token 失效", "重新登录", "凭据失效")
#: **自愈类**(限流/网络/超时)—— 报红就是假红,一律 🟡。
_SELFHEAL_MARKERS = ("200013", "频率限制", "限流", "超时", "timeout", "connection",
                     "ssl", "proxy", "代理")


def _classify_source_error(exc: BaseException) -> tuple[str, str]:
    """源报错归成 `("auth"|"selfheal"|"unknown", 原因文本)`。

    `auth` = **凭据死了、等多久都不会自己好** ⇒ 要人动手,判 🔴;
    其余(限流/网络/超时/认不出)= **自愈或未知** ⇒ 判 🟡 —— **不能报红**。
    """
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    for m in _AUTH_MARKERS:
        if m.lower() in low:
            return "auth", text
    for m in _SELFHEAL_MARKERS:
        if m.lower() in low:
            return "selfheal", text
    return "unknown", text


def check_list_sources(db, settings=None) -> list[dict]:
    """④ 公众号**列表源逐个验活** —— 光看"配了没"是假绿(2026-10-06 补)。

    ⚠️ 与 10-05 给微信读书补验活是**同一个病**:`MultiSourceClient` 把某个源的失效
    当成"换下一个源"的 WARNING 吞掉,链路照跑 ⇒ **降级是静默的**。
    实测代价:`wemp`(自研兜底,本该是"WeRSS 挂了"的保险)自 **2026-10-02 14:00** 起
    每轮报 `200003 会话失效`,**连着 4 天没人知道**;而每天 09:30 推管理群的那份体检报告上,
    **它连一行都没有**。保险失效而无人察觉,比没有保险更糟 —— 人会以为自己有兜底。

    ⚠️ **被动记日志救不了它**:WeRSS 答上了的号,`MultiSourceClient` **命中即返回**,
    `wemp` 根本不会被调用 —— 它的死在日志里**也不会出现**。所以只能**主动**拿一个真号
    逐个去问(每源一次请求,日级频率可忽略)。

    ⚠️ 本函数**只读**(GET 拉一页列表),且**绝不抛**:体检里一个探针崩掉不该毁掉整份报告
    (与 `_weread_alive` 同一条纪律)。
    """
    from sqlalchemy import select

    from app.db.models import WechatBenchmark
    from config.settings import get_settings

    settings = settings or get_settings()
    try:
        from app.services import wechat_monitor as _wm   # 走门面:子模块直 import 会循环导入

        backends = _wm.configured_backends(settings, session=db, user_id=1)
    except Exception as exc:  # noqa: BLE001 - 体检不吃异常
        return [{"name": "公众号·列表源", "level": YELLOW,
                 "detail": f"取不到源清单({type(exc).__name__}: {str(exc)[:80]}),本轮跳过验活"}]
    if not backends:
        return [{"name": "公众号·列表源", "level": RED,
                 "detail": "**一个列表源都没配**(WeRSS / wemp 凭据 / 读书平台全缺) —— "
                           "公众号采集没有任何免费全量列表来源"}]

    # 验活要拿**真号**去问:没有对标号就没法验,明说,别给假绿。
    bm = db.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == 1, WechatBenchmark.biz != "").limit(1))
    feed = _wm.feed_biz(bm) if bm is not None else ""
    if not feed:
        return [{"name": f"公众号·列表源({len(backends)} 个已配)", "level": YELLOW,
                 "detail": "库里没有可试的对标号(biz 不是 MP_WXS_* 形态),本轮**跳过**验活 —— "
                           "**跳过不等于通过**,别把这行当绿灯"}]

    out: list[dict] = []
    alive = 0
    for name, be in backends:
        try:
            items = be.mp_articles(feed, page=1, limit=1)
        except Exception as exc:  # noqa: BLE001 - 一个源失败不能毁掉整份报告
            kind, why = _classify_source_error(exc)
            if kind == "auth":
                out.append({"name": f"公众号·列表源·{name}", "level": RED,
                            "detail": f"**凭据已失效,等多久都不会自己好** —— {why[:110]}。"
                                      f"修法:浏览器登录 mp.weixin.qq.com → F12 复制整条 Cookie + "
                                      f"地址栏里的 token → `python scripts/wemp_cred.py "
                                      f'--cookie "..." --token "..."`'})
            else:
                out.append({"name": f"公众号·列表源·{name}", "level": YELLOW,
                            "detail": f"失败,但**看起来会自愈**(限流/网络):{why[:110]} —— "
                                      f"不报红:假红会让人忽略整份报告"})
        else:
            alive += 1
            # ⚠️ 「答了但是 0 条」**算活** —— 不能判黄:WeRSS 空转时每轮都是 0 条(它至今
            #    一篇文章没抓到过),那会变成**恒黄**,而恒黄的项等于没有。
            out.append({"name": f"公众号·列表源·{name}", "level": GREEN,
                        "detail": f"验活 ✓(问「{bm.nickname}」答了 {len(items)} 条;"
                                  f"0 条也可能是该源没收录这个号,不代表它坏了)"})
    # ★ **单点风险单独说**:只剩一个能答的源时降级链实际没有备胎,而"还有源在答"会让
    #    所有产出指标照常绿 —— 只有这一行看得出来。
    if alive <= 1:
        out.append({"name": "公众号·列表源兜底", "level": RED if alive == 0 else YELLOW,
                    "detail": f"**能答的源只剩 {alive} 个** ⇒ `MultiSourceClient` 的降级链"
                              f"实际上没有备胎。" + (
                                  "链路已经完全取不到列表了。" if alive == 0 else
                                  "挂掉的那个源**不影响产出指标**(它答不上时会被静默跳过),"
                                  "所以只能靠这一行看出来。")})
    return out


_PARTIAL_FAIL_RE = re.compile(r"失败[:=]\s*([^\s;；,。]+)")


def _partial_failures(detail: str) -> list[str]:
    """从 detail 里读「**点名了失败的东西**」→ 失败名单(空列表 = 没有)。

    ⚠️ 必须是**带分隔符**的 `失败:xxx` / `失败=xxx`,**不能**匹配 `失败0` ——
    后者是"失败**计数**为 0"(xunlei_group 的 detail 里就有 `失败0`),
    把它读成"有失败"会把一切判红。
    """
    out: list[str] = []
    for m in _PARTIAL_FAIL_RE.finditer(str(detail or "")):
        name = m.group(1).strip()
        if name and not name.isdigit():
            out.append(name)
    return out


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
            # ⚠️ 分隔符**可选**:仓库里的 detail 既有 `new=1` 也有 `成功0` / `标题0条`
            # (2026-10-08 补三条链时发现 —— 只认 `=` 的话,那几条的指标**永远解析不出来**,
            #  而"解析不出"会落到 else 分支 ⇒ **默认判绿**,等于加了个瞎指标)。
            for r in rows:
                m = re.search(rf"(?:^|\s){re.escape(metric)}[=:：]?\s*(\d+)", str(r.detail or ""))
                if m:
                    val = int(m.group(1))
                    break
        stale_h = age_h > 48
        if last.status not in ("success", "partial"):
            lvl = RED
        elif stale_h:
            lvl = YELLOW
        elif metric and val == 0 and all(
                re.search(rf"(?:^|\s){re.escape(metric)}[=:：]?\s*0", str(r.detail or ""))
                for r in rows):
            lvl = YELLOW          # 连着几次都是 0
        elif _partial_failures(str(last.detail or "")):
            # ⚠️⚠️ **部分失败不许被产出数淹没**(2026-10-08):`resource_presence` 的 detail 是
            # `平台2 命中12 失败:xiaohongshu` —— 命中 12 > 0 ⇒ 原来判**绿**,
            # 而小红书已经**连挂 5 轮**。判据是"有没有点名失败",不是"总数是不是 0"。
            chronic = all(_partial_failures(str(r.detail or "")) for r in rows)
            lvl = RED if chronic else YELLOW
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


def check_job_liveness(db) -> list[dict]:
    """⑤ **作业落实性**:注册的作业 vs 真实心跳 —— "该跑没跑"要自己说出来(2026-10-07)。

    ⚠️ **为什么必须加这一条**:2026-10-07 实测,公众号监听轮**从 10-06 20:02 起 30 小时
    一轮没跑**(应用全程在线、别的作业照跑),而**没有任何一处告警** ——
    唯一的守卫 `scripts/job_liveness.py` ①**只有人手动跑** ②它把
    `hour='4,8,14,20'` 这种多定点判成"算不出间隔"直接放过,**监听作业恰好被排除在外**。
    代价是"阅读数又断了"要靠用户来问。⇒ 判活逻辑已提到 `app.services.job_liveness`
    (服务层,脚本只能是薄壳),并且**接进每天这份体检**。

    分级:`从没执行过` 报红(注册了就该跑);`间隔远超预期` 超过期望 6 倍报红,否则黄
    (留出"晚一轮"的余地 —— 告警变噪音就没人看了)。
    """
    from app.services.job_liveness import STALE_FACTOR, audit

    try:
        r = audit(db)
    except Exception as exc:  # noqa: BLE001 - 对账自己挂了也要说出来,别让整份体检断掉
        logger.exception("作业落实性对账失败")
        return [{"name": "作业落实性", "level": YELLOW,
                 "detail": f"对账本身失败:{type(exc).__name__}: {str(exc)[:120]}"}]

    n_jobs = len(r["jobs"])
    if r["baseline"] is None:
        # 心跳机制刚上线/新库:没有基线时"没记录"分不出真假,别报红(那会是一次假问题)
        return [{"name": "作业落实性", "level": YELLOW,
                 "detail": f"共 {n_jobs} 个作业注册;**心跳表还没有基线**(机制刚上线或新库),"
                           f"本次不判'从没跑过'"}]

    # **最慢的几个作业** —— 排期错峰时要的是"谁真的占着库",不是"谁看着重"。
    # ⚠️ 这条在 2026-10-07 加时长采集之前**根本量不出来**(全仓没有作业时长,
    # 见 `RunRecord.finished_at` 的说明),当时只能靠时刻聚类猜。
    slow = sorted(((b.last_duration_ms, jid) for jid, b in r["beats"].items()
                   if getattr(b, "last_duration_ms", None)),
                  reverse=True)[:3]
    slow_txt = ("" if not slow else
                ";最慢:" + "、".join(f"{jid} {ms / 1000:.1f}s" for ms, jid in slow))

    if not r["never"] and not r["stale"]:
        return [{"name": "作业落实性", "level": GREEN,
                 "detail": f"共 {n_jobs} 个作业,全部有心跳且间隔正常{slow_txt}"}]

    items: list[dict] = []
    if slow_txt:
        items.append({"name": "作业落实性", "level": GREEN, "detail": slow_txt.lstrip(";")})
    if r["never"]:
        items.append({"name": "作业落实性", "level": RED,
                      "detail": f"**注册了却从没执行过**({len(r['never'])} 个):"
                                f"{'、'.join(r['never'])} —— 注册成功 ≠ 跑过,"
                                f"查 trigger / 是否被角色挡下 / 是否每次都提前 return"})
    for s in r["stale"]:
        over_h = s["over_s"] / 3600
        expect_h = s["expect_s"] / 3600
        level = RED if s["over_s"] > s["expect_s"] * STALE_FACTOR * 2 else YELLOW
        items.append({"name": "作业落实性", "level": level,
                      "detail": f"**{s['job_id']}** 已 {over_h:.1f}h 没跑"
                                f"(期望最大空档 {expect_h:.1f}h,超 {STALE_FACTOR}× 即报);"
                                f"上次 {s['last_run_at']:%m-%d %H:%M}"})
    return items


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
    broken = ("查两条路:① 网页 `/web/mp/articles` —— 账号级 `-2041` 时**换新会话也没用**"
              "(2026-10-06 实测:刚续期、验证通过,第 1 个号就被挡);"
              "② App `i.weread.qq.com/book/articles` —— token 过期时「重取」拿回来的是**同一个值**"
              "(模拟器里微信读书登录态失效:在雷电里打开 / 重新登录微信读书 App,"
              "或跑 `scripts/weread_app_login.py` 重取)。")
    # ⚠️⚠️ **"几乎全丢"必须按红灯报**(2026-10-06 修)。此前只有"**恰好一篇都没有**"才判红,
    # 于是 163 篇里有 2 篇侥幸漏进来就掉到 🟡「偏低」—— 而 🟡 是最容易被忽略的那一档。
    # **实测代价**:阅读数从 09-28 起实际上全断,报告上一直写着"🟡 偏低",**8 天没人发现**,
    # 直到用户自己来问"为什么不带阅读数了"。**一个会长期挂在黄档的指标,等于没有指标。**
    # 5% 这条线的依据:轮转窗口正常时覆盖率是三成以上(窗口外的号本来就该显示"—"),
    # 低到 5% 以下时,"还能侥幸漏进来两篇"与"全断"在处置上无从区分。
    if ratio < 0.05:
        return [{"name": "公众号·精确阅读数", "level": RED,
                 "detail": f"{detail}(覆盖率 {ratio:.0%})⇒ **精确阅读数实质上全丢**。{broken}"}]
    if ratio < 0.2:
        return [{"name": "公众号·精确阅读数", "level": YELLOW,
                 "detail": detail + f"(覆盖率 {ratio:.0%},偏低 —— 通常说明列表额度/兜底有问题。" + broken + ")"}]
    return [{"name": "公众号·精确阅读数", "level": GREEN,
             "detail": detail + f"(覆盖率 {ratio:.0%})"}]


def check_weread_list_rotation(db, rounds: int = 3) -> list[dict]:
    """④ **阅读数列表轮转** —— 读运行记录里的**机制指纹**,不看覆盖率。

    ⚠️⚠️ **为什么必须单列这一条**(2026-10-08):`check_read_num_coverage` 判的是
    **近 3 天的覆盖率**(红线 5%)。可**单日全断**时覆盖率还有约 **31%**
    —— 前两天的好数据在稀释它 ⇒ 最多报黄,要衰减到 5% 得**三天**。
    而这类事故(轮转窗口被"没有 bookId 的号"占满 ⇒ **两条路一次都没被调用过**)
    **恰恰是「当天全断、三天后才红」** —— 那个检测窗**结构上就抓不到它**。

    所以这里换判据:**不看产出,看机制**。运行记录里那四个数就是机制本身:

        weread_list(ok=0 app=0 off=0 off_with_new=1 skipped=79)

    | 形状 | 含义 | 处置 |
    |---|---|---|
    | `ok`/`app` 全 0、`skipped` 满员、`off=0` | **谁都没问过** ⇒ **轮转窗口坏了** | 查 `_list_window` 及其入参 |
    | `ok`/`app` 全 0、`off>0` | 两条路**被额度/风控挡掉** | 外因:等 / 换会话,别改代码 |
    | `ok`/`app` 有非 0 | 至少一条路在跑 | 绿(拿不到读数另有原因,看覆盖率那条) |

    ⇒ **一轮就能判**,不必等三天。判据是「**调用过没有**」,不是「调用成没成」。
    """
    import re

    rows = _rows(db, "wechat_listen", max(rounds, 5))
    parsed: list[tuple[int, int, int, int]] = []
    for r in rows:
        m = re.search(r"weread_list\(ok=(\d+) app=(\d+) off=(\d+)[^)]*skipped=(\d+)\)",
                      str(r.detail or ""))
        if m:
            parsed.append(tuple(int(g) for g in m.groups()))   # type: ignore[arg-type]
    name = "公众号·阅读数列表轮转"
    if not rows:
        return [{"name": name, "level": YELLOW, "detail": "最近没有监听轮,无从判断"}]
    if not parsed:
        return [{"name": name, "level": YELLOW,
                 "detail": f"最近 {len(rows)} 轮记录里都没有 `weread_list(...)` 记账"
                           f"(格式变了?那就等于这个指标瞎了)"}]
    ok, app, off, skipped = parsed[0]
    asked = ok + app
    shape = f"最近一轮:网页问成 {ok} / App 问成 {app} / 被挡 {off} / 窗口外 {skipped}"
    if asked:
        return [{"name": name, "level": GREEN, "detail": shape + " —— 至少一条路在跑"}]
    if off:
        return [{"name": name, "level": RED,
                 "detail": f"{shape} ⇒ **两条路都被额度/风控挡掉了**"
                           f"(与「窗口坏了」不是一回事:这是外因,该等或换会话,别改代码)。"
                           f"看 `weread_budget` 的熔断记录"}]
    return [{"name": name, "level": RED,
             "detail": f"{shape} ⇒ **谁都没问过**(ok/app/off 全 0 而窗口外满员)"
                       f"⇒ **轮转窗口坏了**。2026-10-08 那次就是这个形状:"
                       f"`_list_window` 的窗口被**没有 bookId 的号**占满(它们不可能被问列表),"
                       f"于是真有 bookId 的号**恒在窗口外**、两条路一次都没被调用。"
                       f"查 `_listen._list_window` 与 `_list_key`"}]


def collect_sections(db) -> list[tuple[str, list[dict]]]:
    """跑完三层检查,返回 [(小标题, 结果列表)]。**只读**。调用方负责渲染/推送。

    ⚠️ ② 凭证层**不只是列 Cookie**,还有 `check_list_sources`(逐个列表源验活)——
    10-02 起 `wemp` 失效 4 天、报告上却一行都没有,就是缺了这一段。
    """
    return [("依赖(容器/库/venv/档案)", check_dependencies() + check_leaked_browsers()),
            ("凭证(Cookie 在不在 / 源活不活)", check_credentials(db) + check_list_sources(db)),
            ("产出(最近几次真跑出来的东西)",
             check_chains(db) + check_read_num_coverage(db)
             + check_weread_list_rotation(db)),
            # ⑤ **"该跑没跑"** —— 与"跑了但没产出"是两个层次的事,分开报(2026-10-07)。
            ("作业落实性(注册的 vs 真跑的)", check_job_liveness(db))]


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
