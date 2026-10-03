"""迅雷盘同步(2026-10-02):扫盘 → 新资源生成我方分享链 → 入库。

**用户口径**:"我需要你做到自己在抖音发现自己能进行保存然后自动入库跟公众号来源一起管理"。

拆开看,三步里**只有第二步做不了**:「用口令找到资源并转存」是迅雷**客户端专属**
(服务端不开放全网搜索 —— 2026-10-02 实测:`/drive/v1/share/search` 要 share_id、
`api-shoulei-ssl` 的搜索端点带完整登录态+签名仍 403、`/drive/v1/search` 直接 404)。
所以人工只保留"App 里点一下转存",**本模块负责剩下的全自动部分**:

  扫盘 → 挑出还没登记过的 → 生成我方分享链 → 入库(`xunlei_resources`)

**跨源去重**(2026-10-02):群采集/口令解析转存进来的资源**已经在 `xunlei_group_shares` 里
登记过**,扫盘时必须按 `fid` 跳过 —— 否则同一份资源①在资源库**重复展示**、
②被**重新生成一条分享链**(白占额度)。实测两表 fid 交集曾有 10 条。

**扫描范围**:根目录的直接子项(用户口径"扫整个盘")。每个顶层文件夹当成**一个资源包**
(如"右右玩软件"内含 7 个分类目录),根目录下的单个文件也算一条。迅雷自带的系统目录
(`超级保险箱`)跳过。
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import XunleiGroupShare, XunleiResource
from app.utils import get_logger

logger = get_logger(__name__)

# 迅雷自带的系统目录,不是用户转存进来的资源
_SYSTEM_DIRS = frozenset({"超级保险箱"})
_MAX_DEPTH = 5          # 递归下钻上限(防目录循环/过深)

# **分类目录**常带这些通用词(实测:我的转存/我的资源/最全文件),它们里面装的才是资源;
# 与之相对,"右右玩软件"、"白泽的梦"这种具体名字的文件夹本身就是资源包。
# ⚠️ 光看结构分不开这两种(「最全文件」= 4 文件夹 + 简介.doc,「右右玩软件」= 7 文件夹 +
# 使用帮助图,**长得一模一样**),所以必须补这层名字判断。
_CONTAINER_HINTS = ("转存", "资源", "文件", "全部", "合集", "整理", "分类", "其他")


def _is_container(name: str) -> bool:
    """是不是用户的**分类目录**(该继续往下找,而不是整包登记)。"""
    return any(h in name for h in _CONTAINER_HINTS)


def _collect_resources(xt, session, user_id: int, parent_id: str, depth: int,
                       known: set, new: list, parent_name: str = "") -> None:
    """递归挑出"资源包"并登记。

    **判定规则**(2026-10-02 实测定稿):根目录下的「我的转存」「最全文件」这类是用户的
    **分类目录**(纯文件夹),而「右右玩软件」「白泽的梦」才是**资源包**。区别在于:
      文件夹里**含文件** → 当成一个资源包,整包登记(分享链指向它,下载者拿到的是一整套);
      文件夹里**只有文件夹** → 当容器,继续往下走。
    单文件(根目录直接躺着的)也各算一条。
    """
    for f in xt.list_files(parent_id):
        fid = str(f.get("id") or "")
        name = str(f.get("name") or "").strip()
        if not fid or not name or fid in known:
            continue
        if name in _SYSTEM_DIRS:
            continue                                    # 迅雷自带系统目录
        is_dir = f.get("kind") == "drive#folder"
        if is_dir:
            children = xt.list_files(fid)
            if not children:
                continue                                # 空目录不算资源
            # 分类目录(我的转存/最全文件…)或**纯文件夹** → 继续深入,不登记自己
            if depth < _MAX_DEPTH and (_is_container(name) or not any(
                    c.get("kind") != "drive#folder" for c in children)):
                _collect_resources(xt, session, user_id, fid, depth + 1, known, new, name)
                continue
            # 落到这里 = 资源包(或深到上限的兜底)
        out = xt.share_files([fid])
        if out.get("status") != "ok":
            logger.warning("迅雷资源生成分享链失败 %s:%s", name, out.get("message"))
            continue
        session.add(XunleiResource(
            user_id=user_id, fid=fid, name=name[:255],
            kind=str(f.get("kind") or "")[:16], size=str(f.get("size") or "")[:32],
            parent_name=parent_name[:128], share_url=out["share_url"][:500],
            pass_code=(out.get("code") or "")[:32]))
        known.add(fid)                                  # 同轮内也去重
        new.append({"name": name, "share_url": out["share_url"],
                    "code": out.get("code") or "", "kind": str(f.get("kind") or "")})


def sync_xunlei_resources(session, user_id: int, settings=None) -> dict:
    """扫一轮迅雷盘(递归),把新出现的资源包登记进库并生成我方分享链。

    返回 `{"status", "scanned", "new", "items"}`;`items` 里带 `share_url`/`code`,
    供调用方(飞书推送/资源库)直接用。
    """
    from app.services import xunlei_transfer as xt

    if not xt._credentials():
        return {"status": "no_cred", "scanned": 0, "new": 0, "items": [],
                "message": "未配迅雷凭据"}
    try:
        root = xt.list_files("")                        # 根目录
        if not root:
            return {"status": "empty", "scanned": 0, "new": 0, "items": []}

        known = set(session.scalars(
            select(XunleiResource.fid).where(XunleiResource.user_id == user_id)).all())
        # ⚠️ **跨源去重**(2026-10-02 实测:两表 fid 交集 10 条):群采集 / 口令解析转存进来的
        # 资源已经在 `xunlei_group_shares` 里登记过(自带我方分享链),扫盘再登记一遍会
        # ① **资源库重复展示**同一份资源;② 给同一份资源**重新生成一条分享链**(白占分享额度)。
        known |= set(session.scalars(
            select(XunleiGroupShare.fid).where(
                XunleiGroupShare.user_id == user_id,
                XunleiGroupShare.fid != "").distinct()).all())
        new: list[dict] = []
        _collect_resources(xt, session, user_id, "", 0, known, new)
    except xt.XunleiDriveError as exc:
        # ⚠️ **硬失败不能当成"盘是空的"**(2026-10-03 修):凭据失效/网络断时资源**还在盘里**,
        # 而旧实现返回空表 → 上层记 `success(扫0 新0)`,静默丢失且无人知。
        # 注意这里**不 commit**:扫到一半失败时,半截结果落库会让下次"已登记"误判。
        session.rollback()
        logger.warning("迅雷扫盘失败:%s", exc)
        return {"status": "failed", "scanned": 0, "new": 0, "items": [],
                "message": str(exc)[:200]}
    session.commit()
    logger.info("迅雷盘同步:根目录 %d 项 → 新登记 %d", len(root), len(new))
    return {"status": "ok", "scanned": len(root), "new": len(new), "items": new}


def push_new_resources(items: list[dict], settings) -> bool:
    """把新登记的资源推飞书(**管理员群**:这是运营自己的资源清单)。"""
    if not items:
        return False
    webhook = (getattr(settings, "feishu_webhook_admin", "") or
               getattr(settings, "feishu_webhook", ""))
    if not webhook:
        return False

    from app.services.feishu_client import FeishuClient

    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
        f"迅雷盘新转存 **{len(items)}** 个资源,已生成我方分享链:"}}]
    for it in items:
        icon = "📁" if it.get("kind") == "drive#folder" else "📄"
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"{icon} **{it['name']}**\n{it['share_url']}"}})
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "green", "title": {"tag": "plain_text",
                                                       "content": f"📦 迅雷资源入库 · {len(items)} 个"}},
            "elements": elements}
    try:
        return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(card)
    except Exception:  # noqa: BLE001
        logger.exception("迅雷资源推送失败")
        return False


def xunlei_sync_tick(settings=None) -> int:
    """定时:为所有启用用户扫一轮迅雷盘。返回新登记数。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    if not getattr(settings, "xunlei_sync_enabled", True):
        return 0
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            from app.services.tenant_base import _record_run

            try:
                out = sync_xunlei_resources(db, uid, settings=settings)
                total += out.get("new", 0)
                if out.get("items"):
                    push_new_resources(out["items"], settings)
                # ⚠️ **按实际 status 记账**(2026-10-03 修):`failed`(凭据/网络)与 `no_cred`
                # **绝不能记 success** —— 旧实现无条件记 `success(扫0 新0)`,于是"扫盘早就断了"
                # 在运行记录里跟"盘里确实没资源"长得一模一样,静默了整整一天。
                st = str(out.get("status") or "")
                if st == "ok":
                    run_status = "success"
                    note = f"扫{out.get('scanned', 0)} 新{out.get('new', 0)}"
                elif st in ("failed", "no_cred"):
                    run_status = "failed"
                    note = f"{st}: {str(out.get('message') or '')[:140]}"
                else:                      # empty = 盘真的空,这是正常结果不是错误
                    run_status = "success"
                    note = "扫0 新0(盘内无资源)"
                _record_run(db, uid, "xunlei_sync", run_status, note)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("迅雷盘同步失败 user=%s", uid)
                _record_run(db, uid, "xunlei_sync", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return total
