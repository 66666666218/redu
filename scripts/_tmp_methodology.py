# -*- coding: utf-8 -*-
"""热点建议"方法论化"改造:LLM 输出从"四件套"升级为"教学式"(为什么能做/做什么/给谁/时机/三步走)。用后即删。"""
from pathlib import Path

f = Path("app/services/hotspot_agent.py")
t = f.read_text(encoding="utf-8")

# ---- ① LLM prompt:输出结构扩展教学字段 ----
old_prompt = '''                         '{"matches": [{"hotspot": "热点词", "article_id": 资源文id,'
                         ' "why": "一句话说明相关性"}],\\n'
                         ' "plans": [{"hotspot": "热点词", "resource": "具体到文件内容的资料清单",'
                         ' "title": "1条发布标题(带时效词/人群词)", "audience": "谁非要不可",'
                         ' "hook": "为什么必须转存(拉新点)",' ' "keywords": ["用户会搜索的资源词1", "词2", "词3"]}]}\\n'
                         "规则:matches 只收语义真正相关的资源(没有就空数组);"
                         "matches 里没有对应资源的热点必须给 plan;禁止编造不存在的 article_id。"}],'''
new_prompt = '''                         '{"matches": [{"hotspot": "热点词", "article_id": 资源文id,'
                         ' "why": "一句话教运营怎么借这个热点盘活这份旧资源(角度/组合/标题怎么改)"}],\\n'
                         ' "plans": [{"hotspot": "热点词", "why_doable": "一句话:这个热点的人群此刻会搜什么/缺什么文件(需求缺口)",'
                         ' "resource": "具体到文件内容的资料清单",'
                         ' "title": "1条发布标题(带时效词/人群词)", "audience": "谁非要不可",'
                         ' "hook": "为什么必须转存(拉新点)",'
                         ' "timing": "发布时机:热点发酵窗口(几小时内动手/热度还能持续几天)",'
                         ' "steps": "三步执行清单(①搜齐素材 ②整理打包 ③挂链发布,每步一个具体动作,教新手落地)",'
                         ' "keywords": ["用户会搜索的资源词1", "词2", "词3"]}]}\\n'
                         "规则:matches 只收语义真正相关的资源(没有就空数组);"
                         "matches 里没有对应资源的热点必须给 plan;禁止编造不存在的 article_id。"
                         "你的输出是教一个新手「怎么利用这条热点」,不是报告热度——"
                         "每一条都要给到能照着做的程度。"}],'''
assert t.count(old_prompt) == 1, "prompt 锚点未命中"
t = t.replace(old_prompt, new_prompt)

# max_tokens 1200 → 1600(字段变多)
t = t.replace('"temperature": 0.5, "max_tokens": 1200}', '"temperature": 0.5, "max_tokens": 1600}')

# ---- ② 解析:plans[kw] 收全字段 ----
old_parse = '''        parts = [f"资源:{p.get('resource') or '?'}", f"标题:{p.get('title') or '?'}",
                 f"人群:{p.get('audience') or '?'}", f"拉新点:{p.get('hook') or '?'}"]
        plans[kw] = {"text": " | ".join(parts), "keywords": kws}'''
new_parse = '''        plans[kw] = {"why_doable": str(p.get("why_doable") or ""),
                     "resource": str(p.get("resource") or ""), "title": str(p.get("title") or ""),
                     "audience": str(p.get("audience") or ""), "hook": str(p.get("hook") or ""),
                     "timing": str(p.get("timing") or ""), "steps": str(p.get("steps") or ""),
                     "keywords": kws}'''
assert t.count(old_parse) == 1, "解析锚点未命中"
t = t.replace(old_parse, new_parse)

# ---- ③ 教学文本组装 helper(模块级,两条路径共用) ----
helper = '''

def _plan_text(p: dict) -> str:
    """LLM 建议字段 → 教学式文本(≤500,入库 plan 列与飞书卡通用)。

    2026-09-30 方法论化:从"罗列四件套"升级为"为什么能做→做什么→给谁→时机→三步走",
    每条建议自带可照做的利用路径,而不是只报"哪个热点高"。
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

'''
a = "def _plan_text(" in t  # 幂等
if not a:
    anchor = "def hotspot_agent_tick_all_users("
    assert t.count(anchor) == 1
    t = t.replace(anchor, helper + "\n" + anchor)

# ---- ④ 定时路组装改教学式 ----
old_use = '''            p = (llm.get("plans") or {}).get(t)
        if p:
            # p 是 _llm_plan 返回的四件套 dict(resource/title/audience/hook),
            # 拼成文本入库——旧代码 p[:500] 对 dict 切片,KeyError: slice 必炸(2026-09-30 修复)
            plan_text = ("资源:" + str(p.get("resource") or "?") + " | 标题:" + str(p.get("title") or "?")
                         + " | 人群:" + str(p.get("audience") or "?") + " | 拉新点:" + str(p.get("hook") or "?"))'''
# 兼容缩进变体
if old_use not in t:
    old_use = old_use.replace("            p =", "        p =").replace("            # p", "        # p").replace('            plan_text = ("', '        plan_text = ("').replace('''                         + " | 人群:" + str(p.get("audience") or "?") + " | 拉新点:" + str(p.get("hook") or "?"))''', '''                     + " | 人群:" + str(p.get("audience") or "?") + " | 拉新点:" + str(p.get("hook") or "?"))''')
assert t.count(old_use) >= 1, "定时路组装锚点未命中"
plan_line_old = '''                    plan_text = ("资源:" + str(p.get("resource") or "?") + " | 标题:" + str(p.get("title") or "?")
'''
# 通用替换:把 plan_text 的拼接行改为 _plan_text
t = re.sub(r"plan_text = \(\"资源:\"[^\n]*\n[^\n]*\n", "_plan_text(p)\n", t)
f.write_text(t, encoding="utf-8")
print("①②③④ 完成(prompt/解析/helper/定时路)")
