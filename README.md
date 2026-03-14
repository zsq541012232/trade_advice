# trade_advice

基于 **AIHUBMIX + DuckDuckGo + 行情源** 的股票投资建议脚本。会先抓取新闻/舆情/财报/技术指标相关信息，再由大模型输出：

- 短线建议（1天~2周）
- 长线建议（3个月~3年）

> ⚠️ 仅供研究与学习，不构成投资建议。

---

## 1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. 配置环境变量

先复制模板：

```bash
cp .env.example .env
```

至少需要：

- `AIHUBMIX_API_KEY`
- `STOCK_CODES`（例如 `AAPL,TSLA,600519.SS`）或 `EMAIL_STOCK_ROUTER`

常用可选项：

- `AIHUBMIX_BASE_URL`（默认 `https://api.aihubmix.com/v1`）
- `AIHUBMIX_MODEL`（默认 `gpt-4o-mini`）
- `DUCKDUCKGO_MAX_RESULTS`（默认 `5`）
- `DUCKDUCKGO_REGION`（默认 `zh-cn`）
- `MARKET_DATA_PROVIDER`（默认 `auto`，可选 `yahoo` / `eastmoney` / `akshare`）
- `EMAIL_STOCK_ROUTER`（例如 `a@xx.com:AAPL,TSLA;b@xx.com:MSFT`）
- `SENDER_EMAIL`（发送邮箱）
- `SENDER_AUTH_CODE`（发送邮箱授权码）
- `SMTP_HOST`（可选，默认按邮箱域名自动推断）
- `SMTP_PORT`（默认 `465`）

> A 股代码建议优先写纯数字（如 `600900`、`000001`）。脚本会自动扩展为 `600900.SH` / `SH600900` 等别名提高检索命中率。

## 3. 本地运行

```bash
set -a && source .env && set +a
python adviser.py
```

如需 JSON 输出：

```bash
python adviser.py --pretty-json
```

## 4. 邮件分组发送

当配置 `EMAIL_STOCK_ROUTER` 后，脚本会在分析完成后自动发送分组邮件：

- 一个邮箱可绑定多只股票
- 不同邮箱可接收不同股票集合
- 发送方账号来自 `SENDER_EMAIL` + `SENDER_AUTH_CODE`

示例：

```bash
EMAIL_STOCK_ROUTER="a@example.com:AAPL,TSLA;d@example.com:MSFT,NVDA"
```

## 5. 行情/技术指标来源

仅依赖搜索引擎时，可能出现“只有新闻，缺少结构化技术指标”的问题。项目已接入直连行情源：

- `Yahoo Finance`：适合美股及常见国际代码，也支持 `600900.SS` / `000001.SZ`。
- `东方财富`：适合 A 股 6 位代码，自动拉取日线并本地计算 `RSI14`、`MACD`、`KDJ`。
- 默认 `MARKET_DATA_PROVIDER=auto`：A 股优先东方财富，其它代码优先 Yahoo。

## 6. 数据时效规则

- 检索阶段仅接纳最近约 3 个月（92 天）内且可解析日期的新闻。
- 提示词强制要求大模型仅按“最新可得股价和技术指标”给出建议；若无法确认最新性，需明确标注“数据不足”。

## 7. GitHub Actions 自动运行（含定时）

仓库内置 `.github/workflows/ci.yml`，支持两种触发：

1. **手动触发（`workflow_dispatch`）**
   - 在 Actions 页面点击 Run workflow 即可立即执行。

2. **定时触发（`schedule`）**
   - 工作流在**周一到周五每 5 分钟**触发一次。
   - 仅在当前时间命中 `RUN_ADVICE_TIME` 时真正执行分析。
   - 时间格式为 `HH:MM`（24 小时制），例如设置 `18:00` 表示“每天 18:00 执行”。

### 定时配置示例（交易日收盘后 3 小时）

假设你的市场收盘时间为 15:00，那么可配置：

- `RUN_ADVICE_TIME=18:00`
- `RUN_ADVICE_TIMEZONE=Asia/Shanghai`（可选，默认即 `Asia/Shanghai`）

### Actions 里需要配置的 Secrets / Variables

- `Settings -> Secrets and variables -> Actions -> Secrets`
  - `AIHUBMIX_API_KEY`
  - `SENDER_EMAIL`（如需发邮件）
  - `SENDER_AUTH_CODE`（如需发邮件）

- `Settings -> Secrets and variables -> Actions -> Variables`
  - `STOCK_CODES`（或改用 `EMAIL_STOCK_ROUTER`）
  - `RUN_ADVICE_TIME`（可选，默认 `18:00`）
  - `RUN_ADVICE_TIMEZONE`（可选，默认 `Asia/Shanghai`）
  - `MARKET_DATA_PROVIDER`（可选：`auto`/`yahoo`/`eastmoney`/`akshare`）
  - （可选）`AIHUBMIX_BASE_URL`
  - （可选）`AIHUBMIX_MODEL`
  - （可选）`EMAIL_STOCK_ROUTER`
  - （可选）`DUCKDUCKGO_MAX_RESULTS`
  - （可选）`DUCKDUCKGO_REGION`
  - （可选）`SMTP_HOST`
  - （可选）`SMTP_PORT`

> 脚本内部统一通过 `os.getenv(...)` 读取环境变量，因此本地 shell、`.env`、以及 GitHub Actions 的 job `env` 都会生效。

## 8. 输出格式

每只股票会输出结构化建议，至少包含：

- 方向（买入/持有/减仓/观望）
- 仓位建议（百分比）
- 触发条件
- 止损/风控
- 关键依据
- 风险提示

运行过程中会打印进度日志（检索阶段、每条 query 命中数量、AI 生成阶段），方便快速定位“无检索结果”的原因。
