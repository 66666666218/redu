"""文章内容交叉提取:从已监控文章正文里挖出新公众号/搜索词,反哺候选发现。

网盘引流文的正文套路高度一致:
- "关注公众号「XXX」获取更多资源"
- "搜索公众号:XXX"
- "更多网盘资源请关注 XXX"

这些**文中提到的其他公众号**就是天然的新对标号候选——同行在互相引流时,
等于帮我们筛选出了"也在做网盘资源"的账号。交叉提取这些名字,
与已知对标号去重后自动入库为新候选,实现"监听越多→发现越多"的滚雪球效应。
"""
from __future__ import annotations

import re
from collections import Counter

from app.utils import get_logger

logger = get_logger(__name__)

# 引流模式(按精度排列)
_ACCOUNT_PATTERNS = [
    # 关注/搜索/扫码 + [公众号] + [ delimiter ] + 名称
    re.compile(
        r"(?:关注|搜索|扫码关注|点击关注|推荐关注|长按关注|扫描关注)"
        r"\s*(?:公众号|微信公众号|微信)?\s*"
        r"[「『【\"'：:]?\s*"
        r"([\u4e00-\u9fff][\u4e00-\u9fff0-9A-Za-z_]{1,29})"
    ),
    # 公众号：XXX / 公众号:XXX(独立匹配)
    re.compile(
        r"公众号\s*[「『【\"'：:]\s*"
        r"([\u4e00-\u9fff][\u4e00-\u9fff0-9A-Za-z_]{1,29})"
    ),
    # 更多/全套 + 关注 XXX
    re.compile(
        r"(?:更多|海量|全套|全部)[^\n]{0,20}?"
        r"(?:关注|扫码关注)\s*[「『【\"]?\s*"
        r"([\u4e00-\u9fff][\u4e00-\u9fff0-9A-Za-z_]{1,29})"
    ),
]
_REF_EXCLUDE = re.compile(
    r"^(?:我们|小编|作者|官方|平台|系统|大家|用户|朋友|老师|同学|博主|up主|本号|此号"
    r"|该|此|以上|下方|文中|如图|详见|关注|扫码|点击|扫描|长按|公众号|微信|获取|查看|更多|持续|后续)"
)
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")


def _clean_ref(name: str) -> str:
    name = re.sub(r"^(?:公众号|微信公众号|微信)[「『【\"'：:]*", "", name)
    name = re.sub(r"[」』】\"'：:]+$", "", name)
    return name.strip()


def extract_account_refs(content: str) -> set[str]:
    """从文章正文提取被提及/引流的公众号名称(清洗+去重)。"""
    if not content:
        return set()
    refs: set[str] = set()
    for pat in _ACCOUNT_PATTERNS:
        for m in pat.finditer(content):
            name = _clean_ref(m.group(1).strip())
            if 2 <= len(name) <= 30 and _HAS_CJK.search(name) and not _REF_EXCLUDE.match(name):
                refs.add(name)
    return refs


def extract_content_keywords(content: str, top: int = 5) -> list[str]:
    """从正文提取高频内容词(用于搜索发现更多同类文章)。"""
    if not content:
        return []
    clean = re.sub(r"https?://\S+", " ", content)
    clean = re.sub(r"[^\w\u4e00-\u9fff]+", " ", clean)
    counter: Counter = Counter()
    for w in clean.split():
        if 4 <= len(w) <= 12 and len(_HAS_CJK.findall(w)) >= 2:
            counter[w] += 1
    return [w for w, n in counter.most_common(top) if n >= 2]
