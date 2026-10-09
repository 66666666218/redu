"""公众号后台凭据的存取(2026-10-01)。

**为什么要单独一个模块**:这份凭据有三个读写方(监听择源 `_source._wemp_client`、健康页
源链展示、录入脚本),散着写迟早改一处漏一处;更要紧的是——它此前是**明文 JSON** 存在
`system_config` 里,而用户的各平台 Cookie 是 Fernet 加密的。凭据是**运营者级**的(能操作
公众号后台),比用户面 Cookie 权限更大,没理由反而不加密。

修法就是复用 `app.security` 的同一把钥匙(与 `user_cookies` 一致的 Fernet),
值前加 `enc:` 前缀区分**历史明文值**——`load` 认得旧格式,下次写入自动升级为密文,
所以不需要一次性迁移脚本。

读:`load()` 返回 `{cookie, token}`(没有/解不开都返回 `{}`,调用方按"未配置"处理)。
写:`save()`,顺带 `exists()` 给健康页做"源链在位"判断(不必解密)。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SystemConfig
from app.utils import get_logger

logger = get_logger(__name__)

_ENC_PREFIX = "enc:"   # 密文标记;没有它的是 2026-10-01 之前写入的明文值


def _key(user_id: int) -> str:
    return f"wemp_cred_{user_id}"


def _row(session: Session, user_id: int):
    return session.scalar(select(SystemConfig).where(SystemConfig.key == _key(user_id)))


def load(session: Session, user_id: int) -> dict:
    """读凭据 → `{cookie, token}`;未配置、密钥变更解不开、格式损坏一律返回 `{}`。

    解不开时**不抛异常**:调用方全是"有就用、没有就降级到下一个源"的语义,
    让它在这里炸掉会把整轮监听带崩(择源链的设计前提就是单源失效不致命)。
    """
    from app.security import decrypt_cookie

    row = _row(session, user_id)
    if row is None or not row.value:
        return {}
    raw = row.value
    if raw.startswith(_ENC_PREFIX):
        try:
            raw = decrypt_cookie(raw[len(_ENC_PREFIX):])
        except Exception:  # noqa: BLE001 - 密钥变更/密文损坏都按"未配置"处理
            logger.warning("wemp 凭据解密失败(密钥变更?),按未配置处理 user=%s", user_id)
            return {}
    try:
        cred = json.loads(raw)
    except ValueError:
        logger.warning("wemp 凭据不是合法 JSON,按未配置处理 user=%s", user_id)
        return {}
    return cred if isinstance(cred, dict) else {}


def save(session: Session, user_id: int, cookie: str, token: str) -> None:
    """写入凭据(加密落库)。"""
    from app.security import encrypt_cookie

    payload = json.dumps({"cookie": (cookie or "").strip(),
                          "token": (token or "").strip()}, ensure_ascii=False)
    value = _ENC_PREFIX + encrypt_cookie(payload)
    row = _row(session, user_id)
    if row is None:
        session.add(SystemConfig(key=_key(user_id), value=value))
    else:
        row.value = value
    session.commit()


def exists(session: Session, user_id: int) -> bool:
    """是否已配置(健康页"源链在位"用;只看有没有值,不解密)。"""
    row = session.scalar(select(SystemConfig.key).where(
        SystemConfig.key == _key(user_id), SystemConfig.value != ""))
    return row is not None

def probe(session: Session, user_id: int, cookie: str, token: str) -> dict:
    """拿一个**真实对标号**打一枪验凭据 → `{"ok", "mp", "items", "titles", "note"}`。

    ⚠️ **从 `scripts/wemp_cred.py` 搬进服务层**(2026-10-09):凭据录入**界面/接口**也要用它,
    而**服务层不能依赖 `scripts/`** —— Docker 镜像根本不 COPY 它(与 `pan_dedupe`、
    `job_liveness` 被提到服务层是同一条理由)。**脚本现在只调它**,单一实现不会两处飘。

    ⚠️ 异常**原样抛**给调用方分类:`WempAuthError`(会话失效 ⇒ 重登)、
    `WempRateLimited`(200013 ⇒ 等配额/换号)、`WempError`(其它)—— **三种的修法完全不同**,
    在这一层吞掉就等于把它们糊成一句"失败了"。
    """
    from sqlalchemy import select

    from app.db.models import WechatBenchmark
    from app.services.wechat.wemp_client import WempClient

    bm = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.biz != "").limit(1))
    if bm is None:
        # 库里还没对标号 ⇒ **探不了**,但这**不是失败**(凭据已保存,监听轮会用它)
        return {"ok": True, "items": -1, "mp": "", "titles": [],
                "note": "库里还没有可试的对标号,跳过探针(凭据已保存,监听轮会用它)"}
    mp_id = bm.biz if bm.biz.startswith("MP_WXS_") else bm.weread_book_id
    items = WempClient(cookie, token).mp_articles(mp_id, page=1, limit=20)
    return {"ok": True, "items": len(items), "mp": str(bm.nickname or ""),
            "titles": [str(it.get("title") or "")[:44] for it in items[:3]]}
