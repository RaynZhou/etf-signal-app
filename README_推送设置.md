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
- 盘前 08:50
- 盘中 11:00
- 盘后 15:20
（均为北京时间）

### 自选列表（当前）
159545、159941、513120、159330、159699、513330
债市：270044
其他：270023
