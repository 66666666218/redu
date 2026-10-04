"""把本机的**公众号数据单向推给远程**(2026-10-04)。

**为什么需要它**:公众号监听**必须在本机**(微信读书 Cookie 绑家宽出口 IP),但
**选题 Agent 在远程** —— 两边库独立 ⇒ 远程 Agent 的"竞品供给"依据一直是**旧快照**
(实测:远程 `wechat_articles` 停在 2026-09-28,而 Agent 的 `_supply_articles` 读的正是它)。

**方向**:本机 → 远程,**只写远程,不读不改本机业务数据**(水位也只写本机的 `system_config`)。

⚠️ **为什么不能按 id 直接 upsert**(这是本模块最要紧的一条):
   两边**各自独立采集过**,远程 `wechat_articles.id` 已用到 **1130**,本地 id 也在同一区间
   ⇒ 按 id 覆盖会把远程那一行换成**本地同 id 的另一篇文章** —— 数据污染,而且**无声**。
⇒ 三条纪律:
   ⒜ **articles / benchmarks 都不带 id 推**,让远程自增 → 从根上没有 id 冲突;
   ⒝ 按**天然键**去重:benchmarks 用 `(user_id, nickname)`、articles 用 `(user_id, url)`。
      两者远程都有现成索引(`ix_wechat_benchmarks_user_id` / `ix_wa_user_url`);
   ⒞ **pan_links 必须用查回来的远程 `article_id`** —— 用本地 id 会撞外键(它 FK 到 articles.id)。

⚠️ **容错**:远程挂了/连不上,**只记一条日志并返回结构化失败**,绝不影响本机任何流程。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import WechatArticle, WechatBenchmark
from app.utils import get_logger

logger = get_logger(__name__)

DEFAULT_DAYS = 14          # 每次回看多少天(靠天然键去重,天然幂等,重跑无害)
DEFAULT_LIMIT = 3000       # 单轮上限,别一次把远程打爆

# 要推的列(**显式列出、不含 id**)。两边列数实测一致(25/12/5),这里再钉一遍免得
# 将来本地加了列、远程还没迁移时,INSERT 直接报 Unknown column。
_ARTICLE_COLS = ("user_id", "author", "title", "content", "url", "publish_at", "created_at",
                 "source", "benchmark_id", "pan_types", "pan_urls", "my_pan_urls",
                 "read_num", "zan_num", "looking_num", "share_num", "collect_num",
                 "comment_count", "traffic_at", "sample_count", "first_read_num",
                 "trend_flag", "quality", "pushed_at")
_BENCH_COLS = ("user_id", "nickname", "ghid", "weread_book_id", "biz", "anchor_url",
               "note", "active", "miss_count", "last_item_at", "created_at")


def _engine(remote_url: str, settings=None):
    """建远端引擎。

    ⚠️ `connect_timeout` 是 **pymysql 专有**的 connect_arg —— SQLite(测试里当假远端用)
    不认,直接传会 `TypeError`。所以**按方言给**。

    ⚠️ **为什么通常要走 SSH 隧道**(2026-10-04 实测):
    `redu-mysql` 容器**没有把 3306 映射到宿主机**(`docker port` 为空),它在 docker 网络里是
    `172.22.0.2:3306`;公网上那个 3306 是**宝塔自己装的另一个 MySQL**,跟业务库无关。
    ⇒ 本机要写业务库,只能经 SSH 隧道打到 `172.22.0.2:3306`。
    """
    st = settings
    ssh_host = str(getattr(st, "remote_ssh_host", "") or "") if st else ""
    if ssh_host:
        return _tunnel_engine(st)
    kw: dict[str, Any] = {}
    if remote_url.startswith("mysql"):
        kw["connect_args"] = {"connect_timeout": 10}
    return create_engine(remote_url, pool_pre_ping=True, pool_recycle=1800, **kw)


def _tunnel_engine(settings):
    """经 SSH 隧道连远端 MySQL:`creator` 每次新连接都开一个 `direct-tcpip` 通道。

    ⚠️ 密码从 `settings`(即 `.env`)读,**不写进代码/文档/提交**。
    """
    import paramiko
    import pymysql

    host = str(settings.remote_ssh_host)
    port = int(getattr(settings, "remote_ssh_port", 22) or 22)
    user = str(getattr(settings, "remote_ssh_user", "root") or "root")
    pw = str(getattr(settings, "remote_ssh_password", "") or "")
    db_host = str(getattr(settings, "remote_db_host", "172.22.0.2") or "172.22.0.2")
    db_port = int(getattr(settings, "remote_db_port", 3306) or 3306)
    db_user = str(getattr(settings, "remote_db_user", "redu") or "redu")
    db_pw = str(getattr(settings, "remote_db_password", "") or "")
    db_name = str(getattr(settings, "remote_db_name", "redu") or "redu")

    key_file = str(getattr(settings, "remote_ssh_key", "") or "")
    tr = paramiko.Transport((host, port))
    if key_file:
        # ⚠️ **优先用密钥**(可随时吊销,且 .env 里不必留密码)。
        # 需要远端 `PubkeyAuthentication yes` —— 2026-10-04 已开(开前用 `sshd -t` 自检过)。
        # ⚠️ `Transport.connect()` **只接 `pkey`,不接 `key_filename`** ⇒ 得自己把私钥加载成对象;
        #    密钥类型未知,所以按常见几种依次试(ed25519 → rsa → ecdsa)。
        pkey = None
        for loader in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
            try:
                pkey = loader.from_private_key_file(key_file)
                break
            except Exception:  # noqa: BLE001 - 换下一种类型
                continue
        if pkey is None:
            raise RuntimeError(f"读不出私钥({key_file}):类型不支持或文件损坏")
        tr.connect(username=user, pkey=pkey)
    else:
        tr.connect(username=user, password=pw)

    def _creator():
        # ⚠️ **`sock=` 只在 `connect()` 上,不在构造函数上**(pymysql 实测)
        # ⇒ 用 `defer_connect=True` 先建对象、再把 SSH 通道塞给它。
        ch = tr.open_channel("direct-tcpip", (db_host, db_port), ("127.0.0.1", 0))
        conn = pymysql.connect(host=db_host, port=db_port, user=db_user, password=db_pw,
                               database=db_name, charset="utf8mb4",
                               connect_timeout=10, defer_connect=True)
        conn.connect(sock=ch)
        return conn

    eng = create_engine("mysql+pymysql://", creator=_creator, pool_pre_ping=True,
                        pool_recycle=1800)
    eng._rs_transport = tr          # 交给 sync_once 在 finally 里关掉  # noqa: SLF001
    return eng


def _rows(session: Session, model, since: datetime, limit: int) -> list[Any]:
    from sqlalchemy import select

    return session.scalars(
        select(model).where(model.created_at >= since)
        .order_by(model.id).limit(limit)).all()


def _push(table: str, cols: tuple[str, ...], values: list[dict], conn) -> int:
    """批量插入(**)不带 id**)。返回写入行数。"""
    if not values:
        return 0
    col_sql = ", ".join(cols)          # 不加反引号:SQLite(测试用假远端)不认
    val_sql = ", ".join(f":{c}" for c in cols)
    conn.execute(text(f"INSERT INTO {table} ({col_sql}) VALUES ({val_sql})"), values)
    return len(values)


def sync_once(local: Session, remote_url: str, days: int = DEFAULT_DAYS,
              limit: int = DEFAULT_LIMIT, settings=None) -> dict[str, Any]:
    """跑一轮同步。返回 `{"status", "benchmarks", "articles", "links", ...}`。

    ⚠️ **幂等**:靠天然键去重,**同一天跑多次也只是白跑一遍**,不会产生重复。
    """
    # ⚠️ 两条路任一即可:直连 URL,**或** SSH 隧道(实测走隧道,见 `_engine` 的说明)
    if not remote_url and not getattr(settings, "remote_ssh_host", ""):
        return {"status": "disabled", "reason": "既没配 remote_db_url,也没配 remote_ssh_host"}
    since = datetime.now() - timedelta(days=days)
    try:
        remote = _engine(remote_url, settings)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "reason": f"建连接失败:{type(exc).__name__}: {str(exc)[:120]}"}

    sent = {"benchmarks": 0, "articles": 0, "links": 0}
    try:
        with remote.begin() as conn:
            # ---------- ① 对标号:先推(articles.benchmark_id 要引用它)----------
            local_benchs = _rows(local, WechatBenchmark, since - timedelta(days=365), limit)
            bmap: dict[int, int | None] = {}
            if local_benchs:
                names = [b.nickname for b in local_benchs if b.nickname]
                have: dict[str, int] = {}

                def _load_bench_ids(conn, chunk_names: list[str]) -> None:
                    # ⚠️ 分批查(`IN` 的占位符数量有限),别一次塞几百个
                    for i in range(0, len(chunk_names), 200):
                        chunk = chunk_names[i:i + 200]
                        ph = ", ".join(f":n{j}" for j in range(len(chunk)))
                        params = {f"n{j}": v for j, v in enumerate(chunk)}
                        for rid, nick in conn.execute(text(
                                f"SELECT id, nickname FROM wechat_benchmarks "
                                f"WHERE user_id = 1 AND nickname IN ({ph})"), params):
                            have[str(nick)] = int(rid)

                _load_bench_ids(conn, names)
                new_b = [{c: getattr(b, c) for c in _BENCH_COLS}
                         for b in local_benchs if b.nickname and b.nickname not in have]
                sent["benchmarks"] = _push("wechat_benchmarks", _BENCH_COLS, new_b, conn)
                if new_b:                       # 新建的要再查一次才拿得到 id
                    _load_bench_ids(conn, [str(r["nickname"]) for r in new_b])
                bmap = {b.id: have.get(str(b.nickname)) for b in local_benchs}

            # ---------- ② 文章:按 (user_id, url) 去重,不带 id ----------
            local_arts = _rows(local, WechatArticle, since, limit)
            urls = [a.url for a in local_arts if a.url]
            remote_urls: set[str] = set()
            for i in range(0, len(urls), 200):
                chunk = urls[i:i + 200]
                ph = ", ".join(f":u{j}" for j in range(len(chunk)))
                params = {f"u{j}": v for j, v in enumerate(chunk)}
                remote_urls |= {str(u) for (u,) in conn.execute(text(
                    f"SELECT url FROM wechat_articles WHERE user_id = 1 AND url IN ({ph})"),
                    params)}
            new_a, amap = [], {}
            for a in local_arts:
                if not a.url or a.url in remote_urls:
                    continue
                row = {c: getattr(a, c, None) for c in _ARTICLE_COLS}
                row["benchmark_id"] = (bmap.get(a.benchmark_id)
                                       if row.get("benchmark_id") else None)
                new_a.append(row)
            sent["articles"] = _push("wechat_articles", _ARTICLE_COLS, new_a, conn)

            # ---------- ③ 盘链:必须用**远程的** article_id(FK)----------
            for i in range(0, len(urls), 200):
                chunk = urls[i:i + 200]
                ph = ", ".join(f":u{j}" for j in range(len(chunk)))
                params = {f"u{j}": v for j, v in enumerate(chunk)}
                for rid, u in conn.execute(text(
                        f"SELECT id, url FROM wechat_articles "
                        f"WHERE user_id = 1 AND url IN ({ph})"), params):
                    amap[str(u)] = int(rid)
            link_objs = local.execute(
                text("SELECT article_id, pan_url, created_at FROM wechat_pan_links "
                     "WHERE created_at >= :s LIMIT :n"),
                {"s": since, "n": limit}).all()
            # 一次取回这些文章的 url(别在循环里逐条 `local.get`,那是 N 次查询)
            aids = sorted({int(r[0]) for r in link_objs})
            aid2url: dict[int, str] = {}
            for i in range(0, len(aids), 500):
                chunk = aids[i:i + 500]
                ph = ", ".join(f":a{j}" for j in range(len(chunk)))
                params = {f"a{j}": v for j, v in enumerate(chunk)}
                for aid, u in local.execute(text(
                        f"SELECT id, url FROM wechat_articles WHERE id IN ({ph})"), params):
                    aid2url[int(aid)] = str(u or "")
            new_l = []
            for aid, pan, created in link_objs:
                rid = amap.get(aid2url.get(int(aid), ""))
                if not rid:
                    continue                      # 文章没同步过去 → 这条链先不推(不猜 id)
                new_l.append({"user_id": 1, "article_id": rid, "pan_url": pan,
                              "created_at": created})
            # ⚠️ **盘链也要去重**(幂等的关键):按 `(article_id, pan_url)` 查一遍远程已有的。
            # 少了这一步,每跑一轮就会把所有链**重复插一遍** —— 数量会随轮次线性膨胀。
            if new_l:
                pairs = {(r["article_id"], str(r["pan_url"])) for r in new_l}
                arts = sorted({a for a, _ in pairs})
                existing: set[tuple[int, str]] = set()
                for i in range(0, len(arts), 500):
                    chunk = arts[i:i + 500]
                    ph = ", ".join(f":a{j}" for j in range(len(chunk)))
                    params = {f"a{j}": v for j, v in enumerate(chunk)}
                    for aid, pan in conn.execute(text(
                            f"SELECT article_id, pan_url FROM wechat_pan_links "
                            f"WHERE article_id IN ({ph})"), params):
                        existing.add((int(aid), str(pan)))
                new_l = [r for r in new_l if (r["article_id"], str(r["pan_url"])) not in existing]
            if new_l:
                conn.execute(text(
                    "INSERT INTO wechat_pan_links (user_id, article_id, pan_url, created_at) "
                    "VALUES (:user_id, :article_id, :pan_url, :created_at)"), new_l)
                sent["links"] = len(new_l)
    except Exception as exc:  # noqa: BLE001 - 远程任何问题都不该影响本机
        logger.warning("远程同步失败(不影响本机):%s: %s", type(exc).__name__, str(exc)[:200])
        return {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:160]}", **sent}
    finally:
        remote.dispose()
        _tr = getattr(remote, "_rs_transport", None)
        if _tr is not None:
            try:
                _tr.close()          # ⚠️ SSH 通道也要关,否则每轮泄漏一条连接
            except Exception:  # noqa: BLE001
                pass

    logger.info("远程同步完成:%s", sent)
    return {"status": "ok", **sent}


def remote_sync_tick(settings=None) -> int:
    """作业入口:把本机公众号数据推给远程。返回写入行数合计。

    ⚠️ 没配 `remote_db_url` 直接返回 0 且**不记运行记录**(沿用兄弟作业的纪律:
    免得运行记录里出现一批"success 但什么也没干"的空轮)。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    url = str(getattr(settings, "remote_db_url", "") or "")
    if not url and not getattr(settings, "remote_ssh_host", ""):
        return 0
    from app.db import get_session_local

    db = get_session_local()()
    try:
        out = sync_once(db, url, days=int(getattr(settings, "remote_sync_days", DEFAULT_DAYS)),
                        settings=settings)
        # ⚠️ **每轮都记运行记录,哪怕推了 0 条**(2026-10-04 改)。
        # 原本想省掉"什么也没干"的空轮,但那样**链路体检就看不见这个作业的死活**
        # (`pipeline_health` 会显示"从没跑过") —— 而"同步在跑、只是暂时没新数据"
        # 与"同步根本没在跑"是**完全不同的两件事**,不能长得一样。
        # 这跟今天反复踩的那条线是同一条:别让一条链**静默地**存在或不存在。
        if out.get("status") != "ok":
            from app.services.tenant_base import _record_run
            _record_run(db, 1, "remote_sync", "failed", str(out.get("reason") or "")[:200])
            db.commit()
            return 0
        from app.services.tenant_base import _record_run
        total = int(out.get("benchmarks", 0) + out.get("articles", 0) + out.get("links", 0))
        _record_run(db, 1, "remote_sync", "success",
                    f"对标号{out['benchmarks']} 文章{out['articles']} 盘链{out['links']}")
        db.commit()
        return total
    finally:
        db.close()
