"""DeepSeek/OpenAI 兼容大模型叙事层:把苗头/共振/新文数据转成运营可读的解读。

职责边界:只做"文本→文本"的内容解读(为什么值得追/怎么写标题),不做数据采集。
成本参考:deepseek-chat 每篇约 2K tokens ≈ ¥0.002~0.01;`llm_narrate_limit` 控制单次篇数。
失败语义:任何异常返回 None,调用方降级为无叙事推送,绝不阻塞主流程。
"""
from __future__ import annotations

import requests

from app.utils import get_logger

logger = get_logger(__name__)

DEFAULT_BASE = "https://api.deepseek.com"
_SYSTEM_PROMPT = (
    "你是网盘资源推广运营助手。用户经营夸克/百度/UC/迅雷网盘资源的公众号矩阵,"
    "需要第一时间判断热点资源值不值得跟进推广。回答用简体中文,紧凑务实,不客套。"
)


def narrate_articles(base_url: str, api_key: str, model: str,
                     articles: list[dict], signals: list[str] | None = None,
                     timeout: int = 45) -> str | None:
    """对一批文章生成运营解读。

    articles: [{title, summary?, pan_types?, boards?}],signals 为该批文章所在板块的
    苗头信号描述(增速/共振等)。返回多行文本(每篇一段),失败返回 None。
    """
    if not api_key or not articles:
        return None
    lines = []
    for i, a in enumerate(articles, 1):
        row = f"{i}. 标题:{a.get('title', '')}"
        if a.get("summary"):
            row += f"\n   摘要:{str(a['summary'])[:200]}"
        if a.get("pan_types"):
            row += f"\n   网盘:{a['pan_types']}"
        if a.get("boards"):
            row += f"\n   出现板块:{a['boards']}"
        lines.append(row)
    user_prompt = (
        "以下是我们监测到的公众号对标号新发文(网盘资源类):\n"
        + "\n".join(lines)
        + ("\n板块信号:" + ";".join(signals) if signals else "")
        + "\n\n请逐篇给出运营解读,每篇一行,格式:"
          "「标题关键词 → 内容/受众一句话 → 值不值得立即跟进(值得/观望 + 一句理由) → 建议的网盘推广标题」"
    )
    try:
        resp = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                               {"role": "user", "content": user_prompt}],
                  "temperature": 0.7, "max_tokens": 800},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            logger.warning("LLM 叙事失败 HTTP %s:%s", resp.status_code, resp.text[:200])
            return None
        payload = resp.json()
        return (payload.get("choices", [{}])[0].get("message", {}) or {}).get("content")
    except requests.RequestException as exc:
        logger.warning("LLM 叙事请求异常:%s", exc)
        return None
    except (KeyError, IndexError, TypeError):
        return None


def rank_candidates(base_url: str, api_key: str, model: str,
                    candidates: list[dict], timeout: int = 60) -> list[dict] | None:
    """对候选对标号批量评级:是否网盘资源号、监控优先级。

    candidates: [{name, title}](≤15 个)。返回 [{name, verdict: 资源号|营销号|无关,
    priority: 高|中|低, reason}],失败返回 None。
    """
    if not api_key or not candidates:
        return None
    lines = [f"{i}. 公众号名:{c.get('name', '')} | 代表文章:{c.get('title', '')}"
             for i, c in enumerate(candidates, 1)]
    user_prompt = (
        "以下是自动发现微信公众号候选(网盘资源推广业务,想找同类资源号做对标):"
        + chr(10).join(lines)
        + chr(10) + chr(10)
        + "请逐个判断,每行格式:序号|判定(资源号/营销号/无关)|优先级(高/中/低)|一句话理由"
    )
    try:
        resp = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "system",
                                "content": "你是网盘资源推广运营助手。判断公众号候选是否为同类"
                                           "资源号(发网盘链接的资源分享号),回答紧凑务实。"},
                               {"role": "user", "content": user_prompt}],
                  "temperature": 0.3, "max_tokens": 800},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            logger.warning("LLM 候选评级失败 HTTP %s", resp.status_code)
            return None
        content = resp.json().get("choices", [{}])[0].get("message", {}).get("content")
        # 解析为结构化:LLM 输出 "序号|判定|优先级|理由" 或 "名称|判定|…"
        out = []
        for line in (content or "").splitlines():
            line = line.strip().lstrip("*- ")
            if not line or "|" not in line:
                continue
            segs = [x.strip() for x in line.split("|")]
            if len(segs) < 2:
                continue
            # 定位候选:序号(行首数字)优先,其次名称匹配
            idx = None
            head = segs[0]
            if head.isdigit():
                n = int(head)
                if 1 <= n <= len(candidates):
                    idx = n - 1
            if idx is None:
                for i, c in enumerate(candidates):
                    if c.get("name", "") and c["name"] in head:
                        idx = i
                        break
            if idx is None:
                continue
            out.append({"name": candidates[idx].get("name", ""),
                        "verdict": segs[1] if len(segs) > 1 else "",
                        "priority": segs[2] if len(segs) > 2 else "中",
                        "reason": segs[3] if len(segs) > 3 else ""})
        return out or None
    except requests.RequestException as exc:
        logger.warning("LLM 候选评级异常:%s", exc)
        return None
