"""Pulse CLI — 命令行入口"""
import click
import json
import sys
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import load_config, PulseConfig
from .db.models import init_db, get_db
from .collectors.github import GitHubCollector
from .analyzers.llm import LLMAnalyzer
from .notifiers.feishu import FeishuNotifier

# 配置 logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pulse")


def _load_cfg(config_path: str) -> PulseConfig:
    cfg = load_config(config_path)
    init_db(cfg.storage.db_path)
    return cfg


@click.group()
@click.option("--config", "-c", default=None, help="配置文件路径（默认：config.yaml）")
@click.pass_context
def cli(ctx, config):
    """Pulse — AI Harness 竞品情报系统"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command("list")
@click.pass_context
def list_repos(ctx):
    """列出所有监控的 repo"""
    cfg = _load_cfg(ctx.obj.get("config_path"))
    click.echo(f"\n{'Repo':<35} {'状态':<8} {'显示名称'}")
    click.echo("-" * 70)
    for repo in cfg.repos:
        status = "✓ 启用" if repo.enabled else "✗ 禁用"
        click.echo(f"{repo.full_name:<35} {status:<8} {repo.display_name}")


@cli.command("add")
@click.argument("repo", metavar="OWNER/NAME")
@click.option("--display-name", "-d", default=None, help="显示名称")
@click.pass_context
def add_repo(ctx, repo, display_name):
    """添加一个新的监控 repo（格式: owner/name）"""
    if "/" not in repo:
        click.echo("错误: repo 格式应为 owner/name", err=True)
        sys.exit(1)

    owner, name = repo.split("/", 1)
    if not display_name:
        display_name = repo

    cfg = _load_cfg(ctx.obj.get("config_path"))
    config_path = ctx.obj.get("config_path") or str(Path(__file__).parent.parent / "config.yaml")

    import yaml
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    # 检查是否已存在
    for r in raw.get("repos", []):
        if r["owner"] == owner and r["name"] == name:
            click.echo(f"已存在: {repo}")
            return

    raw.setdefault("repos", []).append({
        "owner": owner,
        "name": name,
        "display_name": display_name,
        "enabled": True,
    })

    with open(config_path, "w") as f:
        yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)

    click.echo(f"已添加: {repo} ({display_name})")


@cli.command("fetch")
@click.option("--repo", "-r", default=None, help="只采集指定 repo（owner/name）")
@click.option("--type", "-t", "fetch_type",
              type=click.Choice(["issues", "prs", "commits", "releases", "all"]),
              default="all", help="采集类型")
@click.pass_context
def fetch(ctx, repo, fetch_type):
    """从 GitHub 采集最新数据"""
    cfg = _load_cfg(ctx.obj.get("config_path"))
    collector = GitHubCollector(cfg.collection, cfg.storage.db_path)

    repos = cfg.enabled_repos
    if repo:
        repos = [r for r in repos if r.full_name == repo]
        if not repos:
            click.echo(f"找不到 repo: {repo}", err=True)
            sys.exit(1)

    total_results = {}
    for r in repos:
        click.echo(f"\n采集 {r.full_name} ...")
        if fetch_type == "all":
            results = collector.fetch_all(r)
        elif fetch_type == "issues":
            results = {"issues": collector.fetch_issues(r)}
        elif fetch_type == "prs":
            results = {"prs": collector.fetch_prs(r)}
        elif fetch_type == "commits":
            results = {"commits": collector.fetch_commits(r)}
        elif fetch_type == "releases":
            results = {"releases": collector.fetch_releases(r)}

        total_results[r.full_name] = results
        for k, v in results.items():
            click.echo(f"  {k}: {v} 条")

    click.echo(f"\n采集完成。")


@cli.command("report")
@click.option("--date", "-d", default=None, help="报告日期（YYYY-MM-DD，默认今天）")
@click.option("--repo", "-r", default=None, help="只输出指定 repo 的报告")
@click.option("--generate", "-g", is_flag=True, help="重新生成报告（调用 LLM）")
@click.option("--push", "-p", is_flag=True, help="推送到飞书")
@click.pass_context
def report(ctx, date, repo, generate, push):
    """查看或生成每日报告"""
    cfg = _load_cfg(ctx.obj.get("config_path"))
    analyzer = LLMAnalyzer(cfg.analysis, cfg.storage.db_path)

    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    repos = cfg.enabled_repos
    if repo:
        repos = [r for r in repos if r.full_name == repo]

    if generate:
        click.echo(f"生成 {date} 的分析报告（并行）...")
        repo_reports = {}
        with ThreadPoolExecutor(max_workers=len(repos) or 1) as executor:
            future_to_repo = {executor.submit(analyzer.analyze_repo, r): r for r in repos}
            for future in as_completed(future_to_repo):
                r = future_to_repo[future]
                try:
                    analysis = future.result()
                    if analysis:
                        repo_reports[r.display_name] = analysis
                        click.echo(f"  ✓ {r.full_name}")
                    else:
                        click.echo(f"  ✗ {r.full_name} 分析失败")
                except Exception as e:
                    click.echo(f"  ✗ {r.full_name} 异常: {e}")

        if not repo and len(repo_reports) > 1:
            click.echo("  生成综合分析...")
            global_report = analyzer.analyze_global(repo_reports)
        else:
            global_report = None

        if push:
            notifier = FeishuNotifier(cfg.notification)
            ok = notifier.send_daily_report(repo_reports, global_report or "")
            if ok:
                click.echo("已推送到飞书")
            else:
                click.echo("飞书推送失败")
    else:
        # 读取已有报告
        for r in repos:
            saved = analyzer.get_report(date, r.full_name)
            if saved:
                click.echo(f"\n{'='*60}")
                click.echo(f"## {r.display_name} — {date}")
                click.echo('='*60)
                click.echo(saved)
            else:
                click.echo(f"\n[{r.full_name}] {date} 暂无报告，用 --generate 生成")

        if not repo:
            global_saved = analyzer.get_report(date)
            if global_saved:
                click.echo(f"\n{'='*60}")
                click.echo(f"## 综合分析 — {date}")
                click.echo('='*60)
                click.echo(global_saved)


@cli.command("trends")
@click.option("--days", "-d", default=7, help="查看最近 N 天的趋势（默认 7）")
@click.option("--repo", "-r", default=None, help="指定 repo")
@click.pass_context
def trends(ctx, days, repo):
    """查看趋势数据（issues/PRs/commits 统计）"""
    cfg = _load_cfg(ctx.obj.get("config_path"))

    repos = cfg.enabled_repos
    if repo:
        repos = [r for r in repos if r.full_name == repo]

    with get_db(cfg.storage.db_path) as conn:
        for r in repos:
            click.echo(f"\n📊 {r.display_name} — 最近 {days} 天")
            click.echo("-" * 50)

            # Issues 活跃度
            issue_stats = conn.execute("""
                SELECT state, COUNT(*) as cnt
                FROM issues
                WHERE repo_full_name = ?
                  AND fetched_at >= datetime('now', ?)
                GROUP BY state
            """, (r.full_name, f"-{days} days")).fetchall()

            if issue_stats:
                stats_str = " / ".join([f"{row['state']}: {row['cnt']}" for row in issue_stats])
                click.echo(f"Issues: {stats_str}")

            # PRs
            pr_stats = conn.execute("""
                SELECT state, COUNT(*) as cnt
                FROM pull_requests
                WHERE repo_full_name = ?
                  AND fetched_at >= datetime('now', ?)
                GROUP BY state
            """, (r.full_name, f"-{days} days")).fetchall()

            if pr_stats:
                stats_str = " / ".join([f"{row['state']}: {row['cnt']}" for row in pr_stats])
                click.echo(f"PRs: {stats_str}")

            # Commits
            commit_count = conn.execute("""
                SELECT COUNT(*) as cnt
                FROM commits
                WHERE repo_full_name = ?
                  AND fetched_at >= datetime('now', ?)
            """, (r.full_name, f"-{days} days")).fetchone()

            click.echo(f"Commits: {commit_count['cnt']}")

            # 最新 Release
            latest_release = conn.execute("""
                SELECT tag_name, published_at
                FROM releases
                WHERE repo_full_name = ?
                ORDER BY published_at DESC
                LIMIT 1
            """, (r.full_name,)).fetchone()

            if latest_release:
                click.echo(f"Latest Release: {latest_release['tag_name']} ({latest_release['published_at'][:10]})")
            else:
                click.echo("Latest Release: -")

            # 热门 Issues（高评论数）
            top_issues = conn.execute("""
                SELECT issue_number, title, comments, state
                FROM issues
                WHERE repo_full_name = ?
                ORDER BY comments DESC
                LIMIT 3
            """, (r.full_name,)).fetchall()

            if top_issues:
                click.echo("\n热门 Issues:")
                for i in top_issues:
                    click.echo(f"  #{i['issue_number']} [{i['state']}] {i['title'][:50]} ({i['comments']} 评论)")


@cli.command("status")
@click.pass_context
def status(ctx):
    """查看采集状态"""
    cfg = _load_cfg(ctx.obj.get("config_path"))

    with get_db(cfg.storage.db_path) as conn:
        click.echo(f"\nPulse 采集状态")
        click.echo("=" * 60)

        for r in cfg.repos:
            click.echo(f"\n{r.display_name} ({r.full_name})")
            status_str = "✓ 启用" if r.enabled else "✗ 禁用"
            click.echo(f"  状态: {status_str}")

            # 最近采集时间
            last_fetch = conn.execute("""
                SELECT fetch_type, MAX(fetched_at) as last_time, SUM(items_count) as total
                FROM fetch_log
                WHERE repo_full_name = ? AND status = 'success'
                GROUP BY fetch_type
            """, (r.full_name,)).fetchall()

            if last_fetch:
                for row in last_fetch:
                    click.echo(f"  {row['fetch_type']:10} 最后采集: {row['last_time'][:16]} (共 {row['total']} 条)")
            else:
                click.echo("  尚未采集")

            # DB 数据量
            counts = {
                "issues": conn.execute("SELECT COUNT(*) as c FROM issues WHERE repo_full_name=?", (r.full_name,)).fetchone()["c"],
                "prs": conn.execute("SELECT COUNT(*) as c FROM pull_requests WHERE repo_full_name=?", (r.full_name,)).fetchone()["c"],
                "commits": conn.execute("SELECT COUNT(*) as c FROM commits WHERE repo_full_name=?", (r.full_name,)).fetchone()["c"],
                "releases": conn.execute("SELECT COUNT(*) as c FROM releases WHERE repo_full_name=?", (r.full_name,)).fetchone()["c"],
            }
            click.echo(f"  DB: issues={counts['issues']}, prs={counts['prs']}, commits={counts['commits']}, releases={counts['releases']}")


@cli.command("run")
@click.option("--push/--no-push", default=True, help="是否推送飞书（默认推送）")
@click.pass_context
def run_pipeline(ctx, push):
    """一键执行完整流程：采集 + 分析 + 推送"""
    cfg = _load_cfg(ctx.obj.get("config_path"))
    collector = GitHubCollector(cfg.collection, cfg.storage.db_path)
    analyzer = LLMAnalyzer(cfg.analysis, cfg.storage.db_path)
    notifier = FeishuNotifier(cfg.notification)

    click.echo("🚀 Pulse 开始执行...")

    # 1. 采集
    click.echo("\n[1/3] 数据采集")
    fetch_results = {}
    for r in cfg.enabled_repos:
        click.echo(f"  采集 {r.full_name} ...")
        results = collector.fetch_all(r)
        fetch_results[r.full_name] = results
        click.echo(f"  ✓ {results}")

    # 2. 分析（并行）
    click.echo("\n[2/3] LLM 分析（并行）")
    repo_reports = {}
    with ThreadPoolExecutor(max_workers=len(cfg.enabled_repos) or 1) as executor:
        future_to_repo = {executor.submit(analyzer.analyze_repo, r): r for r in cfg.enabled_repos}
        for future in as_completed(future_to_repo):
            r = future_to_repo[future]
            try:
                analysis = future.result()
                if analysis:
                    repo_reports[r.display_name] = analysis
                    click.echo(f"  ✓ {r.full_name}")
            except Exception as e:
                click.echo(f"  ✗ {r.full_name} 异常: {e}")

    global_report = None
    if len(repo_reports) > 1:
        click.echo("  生成综合分析...")
        global_report = analyzer.analyze_global(repo_reports)
        click.echo("  ✓")

    # 3. 推送
    if push:
        click.echo("\n[3/3] 飞书推送")
        ok = notifier.send_daily_report(repo_reports, global_report or "")
        if ok:
            click.echo("  ✓ 已推送")
        else:
            click.echo("  ✗ 推送失败")

    click.echo("\n✅ 完成！")


@cli.command("watch")
@click.option("--follow", "-f", is_flag=True, help="持续监听（默认收到第一个事件后退出）")
@click.option("--port", "-p", default=None, type=int, help="Web 服务端口（默认从配置读取）")
@click.pass_context
def watch(ctx, follow, port):
    """监听新报告事件（通过 WebSocket）

    连接本地 Pulse Web 服务，等待 report_ready 事件，收到后输出 JSON 并退出。
    加 --follow 可持续监听。
    """
    cfg = _load_cfg(ctx.obj.get("config_path"))
    ws_port = port or cfg.web.port

    try:
        from websockets.sync.client import connect
        from websockets.exceptions import ConnectionClosed
    except ImportError:
        click.echo("需要安装 websockets: pip install websockets", err=True)
        sys.exit(1)

    url = f"ws://localhost:{ws_port}/ws"
    click.echo(f"连接 {url} ...", err=True)

    try:
        with connect(url) as ws:
            click.echo("已连接，等待事件...", err=True)
            while True:
                try:
                    msg = ws.recv(timeout=60)
                    data = json.loads(msg)
                    if data.get("type") == "report_ready":
                        click.echo(json.dumps(data, ensure_ascii=False))
                        if not follow:
                            break
                except TimeoutError:
                    # 发送 ping 保持连接
                    ws.send("ping")
    except ConnectionClosed:
        click.echo("连接已关闭", err=True)
        sys.exit(1)
    except OSError as e:
        click.echo(f"连接失败: {e}（请确认 pulse web 服务已启动，端口: {ws_port}）", err=True)
        sys.exit(1)


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
