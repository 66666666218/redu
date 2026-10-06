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

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
UI_SCRIPT = ROOT / "scripts" / "quark_kouling_ui.py"
# 一个口令 15–20 秒,但模拟器冷启动/弹窗卡住时要留足;超时**必须当失败**,不能假装成功
_UI_TIMEOUT = 240
_SAVE_DIR = "来自：分享"          # App 保存分享文件的默认目录
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
    if not key:
        return ""
    cand = [x for x in qt.search_files(key, size=20)
            if key in str(x.get("file_name") or "") or str(x.get("file_name") or "") in key]
    cand.sort(key=lambda x: int(x.get("updated_at") or 0), reverse=True)
    return str(cand[0].get("fid") or "") if cand else ""


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
    todo = session.scalars(
        select(DouyinLead).where(DouyinLead.user_id == user_id,
                                 DouyinLead.our_url == "",
                                 DouyinLead.kouling_tried_at.is_(None))
        .order_by(DouyinLead.id.desc()).limit(max(limit * 8, 24))).all()
    # **看起来像夸克口令的排前面** —— 它们才是真会成的;其余的也会试,但排在后面。
    todo = sorted(todo, key=lambda r: (not _looks_like_quark(r.title), -int(r.id)))[:limit]
    done = failed = 0
    try:
        qt = QuarkTransfer(ck)
    except Exception as exc:  # noqa: BLE001
        return {"status": "no_client", "reason": str(exc)[:80], "tried": 0}

    from datetime import datetime as _dt
    for lead in todo:
        # **无论成败都盖章** —— 这一条是防"无限重试失败项"的关键。
        lead.kouling_tried_at = _dt.now()
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
            lead.our_url = str(have["my_link"])[:500]
            logger.info("夸克口令:线索 %s 命中三盘互通,直接复用 %s", lead.aweme_id, lead.our_url)
            done += 1
            continue

        for attempt in (1, 2):
            res = resolve(lead.title or lead.mark or "", save=True)
            if not res.get("ok"):
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
            logger.warning("夸克口令:解出「%s」但两次都在 %s 里找不到文件", title or "?", _SAVE_DIR)
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
            lead.our_url = str(sh["share_url"])[:500]
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
            logger.info("夸克口令:「%s」→ %s", title[:30], lead.our_url)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余
            session.rollback()
            failed += 1
            logger.warning("夸克口令:线索 %s 处理失败:%s", lead.aweme_id, str(exc)[:120])
            continue
    session.commit()
    return {"status": "ok", "tried": len(todo), "done": done, "failed": failed}


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
                _record_run(db, uid, "quark_kouling", "success",
                            f"试{out.get('tried', 0)} 成功{out.get('done', 0)} "
                            f"失败{out.get('failed', 0)}")
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.exception("夸克口令链路失败 user=%s", uid)
                _record_run(db, uid, "quark_kouling", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return total
