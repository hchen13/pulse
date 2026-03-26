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

        # Phase 1: 12 个维度分析并行
        click.echo("  [Phase 1] 维度分析（并行）...")
        with ThreadPoolExecutor(max_workers=len(repos) or 1) as executor:
            future_to_repo = {executor.submit(analyzer.analyze_repo, r): r for r in repos}
            for future in as_completed(future_to_repo):
                r = future_to_repo[future]
                try:
                    ok = future.result()
                    if ok:
                        click.echo(f"  ✓ {r.full_name} 维度分析完成")
                    else:
                        click.echo(f"  ✗ {r.full_name} 维度分析失败")
                except Exception as e:
                    click.echo(f"  ✗ {r.full_name} 异常: {e}")

        # Phase 2: repo 合成 + 全局综合 同时并行（都只依赖 Phase 1）
        global_report = None
        repo_reports = {}
        if not repo:
            click.echo("  [Phase 2] repo 合成 + 全局综合（并行）...")
            with ThreadPoolExecutor(max_workers=len(repos) + 1) as executor:
                # 3 个 repo 合成
                synthesis_futures = {executor.submit(analyzer.analyze_repo_synthesis, r): r for r in repos}
                # 1 个全局综合（同时启动）
                global_future = executor.submit(analyzer.analyze_global)

                for future in as_completed(synthesis_futures):
                    r = synthesis_futures[future]
                    try:
                        synthesis = future.result()
                        if synthesis:
                            repo_reports[r.display_name] = synthesis
                            click.echo(f"  ✓ {r.display_name} 合成完成")
                        else:
                            click.echo(f"  ✗ {r.display_name} 合成失败")
                    except Exception as e:
                        click.echo(f"  ✗ {r.display_name} 合成异常: {e}")

                try:
                    global_report = global_future.result()
                    if global_report:
                        click.echo("  ✓ 全局综合完成")
                    else:
                        click.echo("  ✗ 全局综合失败")
                except Exception as e:
                    click.echo(f"  ✗ 全局综合异常: {e}")

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

    # 2. Phase 1: 维度分析（并行）
    click.echo("\n[Phase 1] 维度分析（并行）")
    with ThreadPoolExecutor(max_workers=len(cfg.enabled_repos) or 1) as executor:
        future_to_repo = {executor.submit(analyzer.analyze_repo, r): r for r in cfg.enabled_repos}
        for future in as_completed(future_to_repo):
            r = future_to_repo[future]
            try:
                ok = future.result()
                if ok:
                    click.echo(f"  ✓ {r.full_name}")
            except Exception as e:
                click.echo(f"  ✗ {r.full_name} 异常: {e}")

    # Phase 2: repo 合成 + 全局综合（并行）
    repo_reports = {}
    global_report = None
    if len(cfg.enabled_repos) > 1:
        click.echo("\n[Phase 2] repo 合成 + 全局综合（并行）")
        with ThreadPoolExecutor(max_workers=len(cfg.enabled_repos) + 1) as executor:
            synthesis_futures = {executor.submit(analyzer.analyze_repo_synthesis, r): r for r in cfg.enabled_repos}
            global_future = executor.submit(analyzer.analyze_global)

            for future in as_completed(synthesis_futures):
                r = synthesis_futures[future]
                try:
                    synthesis = future.result()
                    if synthesis:
                        repo_reports[r.display_name] = synthesis
                        click.echo(f"  ✓ {r.display_name} 合成")
                except Exception as e:
                    click.echo(f"  ✗ {r.display_name} 合成异常: {e}")

            try:
                global_report = global_future.result()
                click.echo("  ✓ 全局综合")
            except Exception as e:
                click.echo(f"  ✗ 全局综合异常: {e}")

    # 3. 推送
    if push:
        click.echo("\n[3/3] 飞书推送")
        ok = notifier.send_daily_report(repo_reports, global_report or "")
        if ok:
            click.echo("  ✓ 已推送")
        else:
            click.echo("  ✗ 推送失败")

    click.echo("\n✅ 完成！")


@cli.command("progress")
@click.option("--port", "-p", default=None, type=int, help="Web 服务端口（默认从配置读取）")
@click.pass_context
def progress(ctx, port):
    """查看当前执行进度"""
    cfg = _load_cfg(ctx.obj.get("config_path"))
    ws_port = port or cfg.web.port
    import requests

    url = f"http://localhost:{ws_port}/api/run/status"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        s = resp.json()
    except Exception as e:
        click.echo(f"无法连接到 pulse web 服务（端口 {ws_port}）: {e}", err=True)
        return

    if not s.get("running") and not s.get("finished_at"):
        click.echo("当前没有正在运行或最近完成的任务。")
        return

    click.echo("")
    if s.get("started_at"):
        started = s["started_at"][:16].replace("T", " ")
        click.echo(f"Run:      {started}")

    progress_str = s.get("progress")
    if progress_str:
        total = s.get("total_steps", 0)
        done_count = int(progress_str.split("/")[0]) if "/" in progress_str else 0
        pct = round(done_count / total * 100) if total else 0
        click.echo(f"Progress: {progress_str} ({pct}%)")

    if s.get("running") and s.get("current_step"):
        click.echo(f"Current:  {s['current_step']} 分析中")
    elif s.get("result"):
        click.echo(f"Status:   {s['result']}")
    elif s.get("error"):
        click.echo(f"Error:    {s['error']}")

    elapsed = s.get("elapsed_s")
    if elapsed is not None:
        mins = int(elapsed) // 60
        secs = int(elapsed) % 60
        if mins:
            click.echo(f"Elapsed:  {mins}m{secs:02d}s")
        else:
            click.echo(f"Elapsed:  {secs}s")

    steps = s.get("steps", [])
    if steps:
        click.echo("")
        click.echo("Steps:")
        for step in steps:
            status_icon = "✓" if step["status"] == "done" else "→" if step["status"] == "running" else "·"
            dur = f" ({step['duration_s']}s)" if step.get("duration_s") else ""
            click.echo(f"  {status_icon} {step['name']}{dur}")
    click.echo("")


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
