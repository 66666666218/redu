# MediaCrawler:显式给浏览器出口 + 浏览器 channel 探测(2026-10-22)

`tools/MediaCrawler/` 是 gitignore 的第三方代码,这里留一份改法说明 ——
重装/更新 MediaCrawler 后按它对一遍,否则两个故障会原样回来。

## 为什么改

同一天撞到两个**叠在一起**的故障,报错都长得像"浏览器起不来":

1. **死系统代理**。代理软件退出后 Windows 注册表残留 `ProxyEnable=1` 指向
   `127.0.0.1:7892`;MediaCrawler 的 Chromium 不传 `proxy` 参数,于是自己去探测系统代理
   ⇒ `net::ERR_PROXY_CONNECTION_FAILED`(整平台退出码 1)。
2. **`channel="chrome"`**。本机只装了 Edge,`chromium.launch_persistent_context(channel="chrome")`
   ⇒ `Chromium distribution 'chrome' is not found`。

## 改法

### A. 浏览器出口(每个平台两个 launch 点)

持久化上下文:

```python
browser_context = await chromium.launch_persistent_context(
    user_data_dir=user_data_dir,
    accept_downloads=True,
    headless=headless,
    proxy=playwright_proxy,
    # 2026-10-22:显式给出口(系统代理可能是死端口)
    args=None if playwright_proxy else ["--proxy-server=direct://"],
    ...
)
```

非持久化:

```python
browser = await chromium.launch(
    headless=headless, proxy=playwright_proxy, channel=utils.browser_channel(),
    args=None if playwright_proxy else ["--proxy-server=direct://"])
```

涉及文件:`media_platform/{bilibili,kuaishou,tieba,weibo,zhihu,xhs,douyin}/core.py`。

⚠️ **`--no-proxy-server` 不生效,`--proxy-server=direct://` 才生效**
(Playwright 1.61 / 1.63 各测一遍,三组对照;不传参数则 `ERR_PROXY_CONNECTION_FAILED`)。

### B. 浏览器 channel

`tools/utils.py` 新增:

```python
def browser_channel():
    """Chrome → Edge → None(用 Playwright 自带 Chromium)。"""
    import os
    cands = (
        ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ("chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ("msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ("msedge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    )
    for channel, path in cands:
        if os.path.exists(path):
            return channel
    return None
```

把 5 个平台的 `channel="chrome"` 换成 `channel=utils.browser_channel()`
(这些文件都已经 `from tools import utils`)。

## 怎么确认改没改丢

```bash
grep -rn 'channel="chrome"' tools/MediaCrawler/media_platform/          # 应为空
grep -rn 'proxy-server=direct://' tools/MediaCrawler/media_platform/ | wc -l   # 应为 14
tools/MediaCrawler/.venv/Scripts/python.exe -m py_compile \
  tools/MediaCrawler/media_platform/*/core.py
```
