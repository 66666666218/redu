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

sys.path.insert(0, ".")

GREEN, YELLOW, RED = "🟢", "🟡", "🔴"

# 每条链:`(名称, 运行记录 kind, 期望产出的字段, 说明)`
# 「期望产出字段」取运行记录 detail 里的 `key=数字`;`None` = 该链没有产出计数
CHAINS: tuple[tuple[str, str, str | None, str], ...] = (
    ("公众号·发现候选号", "wechat_candidates", "new", "搜狗微信搜索(免费免账号)"),
    ("公众号·收录对标号", "wechat_candidate_import", "listenable", "WeRSS search_mp → MP_WXS_*"),
    ("公众号·监听采文", "wechat_listen", "new", "微信读书 cover + 列表轮转"),
    ("抖音·口令线索", "douyin_leads", "线索", "MediaCrawler 搜索 → 《口令》→ 转存"),
    ("知乎/贴吧·盘链发现", "pan_discovery", None, "自研搜知乎 + MediaCrawler 搜贴吧"),
    ("小红书/贴吧·名字型热度", "resource_presence", "命中", "按资源名探平台 → 库内配对 → 转存"),
    ("迅雷·群转存", "xunlei_group", None, "群消息 → 转存 → 推卡"),
    ("迅雷·盘同步", "xunlei_sync", None, "扫盘入库"),
    ("闲鱼·采集", "xianyu", None, "Playwright 页面内调 mtop"),
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

    # newsnow(本机已停;只在 hot_source 跑在本地时才需要)
    try:
        r = subprocess.run(["docker", "ps", "-a", "--filter", "name=newsnow",
                            "--format", "{{.Status}}"],
                           capture_output=True, text=True, timeout=15)
        st = (r.stdout or "").strip()
        add("newsnow 容器(热榜长尾)", bool(st) and st.startswith("Up"),
            st or "容器不存在 —— 本机 role=wechat 不需要它,但远程要配 HOT_NEWSNOW_URL")
    except Exception as exc:  # noqa: BLE001
        out.append({"name": "newsnow 容器(热榜长尾)", "level": YELLOW,
                    "detail": f"探不到 docker:{type(exc).__name__}"})
    return out


def check_credentials(db) -> list[dict]:
    """② 凭证层:各平台 Cookie 在不在、最后一次更新是什么时候。"""
    from sqlalchemy import select

    from app.db.models import UserCookie

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
        else:
            age = (datetime.now() - at).days
            out.append({"name": label, "level": GREEN if age <= 7 else YELLOW,
                        "detail": f"更新于 {str(at)[:16]}({age} 天前)"})
    return out


def check_chains(db, days: int = 3) -> list[dict]:
    """③ 产出层:每条链最近几次运行的产出(判据取自运行记录的 detail)。"""
    import re

    out: list[dict] = []
    for label, kind, metric, _desc in CHAINS:
        rows = _rows(db, kind)
        if not rows:
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


def main() -> int:
    from app.db.database import get_session_local

    db = get_session_local()()
    try:
        sections = [("依赖(容器/库/venv/档案)", check_dependencies()),
                    ("凭证(Cookie 在不在)", check_credentials(db)),
                    ("产出(最近几次真跑出来的东西)", check_chains(db))]
    finally:
        db.close()
    if "--json" in sys.argv:
        print(json.dumps({t: rs for t, rs in sections}, ensure_ascii=False, indent=2,
                         default=str))
    else:
        print(render(sections))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
