"""DeepSeek/OpenAI 兼容大模型叙事层:把苗头/共振/新文数据转成运营可读的解读。

职责边界:只做"文本→文本"的内容解读(为什么值得追/怎么写标题),不做数据采集。
成本参考:deepseek-chat 每篇约 2K tokens ≈ ¥0.002~0.01;`llm_narrate_limit` 控制单次篇数。
失败语义:任何异常返回 None,调用方降级为无叙事推送,绝不阻塞主流程。
"""
from __future__ import annotations

import requests

from app.utils import get_logger

logger = get_logger(__name__)

# 内存成本计数器(进程级;重启清零,日报读取后重置)
_llm_usage = {"calls": 0, "failures": 0, "total_tokens": 0}


def llm_usage_snapshot(reset: bool = False) -> dict:
    """LLM 用量快照(calls/failures/total_tokens);reset=True 时读取后清零(供日报)。"""
    snap = dict(_llm_usage)
    if reset:
        _llm_usage.update(calls=0, failures=0, total_tokens=0)
    return snap


def _record_usage(payload: dict | None, ok: bool) -> None:
    _llm_usage["calls"] += 1
    if not ok:
        _llm_usage["failures"] += 1
        return
    usage = (payload or {}).get("usage") or {}
    _llm_usage["total_tokens"] += int(usage.get("total_tokens") or 0)
    logger.info("LLM 调用完成:tokens=%s(累计 %s)", usage.get("total_tokens"),
                _llm_usage["total_tokens"])

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
            _record_usage(None, ok=False)
            logger.warning("LLM 叙事失败 HTTP %s:%s", resp.status_code, resp.text[:200])
            return None
        payload = resp.json()
        _record_usage(payload, ok=True)
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
            _record_usage(None, ok=False)
            logger.warning("LLM 候选评级失败 HTTP %s", resp.status_code)
            return None
        payload = resp.json()
        _record_usage(payload, ok=True)
        content = payload.get("choices", [{}])[0].get("message", {}).get("content")
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


def rewrite_article(base_url: str, api_key: str, model: str,
                    title: str, content: str, my_link: str = "",
                    style: str = "实用资源分享", timeout: int = 120) -> dict | None:
    """AI 改写:对标文 → 自己的可发布稿(保留资源信息,替换推广角度)。

    返回 {title, content},失败返回 None。content 为空或过短时返回 None。
    """
    nl = chr(10)
    if not api_key or not content or len(content) < 100:
        return None
    link_line = f"我的网盘链接(必须原样保留在文中,并自然引导读者保存):{my_link}" if my_link else "文中如无网盘链接则不虚构,以资源收集攻略角度改写"
    user_prompt = (
        f"请把以下公众号文章改写为原创可发布稿。{nl}"
        + f"原文标题:{title}{nl}"
        + (f"我的网盘链接(必须原样保留在文中,并自然引导读者保存):{my_link}{nl}" if my_link else "文中如无网盘链接则不虚构,以资源收集攻略角度改写" + nl)
        + f"风格:{style}{nl}"
        + "要求:① 保留全部资源信息与获取方式 ② 结构/用词/表达全面重写(防抄袭判定) ③ 输出格式:第一行=新标题,空一行,之后=正文(纯文本,分段清晰,适合公众号)"
        + nl + nl
        + f"原文正文:{content[:6000]}"
    )
    try:
        resp = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "system",
                                "content": "你是网盘资源类公众号的资深写手,产出高质量原创资源推荐文章。"},
                               {"role": "user", "content": user_prompt}],
                  "temperature": 0.8, "max_tokens": 3000},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            _record_usage(None, ok=False)
            logger.warning("AI 改写失败 HTTP %s", resp.status_code)
            return None
        payload = resp.json()
        _record_usage(payload, ok=True)
        text = payload.get("choices", [{}])[0].get("message", {}).get("content") or ""
        text = text.strip()
        if not text:
            return None
        parts = text.split(chr(10) + chr(10), 1)
        new_title = parts[0].lstrip("# ").strip()[:64]
        new_content = parts[1].strip() if len(parts) > 1 else text
        return {"title": new_title, "content": new_content}
    except requests.RequestException as exc:
        logger.warning("AI 改写请求异常:%s", exc)
        return None
    except (KeyError, IndexError, TypeError):
        return None
