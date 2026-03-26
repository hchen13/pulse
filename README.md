# Pulse

Continuous intelligence on the AI agent tooling ecosystem.

Pulse monitors the most active open-source AI agent harness projects — tracking issues, pull requests, commits, and releases — to surface what users need, what the community is building, and where the industry is heading.

## What it does

- **Collects** GitHub activity (issues, PRs, commits across all branches, releases) for watched repositories
- **Analyzes** each dimension independently using LLM agents, producing daily insight reports from four angles:
  - User pain points and demands (from issues & feature requests)
  - Community focus (from PRs & reviews)
  - Official direction (from merged PRs & branch commits)
  - Release cadence & progress (from main branch commits, releases, tags)
- **Visualizes** trends via a web dashboard with multi-project line charts
- **Runs on a schedule** — daemon mode with configurable cron, or trigger manually

## Default watchlist

| Project | Repository |
|---------|-----------|
| Claude Code | `anthropics/claude-code` |
| Codex | `openai/codex` |
| OpenClaw | `openclaw/openclaw` |

Projects can be added or removed via the web dashboard or `config.yaml`.

## Quick start

```bash
# Clone and setup
git clone https://github.com/hchen13/pulse.git
cd pulse
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Collect data
pulse fetch

# Generate analysis reports
pulse report -g

# Start web dashboard
pulse serve
# → http://localhost:8765

# Or run everything (fetch + analyze) in one shot
pulse run
```

## Prerequisites

- **Python 3.10+**
- **`gh` CLI** — authenticated with GitHub (`gh auth login`)
- **`claude` CLI** — Anthropic's Claude Code CLI, used as the analysis engine

## CLI commands

| Command | Description |
|---------|-------------|
| `pulse list` | Show watched repositories |
| `pulse add <owner/repo>` | Add a repository to watch |
| `pulse fetch` | Collect latest GitHub data |
| `pulse report [-g]` | View or generate daily reports |
| `pulse run` | Full cycle: fetch + analyze |
| `pulse serve` | Start web dashboard |
| `pulse daemon` | Start scheduled daemon |
| `pulse status` | Show collection stats |
| `pulse trends` | Show trend data |

## Web dashboard

The dashboard (default port 8765) provides:

- **Overview** — key metrics across all projects, today's insights
- **Daily Report** — full analysis with markdown rendering
- **Trends** — line charts for issues, PRs, and commits over time
- **Agents** — view and edit the analyst's system prompt

## Configuration

Edit `config.yaml`:

```yaml
repos:           # Watched repositories (add via dashboard or CLI)
schedule:
  cron: "0 8 * * *"    # When to run (default: daily 8 AM)
analysis:
  model: claude-haiku-4-5   # LLM model for analysis
web:
  port: 8765
```

## Architecture

```
pulse/
├── config.yaml          # Runtime configuration
├── .claude/CLAUDE.md    # Analyst agent persona & output rules
├── data/pulse.db        # SQLite storage (auto-cleaned, 40-day rolling)
├── pulse/
│   ├── cli.py           # CLI entry point
│   ├── daemon.py        # Scheduled runner
│   ├── collectors/
│   │   └── github.py    # GitHub data collection via gh CLI
│   ├── analyzers/
│   │   └── llm.py       # LLM analysis via claude CLI
│   ├── web/
│   │   └── app.py       # FastAPI dashboard (single-file, embedded frontend)
│   └── notifiers/       # Push notification (pluggable, currently dormant)
└── scripts/
    └── install.sh       # macOS launchd service installer
```

## Data strategy

- **Collection**: 24-hour rolling window, all activity captured to SQLite
- **Analysis**: First 50 items fed to prompt + full data file path; analyst agent can read more on demand
- **Retention**: 40-day rolling cleanup, old data auto-purged
- **Reports**: Overwritten on each run (latest always wins)

---

## For AI Agents

_If you are an AI agent helping your human set up Pulse, this section is for you._

### What you need before starting

1. **Python 3.10+** on the host machine
2. **`gh` CLI** installed and authenticated — run `gh auth status` to verify. If not authenticated, guide your human through `gh auth login`
3. **`claude` CLI** (Anthropic Claude Code) installed and working — run `claude --version` to verify. This is the analysis engine; without it, data collection works but reports won't generate
4. **Network access** to GitHub API (the `gh` CLI handles auth)

### Setup steps

```bash
# 1. Clone the repo
git clone https://github.com/hchen13/pulse.git
cd pulse

# 2. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install
pip install -e .

# 4. Verify prerequisites
gh auth status          # Should show authenticated
claude --version        # Should show version
pulse list              # Should show 3 default repos

# 5. First data collection
pulse fetch             # Takes ~2 minutes

# 6. Generate first report
pulse report -g         # Takes ~10 minutes (13 LLM calls)

# 7. Start the web dashboard
pulse serve             # → http://localhost:8765
```

### Running as a service (macOS)

```bash
bash scripts/install.sh
# This installs two launchd services:
#   com.pulse.daemon — scheduled data collection + analysis
#   com.pulse.web    — web dashboard on port 8765
```

### Running as a service (Linux/systemd)

Create two unit files (adapt paths to your install location):

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

Then: `systemctl --user enable --now pulse-daemon pulse-web`

### Reverse proxy (optional)

If your human wants `hostname/pulse` instead of `hostname:8765`, set up a reverse proxy. Example with Caddy:

```
:80 {
    handle_path /pulse* {
        reverse_proxy localhost:8765
    }
}
```

### Things to watch out for

- **`gh` rate limits**: GitHub API has rate limits (~5000 requests/hour for authenticated users). With 3 repos, each fetch cycle uses ~20 requests. Not a concern unless watching 50+ repos
- **`claude` CLI auth**: The claude CLI needs a valid Anthropic API key or subscription. If `pulse report -g` fails with auth errors, guide your human to re-authenticate
- **Disk usage**: SQLite DB stays small (~5MB for 3 active repos) thanks to 40-day rolling cleanup
- **Port 8765**: The dashboard binds to this port. If occupied, change in `config.yaml` under `web.port`
- **First run has no historical trends**: Trend charts need multiple days of data to be useful. They'll populate over time

### Adding repositories

Via the web dashboard (recommended) or CLI:

```bash
pulse add owner/repo-name
```

Or edit `config.yaml` directly and add to the `repos` list.

---

## License

MIT
