"""吃瓜资料(测试版)单测:解析 / 免责声明闸门 / PDF+Word 真出文件。

⚠️ 这里**不联网**:生成那一层只验"没有素材就拒绝",不调模型。
渲染那层是真渲染(本机有中文字体),因为"能不能出文件"正是要验的东西。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import gossip_doc as gd

_MD = """# 某事件资料

## 事件概览
据某站报道,事情是这样的。

## 时间线
- 2026-10-24:发生了一件事
- 2026-10-25:又发生了一件事

## 免责声明
本资料仅整理自公开报道,请读者自行判断。
"""


def test_解析出标题与小节():
    title, sections = gd.parse_markdown(_MD)
    assert title == "某事件资料"
    assert [h for h, _ in sections] == ["事件概览", "时间线", "免责声明"]
    assert sections[1][1][0].startswith("- 2026-10-24")


def test_没有免责声明就拒绝出文件(tmp_path):
    """★ 吃瓜物料少了这句话,等于我们替未经证实的内容背书 —— 宁可不出。"""
    with pytest.raises(gd.GossipDocError) as ei:
        gd.build("# 标题\n\n## 事件概览\n内容", tmp_path, stem="x")
    assert "免责声明" in str(ei.value)


def test_真的生成_pdf_和_docx(tmp_path):
    out = gd.build(_MD, tmp_path, stem="测试事件")
    from pathlib import Path

    pdf, docx = Path(out["pdf"]), Path(out["docx"])
    assert pdf.exists() and pdf.stat().st_size > 1000, "PDF 太小,多半是空壳"
    assert docx.exists() and docx.stat().st_size > 1000
    assert out["title"] == "某事件资料"


def test_docx_里能读回中文(tmp_path):
    """中文没设 `eastAsia` 字体时,Word 里会退回宋体甚至乱码 —— 这里读回来验一遍。"""
    from docx import Document

    out = gd.build(_MD, tmp_path, stem="回读")
    d = Document(out["docx"])
    text = "\n".join(p.text for p in d.paragraphs)
    assert "事情是这样的" in text and "2026-10-25" in text


def test_没有素材就不让模型编(monkeypatch):
    """生成层的底线:**素材为空直接返回 None**,不"先写着看看"。"""
    from app.services import llm_client

    assert llm_client.compose_gossip_doc("http://x", "k", "m", title="某瓜", sources="") is None
    assert llm_client.compose_gossip_doc("http://x", "k", "m", title="", sources="素材") is None
