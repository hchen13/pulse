**中文** | [English](README_EN.md)

# Pulse

持续感知 AI agent 工具生态的前沿动态。

Pulse 监控最活跃的 AI agent harness 开源项目——追踪 issues、pull requests、commits 和 releases——洞察用户需求、社区动向和行业方向。

## 功能

- **采集** 监控项目的 GitHub 活动（issues、PRs、所有分支的 commits、releases）
- **分析** 三层 LLM 分析流水线并行执行：
  - **维度分析**（4个独立分析师并行）：用户痛点、社区动向、官方工作重心、版本节奏
  - **项目合成**：将 4 份维度报告合并为单项目综合报告
  - **全局综合**：跨项目提炼，生成赛道级洞察报告
- **双段报告格式** 每个章节包含两段，面向不同读者：
  - **说人话** — 大白话，任何人可读，直接说结论
  - **说行话** — 技术细节 + 数量支撑，面向 AI agent 开发者
- **创业者视角** 报告包含「创业者的窗口」章节，基于本期信号推断可切入的方向
- **可视化** Web Dashboard 展示多项目趋势折线图（matrix 绿配色，线尾项目 logo）
- **定时运行** 支持 daemon 模式定时执行，也可手动触发

## 默认监控列表

| 项目 | 仓库 |
|------|------|
| Claude Code | `anthropics/claude-code` |
| Codex | `openai/codex` |
| OpenClaw | `openclaw/openclaw` |

可通过 Web Dashboard 或 `config.yaml` 添加/移除项目。

## 快速开始

```bash
# 克隆并安装
git clone https://github.com/hchen13/pulse.git
cd pulse
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 采集数据
pulse fetch

# 生成分析报告
pulse report -g

# 启动 Web Dashboard
pulse serve
# → http://localhost:8765

# 或一键执行（采集 + 分析）
pulse run
```

## 前置条件

- **Python 3.10+**
- **`gh` CLI** — 已认证 GitHub（`gh auth login`）
- **`claude` CLI** — Anthropic Claude Code CLI，用作分析引擎

## CLI 命令

| 命令 | 说明 |
|------|------|
| `pulse list` | 查看监控列表 |
| `pulse add <owner/repo>` | 添加监控项目 |
| `pulse fetch` | 采集最新数据 |
| `pulse report [-g]` | 查看或生成每日报告 |
| `pulse run` | 完整流程：采集 + 分析 |
| `pulse serve` | 启动 Web Dashboard |
| `pulse daemon` | 启动定时 daemon |
| `pulse status` | 查看采集统计 |
| `pulse trends` | 查看趋势数据 |
| `pulse watch [-f]` | 监听新报告事件 |

## Web Dashboard

Dashboard（默认端口 8765）包含：

- **概览** — 所有项目的关键指标 + 今日洞察
- **日报** — 完整分析报告，Markdown 渲染（终端绿主题，双段结构）
- **趋势** — Issues / PRs / Commits 多项目折线图（matrix 绿配色，线尾显示项目 logo）
- **Workflow** — 分析师配置（system prompt 可在线编辑）
- **Settings** — 定时配置、WebSocket 开关、Webhook 管理

## 通知机制

报告生成后支持两种推送方式。

### WebSocket

连接 Dashboard 的 `/ws` 端点：

```javascript
const ws = new WebSocket('ws://your-host/pulse/ws');
ws.onmessage = (e) => {
  const event = JSON.parse(e.data);
  if (event.type === 'report_ready') {
    console.log('新报告:', event.data);
  }
};
```

也可以用 CLI 监听：

```bash
pulse watch              # 等待下一个报告后退出
pulse watch --follow     # 持续监听
```

### Webhook

在 Settings 页面配置 Webhook URL，报告生成后自动 POST：

```json
{
  "event": "report_ready",
  "date": "2026-03-26",
  "repos": ["claude-code", "codex", "openclaw"],
  "dashboard_url": "http://localhost:8765"
}
```

## 配置

编辑 `config.yaml`：

```yaml
repos:           # 监控项目列表（可通过 Dashboard 或 CLI 添加）
schedule:
  cron: "0 6 * * *"    # 定时执行（默认每天 6:00）
analysis:
  model: claude-haiku-4-5   # 分析模型
web:
  port: 8765
notification:
  websocket:
    enabled: true
  webhooks: []       # Webhook URL 列表
```

## 架构

```
pulse/
├── config.yaml              # 运行时配置
├── .claude/analysts/        # 分析师 system prompt（可在 Dashboard 在线编辑）
│   ├── issues.md            # 用户研究分析师（Issues → 用户痛点）
│   ├── prs.md               # 社区生态分析师（Open PRs → 社区动向）
│   ├── commits.md           # 工程方向分析师（Merged PRs + Commits → 官方方向）
│   └── synthesis.md         # 综合分析师（跨项目提炼 + 双段报告格式 + 创业者视角）
├── data/pulse.db            # SQLite 存储（40 天滚动清理）
├── pulse/
│   ├── cli.py               # CLI 入口
│   ├── daemon.py            # 定时调度
│   ├── collectors/
│   │   └── github.py        # GitHub 数据采集（gh CLI）
│   ├── analyzers/
│   │   └── llm.py           # LLM 分析（claude CLI，并行执行）
│   ├── web/
│   │   └── app.py           # FastAPI Dashboard（单文件，内嵌前端）
│   └── notifiers/           # 推送通知（可插拔）
└── scripts/
    └── install.sh           # macOS launchd 服务安装脚本
```

## 数据策略

- **采集**：24 小时滚动窗口，全量采集到 SQLite
- **分析**：前 50 条数据放入 prompt + 完整数据文件路径；分析师按需读取更多
- **存储**：40 天滚动清理，过期数据自动清除
- **报告**：每次执行覆盖写入

---

## 给 AI Agent 的部署指南

_如果你是一个 AI agent，正在帮助你的人类部署 Pulse，这部分是给你看的。_

### 开始前检查

1. **Python 3.10+**
2. **`gh` CLI** 已安装并认证 — `gh auth status` 验证
3. **`claude` CLI** 已安装 — `claude --version` 验证。这是分析引擎，没有它数据采集能工作但报告无法生成
4. **网络** 可以访问 GitHub API

### 部署步骤

```bash
# 1. 克隆仓库
git clone https://github.com/hchen13/pulse.git
cd pulse

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装
pip install -e .

# 4. 验证前置条件
gh auth status          # 应显示已认证
claude --version        # 应显示版本号
pulse list              # 应显示 3 个默认项目

# 5. 首次采集
pulse fetch             # 约 2 分钟

# 6. 生成首份报告
pulse report -g         # 约 2 分钟（13 次并行 LLM 调用）

# 7. 启动 Dashboard
pulse serve             # → http://localhost:8765
```

### 作为服务运行（macOS）

```bash
bash scripts/install.sh
# 安装两个 launchd 服务：
#   com.pulse.daemon — 定时采集 + 分析
#   com.pulse.web    — Web Dashboard（端口 8765）
```

### 作为服务运行（Linux/systemd）

创建两个 unit 文件（路径按实际安装位置修改）：

```ini
# /etc/systemd/user/pulse-daemon.service
[Unit]
Description=Pulse Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/pulse
ExecStart=/path/to/pulse/.venv/bin/pulse daemon
Restart=on-failure

[Install]
WantedBy=default.target
```

```ini
# /etc/systemd/user/pulse-web.service
[Unit]
Description=Pulse Web Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/pulse
ExecStart=/path/to/pulse/.venv/bin/pulse serve --host 0.0.0.0
Restart=on-failure

[Install]
WantedBy=default.target
```

启用：`systemctl --user enable --now pulse-daemon pulse-web`

### 反向代理（可选）

如果希望用 `hostname/pulse` 而不是 `hostname:8765` 访问，配置反向代理。Caddy 示例：

```
:80 {
    handle_path /pulse* {
        reverse_proxy localhost:8765
    }
}
```

### 注意事项

- **GitHub 限速**：认证用户约 5000 次/小时。3 个 repo 每次采集约 20 次请求，50+ repo 时需注意
- **`claude` CLI 认证**：需要有效的 Anthropic API key 或订阅。报告生成失败时引导人类重新认证
- **磁盘**：SQLite 约 5MB（3 个活跃 repo），40 天滚动清理
- **端口 8765**：被占用时在 `config.yaml` 的 `web.port` 修改
- **首次运行无历史趋势**：趋势图需要多天数据积累

### 添加项目

通过 Dashboard（推荐）或 CLI：

```bash
pulse add owner/repo-name
```

---

## License

MIT
