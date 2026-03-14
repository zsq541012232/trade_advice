# trade_advice

基于 **AIHUBMIX + DuckDuckGo** 的股票投资建议脚本。会先抓取新闻/舆情/财报/技术指标相关搜索信息，再由大模型直接给出：

- 短线建议（1天~2周）
- 长线建议（3个月~3年）

> ⚠️ 仅供研究与学习，不构成投资建议。

## 1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. 配置环境变量

复制并编辑：

```bash
cp .env.example .env
```

最少需要：

- `AIHUBMIX_API_KEY`
- `STOCK_CODES`（例如 `AAPL,TSLA,600519.SS`）或 `EMAIL_STOCK_ROUTER`

你也可以自定义：

- `AIHUBMIX_BASE_URL`（默认 `https://api.aihubmix.com/v1`）
- `AIHUBMIX_MODEL`（默认 `gpt-4o-mini`）
- `DUCKDUCKGO_MAX_RESULTS`（默认 `5`）
- `DUCKDUCKGO_REGION`（默认 `zh-cn`）
- `EMAIL_STOCK_ROUTER`（例如 `a@xx.com:AAPL,TSLA;b@xx.com:MSFT`，用于按邮箱分组发送）
- `SENDER_EMAIL`（发送邮箱）
- `SENDER_AUTH_CODE`（发送邮箱授权码）
- `SMTP_HOST`（可选，默认按发送邮箱域名自动推断）
- `SMTP_PORT`（默认 `465`）

> A 股代码建议优先写纯数字（如 `600900`、`000001`）。脚本会自动扩展为 `600900.SH` / `SH600900` 等别名提高检索命中率。


## 邮件分组发送

当配置 `EMAIL_STOCK_ROUTER` 后，脚本会在分析完成后自动发送分组邮件：

- 一个邮箱可绑定多只股票
- 不同邮箱可接收不同股票集合
- 发送方账号从 `SENDER_EMAIL` + `SENDER_AUTH_CODE` 读取

示例：

```bash
EMAIL_STOCK_ROUTER="a@example.com:AAPL,TSLA;d@example.com:MSFT,NVDA"
```

## 数据时效规则

- 检索阶段仅接纳最近约 3 个月（92 天）内且可解析日期的新闻。
- 提示词强制要求大模型仅按“最新可得股价和技术指标”给出建议；若无法确认最新性，需明确标注“数据不足”。

## 3. 本地运行

```bash
set -a && source .env && set +a
python adviser.py
```

如需附加输出 JSON：

```bash
python adviser.py --pretty-json
```

## 4. GitHub Actions 运行

仓库已提供 `.github/workflows/ci.yml`，包含两类任务：

1. **CI 测试任务（push / pull_request）**
   - 安装依赖
   - 运行 `pytest`
   - 运行 `python adviser.py --help` 冒烟检查

2. **手动执行建议任务（workflow_dispatch）**
   - 读取 GitHub Actions 的 `Secrets` / `Variables` 作为环境变量
   - 执行 `python adviser.py --pretty-json`

请在仓库设置中配置：

- `Settings -> Secrets and variables -> Actions -> Secrets`
  - `AIHUBMIX_API_KEY`
- `Settings -> Secrets and variables -> Actions -> Variables`
  - `STOCK_CODES`（或改用 `EMAIL_STOCK_ROUTER`）
  - （可选）`AIHUBMIX_BASE_URL`
  - （可选）`AIHUBMIX_MODEL`
  - （可选）`EMAIL_STOCK_ROUTER`
  - （可选）`DUCKDUCKGO_MAX_RESULTS`
  - （可选）`DUCKDUCKGO_REGION`

> 脚本内部统一通过 `os.getenv(...)` 读取环境变量，因此本地 shell、`.env`、以及 GitHub Actions 的 job `env` 都会生效。

## 输出格式

每只股票会输出结构化建议，至少包含：

- 方向（买入/持有/减仓/观望）
- 仓位建议（百分比）
- 触发条件
- 止损/风控
- 关键依据
- 风险提示

运行过程中会打印进度日志（检索阶段、每条 query 命中数量、AI 生成阶段），方便快速定位“无检索结果”的原因。
