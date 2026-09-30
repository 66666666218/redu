# -*- coding: utf-8 -*-
"""架构手术:重写 _llm_plan 为教学式输出(方法论化,2026-09-30 用户要求),并接好 _plan_text。用后即删。"""
import re
from pathlib import Path

f = Path("app/services/hotspot_agent.py")
lines = f.read_text(encoding="utf-8").splitlines(keepends=True)

# 边界:266 行 def _llm_plan 起,361 行 def resource_risk 前(1-based)
start = 265  # 0-based:def _llm_plan 在 266 行
end = next(i for i, ln in enumerate(lines) if ln.startswith("def resource_risk"))
assert lines[start].startswith("def _llm_plan"), f"边界漂移: {lines[start][:50]}"

NEW = '''def _plan_text(p: dict) -> str:
    """LLM 建议字段 → 教学式文本(≤500,入库 plan 列与飞书卡通用)。

    2026-09-30 方法论化(用户要求):从"罗列四件套"升级为
    「为什么能做→做什么→给谁→时机→三步走」——每条建议自带可照做的利用路径,
    而不是只报"哪个热点高就去发"。
    """
    seg: list[str] = []
    if p.get("why_doable"):
        seg.append("【为什么能做】" + str(p["why_doable"]))
    res = "【做什么】" + (str(p.get("resource")) or "?")
    if p.get("title"):
        res += " | 标题:" + str(p["title"])
    seg.append(res)
    seg.append("【给谁】" + (str(p.get("audience")) or "?")
               + (f" | 钩子:{p.get('hook')}" if p.get("hook") else ""))
    if p.get("timing"):
        seg.append("【时机】" + str(p["timing"]))
    if p.get("steps"):
        seg.append("【三步走】" + str(p["steps"]))
    return "\\n".join(seg)[:500]


def _llm_plan(settings: Settings, hotspots: list[dict],
              supply: list[WechatArticle], proven: list[str]) -> dict:
    """一次 LLM 调用同时完成:①热点↔资源语义匹配 ②无资源热点的拉新选题。

    2026-09-30 方法论化:输出从"四件套"升级为教学式(为什么能做/做什么/给谁/时机/三步走),
    plans[kw] 为字段 dict,展示用 _plan_text() 组装。
    返回 {"matches": {热点词: {"article_id": id, "why": 教怎么盘活旧资源}},
          "plans": {热点词: 字段dict}};失败/未配 key 返回 {}。
    """
    if not settings.deepseek_api_key or not hotspots:
        return {}

    def _hline(i: int, h: dict) -> str:
        """热点输入行:带多平台证据(共振 = 全网真实需求,单平台 = 待观察)。"""
        line = f"{i}. 《{h['keyword']}》抖音热度增长 +{h['growth']:.0f}%"
        if h.get("weibo"):
            line += f" [微博热搜第{h['weibo']['rank']}名·热度{h['weibo']['heat']}]"
        if h.get("baidu"):
            line += f" [百度热搜第{h['baidu']['rank']}名]"
        if h.get("platforms") and h["platforms"] != "douyin":
            line += f" → {len(h['platforms'].split('+'))}平台共振(全网级需求)"
        return line

    hs = [_hline(i, h) for i, h in enumerate(hotspots, 1)]
    sup = [f"{a.id}. {a.title}" for a in supply[:60]]
    proven_block = "\\n".join(f"- {t}" for t in proven) if proven else "- (暂无历史数据)"
    try:
        resp = requests.post(
            settings.deepseek_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}",
                     "Content-Type": "application/json"},
            json={"model": settings.deepseek_model,
                  "messages": [
                      {"role": "system", "content":
                       "你是网盘拉新运营教练,专注「项目拆解/网盘资源」赛道。商业模式:"
                       "借社会热点制作/整理「配套资料」(课件/真题/模板/安装包/壁纸/攻略),"
                       "用夸克网盘分享链发布,用户为拿资料必须转存 → 完成拉新。"
                       "你的任务不是报告哪个热点火,而是**教会运营怎么利用这个热点**:"
                       "每条建议都要讲清「为什么这个热点的人群会非要一份文件不可(需求缺口)"
                       "→ 做什么资料 → 发什么标题 → 给谁 → 什么时机发 → 新手三步怎么落地」。"
                       "选题铁律:①热点事件决定人群,人群决定他们非要不可的那份资料;"
                       "宁要一个急用人群,不要十个围观者;"
                       "②热点可以来自任何领域(体育/影视/节日/社会事件),但变现方案"
                       "必须能落到网盘资源上——问自己「这群人此刻会搜什么、要什么文件」;"
                       "③该资源若已被同行大量跟发(竞争密度高),给出差异化角度而不是放弃。"
                       "④多平台共振(抖音+微博/百度同现)的热点是全网级真实需求,优先出方案;"
                       "单平台热点仅供参考,方案要更保守。"},
                      {"role": "user", "content":
                       "rising 热点如下(平台证据已标注):\\n" + "\\n".join(hs)
                       + "\\n\\n我们近 72h 已采集到的资源文(id. 标题;语义相关即可匹配,"
                         "标题不必字面含热点词):\\n" + ("\\n".join(sup) if sup else "(无)")
                       + "\\n\\n历史高转载资源文标题(个人+对标号混合样本,仅参考选题套路,"
                         "不代表当前需求,勿直接照抄):\\n"
                         + proven_block
                       + "\\n\\n严格只输出一个 JSON 对象(无多余文字/无代码围栏):\\n"
                         '{"matches": [{"hotspot": "热点词", "article_id": 资源文id,'
                         ' "why": "一句话教运营怎么借这个热点盘活这份旧资源(切入角度/标题怎么改/怎么组合)"}],\\n'
                         ' "plans": [{"hotspot": "热点词",'
                         ' "why_doable": "一句话讲透需求缺口:这个热点的人群此刻会搜什么/缺什么文件",'
                         ' "resource": "具体到文件内容的资料清单",'
                         ' "title": "1条发布标题(带时效词/人群词)", "audience": "谁非要不可",'
                         ' "hook": "为什么必须转存(拉新点)",'
                         ' "timing": "发布时机:热点发酵窗口(几小时内动手/热度还能持续几天)",'
                         ' "steps": "三步执行清单:①… ②… ③…(每步一个具体动作,教新手落地)",'
                         ' "keywords": ["用户会搜索的资源词1", "词2", "词3"]}]}\\n'
                         "规则:matches 只收语义真正相关的资源(没有就空数组);"
                         "matches 里没有对应资源的热点必须给 plan;禁止编造不存在的 article_id。"
                         "你的输出是教一个新手「怎么利用这条热点」,不是报告热度——"
                         "每条建议都要给到能照着做的程度。"}],
                  "temperature": 0.5, "max_tokens": 1600},
            timeout=60)
        if resp.status_code >= 400:
            logger.warning("热点 LLM 规划失败 HTTP %s", resp.status_code)
            return {}
        text = (resp.json().get("choices", [{}])[0].get("message", {}) or {}).get("content") or ""
    except requests.RequestException as exc:
        logger.warning("热点 LLM 规划请求异常:%s", exc)
        return {}
    text = text.strip()
    if text.startswith("```"):   # 剥掉可能的代码围栏
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text.strip())
    except ValueError:
        logger.warning("热点 LLM 返回非 JSON,丢弃(%s...)", text[:80])
        return {}
    matches = {str(m.get("hotspot") or "").strip(): {"article_id": m.get("article_id"),
                                                     "why": str(m.get("why") or "")}
               for m in data.get("matches", []) if m.get("hotspot")}
    plans: dict[str, dict] = {}
    for p in data.get("plans", []):
        kw = str(p.get("hotspot") or "").strip()
        if not kw:
            continue
        kws = [str(x).strip() for x in (p.get("keywords") or []) if str(x).strip()]
        plans[kw] = {"why_doable": str(p.get("why_doable") or ""),
                     "resource": str(p.get("resource") or ""), "title": str(p.get("title") or ""),
                     "audience": str(p.get("audience") or ""), "hook": str(p.get("hook") or ""),
                     "timing": str(p.get("timing") or ""), "steps": str(p.get("steps") or ""),
                     "keywords": kws}
    return {"matches": matches, "plans": plans}


'''

new_lines = NEW.splitlines(keepends=True)
lines[start:end] = new_lines
f.write_text("".join(lines), encoding="utf-8")
print(f"_llm_plan 已重写({len(new_lines)} 行),_plan_text helper 已含")
