"""吃瓜资料 · **测试版**(2026-10-25)。本地出 PDF/Word,**不上线、不排程、不推送**。

用法:
    # 1) 给标题 + 素材(素材可以是:一段文字 / 一个 .txt|.md 文件 / 一个网址)
    python scripts/gossip_doc_test.py --title "某热点事件" \
        --source "某站报道:……" --source D:/tmp/report.txt

    # 2) 已经有一份 markdown,只重新排版
    python scripts/gossip_doc_test.py --from-md data/_gossip_test/x.md

产物落在 `data/_gossip_test/`。

## 为什么必须要素材

模型**不许凭记忆写** —— 吃瓜内容里编一个细节就是造谣(`llm_client._GOSSIP_SYSTEM`)。
没有素材时本脚本直接拒绝,不去"让模型先写着看看"。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT_DIR = Path("data/_gossip_test")


def _strip_html(html: str) -> str:
    """粗洗网页:去 script/style/标签,留文字。够喂给模型就行。"""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return " ".join(text.split())


def _load_source(src: str) -> str:
    """一条素材 → 纯文本。支持:网址 / 存在的文件 / 直接输入的文字。"""
    s = (src or "").strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://")):
        import requests

        r = requests.get(s, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"})
        r.raise_for_status()
        return _strip_html(r.text)
    p = Path(s)
    if p.exists() and p.is_file():
        return p.read_text(encoding="utf-8", errors="ignore")
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="吃瓜资料测试版:热点 + 素材 → PDF/Word")
    ap.add_argument("--title", help="热点标题")
    ap.add_argument("--source", action="append", default=[],
                    help="素材(可多次):网址 / 文件路径 / 直接文字")
    ap.add_argument("--from-md", help="已有 markdown,只重新排版(不调模型)")
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录")
    args = ap.parse_args()

    from app.services import gossip_doc

    if args.from_md:
        md = Path(args.from_md).read_text(encoding="utf-8")
    else:
        if not args.title:
            print("✗ 缺 --title"); return 2
        parts = [t for t in (_load_source(s) for s in args.source) if t.strip()]
        if not parts:
            print("✗ 没有任何素材 —— **不让模型凭记忆编**(吃瓜内容编一句就是造谣)。\n"
                  "  请用 --source 给报道正文/文件/网址。")
            return 2
        from config.settings import get_settings

        s = get_settings()
        if not getattr(s, "deepseek_api_key", ""):
            print("✗ 未配置 DEEPSEEK_API_KEY,无法生成"); return 2
        from app.services import llm_client

        print(f"… 整理素材({sum(len(x) for x in parts)} 字)→ 生成中", flush=True)
        md = llm_client.compose_gossip_doc(
            s.deepseek_base_url, s.deepseek_api_key, s.deepseek_model,
            title=args.title, sources="\n\n".join(parts))
        if not md:
            print("✗ 生成失败(模型没返回内容)"); return 1

    try:
        out = gossip_doc.build(md, args.out, stem=args.title or "")
    except gossip_doc.GossipDocError as exc:
        print(f"✗ {exc}")
        return 1
    #: 顺手把中间 markdown 也落盘 —— 改排版/重出不用再调一次模型(省钱,也便于人工核对)
    md_path = Path(out["pdf"]).with_suffix(".md")
    md_path.write_text(md, encoding="utf-8")
    print(f"✓ 标题:{out['title']}")
    print(f"  PDF : {out['pdf']}")
    print(f"  DOCX: {out['docx']}")
    print(f"  MD  : {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
