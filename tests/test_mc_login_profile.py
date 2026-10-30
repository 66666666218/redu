"""**登录窗口的档案目录**必须与"导出写库"读的目录一致(2026-10-30)。不联网、不开浏览器。

## 守的是什么

微博的重登流程被人写成两步:

    ① `tools/mc_login.py weibo`      ← 开窗口,人在里面扫码
    ② `python tools/weibo_export_cookie.py`  ← 从档案读 cookie、验活、写库

而 ① 原来按平台名拼目录 ⇒ `cdp_weibo_user_data_dir`,
② 读的是 MediaCrawler 认的那一份 ⇒ `cdp_wb_user_data_dir`(`wb` 是微博在 MediaCrawler 里的代号)。
**两个目录** ⇒ 人扫完码,② 读到的仍是旧 cookie,报「没登录」——
与 2026-10-13 那次「叫人去扫码,结果库里那枚还是旧的」是同一类**白跑**。

这里把这条不变量钉死:**微博的登录档案必须就是导出工具读的那一份**。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
#: ⚠️ **`tools/` 整个目录在 `.gitignore` 里**(见 `.gitignore:43`)—— 这几个工具**不在仓库里**,
#: 别人的克隆/CI 上没有它们。所以这组测试是"有文件才跑",没有就跳过:
#: 它守的是本机那份工具的不变量,不该因为文件不存在就红。
_TOOLS_MC_LOGIN = ROOT / "tools" / "mc_login.py"
_TOOLS_EXPORT = ROOT / "tools" / "weibo_export_cookie.py"

pytestmark = pytest.mark.skipif(
    not (_TOOLS_MC_LOGIN.exists() and _TOOLS_EXPORT.exists()),
    reason="tools/ 不在仓库里(.gitignore),本组只守本机那份工具")


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mc_login():
    #: 模块顶层 import playwright —— 它只做 import,不启动浏览器,测试里是安全的
    return _load("tools_mc_login", "tools/mc_login.py")


def _profile_name(mc_login, plat: str) -> str:
    """复刻 `main()` 里拼目录的那一行(不启动浏览器,也就不用 mock Playwright)。"""
    return mc_login.PROFILE_DIRS.get(plat, f"cdp_{plat}_user_data_dir")


def test_微博登录档案就是导出工具读的那一份(mc_login):
    export = _load("tools_weibo_export_cookie", "tools/weibo_export_cookie.py")
    assert _profile_name(mc_login, "weibo") == export.PROFILE.name, (
        "登录窗口与导出工具用了两个不同的档案目录 —— 扫完码导出会读成「没登录」。"
        f"登录={_profile_name(mc_login, 'weibo')} 导出={export.PROFILE.name}"
    )
    assert export.PROFILE.parent.name == "browser_data"


def test_微博登录档案与扫码登录脚本一致(mc_login):
    """`scripts/weibo_login.py` 是**一步到位**那条路(开窗→验活→写库),档案也得对上。"""
    login = _load("scripts_weibo_login", "scripts/weibo_login.py")
    assert _profile_name(mc_login, "weibo") == login.DEFAULT_PROFILE.name


def test_其余平台仍按平台名拼(mc_login):
    """只在**对不上号**的平台登记例外,别顺手把 xhs/tieba 也改了(它们的档案就叫 cdp_xhs/cdp_tieba)。"""
    assert _profile_name(mc_login, "xhs") == "cdp_xhs_user_data_dir"
    assert _profile_name(mc_login, "tieba") == "cdp_tieba_user_data_dir"
    assert "xhs" not in mc_login.PROFILE_DIRS and "tieba" not in mc_login.PROFILE_DIRS
