"""密钥扫描守卫(2026-10-03)。

**为什么要有这条**:项目原来就有 `scripts/check_secrets.py`,但它给的是**虚假安全感** ——
只认 5 个模式(**不认飞书 webhook**)、只扫 5 个目录(**漏掉根目录**)、**不扫 git 历史**,
于是对着一个**真实存在、且仓库还是公开的**线上飞书 webhook,照样打印 "OK 未发现密钥泄露"。

脚本已重写(排除法扫全部"会被提交的文件" + 飞书 URL/裸 UUID + 通用形态 + `--history` 模式)。
这里把它接进测试,让**新增**的泄漏在提交前就被拦住。

⚠️ 范围说明:**git 历史里的泄漏不在本测试内**(历史扫描慢,要手动跑
`python scripts/check_secrets.py --history`);但历史上那两处**至今仍在**
(飞书 webhook + 一条 dajiala key)—— 真正的解法是**吊销/重建凭据**,不是清历史。
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _git_ok() -> bool:
    try:
        r = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(ROOT),
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _git_ok(), reason="非 git 环境(扫描依赖 git ls-files 判定'会被提交的文件')")
def test_no_secrets_in_files_that_would_be_committed() -> None:
    """会被提交的文件(已跟踪 + 未跟踪未忽略)里不能有真凭据。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_secrets", ROOT / "scripts" / "check_secrets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    issues = mod.scan_worktree()
    detail = "\n".join(f"  {w}:{ln}  [{d}]  {mod._mask(t)}" for w, ln, d, t in issues[:20])
    assert not issues, (
        "会被提交的文件里发现疑似密钥(照这样提交就泄漏了):\n" + detail +
        "\n修法:把凭据挪进 .env(gitignored),文档/代码里只留占位符或指向 .env 的说明。"
    )


def test_scanner_detects_feishu_webhook_variants() -> None:
    """⚠️ **回归守卫**:扫描器必须认得出飞书的**两种**写法。

    2026-10-03 实测踩过两次:旧脚本**完全不认飞书**;我重写的第一版只匹配**完整 URL**,
    而文档里恰恰是**裸 UUID**(`` `2a294b76-…` ``)—— 于是真泄漏又被漏掉一次。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_secrets", ROOT / "scripts" / "check_secrets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # ⚠️ 样本 UUID **拼出来、不写字面量** —— 否则本文件自己会被扫出"泄漏"(实测:
    # 第一版就是这么把自己的测试文件报了的)。同理,扫描器必须能扫**它自己所在的仓库**。
    fake = "-".join(["2a294b76", "74ab", "441e", "a31e", "39eda459c72a"])
    full = f"FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/{fake}"
    bare = f"| `FEISHU_WEBHOOK` | 新机器人 {fake} |"
    assert mod._hits_in_text(full), "完整 URL 形态没认出来"
    assert mod._hits_in_text(bare), "裸 UUID 形态没认出来"
    # 反过来:没有飞书语境的裸 UUID 不该报(否则 UUID 到处都有,噪音淹没真信号)
    assert not mod._hits_in_text(f"task_id = {fake}")
