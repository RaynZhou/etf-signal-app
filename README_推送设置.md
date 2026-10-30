# ETF信号助手 - 微信定时推送设置说明

## 文件说明
- `daily_etf_push.py` ：分析 + 推送主脚本
- `daily_push.yml` ：GitHub Actions 定时配置

## 设置步骤

### 1. 把文件放到你的 GitHub 仓库

建议结构：
```
你的仓库/
├── etf_signal_app.py          # 网页版（已有）
├── requirements.txt
├── daily_etf_push.py          # 新增
└── .github/
    └── workflows/
        └── daily_push.yml     # 新增
```

### 2. 添加 SendKey 到 GitHub Secrets（重要！）

1. 打开你的仓库 → Settings → Secrets and variables → Actions
2. 点击 New repository secret
3. Name 填写：`SERVERCHAN_SENDKEY`
4. Secret 填写：你的 SendKey（SCT开头的那串）
5. 点击 Add secret

### 3. 启用 GitHub Actions

如果是新仓库，可能需要在 Actions 页面点击启用。

### 4. 测试

在仓库的 Actions 页面，找到 “Daily ETF Signal Push”，点击 “Run workflow” 手动运行一次，检查微信是否收到消息。

### 推送时间（工作日）
- 盘中 11:00–11:30 窗口内
- 盘后 15:20–15:50 窗口内
（均为北京时间）

原来还有一条盘前 08:50。删掉它不是为省钱：盘前拿不到当日 K 线，它的数据基准和结论
跟昨晚盘后那条逐字相同，只是把同一句话再说一遍，却占掉 Server酱每天 5 条额度里的 1 条。
现在一天两条，腾出的额度留给真正需要重试的时候。

### 5. 首次部署后不用做任何手动初始化

（这一节是给“直接替换文件”的人看的，看完可以删）

不用手动跑 `--backfill-prem`。盘后那条推送在写台账前会检查「哪些标的还没有溢价基准」，
缺的那几只自动去拉 90 天净值历史，补进 `signals.json` 的 `prem_hist` 字段；建好之后
`ensure_prem_history()` 直接返回，一次请求都不再发。所以第一次盘后推送会比平时慢
1–2 分钟，之后恢复正常。

`signals.json` 里旧的 `c` 字段（当日收盘，已不再写入）、新加的 `sig_date` 和 `prem_hist`
同时存在是正常的，读的时候会忽略不认识的字段。不懂 JSON 结构的话不要手动编辑它——
台账只会在盘后那条 workflow 里被覆盖写，手改很容易把整个文件写坏导致停止累积。

### 自选列表（当前）
159545、159941、513120、159330、159699、513330
债市：270044
其他：270023
