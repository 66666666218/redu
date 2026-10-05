"""热榜源自检 —— **注册之前先跑这个**(2026-10-05)。

两种模式:
    python scripts/probe_hot_sources.py                # 冒烟:把 SOURCES 里**全部**源跑一遍
    python scripts/probe_hot_sources.py --registry     # 同上(显式)
    python scripts/probe_hot_sources.py --cand         # 实测一批**候选**配置(注册前用)

⚠️ **判据是"解析出 ≥5 条",不是"HTTP 200"** —— 200 但 0 条会被下游读成
"今天没热点",那是本仓反复出现的静默失败。这个脚本存在的意义就是**把它显性化**。

⚠️ **在哪台机器上跑很要紧**:`hot_source` 是 `hotspot` 角色(**远程侧**),
本机(`SCHEDULER_ROLE=wechat`)**没有 newsnow 容器** ⇒ 本机跑冒烟,23 个容器源
会全 `ConnectionError`。**那是"测错机器",不是故障** —— 别据此报警。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.services.hot_sources import (  # noqa: E402
    SOURCES, JsonListSource, NewsnowSource, RssSource, _PLAT_LABEL)

# 候选配置:从 newsnow 的源定义里抠出来的 URL(`data/_nn/urls2.json`)。
# 跑 `--cand` 会逐个实测,**只把通的写进 SOURCES**(不通的留容器并写明原因)。
CANDIDATES: list[tuple[str, object]] = [
    ("solidot", RssSource("solidot", "https://www.solidot.org/index.rss", "Solidot")),
    ("aihot", RssSource("aihot", "https://aihot.virxact.com/feed/all.xml", "AI热点")),
    ("freebuf", RssSource("freebuf", "https://www.freebuf.com/feed", "FreeBuf")),
    ("hackernews", RssSource("hackernews", "https://hnrss.org/frontpage?count=30", "HN")),
    ("chongbuluo-latest", RssSource(
        "chongbuluo-latest", "https://www.chongbuluo.com/forum.php?mod=rss&view=newthread",
        "虫部落")),
    ("producthunt", RssSource("producthunt", "https://www.producthunt.com/feed", "Product Hunt")),
    ("juejin", JsonListSource(
        "juejin", "https://api.juejin.cn/content_api/v1/content/article_rank"
                  "?category_id=1&type=hot&spider=0",
        title_keys=("content.title",), list_path="data", extra_label="掘金",
        headers={"Referer": "https://juejin.cn/"})),
    ("tieba", JsonListSource(
        "tieba", "https://tieba.baidu.com/hottopic/browse/topicList",
        title_keys=("topic_name", "title"), url_key="topic_url", hot_key="discuss_num",
        headers={"Referer": "https://tieba.baidu.com/"})),
    ("thepaper", JsonListSource(
        "thepaper", "https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar",
        title_keys=("name", "title"), headers={"Referer": "https://www.thepaper.cn/"})),
    ("dongqiudi", JsonListSource(
        "dongqiudi", "https://api.dongqiudi.com/app/tabs/web/1.json",
        title_keys=("title",), headers={"Referer": "https://www.dongqiudi.com/"})),
    ("nowcoder", JsonListSource(
        "nowcoder", "https://gw-c.nowcoder.com/api/sparta/hot-search/top-hot-pc?size=20",
        title_keys=("title", "content"), hot_key="hotValue",
        headers={"Referer": "https://www.nowcoder.com/"})),
    ("jin10", JsonListSource(
        "jin10", "https://www.jin10.com/flash_newest.js",
        title_keys=("data.content", "data.title"), strip_js=True,
        headers={"Referer": "https://www.jin10.com/"})),
    ("mktnews", JsonListSource(
        "mktnews", "https://api.mktnews.net/api/flash?type=0&limit=50",
        title_keys=("title", "content"), headers={"Referer": "https://mktnews.net/"})),
    ("wallstreetcn-hot", JsonListSource(
        "wallstreetcn-hot", "https://api-one.wallstcn.com/apiv1/content/articles/hot"
                            "?period=all",
        title_keys=("title",), list_path="data.day_items", url_key="uri", hot_key="pageviews",
        ok_codes=(20000, 0, None), headers={"Referer": "https://wallstreetcn.com/"})),
    ("wallstreetcn-news", JsonListSource(
        "wallstreetcn-news", "https://api-one.wallstcn.com/apiv1/content/information-flow"
                             "?channel=global-channel&accept=article&limit=30",
        title_keys=("resource.title", "resource.content_short"), list_path="data.items",
        url_key="resource.uri", ok_codes=(20000, 0, None),
        headers={"Referer": "https://wallstreetcn.com/"})),
    ("wallstreetcn-quick", JsonListSource(
        "wallstreetcn-quick", "https://api-one.wallstcn.com/apiv1/content/lives"
                              "?channel=global-channel&limit=30",
        title_keys=("title", "content"), ok_codes=(20000, 0, None),
        headers={"Referer": "https://wallstreetcn.com/"})),
    ("iqiyi", JsonListSource(
        "iqiyi", "https://mesh.if.iqiyi.com/portal/lw/v7/channel/card/videoTab"
                 "?channelName=recommend",
        title_keys=("title", "name"), headers={"Referer": "https://www.iqiyi.com/"})),
    ("hupu", JsonListSource(
        "hupu", "https://bbs.hupu.com/topic-daily-hot",
        title_keys=("title",), url_key="url")),
    ("xueqiu-hotstock", JsonListSource(
        "xueqiu-hotstock", "https://stock.xueqiu.com/v5/stock/hot_stock/list.json"
                           "?size=30&_type=10&type=10",
        title_keys=("name",), headers={"Referer": "https://xueqiu.com/"})),
    ("cankaoxiaoxi", JsonListSource(
        "cankaoxiaoxi", "http://china.cankaoxiaoxi.com/json/channel/1/list.json",
        title_keys=("title",), url_key="url")),
    ("sspai", JsonListSource(
        "sspai", "https://sspai.com/api/v1/article/tag/page/get?limit=20&offset=0"
                 "&tag=%E7%83%AD%E9%97%A8%E6%96%87%E7%AB%A0&released=false",
        title_keys=("title",), headers={"Referer": "https://sspai.com/"})),
    ("linuxdo", JsonListSource(
        "linuxdo", "https://linux.do/latest.json?order=created", title_keys=("title",))),
    ("cls-telegraph", JsonListSource(
        "cls-telegraph", "https://www.cls.cn/v1/roll/get_roll_list",
        title_keys=("title", "content"), headers={"Referer": "https://www.cls.cn/telegraph"})),
]


def _line(sid: str, kind: str, note: str) -> None:
    print(f"  {sid:22s} {kind:6s} {note}   {_PLAT_LABEL.get(sid, '')}", flush=True)


def smoke(sources: list[tuple[str, object]] | None = None, min_rows: int = 5) -> int:
    """跑一遍并打印结果。返回**不可用**的源数。

    ⚠️ `min_rows` 默认 **5**,与 `_dig_titled` 的判据一致 —— **"HTTP 200 且没抛异常"
    不等于"能用"**:`solidot` 的 RSS 就回 HTTP 200 但**只有 1 条**(站点 feed 半废),
    若把这种算"通过",就正好复刻了本仓那个"闸门失准"的老毛病。低于 `min_rows`
    记 ⚠(算不可用),不算 ✓。
    """
    pairs = sources or [(k, v) for k, v in sorted(SOURCES.items())]
    ok = bad = 0
    t0 = time.time()
    for sid, src in pairs:
        kind = "容器" if isinstance(src, NewsnowSource) else "自研"
        try:
            rows = src.fetch(limit=30)
        except Exception as exc:  # noqa: BLE001 - 自检脚本,任何异常都要显性报出来
            bad += 1
            _line(sid, kind, f"✗ {type(exc).__name__}: {str(exc)[:52]}")
            continue
        if len(rows) < min_rows:
            bad += 1
            _line(sid, kind, f"⚠ 只有 {len(rows)} 条(<{min_rows},形同不可用)  "
                             f"{str(rows[0].get('title'))[:24]}")
        else:
            ok += 1
            _line(sid, kind, f"✓ {len(rows):3d} 条  {str(rows[0].get('title'))[:34]}")
    print(f"\n=== 可用 {ok} / 不可用 {bad}({time.time() - t0:.0f}s) ===")
    if bad and not pairs:
        print("⚠️ 容器源失败先看**是不是跑错机器**:本机 SCHEDULER_ROLE=wechat,")
        print("   hot_source 是 hotspot(远程)作业,本机没有 newsnow 容器。")
    return bad


def main() -> int:
    cand = "--cand" in sys.argv
    pairs = CANDIDATES if cand else None
    print(f"=== {'候选配置实测' if cand else '全量注册表冒烟'} ===")
    return 1 if smoke(pairs) else 0


if __name__ == "__main__":
    sys.exit(main())
