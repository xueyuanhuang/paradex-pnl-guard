# Paradex P&L Guard

本项目用于监控 Paradex 账户的【总浮动盈亏（Total Unrealized P&L）】，并提供阈值报警和定时交易提醒功能。

## 功能说明

1. **总浮动盈亏监控与阈值报警**
   - 每分钟（默认 60 秒）通过 REST API 获取账户所有仓位信息。
   - 自动计算所有 OPEN 状态仓位的未实现盈亏总和。
   - 系统使用状态机追踪 PnL 区间：`NORMAL`（正常）/ `ABOVE`（超上限）/ `BELOW`（超下限）。
   - 当 PnL 从 `NORMAL` 进入 `ABOVE` 或 `BELOW` 状态时，立即通过 Telegram 发送阈值报警。
   - 仅在状态变化时触发报警，持续停留在同一状态不会重复报警。

2. **交易提醒（可被阈值报警重置）**
   - 基于时间戳调度的交易提醒机制，提醒用户进行交易操作。
   - **不是固定周期触发**，而是基于"最近一次阈值报警或交易提醒事件"动态计算下一次提醒时间。
   - 当触发 PnL 阈值报警时（进入 `ABOVE` 或 `BELOW` 状态），系统会**重置交易提醒计时器**，避免短时间内重复提醒。
   - 可配置提醒间隔（默认 3600 秒），或设置为 0 完全禁用此功能。

## 提醒机制说明

系统内部使用状态机来追踪浮动盈亏的区间状态：`NORMAL`（正常） / `ABOVE`（超上限） / `BELOW`（超下限）。

**阈值报警与交易提醒的联动逻辑：**

1. 当 PnL 从 `NORMAL` 进入 `ABOVE` 或 `BELOW` 状态时：
   - 立即发送一次阈值报警
   - **同时重置交易提醒计时器**，下一次交易提醒时间 = 当前时间 + `trade_reminder_interval`

2. 当 PnL 从 `ABOVE` / `BELOW` 回到 `NORMAL`（恢复）时：
   - 仅记录日志，不发送报警
   - **不重置交易提醒计时器**

3. 当 PnL 持续停留在某一状态时：
   - 不会重复发送阈值报警（仅在状态变化时触发）

**设计目的：** 当触发盈亏阈值报警后，系统认为用户已介入交易，因此重新计算下一次定时交易提醒的触发时间，避免短时间内的重复提醒。

## 安装与运行

### 1. 环境准备

确保已安装 Python 3.8 或更高版本。

```bash
# 克隆仓库
git clone <repo_url>
cd paradex-pnl-guard

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate  # Windows 用户使用: venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置文件

复制 `.env.example` 文件为 `.env`，并填入必要的认证信息：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```ini
PARADEX_JWT=your_jwt_token_here      # Paradex JWT 令牌
TG_BOT_TOKEN=your_telegram_bot_token # Telegram Bot Token
TG_CHAT_ID=your_chat_id              # 接收消息的 Chat ID
```

**Telegram 说明**：
- 请先在 Telegram 中与 Bot 进行对话，否则 Bot 无法主动发送消息。
- 在中国大陆地区运行可能需要配置系统代理。

### 3. 启动

使用默认配置运行（检查间隔 60s，阈值 +20/-20，交易提醒间隔 1小时）：

```bash
python src/main.py
```

## CLI 参数说明

可以通过命令行参数覆盖默认配置。优先级：CLI 参数 > 默认值。

| 参数名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `--jwt` | (无) | Paradex JWT 令牌（覆盖环境变量） |
| `--interval` | 60 | 盈亏检查间隔（秒） |
| `--upper` | 20.0 | 浮动盈亏上限阈值（达到或超过时报警） |
| `--lower` | -20.0 | 浮动盈亏下限阈值（达到或低于时报警） |
| `--trade-reminder-interval` | 3600 | 交易提醒间隔（秒）。设置为 0 表示禁用此功能 |

### 运行示例

**示例 1：自定义报警阈值**
设置检查间隔为 30 秒，上限为 50 USDC，下限为 -50 USDC：

```bash
python src/main.py --interval 30 --upper 50 --lower -50
```

**示例 2：调整交易提醒频率**
每 30 分钟（1800 秒）发送一次交易提醒：

```bash
python src/main.py --trade-reminder-interval 1800
```

**示例 3：禁用交易提醒**
仅使用盈亏监控功能：

```bash
python src/main.py --trade-reminder-interval 0
```

**示例 4：临时使用特定 JWT**
不修改 `.env` 文件，直接通过命令行传入 Token：

```bash
python src/main.py --jwt "YOUR_TEMP_JWT_TOKEN"
```

## 项目结构

- `src/main.py`: 程序入口，包含主循环和业务逻辑。
- `src/paradex.py`: Paradex API 客户端，负责数据获取与重试。
- `src/notifier.py`: Telegram 消息发送模块。
- `src/config.py`: 配置管理，处理 CLI 参数与环境变量。
- `requirements.txt`: Python 依赖列表。

## 风险提示

1. 本项目**不会执行任何下单操作**，仅用于被动监控。
2. 本项目不构成任何投资建议。
3. 由于网络波动或 API 限制，报警可能会有延迟或失败，请勿完全依赖此工具进行高频风控。
