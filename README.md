# Pulse — AI Harness 情报系统

持续监控 AI agent harness 领域最先锋开源项目动态，感知工具生态的脉搏。

## 监控目标

- **anthropics/claude-code** — Anthropic Claude Code
- **openai/codex** — OpenAI Codex CLI
- **openclaw/openclaw** — OpenClaw

## 快速开始

```bash
cd projects/pulse

# 安装
python3 -m venv venv
./venv/bin/pip install -e .

# 采集数据
./venv/bin/pulse fetch

# 查看状态
./venv/bin/pulse status

# 查看趋势
./venv/bin/pulse trends

# 启动 Web Dashboard
./venv/bin/pulse serve
# 访问 http://localhost:8765

# 启动定时 daemon
./venv/bin/pulse daemon

# 一键执行完整流程（采集+分析+推送）
./venv/bin/pulse run
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `pulse list` | 列出所有监控 repo |
| `pulse add owner/name` | 添加新 repo |
| `pulse fetch [--repo] [--type]` | 采集数据 |
| `pulse status` | 查看采集状态和 DB 统计 |
| `pulse trends [--days]` | 趋势数据 |
| `pulse report [--date] [--generate] [--push]` | 查看/生成报告 |
| `pulse run [--no-push]` | 一键完整流程 |
| `pulse serve [--port]` | 启动 Web Dashboard |
| `pulse daemon [--no-push]` | 启动定时 daemon |

## 架构

```
pulse/
├── config.yaml         # 配置文件（可热更新）
├── data/
│   └── pulse.db        # SQLite 存储
├── logs/               # 日志
└── pulse/
    ├── config.py       # 配置加载
    ├── cli.py          # CLI 入口
    ├── daemon.py       # 定时调度
    ├── collectors/
    │   └── github.py   # GitHub 数据采集（gh CLI）
    ├── analyzers/
    │   └── llm.py      # LLM 分析（claude CLI）
    ├── notifiers/
    │   └── feishu.py   # 飞书推送
    └── web/
        └── app.py      # FastAPI Dashboard
```

## 配置

编辑 `config.yaml`：

- `repos` — 监控 repo 列表（支持动态添加）
- `schedule.cron` — 调度时间（默认每天 8:00）
- `analysis.model` — 分析模型（默认 claude-haiku-4-5）
- `notification.feishu.user_open_id` — 飞书推送目标
- `web.port` — Dashboard 端口（默认 8765）

## 采集维度

1. **Issues** — open + 最近关闭，感知用户需求和痛点
2. **PRs** — open + merged，了解社区贡献方向
3. **Commits** — 默认分支最新提交，最灵敏的前瞻信号
4. **Releases** — 版本节奏

## 依赖

- `gh` CLI（已登录 GitHub）
- `claude` CLI（分析层）
- Python 3.10+

## V1.1 更新 (2026-03-26)

- **分支自动发现**：`fetch_commits` 改为自动拉取所有分支（`gh api repos/{owner}/{repo}/branches?per_page=100`），去掉 `watch_branches` 手动配置
- **飞书直推禁用**：`notifiers/feishu.py` 的 openclaw message CLI 调用已注释，推送层保留接口，后续走 OpenClaw plugin 接入
- **launchd 常驻**：`com.pulse.daemon.plist` + `com.pulse.web.plist`，`scripts/install.sh` 一键安装，web 绑 `0.0.0.0:8765`
- **服务状态**：daemon + web 均已通过 launchctl 拉起并运行中
