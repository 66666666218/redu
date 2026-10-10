"""夸克清理的**扫描快照**(2026-10-10)。

## 为什么需要它

一轮「扫 `redian监听_*` 下的跨目录重复」要对盘做几十到上千次 `list_dir`
(实测每次约 1.2 秒,19 个家加起来 14~60 分钟)。而**一轮跑不完就一点产出都没有** ——
下一轮又从头发起,永远在扫同样那几个家。
⇒ 把**扫完的家**落盘,下轮直接复用;**没扫完的家**记下来,多轮收敛。

## 安全边界(这份缓存会参与「删文件」的判据,所以每条都想清楚了)

1. **TTL**:过期的家**重扫**,不吃旧数据(`build_plan` 的 `ttl_hours`);
2. **判据只在"所有家都有有效快照"时才可信** —— 没扫完时 `complete=False`,
   执行器**默认拒绝删**(见 `scripts/quark_dup_apply.py`);
3. ⚠️ **危险方向**:快照说「某文件在 A、B 两个家都有」,而其实 A 那份已经没了
   ⇒ 会删掉**唯一的副本**。兜底两层:
   ① TTL 把窗口限住;② 删除**永远进回收站**(`apply_plan` 的 `to_recycle=True`)。
4. **删过东西的家立刻作废** —— 那些家的内容变了,快照不再代表现状(`drop_homes`)。

## 存储

一个 JSON 文件(**原子替换**:先写 `.tmp` 再 `os.replace`,半途挂掉不会留下半个文件)。
按**家名**索引,并记下 `fid` —— 同名但 `fid` 变了(家被重建过)时自动作废。
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from app.utils import get_logger

logger = get_logger(__name__)

#: 快照格式版本。**改结构就 +1** —— 老文件会被直接丢弃重扫,而不是按老结构硬读。
CACHE_VERSION = 1

#: 默认落盘位置(在 gitignored 的 `data/` 下,不进仓库)
DEFAULT_PATH = "data/quark_dup_cache.json"


class ScanCache:
    """按「家」缓存的扫描结果。**只读写自己的一个文件,不碰生产库。**"""

    def __init__(self, path: str | Path = DEFAULT_PATH) -> None:
        self.path = Path(path)

    # ───────────── 读写 ─────────────

    def _read(self) -> dict[str, Any]:
        """读快照。**文件坏了/版本不对 ⇒ 当空**(重扫即可,不能让它把清理卡死)。"""
        if not self.path.exists():
            return {"version": CACHE_VERSION, "homes": {}}
        try:
            with io.open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning("清理快照读不动(%s),当作空重扫:%s", self.path, str(exc)[:120])
            return {"version": CACHE_VERSION, "homes": {}}
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            logger.info("清理快照版本不符(期望 %s),丢弃重扫", CACHE_VERSION)
            return {"version": CACHE_VERSION, "homes": {}}
        if not isinstance(data.get("homes"), dict):
            return {"version": CACHE_VERSION, "homes": {}}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        """**原子替换** —— 先写临时文件再 rename,半途挂掉不会留下半个 JSON。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ───────────── 对外接口 ─────────────

    def get_home(self, name: str, fid: str, ttl_hours: float,
                 depth: int) -> list[dict] | None:
        """取某个家的缓存文件清单。

        `None` = **不可用**(没有 / 过期 / `fid` 变了 / **`depth` 变了**)⇒ 调用方应当重扫。

        ⚠️ `depth` 必须参与判定:深度不同扫到的东西**不是一回事**,
        拿 depth=2 的旧快照去跑 depth=1 会以为什么都扫过了 —— 判据直接失真。
        """
        rec = self._read()["homes"].get(name)
        if not isinstance(rec, dict):
            return None
        if str(rec.get("fid") or "") != str(fid or ""):
            logger.info("家「%s」的 fid 变了(家被重建过)⇒ 作废重扫", name)
            return None
        if int(rec.get("depth") or -1) != int(depth):
            logger.info("家「%s」的扫描深度变了(%s→%s)⇒ 作废重扫",
                        name, rec.get("depth"), depth)
            return None
        age_h = (time.time() - float(rec.get("scanned_at") or 0)) / 3600.0
        if age_h > ttl_hours:
            return None
        files = rec.get("files")
        return files if isinstance(files, list) else None

    def put_home(self, name: str, fid: str, files: list[dict], depth: int) -> None:
        """记下一个家**扫完**的结果。"""
        data = self._read()
        data["homes"][name] = {"fid": str(fid or ""), "depth": int(depth),
                               "scanned_at": time.time(), "files": files}
        self._write(data)

    def drop_homes(self, names: Iterable[str]) -> int:
        """作废若干个家(删过东西之后必须调 —— 那些家的内容变了)。返回作废个数。"""
        data = self._read()
        n = 0
        for nm in names:
            if data["homes"].pop(nm, None) is not None:
                n += 1
        if n:
            self._write(data)
        return n

    def clear(self) -> None:
        self._write({"version": CACHE_VERSION, "homes": {}})

    def summary(self) -> dict:
        """`{"homes": n, "files": n, "oldest_h": x}` —— 给报告用。"""
        homes = self._read()["homes"]
        files = sum(len(r.get("files") or []) for r in homes.values() if isinstance(r, dict))
        stamps = [float(r.get("scanned_at") or 0) for r in homes.values() if isinstance(r, dict)]
        oldest = (time.time() - min(stamps)) / 3600.0 if stamps else 0.0
        return {"homes": len(homes), "files": files, "oldest_h": oldest}
