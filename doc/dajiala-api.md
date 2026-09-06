# 大家拉/极致了数据(dajiala.com)公众号 API 接入规范

> 2026-09-07 实测破解。来源:控制台"使用示例"页(登录态)+ 真实调用验证。
> key 配置于 `.env` 的 `DAJIALA_KEY`(形如 `JZLxxxx`);控制台账号 16653036216。
> ⚠️ 两个天坑(都踩过):① 部分接口收 **JSON body**、部分收 **form 表单**,写错不报错但返回空**照样扣费**;
> ② 参数格式错误的调用(校验码 100/30001/20002)**不扣费**,但"格式合法但值不对"的调用**会扣费**(如 104)。

## 通用约定

- Base:`https://www.dajiala.com/fbmain/monitor/v3/`
- 鉴权:每个请求带 `key`
- QPS:**≤2 次/秒**,超限返回 `-1`(提示 5 秒后再试)
- 通用响应信封:`{"code": 0, "msg": ..., "cost_money": 本次扣费, "remain_money": 余额, "data": ...}`
- 错误码:`-1`QPS超限;`100`缺参数;`101`文章已删;`102`获取原始id失败;`103`请求失败;`104`原始id错误(扣费);
  `105`网络错误重试1~3次;`10002`key有误;`20001`金额不足;`20002`请输入微信链接;`30001`请传文章链接或原始id
- 计费规则:充值后十天内每日消费不足 1 元,第 11 天起按 1 元/天保底补扣 → 月成本地板 ≈¥30
- 余额查询(免费)可做运维告警

## 1. get_remain_money — 账户余额(免费,已实测✅)

```
POST {BASE}/get_remain_money        # form 表单
data={"key": KEY}
→ {"code":0, "remain_money":1.0, "yesterday_money":1.0, "request_time":"..."}
```

## 2. post_condition — 公众号当天发文(¥0.14/次,已实测✅)

```
POST {BASE}/post_condition          # form 表单
data={"key": KEY, "url": "<文章长链接>"}   # ⚠️实测传公众号名称被拒(20002),必须传微信链接
→ {"code":0, "nickname":"微信派", "ghid":"gh_bc5ec2ee663f", "_type":"1",
   "data":[...当天全部发文...], "msg":"当天没有发文!", ...}
```
- 一次返回**当天全部发文**,不可翻页 → 监控主力:每号每天 1~2 次。
- `data` 非空时的字段结构未实测(测的时候该号当天没发文),部署后首轮采集确认。

## 3. read_zan_pro — 单篇流量六指标(¥0.06/次,已实测✅)

```
POST {BASE}/read_zan_pro            # form 表单
data={"key": KEY, "url": "<文章长链接>"}
→ {"code":0, "data":{"read":100001, "zan":449, "looking":341,
                     "share_num":6, "collect_num":0, "comment_count":100}, ...}
```
- 精确值;历史发文列表自带的 Read/Zan 超过 1 万不精确("1.2万"),精确采样必须走本接口。
- 同一篇重复调用重复扣费:项目侧按 (url, 采样点) 去重。
- 便宜的 `read_zan`(¥0.04)只有 read/zan/looking,丢转发/收藏/评论,不推荐——转发是网盘引流核心指标。

## 4. web_search — 搜一搜实时搜文章(¥0.5/次,文档已破解,未实测)

```
POST {BASE}/web_search              # ⚠️ JSON body(json=data),不是表单!
json = {
  "key": KEY,
  "keyword": "夸克网盘",     # 搜索词
  "mode": 1,                # 固定 1 = 搜「全部」里的公众号文章
  "currentPage": 1,
  "offset": 0,              # 首页 0;翻页传上一页返回的 offset
  "publish_time_type": 1,   # 0不限 / 1最近1天 / 2最近7天 / 3最近半年  ← 时效过滤
  "search_type": 1,         # 1 = 文章
  "sort_type": 1            # 0综合 / 1最新 / 2最热            ← 排序
}
→ data: [ { boxID, items: [ { title, desc, doc_url, thumbUrl,
      source:{dateTime:"2分钟前", title:"公众号名"},
      bizUin, srcUserName:"gh_xxx"(原始ID), timestamp } ],
      totalCount, real_type, type } ],
  continueFlag(1可翻页/0结束), cookies(翻页用), offset, pageNumber, query
```
- **一条结果同时带文章(标题/链接/摘要/时间)和账号(名称+原始ID)** → 批量采号/补池的入口:
  搜"夸克网盘"等词 → 取 `srcUserName` 去重 → 入对标候选池。
- 监控"最新发文"场景用 `publish_time_type=1 & sort_type=1`。
- 2026-09-07 学费:用 form 表单传 `word` 参数,code=0 但空数据,被扣 ¥0.5 —— 正确格式即上。

## 5. history_by_ghid — 历史发文列表 Pro(¥0.14/次,文档已破解,未实测)

```
POST {BASE}/history_by_ghid         # ⚠️ JSON body(json=payload)
payload = {"key": KEY, "ghid": "gh_xxx 或空", "url": "<文章链接 或空>", "offset": "<翻页参数或空>"}
# ghid / url 二选一;offset 取上一页返回的 PagingInfo.Offset,首页传空
→ {
  "AccountInfo": {"UserName"(原始id), "NickName", "ServiceType"(1服务号/0订阅号), "HeadImgUrl"},
  "HasArtile": bool,
  "MsgList": {"Msg": [ {"AppMsg": {
      "BaseInfo": {"Type": 9有通知/1002无通知发文},
      "DetailInfo": [ { "Title", "Digest", "ContentUrl"(文章长链), "ItemIndex"(发文位置),
        "SourceUrl"(阅读原文), "CoverImgUrl*", "IsOriginal"(1原创),
        "ItemShowType"(0图文/5视频/8小绿书/10文字/11转载), "send_time"(时间戳),
        "Read", "Zan"   # ⚠️>1万不精确("1.2万"),精确值走 read_zan_pro
      } ] } } ] },
  "PagingInfo": {"Offset"(翻页参数), "IsEnd"(0可翻页/1结束)}
}
```
- 每页 10 次发文(每次 1~8 篇);"一键同步账号所有文章" = 循环翻页直到 `IsEnd=1`。
- 控制台页面上方的参数表(keyword/key/verifycode)是复制粘贴错误,**以 Python 示例为准**。

## 6. article_detail — 文章正文(改写素材;长链 ¥0.01 / 短链 ¥0.03,文档已破解)

```
GET {BASE}/article_detail?key=KEY&url=<文章链接>&mode=1
# mode: 1=带img标签图片 / 2=不带图片 / 不传=返回 |@@| 分割的图片列表
# ⚠️ 长链接 ¥0.01/次,短链接 ¥0.03/次 —— 用 history 的 ContentUrl(长链)最省
→ {"code":0, "msg":"OK", "data":{"title":..., "url":..., "content":<纯文本正文>}}
```

## 集成映射(redian 公众号监听管线)

```
[发现/补池] web_search("夸克网盘"等词, 最新+近1天)
              → srcUserName 去重 → 对标候选池(≥30 个日更号保底,不足自动补)
[活性验证]  post_condition(候选号文章链) → 有发文者入池,连7天空数据标记沉睡
[监听]      每号每天 2 次 post_condition → 新文(标题粗筛:夸克/百度网盘/UC/迅雷/网盘/链接)
              → article_detail(长链 ¥0.01) 取正文 → 本地正则
              pan.quark.cn/s/ | pan.baidu.com/s/ | drive.uc.cn/s/ | pan.xunlei.com/s/
              → 确认带盘链 → 入库 + 飞书推送
[流量]      read_zan_pro 采样(发布后 ~2h/~24h 两点) → trend_analyzer 判涨/爆文
[全量同步]  history_by_ghid 循环翻页 → wechat_articles → 改写素材(article_detail)
[改写]      dajiala 不提供;正文取回后接 LLM(另计费)或规则改写
[运维]      get_remain_money 免费轮询 → 余额低于阈值推飞书
```

## 成本基线(30 个日更号 × 每天 2 次)

| 项 | 计算 | 月成本 |
| --- | --- | --- |
| 监听发文 | 30×2×0.14×30 | ¥252 |
| 正文盘链确认 | 30号×3篇×0.01×30(长链价) | ≈¥27 |
| 流量采样 | 20篇/天×2点×0.06×30 | ≈¥72 |
| 补池发现 | ≈¥4/周 | ≈¥17 |
| **稳态合计** | | **≈¥368/月**(首月另加历史同步一次性 ¥13~150 视深度) |

key 当前余额 ¥0.16(2026-09-07,累计测试消费 ¥0.84),上线前需充值(最低 ¥5)。
