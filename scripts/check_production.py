"""生产就绪自检:部署后一键验证配置/模块/数据表。

用法: python scripts/check_production.py
退出码: 0=就绪;1=有缺失(按输出逐项修复)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

issues = []

from pathlib import Path

env_path = Path(".env")
env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
required = {
    "JWT_SECRET": "登录密钥",
    "DATABASE_URL": "数据库连接",
    "WEREAD_COOKIE": "微信读书 Cookie(免费监听源)",
    "DEEPSEEK_API_KEY": "DeepSeek key(LLM 叙事/改写)",
    "QUARK_COOKIE": "夸克 Cookie(自动转存)",
    "FEISHU_WEBHOOK": "飞书总群 webhook",
}
optional = {
    "DAJIALA_KEY": "dajiala(阅读量采样,不充可跳过)",
    "FEISHU_WEBHOOK_WECHAT": "公众号专属群(空则推总群)",
    "XIANYU_PROXY_URL": "闲鱼固定代理(风控时配置)",
}
NL = chr(10)
print("== 1. 配置 ==")
for key, desc in {**required, **optional}.items():
    val = ""
    for line in env.splitlines():
        if line.strip().startswith(key + "="):
            val = line.split("=", 1)[1].strip()
            break
    ok = bool(val)
    tag = "OK " if ok else ("MISS" if key in required else "OPT ")
    print(f"  {tag} {key:24s} {desc}")
    if not ok and key in required:
        issues.append(f"缺少 {key}: {desc}")

print(NL + "== 2. 模块 ==")
mods = ["app.services.weread_client", "app.services.dajiala_client", "app.services.quark_transfer",
        "app.services.llm_client", "app.services.content_extract", "app.services.wechat_monitor",
        "app.services.early_agent", "app.services.focus_alert", "app.services.agent_learning",
        "app.services.reader_platform_client"]
for mod in mods:
    try:
        __import__(mod)
        print(f"  OK  {mod}")
    except Exception as e:
        print(f"  FAIL {mod}: {e}")
        issues.append(f"模块 {mod}: {e}")

print(NL + "== 3. 数据表 ==")
try:
    from sqlalchemy import create_engine
    from sqlalchemy.inspection import inspect as _insp
    from config.settings import get_settings
    engine = create_engine(get_settings().database_url)
    existing = set(_insp(engine).get_table_names())
    for t in ["wechat_benchmarks", "wechat_articles", "wechat_pan_links",
              "wechat_traffic_samples", "agent_stages", "wechat_candidates", "user_schedules"]:
        ok = t in existing
        print(f"  {'OK ' if ok else 'MISS'} {t}")
        if not ok:
            issues.append(f"缺表 {t}(启动服务自动建表)")
except Exception as e:
    print(f"  FAIL 数据库: {e}")
    issues.append(f"数据库: {e}")

print(NL + "== 4. 外部连通(可选)==")
try:
    import requests
    r = requests.get("https://weread.qq.com", timeout=8)
    print(f"  {'OK ' if r.status_code < 500 else 'WARN'} weread.qq.com HTTP {r.status_code}")
except Exception as e:
    print(f"  WARN weread.qq.com 不可达: {e}")

print(NL + "=" * 50)
if issues:
    print(f"FAIL {len(issues)} 个问题:")
    for i in issues:
        print(f"  - {i}")
    sys.exit(1)
print("ALL OK - 生产就绪(可选配置按需补充)")
sys.exit(0)
