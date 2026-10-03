"""前端静态检查守卫(2026-10-03)。

**为什么加**:这个项目此前**完全没有前端 lint** —— 构造上只有 `vite build`,而
**构建通过 ≠ 模板正确**(未声明的变量会整页白屏,构建照样成功;见 memory)。
今天补了 `eslint-plugin-vue`,第一次跑就抓出**两个真问题**:

  1. `WechatListen.vue` 的 `<textarea>{{ rewriteText }}</textarea>` —— Vue 官方明确说
     textarea 里别用插值(渲染不出来时用户看到的是**一个空框**,而"AI 改写稿"正靠它显示);
  2. `Admin.vue` 的 `重试<3次` —— 裸 `<` 被 HTML 词法器当成**标签开头**
     (`invalid-first-character-of-tag-name`),属于未定义行为。

这里把 `npx eslint src` 接进测试:以后**新增**的这类问题在提交前就会被拦住。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def _ready() -> bool:
    """node/npm/依赖都在才能跑 —— 缺了就 skip(CI 里没有 node 不该算失败)。"""
    if not (FRONTEND / "node_modules" / "eslint").exists():
        return False
    if not (FRONTEND / "eslint.config.js").exists():
        return False
    if shutil.which("npx") is None:
        return False
    return (FRONTEND / "node_modules" / ".bin").exists()


@pytest.mark.skipif(not _ready(), reason="前端依赖未安装(node_modules/eslint 不在)")
def test_frontend_passes_eslint() -> None:
    """`npx eslint src` 必须零错误 —— 抓的是**会出错**的规则,不是风格。"""
    try:
        proc = subprocess.run(["npx", "eslint", "src"], cwd=str(FRONTEND),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="ignore", timeout=300, shell=True)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"eslint 跑不起来:{exc}")
    assert proc.returncode == 0, (
        "前端 ESLint 报错(这些是会真出错的问题,不是风格):\n"
        + (proc.stdout or "")[-2500:] + (proc.stderr or "")[-500:]
    )
