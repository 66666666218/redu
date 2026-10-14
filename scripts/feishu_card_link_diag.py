"""卡片链接诊断:一次把**多种链接写法**放进同一张飞书卡片,用"哪几行点得动"定位渲染问题。

背景(2026-10-14):用户在**抖音推广线索卡片**上说「点『作品』列标题完全没反应」。
而离线重建的那张卡里,`(库里已有)` 那几行的单元格确实长这样:

    [新更新  22集。新更新22集#22集简直太…](https://pan.quark.cn/s/5bea6da448fa)

—— **形状是对的**(与公众号/闲鱼卡片同一个 `_col_set_row` 助手),所以问题只可能在这三种里:
  ① 列内 `div/lark_md` 的链接在某些客户端根本不渲染;
  ② **网盘域名**被飞书当风险链接处理(点了不跳);
  ③ 用户点到的其实是**没有链接的那些行**(📋口令未解析 / 👥已加群 / ⏸盘满)。

⇒ 本工具就是为分辨这三者设计的**控制变量**实验:一张卡里并排放若干写法,
  差别只在"写法"和"目标域名",让用户回一句"哪几行点得动"即可定性。

用法:
    python scripts/feishu_card_link_diag.py                  # 默认**只打印**卡片 JSON,不发
    python scripts/feishu_card_link_diag.py --send --section admin   # 真发(管理员群)

⚠️ 这是"给真人看"的工具,发出去收不回来 —— 所以**默认 dry-run**,必须显式 `--send`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.services.feishu._cards import _col_set_row  # noqa: E402

#: 现成可点的两条我方链(实测存在,直接借用;不产生任何新分享)
QUARK = "https://pan.quark.cn/s/5bea6da448fa"
XUNLEI = "https://pan.xunlei.com/s/VP3njCLCfh_pqF6ks7EgF-tuA1?pwd=3pkn"
DOUYIN = "https://www.douyin.com/"
OURS = "https://redu.tian1she.xyz/healthz"

#: (编号, 说明, 该行格子的 lark_md 内容)。**每一行只变一个东西**。
#:
#: 2026-10-14 实测已把范围压到一处:同一张卡里 **▶视频(douyin)点得动、网盘链接点不动**
#: ⇒ 列内 markdown 链接本身是通的,**差别只在目标域名**。所以这一版把变量收窄成:
#:   · 网盘域名(①) vs 公认网站(②) vs **我们自己的域名**(③ = 跳转方案的前置验证)
#:   · 再加两个"按钮"写法(⑤⑦):若 markdown 链接被筛、按钮不被筛,按钮就是兜底。
ROWS: list[tuple[str, str, str]] = [
    ("①", "列内链接 → 夸克网盘(就是现在点不动的那个)", f"[夸克盘]({QUARK})"),
    ("②", "列内链接 → 抖音(对照:已知点得动)", f"[抖音]({DOUYIN})"),
    ("③", "列内链接 → **我们自己的域名**(跳转方案要用的)", f"[我方域名]({OURS})"),
    ("④", "列内纯 URL 文本(不写 markdown)", QUARK),
]


def build_card() -> dict:
    """构造诊断卡:**同一张卡里并排多种写法**,再补一行"非列内"的对照。"""
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content":
            "🔧 **卡片链接诊断(一次性测试,可忽略)**\n"
            "请**逐个点**下面各行,回一句「**哪几个编号点得动**」即可 —— "
            "只有那一列数字是我需要的,不用截图。"}},
        {"tag": "note", "elements": [{"tag": "plain_text",
            "content": "控制变量:每行只改一处(写法或目标域名)"}]},
        _col_set_row([("**编号**", 1), ("**说明**", 5), ("**点这里**", 3)], grey=True),
    ]
    for no, desc, cell in ROWS:
        elements.append(_col_set_row([(no, 1), (desc, 5), (cell, 3)]))
    # ⑤⑥⑦ = **非列内**的对照:普通 div(⑥)与按钮(⑤⑦)。
    #   若 ①②③④ 全点不动而 ⑥ 点得动 ⇒ 问题锁死在"列内元素"这一层;
    #   若 ① 点不动而 ⑤ 点得动 ⇒ markdown 链接被筛、按钮不被筛 ⇒ 按钮就是兜底写法。
    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
        f"**⑤**(对照 · **非列内**普通 div,夸克):[点这里]({QUARK})"}})
    elements.append({"tag": "action", "actions": [{
        "tag": "button", "type": "default",
        "text": {"tag": "plain_text", "content": "⑥ 按钮 → 夸克盘"}, "url": QUARK},
        {"tag": "button", "type": "primary",
        "text": {"tag": "plain_text", "content": "⑦ 按钮 → 我方域名"}, "url": OURS}]})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
        "content": "回一句「哪几个编号点得动」即可,不用截图;这张卡只发一次"}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "turquoise",
                       "title": {"tag": "plain_text",
                                 "content": "🔧 卡片链接诊断 · 请回「哪几个编号点得动」"}},
            "elements": elements}


def build_card2() -> dict:
    """**第 2 张**:第 1 张证明"网盘域名不被拦"之后,把剩下两个变量拆开。

    第 1 张实测结果(2026-10-14):**①②③④⑤⑥⑦ 全部点得动** ⇒
      · 列内 `lark_md` 链接**能渲染** · **网盘域名不被拦** · 纯 URL 文本也点得动
    ⇒ 剩下的变量只有两个:
      **⒜ 链接的文字**(诊断卡写的是"夸克盘"三个字;真实卡里是**那一长串标题**——
        含 `#`、`｜`、双空格、`…`);
      **⒝ 卡片版式**(真实卡是 **6 列** 3/5/2/2/2/1,第 1 张只有 3 列)。

    所以这张卡**只变这两个**:
      A 组 = 3 列排版(同第 1 张),②③ 只差文字里有没有 `#`;
      B 组 = **6 列(与真实卡逐字同形)**,④⑤⑥ 逐字复制真实那一行。
    读法:②死③活 ⇒ 是 `#`;④死而 A① 活 ⇒ 是**版式**;⑤死④活 ⇒ 也是文字。
    """
    #: 逐字取自生产卡片(2026-10-14 08:11 那轮的「夏末倒置」行,`title[:22]+"…"`)
    REAL_TITLE = "新更新  22集。新更新22集#22集简直太…"
    REAL_TITLE_NO_HASH = REAL_TITLE.replace("#", "＃")
    VIDEO = "https://www.douyin.com/video/7538991122000000000"  # 形态同真实卡,不必真实可达

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content":
            "🔧 **卡片链接诊断 · 第 2 张(最后一张,可忽略)**\n"
            "第 1 张已证明「网盘链接本身点得动」。这张只测**剩下两个变量**:"
            "链接的**文字**、卡片的**版式**。请回「哪几个编号点得动」。"}},
        {"tag": "note", "elements": [{"tag": "plain_text",
            "content": "A 组=3 列版式;B 组=6 列(与生产卡逐字同形)"}]},
        {"tag": "div", "text": {"tag": "lark_md", "content": "**A 组 · 3 列版式**"}},
        _col_set_row([("**编号**", 1), ("**说明**", 5), ("**点这里**", 3)], grey=True),
        _col_set_row([("A①", 1), ("短文字链(第 1 张已证可点)=对照", 5), (f"[夸克盘]({QUARK})", 3)]),
        _col_set_row([("A②", 1), ("**真实那一串标题**(含 `#`)", 5),
                      (f"[{REAL_TITLE}]({QUARK})", 3)]),
        _col_set_row([("A③", 1), ("同 A②,但把 `#` 换成 `＃`", 5),
                      (f"[{REAL_TITLE_NO_HASH}]({QUARK})", 3)]),
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content":
            "**B 组 · 6 列版式(与生产卡逐字同形:作者/作品/资源/视频/转发数/条数)**"}},
        _col_set_row([("**作者**", 3), ("**作品**", 5), ("**资源**", 2), ("**视频**", 2),
                      ("**转发数**", 2), ("**条数**", 1)], grey=True),
        _col_set_row([("B④对照", 3), (f"[短文字]({QUARK})", 5),
                      ("🔴夸克(库里已有)", 2), (f"[▶视频]({VIDEO})", 2), ("↗21", 2), ("1", 1)]),
        _col_set_row([("B⑤逐字", 3), (f"[{REAL_TITLE}]({QUARK})", 5),
                      ("🔴夸克(库里已有)", 2), (f"[▶视频]({VIDEO})", 2), ("↗21", 2), ("1", 1)]),
        _col_set_row([("B⑥去#", 3), (f"[{REAL_TITLE_NO_HASH}]({QUARK})", 5),
                      ("🔴夸克(库里已有)", 2), (f"[▶视频]({VIDEO})", 2), ("↗21", 2), ("1", 1)]),
        {"tag": "note", "elements": [{"tag": "plain_text",
            "content": "回「哪几个编号点得动」即可;这张之后不再发"}]},
    ]
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "turquoise",
                       "title": {"tag": "plain_text",
                                 "content": "🔧 卡片链接诊断 · 第 2 张(文字 / 版式)"}},
            "elements": elements}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--send", action="store_true", help="真发(默认只打印)")
    ap.add_argument("--section", default="admin",
                    help="发到哪个群:admin(默认,管理员群)/ douhot(抖音群)…")
    ap.add_argument("--card", choices=("links", "text"), default="links",
                    help="links=第 1 张(域名/写法);text=第 2 张(文字/版式)")
    args = ap.parse_args()
    card = build_card2() if args.card == "text" else build_card()
    if not args.send:
        print(json.dumps(card, ensure_ascii=False, indent=1))
        print("\n[dry-run] 加 --send 才真发(默认不碰任何群)")
        return 0
    from app.services.feishu_client import FeishuClient, webhook_for
    from config.settings import get_settings

    settings = get_settings()
    wh = webhook_for(settings, args.section)
    if not wh:
        print(f"群 {args.section} 没配 webhook")
        return 2
    ok = FeishuClient(wh, getattr(settings, "feishu_secret", "")).send_card(card)
    print("已发送" if ok else "发送失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
