"""夸克**口令(U口令)** → 搬进我们盘 → 建我方分享链(2026-10-06)。

## 为什么走 UI 而不是接口
解析接口是**带 `sign` 签名的 JSON**(端点 `utoken2.quark.cn/utoken/v2/parse` 已找到、
参数也读出来了,但签名算法没解出),抓包又被**证书固定**挡住。
⇒ 让 App 自己去解析,我们只读结果。**全部踩坑见 `scripts/quark_kouling_ui.py` 的 docstring。**

## 实测走通的链路
```
抖音线索标题(原样,不用提取口令 —— App 会做模糊匹配)
  → Set-Clipboard(Unicode) → 激活雷电窗口 → 夸克自动识别
  → 卡片显示资源名 → 点「立即查看」→「保存」→ 文件进我们夸克盘 `来自：分享/<资源名>`
  → 本模块用**已有 API**(QuarkTransfer.list_dir / share_fids)找到它并建我方分享链
```

⚠️ 实测两个**改写过的**抖音标题都能解:
`咐置铸剑上供叩苓` → 「铸剑纳贡（ForgeTax）」;`咐置人生指南得叩苓｜…` → 「高性价比人生指南…」。
**所以不需要"提取口令"这一步** —— 这是本模块能自动化的关键。

## 待办队列就复用现成字段
`DouyinLead.our_url`(它当初就是为"这个口令到底搬没搬成"加的)。
空 = 还没搬。**不新建表**。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import select

from app.utils import get_logger
from app.services.chain_delivery import mark_newly_moved

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
# ⚠️ **故意放在 `tools/`(gitignored)** —— 那份脚本就是"怎么用模拟器把夸克口令变成我方链"
# 的**全部 know-how**,与迅雷的 sniff/captcha 脚本同一处。**不进仓库**:
# 别人 clone 下来也拿不到这条路(用户口径:「**不想被别人拿来就能用**」)。
UI_SCRIPT = ROOT / "tools" / "quark_kouling_ui.py"
# 一个口令 15–20 秒,但模拟器冷启动/弹窗卡住时要留足;超时**必须当失败**,不能假装成功
_UI_TIMEOUT = 240
_SAVE_DIR = "来自：分享"          # App 保存分享文件的默认目录
_SAVE_DIR_FID_KEY = "quark_share_save_dir_fid"   # 该目录的 fid 缓存(见 _find_saved_fid)
# "刚刚新增"的时间窗(毫秒):只认 5 分钟内更新过的,避免误取别人的文件
_RECENT_SAVE_MS = 5 * 60 * 1000
# **夸克口令的形态标记**(2026-10-06 实测得到):抖音上的规避写法是「复制…口令」的形近替换,
# 我们见过的样本长 `咐置<资源名>叩苓`;App 自己生成的那段还带 `/~<token>~/`。
# ⚠️ **这只是"优先"不是"判据"** —— 不同推广号会换替换字,所以漏网的要靠"试过就不再试"兜住,
# 而不是靠这个正则判死。
_QUARK_HINTS = ("咐置", "叩苓", "/~")


_RE_TOKEN = re.compile(r"/~[A-Za-z0-9]{6,16}~")
_RE_KOU = re.compile(r"咐置[^，。｜#\s]{1,14}叩苓")


def mark_of(text: str) -> str:
    """从抖音标题里取**夸克口令标记**;取不到返回空串。

    两种形态都是**实测**来的(2026-10-06):
      · `/~xxxx~:/` —— App 拼分享文案时写的令牌,**最可靠**(不是人手打的);
      · `咐置…叩苓` —— 推广号的规避写法(`咐置`≈复制、`叩苓`≈口令)；
    取到标记 ⇒ 这条帖子值得交给夸克 App 去解(它能做模糊匹配)。
    """
    s = str(text or "")
    m = _RE_TOKEN.search(s)
    if m:
        return m.group(0)
    m = _RE_KOU.search(s)
    return m.group(0) if m else ""


def _looks_like_quark(title: str) -> bool:
    return any(h in str(title or "") for h in _QUARK_HINTS)


def resolve(text: str, *, save: bool = True, timeout: int = _UI_TIMEOUT) -> dict:
    """跑一遍 UI 流程。返回 `{"ok","title","saved"}`;失败也是结构化返回,不抛。

    ⚠️ **子进程超时必须当失败**(返回 `ok=False`),绝不能让"卡住了"读成"没这条口令"。
    """
    if not str(text or "").strip():
        return {"ok": False, "reason": "空文本"}
    if not UI_SCRIPT.exists():      # 换机器/新克隆时会走到这 —— 说清楚,别让人对着 failed 猜
        return {"ok": False, "reason": f"本机没有 {UI_SCRIPT.name}(它是 gitignored 的本地脚本,不随仓库走)"}
    try:
        cmd = [sys.executable, str(UI_SCRIPT), str(text)]
        if save:
            cmd.append("--save")          # ⚠️ 漏传这个 ⇒ 只会解析出名字、**永远不保存**
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"UI 流程超时({timeout}s) —— 模拟器可能卡住了"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:80]}"}
    out = (r.stdout or b"").decode("utf-8", "replace")
    m = re.search(r"结果:\s*(\{.*\})", out)
    if not m:
        return {"ok": False, "reason": f"读不出结果:{out[-160:]}"}
    try:
        return dict(ast.literal_eval(m.group(1)))
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "结果不是合法 dict"}


def _find_saved_fid(qt, title: str, session=None, user_id: int = 0) -> str:
    """找 **App 刚存进来的那个文件** —— 按资源名**搜我盘**,取最新的一个。

    ⚠️ **为什么用搜索而不是"列目录"**(2026-10-06 实测踩到):曾想先列出「来自：分享」目录再翻,
    结果 `list_dir("0")` **正好返回 10000 项**(= 50 页 × 200 的硬上限),而该目录**排在 1 万条之外**
    ⇒ **根本够不着**,我当时还据此误判成"保存没成功"。搜索接口**一次命中**。
    """
    key = str(title or "").strip()
    # ⚠️⚠️ **标题为空时不能提前返回**(2026-10-06 实测到的**真原因**):
    # 日志一直在打「解出「?」但两次都找不到文件」—— 那个 `?` 就是**空标题**。
    # 也就是说:**卡片弹出来了、但标题没读出来**(`resolve` 只认「来自剪贴板」这句话,
    # 标题抽不到就给了空串)。而原来这里 `if not key: return ""` ⇒
    # **连"刚刚新增"的兜底都走不到**,一条明明搬成了的线索被判成失败。
    # 空标题时**跳过去名字匹配,直接走兜底** —— App 刚存的那条一定是最新的。
    cand = []
    if key:
        cand = [x for x in qt.search_files(key, size=20)
                if key in str(x.get("file_name") or "") or str(x.get("file_name") or "") in key]
    cand.sort(key=lambda x: int(x.get("updated_at") or 0), reverse=True)
    if cand:
        return str(cand[0].get("fid") or "")

    # ⚠️⚠️ **兜底:名字匹配不上时,取"刚刚新增的那个"**(2026-10-06 找到的真原因)。
    # **卡片标题与保存后的文件名经常对不上**(实测):
    #   卡片:「第五人格美化包（先保存再下载）」
    #   盘里:「第五人格美化包教程下载-」      ← 互不包含 ⇒ 按名字永远找不到
    # 而 **App 刚存的那条一定是最新的**。加一个**时间窗**(5 分钟)防止误取别人的:
    # 只认"刚刚更新过"的,不是无脑取最新。
    from app.db.models import SystemConfig as _SC

    d_fid = ""
    if session is not None:
        row = session.scalar(select(_SC).where(_SC.key == _SAVE_DIR_FID_KEY))
        d_fid = str(row.value) if row and row.value else ""
    if not d_fid:
        # ⚠️⚠️ **必须用搜索接口找目录,不能用 `list_dir`**(2026-10-06 实测踩到,而且是**第二次**):
        # `list_dir("0")` 最多 50 页 × 200 = **10000 条就停**,而「来自：分享」**排在第 1 万条之外**
        # ⇒ d_fid 永远为空 ⇒ 兜底直接返回 ""(失败照旧),**而且每个口令都白翻 50 页**
        # (一轮 8 条从 83 秒涨到 **795 秒**)。搜索接口一次命中 —— 上面按名字找文件时就是这么修的,
        # 这里**又栽了同一跤**。
        for x in qt.search_files(_SAVE_DIR, size=5):
            if str(x.get("file_name")) == _SAVE_DIR and x.get("fid"):
                d_fid = str(x["fid"])
                break
        if d_fid and session is not None:
            row = session.scalar(select(_SC).where(_SC.key == _SAVE_DIR_FID_KEY))
            if row is None:
                session.add(_SC(key=_SAVE_DIR_FID_KEY, value=d_fid))
            else:
                row.value = d_fid
            session.commit()
    if not d_fid:
        return ""
    now_ms = int(time.time() * 1000)
    for x in qt.list_recent(d_fid, size=10):
        age_ms = now_ms - int(x.get("updated_at") or 0)
        if 0 <= age_ms <= _RECENT_SAVE_MS:
            logger.info("夸克口令:名字对不上(%r),按**刚刚新增**兜底取到 %s",
                        key[:20], str(x.get("file_name"))[:24])
            return str(x.get("fid") or "")
    return ""


def _snapshot_failure() -> str:
    """失败时把模拟器屏幕拍下来(子进程跑 UI 脚本里的 `snapshot`)。失败返回空串,**绝不抛**。

    ⚠️⚠️ **单独抽出来,是为了能在单测里被替换掉** —— 因为它**真的会去操作模拟器**:
    原来这段内联在 `drain` 的失败分支里,于是**每跑一次单测就真的截一次模拟器屏幕**
    (2026-10-06 实测:单测跑一遍生成 4 张 `_quark_nosave_*.png`,一天堆了 47 张)。

    **单测不该碰真设备**:慢、有副作用,而且**会污染诊断** —— 当天我正是拿这批
    "截图证据"当成了生产失败现场,一路推出一个错误根因(见
    [[falsification-needs-control-variables]])。**先让证据干净,再谈归因。**
    """
    try:
        import subprocess as _sp
        r = _sp.run([sys.executable, "-c",
                     "import sys; sys.path.insert(0,'tools');"
                     "from quark_kouling_ui import snapshot; print(snapshot('nosave'))"],
                    capture_output=True, timeout=90)
        return (r.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - 截图是诊断附属品,失败不影响判定
        return ""


def drain(session, user_id: int, settings=None, limit: int | None = None) -> dict:
    """处理 N 条待办抖音线索。返回统计。

    **每条都在自己的事务边界内**:一条失败不影响其余(`rollback` 后继续)。
    """
    from app.db.models import DiscoveredPanLink, DouyinLead
    from app.services.cookie_store import get_cookie
    from app.services.quark_transfer import QuarkTransfer

    if limit is None:
        limit = int(getattr(settings, "quark_kouling_per_run", 2) or 2)
    ck = (get_cookie(session, user_id, "quark") or "").strip()
    if not ck:
        return {"status": "no_cookie", "tried": 0}

    # ⚠️ **只取"还没试过"的**(`kouling_tried_at IS NULL`)。
    # 起因(2026-10-06 实测):抖音线索里**绝大多数是迅雷形态**(《》里的群/分享口令),
    # 而夸克 App **对它们不弹卡片** —— 同样两条标题,迅雷型失败、夸克型成功。
    # 不留痕的话每轮都会拿同一批"匹配不了"的线索去烧模拟器时间(一条 15–20 秒)。
    # ⚠️ **判据从"试过一次没有"改成"试够几次没有"**(2026-10-07)—— 见 `kouling_tries` 的说明。
    max_tries = max(1, int(getattr(settings, "quark_kouling_max_tries", 2) or 2))
    todo = session.scalars(
        select(DouyinLead).where(DouyinLead.user_id == user_id,
                                 DouyinLead.our_url == "",
                                 DouyinLead.kouling_tries < max_tries)
        .order_by(DouyinLead.id.desc()).limit(max(limit * 12, 60))).all()
    # 排序的三档,按重要性从高到低:
    #   ① **看起来像夸克口令的排前面** —— 它们才是真会成的;
    #   ② **帖子越新越优先**(`publish_at` 倒序)—— 用户要的是**新鲜资源**,
    #      而老帖会被推广号**反复推**,把它们排在前面就是在重复搬已有的东西;
    #   ③ 都没发布时间的老数据(2026-10-06 之前入库的)排最后。
    todo = sorted(todo, key=lambda r: (
        not _looks_like_quark(r.title),
        -(r.publish_at.timestamp() if r.publish_at else 0),
        -int(r.id)))[:limit]
    done = failed = reused = 0      # `reused` = **三盘互通命中**(连模拟器都没跑)
    retried_ok = 0                  # 重试才成的条数(一次就成的不算)—— 用来量"重试到底值不值"
    reasons: list[str] = []         # 失败原因(落库 + 汇总进运行记录)
    env_streak = 0                  # 连续环境故障计数;够 2 条就提前收工(见下面的 break)
    try:
        qt = QuarkTransfer(ck)
    except Exception as exc:  # noqa: BLE001
        return {"status": "no_client", "reason": str(exc)[:80], "tried": 0}

    from datetime import datetime as _dt
    for lead in todo:
        # **无论成败都盖章** —— 这一条是防"无限重试失败项"的关键。
        # ⚠️ 例外:下面判成**环境故障**(`env_fail`)时会把章撤回 —— 那是模拟器/焦点的问题,
        # 与这条口令有没有内容无关,不该拿它判死线索。
        lead.kouling_tried_at = _dt.now()
        env_fail = False        # 本条是否"环境故障"(与口令内容无关)
        # ⚠️⚠️ **先落一次并释放写锁,再去做慢活**(2026-10-06 实测踩到):
        # 下面要跑 **15–20 秒的模拟器**(×8 条 ≈ 2.7 分钟),而 SQLite 是**单写者**。
        # 原来整轮都在一个写事务里 ⇒ **把别的作业全饿死**:14:00 那一分钟里
        # `bili_account_scan` / `xunlei_sync` / `xunlei_group` **三个作业同时**
        # 报 `database is locked`(它们的 `busy_timeout` 只有 30 秒)。
        # 纪律:**慢活(网络/浏览器/模拟器)一律不要在事务里做**。
        session.commit()
        title, fid = "", ""
        # ⚠️ **"解析出来了、但没找到文件"要重试一次**(2026-10-06 实测会遇到) ——
        # 那多半是**保存那一步偶发失败**(UI 点击落空 / 保存任务还没落盘),
        # 而不是"这条口令没内容"。**只重试这一种失败,且最多两次**:
        # "压根没解析出来"重试一百次也没用,给它翻倍烧模拟器时间纯属浪费。
        # ★ **三盘互通**(用户口径:「所有资源都走三盘互通」):先拿**线索标题**查一次库 ——
        # 这一步**不花模拟器时间**;命中就直接复用已有链,**不保存、不建链**(省 20 秒 + 省一次保存)。
        # ⚠️ 线索标题是抖音文案、不是资源名,所以 `already_have` 靠**抽取名字**去匹配,
        # 会有漏(漏了不要紧 —— 下面解析出**真资源名**后还会再查一次)。
        from app.services.pan_discovery import reuse_if_have

        have = reuse_if_have(session, user_id, lead.title or lead.mark or "")
        if have and have.get("my_link"):
            _had = bool(str(lead.our_url or ""))
            lead.our_url = str(have["my_link"])[:500]
            lead.last_error = ""             # 搬成了就清掉上次的失败原因
            mark_newly_moved(lead, _had)     # **首次搬成**(复用已有链也算搬成)
            logger.info("夸克口令:线索 %s 命中三盘互通,直接复用 %s", lead.aweme_id, lead.our_url)
            done += 1
            reused += 1
            if int(lead.kouling_tries or 0) > 0:
                retried_ok += 1
            continue

        reason = ""
        for attempt in (1, 2):
            res = resolve(lead.title or lead.mark or "", save=True)
            if not res.get("ok"):
                reason = str(res.get("reason") or "")[:180]
                # ⚠️⚠️ **环境故障不该消耗线索**(2026-10-06):`resolve` 用 `env=True` 标出
                # "模拟器/焦点/超时"这类**与口令内容无关**的失败。上面已经给这条盖了"试过"的章
                # —— 那是防"无限重试没内容的口令"的,而**环境故障被盖成"试过"是误伤**:
                # 实测当天前台被全屏游戏占着,夸克收不到剪贴板,**一条都成不了**,
                # 若照旧盖章,整批线索会被一次环境抖动**永久判死**。所以这里撤销那个章。
                if res.get("env"):
                    env_fail = True
                logger.info("夸克口令:线索 %s 没解出来(%s)", lead.aweme_id, res.get("reason"))
                break
            title = str(res.get("title") or "")
            fid = _find_saved_fid(qt, title, session, user_id)
            if fid:
                break
            if attempt == 1:
                logger.info("夸克口令:「%s」第一次没找到文件(保存可能没落地),重试一次", title[:24])
                time.sleep(3)          # 给保存任务一点落盘时间
        if not fid:
            failed += 1
            if env_fail:
                # ★ 环境故障(**整轮级**,与这条口令有没有内容无关):
                #   · 原因照实落库,但**措辞不同** —— 原来一律写成"解出「?」但找不到文件",
                #     那是**误导**(根本没解出东西来,是模拟器没反应);
                #   · **不拍截图**:截图是给"保存没产出文件"那类失败定位用的,环境故障的原因
                #     已经在 `reason` 里写着,再拍只是往 data/ 里堆垃圾
                #     (而且实测那种垃圾会污染诊断 —— 我拿测试产的截图当过生产证据);
                #   · **撤回"试过"的章**:不撤的话,一次环境抖动会把整批线索**永久判死**;
                #   · **够 2 条就早退**:见下面 ★★ 的说明。
                lead.last_error = (reason or "模拟器拿不到焦点等环境问题")[:200]
                reasons.append(lead.last_error[:60])
                lead.kouling_tried_at = None
                env_streak += 1
                if env_streak >= 2:
                    logger.warning("夸克口令:**连续 %d 条都是环境故障**"
                                   "(模拟器拿不到焦点/前台被别的程序占着),本轮提前结束 —— "
                                   "别把剩下的线索也各烧 15~20 秒", env_streak)
                    break
                continue
            env_streak = 0
            # ⚠️ **把原因落库**(2026-10-07):原来它只进 `logger.info`,而日志不落盘 ⇒
            # "失败 N 条"永远只有一个数字,查不出是超时、没保存成、还是口令本身没内容。
            lead.last_error = (reason or f"解出「{title}」但保存没产出文件")[:200]
            reasons.append(lead.last_error[:60])
            # **这一次真的试过了** ⇒ 记数;试够 `max_tries` 才不再进待办
            # (环境故障那条路**不记** —— 它压根没试成,记了就等于判死线索)
            lead.kouling_tries = int(lead.kouling_tries or 0) + 1
            # **把屏幕拍下来** —— 这一类是"保存没产出文件",光看日志只能猜
            shot = _snapshot_failure()
            logger.warning("夸克口令:解出「%s」但两次都在 %s 里找不到文件(截图:%s)",
                           title or "?", _SAVE_DIR, shot or "失败")
            continue
        try:
            # ★ **三盘互通(第二道)**:解析出**真资源名**后再查一次 —— 这一道比"拿线索标题查"准得多。
            # 命中就**直接用已有的链**,不再另建一条重复分享(资源本来就在我们盘里)。
            have2 = reuse_if_have(session, user_id, title)
            if have2 and have2.get("my_link"):
                sh = {"share_url": str(have2["my_link"]), "password": "", "share_id": ""}
                logger.info("夸克口令:「%s」三盘互通命中,复用已有链(不另建分享)", title[:24])
            else:
                sh = qt.share_fids([fid], title=title or "口令转存")
            _had = bool(str(lead.our_url or ""))
            lead.our_url = str(sh["share_url"])[:500]
            lead.last_error = ""
            mark_newly_moved(lead, _had)     # **首次搬成时刻**(2026-10-07)
            # 同时进**资源库**那条路:`status='ok'` 是有意的 ——
            # `pan_discovery` 只把 ok/skipped 当"已知",于是**不会被重复转存**;
            # 而 `resource_library` 读这张表,agent 的 `_library_evidence` 就能看见它。
            exists = session.scalar(select(DiscoveredPanLink.id).where(
                DiscoveredPanLink.user_id == user_id,
                DiscoveredPanLink.origin_url == lead.our_url).limit(1))
            if not exists:
                session.add(DiscoveredPanLink(
                    user_id=user_id, platform="douyin-kouling", origin_url=lead.our_url,
                    title=title[:255], author=str(lead.author or "")[:64],
                    source_url=str(lead.url or "")[:500], status="ok",
                    message="夸克口令转存", our_url=lead.our_url,
                    pass_code=str(sh.get("password") or "")[:32]))
            session.flush()
            done += 1
            if int(lead.kouling_tries or 0) > 0:
                retried_ok += 1      # 这条是**重试才成**的 —— 直接量出"重试值不值"
            logger.info("夸克口令:「%s」→ %s", title[:30], lead.our_url)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余
            session.rollback()
            failed += 1
            lead.last_error = f"{type(exc).__name__}: {str(exc)[:150]}"
            reasons.append(lead.last_error[:60])
            lead.kouling_tries = int(lead.kouling_tries or 0) + 1
            logger.warning("夸克口令:线索 %s 处理失败:%s", lead.aweme_id, str(exc)[:120])
            continue
    session.commit()
    return {"status": "ok", "tried": len(todo), "done": done, "failed": failed,
            "reused": reused, "reasons": reasons, "retried_ok": retried_ok}


def quark_kouling_tick(settings=None) -> int:
    """定时入口(角色 **wechat**:模拟器在本机)。返回本轮搬成的条数。"""
    from app.db import get_session_local
    from app.db.models import User
    from app.services.tenant_base import _record_run

    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    if not getattr(settings, "quark_kouling_enabled", True):
        return 0
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                out = drain(db, uid, settings)
                total += int(out.get("done") or 0)
                if out.get("status") == "no_cookie":
                    continue
                # 失败原因取**最高频那条**带进运行记录 —— 一堆数字里,"主因是什么"最关键
                # (以前只有一个"失败 7",查不出是超时、没保存成、还是口令本身没内容)。
                import collections as _c

                top = _c.Counter(out.get("reasons") or []).most_common(1)
                why = f";主因:{top[0][0]}({top[0][1]}×)" if top else ""
                # **重试才成的条数**要报出来 —— 那是"重试值不值"这个实验的直接读数
                ro = int(out.get("retried_ok") or 0)
                why += f";**重试才成{ro}条**" if ro else ""
                _record_run(db, uid, "quark_kouling", "success",
                            f"试{out.get('tried', 0)} 成功{out.get('done', 0)} "
                            f"(其中**三盘互通复用{out.get('reused', 0)}**)"
                            f" 失败{out.get('failed', 0)}{why}")
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.exception("夸克口令链路失败 user=%s", uid)
                _record_run(db, uid, "quark_kouling", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return total

# ---------------------------------------------------------------------------
# 把口令喂进模拟器(2026-10-09 换的实现:**走 IME 通道,不走剪贴板**)
# ---------------------------------------------------------------------------
# ⚠️⚠️ **为什么必须换**(2026-10-09 查清,有硬证据):
# 这条链原来把口令**写进宿主的剪贴板**,指望雷电把它同步给客机。而
# `vms/<vm>/Logs/VBox.log` 每次启动都写着 **`Shared Clipboard: Mode: Off`**
# (四份轮转日志**全是 Off**)⇒ **宿主写的那一份客机从来拿不到**。
# 这也解释了此前所有对不上的现象:
#   · "提供剪贴板的进程必须存活(存活 20 秒就弹卡片)" —— **假相关**:
#     真正相关的变量是「**客机自己的剪贴板里有没有东西**」,那次多半是上一次
#     手动 Ctrl+C 的内容还留着;
#   · "焦点"那条线(已用控制变量排除过)同理查不出所以然,因为**根因根本不在宿主侧**。
# 而且那个开关**不在任何我们能碰的地方**(四条路逐个证伪):雷电的 JSON 设置里没有该键、
# `ldconsole modify` 没有该选项、UI 是**远程网页**(标签本地搜不到,连 UTF-16LE 都 0 命中)、
# 手动往 `.vbox` 加 `<Clipboard mode="Bidirectional"/>` 会被 LDPlayer 启动时**整份重写丢弃**。
#
# ⇒ 换成 **adb 的 IME 通道**:`am broadcast` 把文本打进**客机里任意一个可输入框**
#   (走 InputConnection,**完全不经过剪贴板**),再在**客机内**用 `Ctrl+A / Ctrl+C`
#   把它放进**客机自己的剪贴板** —— 夸克启动时读的是**客机**剪贴板,于是卡片就弹了。
#   实测(2026-10-09):清空输入框 → 广播 → Ctrl+A/C → 启动夸克 ⇒ 卡片出现,
#   且顶上写的是**解析出来的资源名**(逐条核过:`监听宣传` / `铸剑纳贡（ForgeTax）`)。
#
# ⚠️ **前置条件**(坏了会静默不弹,所以体检里单列一行):客机里要装 **ADBKeyboard**
#   (`data/_ADBKeyboard.apk`,17KB)且**把它设为当前输入法**。少了它,广播没人接。
ADB = "D:/leidian/LDPlayer14/adb.exe"   # ⚠️ 正斜杠:反斜杠会被当成转义(实测把  吃成了响铃)
#: ADBKeyboard 的输入法 id —— 它就是"广播打字"的接收端
IME_ID = "com.android.adbkeyboard/.AdbIME"
#: 用来当"打字靶子"的可输入框。⚠️ **不必是夸克的输入框** —— 我们只要文本进客机剪贴板,
#: 用系统搜索框是因为它**永远在、且是标准 EditText**(夸克首页是 WebView 混合 UI,
#: uiautomator 抓不到它的输入框,实测)。
TYPING_TARGET = "com.android.settings.intelligence/.search.SearchActivity"
#: Android 键码:CTRL_LEFT=113 / A=29 / C=31
_KEY_CTRL, _KEY_A, _KEY_C = 113, 29, 31


def _adb(args: list[str], timeout: int = 30) -> tuple[bool, str]:
    """跑一条 adb 命令 → `(成功?, 输出)`。**失败不抛**(调用方要能降级/报警)。"""
    import subprocess

    try:
        r = subprocess.run([ADB, *args], capture_output=True, timeout=timeout)
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        return r.returncode == 0, out or err
    except Exception as exc:  # noqa: BLE001 - adb 掉线/路径不对都算"没成功"
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def ime_ready() -> tuple[bool, str]:
    """喂口令的**前置条件**是否就绪 → `(是否就绪, 人话说明)`。

    ⚠️ 单列它是为了**别让前置条件静默失效**:ADBKeyboard 被卸载、或输入法被换回拼音,
    广播就没人接 —— 而症状是"卡片不弹",与"口令无效"**长得一模一样**(本仓最忌的那类)。
    """
    ok, listed = _adb(["shell", "ime", "list", "-s"])
    if not ok:
        return False, f"adb 不可用({listed})"
    if IME_ID not in listed:
        return False, (f"**当前输入法不是 ADBKeyboard**(现在是 {listed.strip() or '空'})—— "
                       f"广播没人接,口令进不去;修法:装 `data/_ADBKeyboard.apk` 并 "
                       f"`adb shell settings put secure default_input_method {IME_ID}`")
    return True, f"ADBKeyboard 已在用({IME_ID})"


def ensure_ime() -> tuple[bool, str]:
    """确保 **ADBKeyboard 是当前输入法** —— 能自己设就自己设,不行才报没就绪。

    ⚠️⚠️ **为什么必须自愈**(2026-10-09 实测):这个设置**扛不过模拟器重启** ——
    重启后 `default_input_method` 会退回拼音输入法,而 ADBKeyboard **仍然装着**。
    只检查不修的话,每次重启后这条链都会**静默失败**(症状与"口令无效"一模一样)。
    ⇒ 装了就自己设回去(`settings put secure ...`,不需要人动手),设不回来才报错。
    """
    ok, why = ime_ready()
    if ok:
        return True, why
    listed = _adb(["shell", "pm", "list", "packages"])
    if not listed[0] or "com.android.adbkeyboard" not in listed[1]:
        return False, ("客机里**没装 ADBKeyboard**(广播没人接);"
                       "修法:`adb install data/_ADBKeyboard.apk`")
    _adb(["shell", "settings", "put", "secure", "enabled_input_methods",
          f"{IME_ID}:com.android.inputmethod.pinyin/.InputService"])
    _adb(["shell", "settings", "put", "secure", "default_input_method", IME_ID])
    import time

    time.sleep(1.5)
    return ime_ready()


def feed_via_ime(text: str) -> tuple[bool, str]:
    """把 `text` 送进**客机自己的剪贴板**(走 IME 通道,不碰宿主剪贴板)。

    三步:① 打开一个可输入框当靶子;② 广播打进去;③ 客机内 `Ctrl+A / Ctrl+C`。
    返回 `(成功?, 人话)`。**任何一步失败都返回 False + 原因**,不抛 —— 调用方据此报警。
    """
    if not str(text or "").strip():
        return False, "口令为空"
    ok, why = ensure_ime()      # ★ 自愈:重启会把它重置掉(见该函数注释)
    if not ok:
        return False, why
    _adb(["shell", "am", "start", "-n", TYPING_TARGET])
    import time

    time.sleep(4)                      # 等靶子窗口起来(实测 3~5 秒)
    ok, out = _adb(["shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", text])
    if not ok:
        return False, f"广播打字失败:{out}"
    time.sleep(2)
    _adb(["shell", "input", "keycombination", str(_KEY_CTRL), str(_KEY_A)])
    time.sleep(1)
    ok, out = _adb(["shell", "input", "keycombination", str(_KEY_CTRL), str(_KEY_C)])
    if not ok:
        return False, f"客机内复制失败:{out}"
    return True, "已送进客机剪贴板(IME 通道)"

#: 卡片上的**判据词**。⚠️ **两种写法都要认**(2026-10-09 实测):
#: 夸克实际渲染的是「来自剪**切**板」,而这条链原来只认「来自剪**贴**板」——
#: **一个字的差别**,于是**卡片明明弹出来了却判成「口令无效」**,调用方据此**撤回线索**
#: (等于把一条**成功解析**的口令记成失败 —— 本仓最忌的"看起来失败实则成功")。
#: ⚠️ 这个函数放在**服务层**是故意的:它原来住在 gitignore 的 `tools/` 里,
#: 那样的判据**改完也不会被提交**(同一族问题在导出逻辑上已经吃过一次)。
CARD_MARKS = ("来自剪贴板", "来自剪切板")


def judge_card(texts: list[str]) -> str:
    """从 uiautomator 读到的屏幕文字里取出**卡片上的资源名**;没弹卡片返回空串。

    卡片的形状:`[<资源名>, '来自剪X板', '立即查看']` ⇒ 资源名就在判据词的**上一条**。
    """
    for i, t in enumerate(texts or []):
        if any(m in str(t) for m in CARD_MARKS) and i > 0:
            return str(texts[i - 1])
    return ""
