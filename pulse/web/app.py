"""FastAPI Web Dashboard — Pulse V2"""
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
import asyncio
import json
import logging
import queue
import threading
import re
import requests as _requests
import yaml
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from ..config import load_config, RepoConfig
from ..db.models import init_db, get_db

logger = logging.getLogger(__name__)

# 全局配置（由 daemon 或 CLI 设置）
_config_path: Optional[str] = None
_db_path: str = "./data/pulse.db"

# 后台任务状态
_run_status = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}
_run_lock = threading.Lock()

# WebSocket 连接列表
_ws_clients: List[WebSocket] = []
_ws_clients_lock = threading.Lock()

# 事件队列（后台线程 → asyncio loop）
_event_queue: queue.Queue = queue.Queue()

# asyncio event loop 引用（在 create_app 时设置）
_event_loop: Optional[asyncio.AbstractEventLoop] = None


class AddRepoRequest(BaseModel):
    url: str
    alias: Optional[str] = None


class UpdateRepoRequest(BaseModel):
    alias: Optional[str] = None
    enabled: Optional[bool] = None


class UpdateAgentRequest(BaseModel):
    content: str


class SettingsRequest(BaseModel):
    schedule_hour: Optional[int] = None
    schedule_minute: Optional[int] = None
    webhooks: Optional[List[str]] = None
    websocket_enabled: Optional[bool] = None


def _parse_github_url(url: str) -> Optional[tuple]:
    """解析 GitHub URL，返回 (owner, name) 或 None"""
    url = url.strip().rstrip("/")
    # https://github.com/owner/name 或 owner/name
    m = re.match(r"(?:https?://github\.com/)?([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    return None


def _load_config_path() -> Path:
    """获取 config.yaml 的绝对路径"""
    if _config_path:
        return Path(_config_path)
    return Path(__file__).parent.parent.parent / "config.yaml"


def _save_repos_to_config(repos: List[RepoConfig]):
    """将 repo 列表写回 config.yaml"""
    cfg_path = _load_config_path()
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw["repos"] = [
        {
            "owner": r.owner,
            "name": r.name,
            "display_name": r.display_name,
            "enabled": r.enabled,
        }
        for r in repos
    ]

    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, allow_unicode=True, sort_keys=False)


async def _broadcast_event_async(event_type: str, data: dict):
    """异步广播 WebSocket 事件给所有连接的客户端"""
    msg = json.dumps({"type": event_type, "data": data})
    dead = []
    with _ws_clients_lock:
        clients = list(_ws_clients)
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    if dead:
        with _ws_clients_lock:
            for ws in dead:
                if ws in _ws_clients:
                    _ws_clients.remove(ws)


def broadcast_event(event_type: str, data: dict):
    """从后台线程安全地触发 WebSocket 广播（放入事件队列）"""
    _event_queue.put({"type": event_type, "data": data})


def notify_webhooks(report_date: str, repos: List[str]):
    """向所有配置的 webhook URL 发送 POST 通知"""
    try:
        cfg = load_config(_config_path)
        webhooks = cfg.notification.webhooks or []
        if not webhooks:
            return
        port = cfg.web.port
        payload = {
            "event": "report_ready",
            "date": report_date,
            "repos": repos,
            "dashboard_url": f"http://localhost:{port}",
        }
        for url in webhooks:
            try:
                _requests.post(url, json=payload, timeout=10)
                logger.info(f"[webhook] 已通知: {url}")
            except Exception as e:
                logger.warning(f"[webhook] 失败: {url}: {e}")
    except Exception as e:
        logger.warning(f"[webhook] 通知异常: {e}")


def _run_full_cycle():
    """后台线程：完整采集+分析流程"""
    global _run_status
    try:
        from ..config import load_config
        from ..collectors.github import GitHubCollector
        from ..analyzers.llm import LLMAnalyzer

        cfg = load_config(_config_path)
        collector = GitHubCollector(cfg.collection, cfg.storage.db_path)
        analyzer = LLMAnalyzer(cfg.analysis, cfg.storage.db_path)

        repo_reports = {}
        for repo in cfg.enabled_repos:
            logger.info(f"[run] 采集 {repo.full_name}")
            try:
                collector.fetch_all(repo)
            except Exception as e:
                logger.error(f"[run] 采集失败 {repo.full_name}: {e}")

        logger.info("[run] LLM 分析（并行）")
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        with ThreadPoolExecutor(max_workers=len(cfg.enabled_repos) or 1) as executor:
            future_to_repo = {executor.submit(analyzer.analyze_repo, repo): repo for repo in cfg.enabled_repos}
            for future in _as_completed(future_to_repo):
                repo = future_to_repo[future]
                try:
                    analysis = future.result()
                    if analysis:
                        repo_reports[repo.display_name] = analysis
                except Exception as e:
                    logger.error(f"[run] 分析失败 {repo.full_name}: {e}")

        if len(repo_reports) > 1:
            logger.info("[run] 全局综合分析")
            try:
                analyzer.analyze_global(repo_reports)
            except Exception as e:
                logger.error(f"[run] 全局分析失败: {e}")

        try:
            analyzer.cleanup_old_data(days=40)
        except Exception as e:
            logger.warning(f"[run] 清理失败: {e}")

        report_date = datetime.now().strftime("%Y-%m-%d")
        with _run_lock:
            _run_status["running"] = False
            _run_status["finished_at"] = datetime.now().isoformat()
            _run_status["result"] = f"完成，共分析 {len(repo_reports)} 个 repo"

        # 广播 WebSocket 事件
        broadcast_event("report_ready", {
            "date": report_date,
            "repos": list(repo_reports.keys()),
        })
        # Webhook 通知
        notify_webhooks(report_date, list(repo_reports.keys()))

    except Exception as e:
        logger.error(f"[run] 全局异常: {e}")
        with _run_lock:
            _run_status["running"] = False
            _run_status["finished_at"] = datetime.now().isoformat()
            _run_status["error"] = str(e)


def create_app(config_path: Optional[str] = None) -> FastAPI:
    global _config_path, _db_path, _event_loop

    _config_path = config_path
    cfg = load_config(config_path)
    _db_path = cfg.storage.db_path
    init_db(_db_path)

    app = FastAPI(
        title="Pulse — AI Harness 情报系统",
        description="监控 AI agent harness 类开源项目动态",
        version="2.0.0",
    )

    @app.on_event("startup")
    async def startup():
        global _event_loop
        _event_loop = asyncio.get_event_loop()
        # 启动事件队列消费 task
        asyncio.create_task(_event_queue_consumer())

    async def _event_queue_consumer():
        """持续消费事件队列，将事件广播到 WebSocket 客户端"""
        while True:
            try:
                # 非阻塞检查队列
                try:
                    event = _event_queue.get_nowait()
                    await _broadcast_event_async(event["type"], event["data"])
                except queue.Empty:
                    pass
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"[ws] 事件队列消费异常: {e}")
                await asyncio.sleep(1)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        with _ws_clients_lock:
            _ws_clients.append(websocket)
        logger.info(f"[ws] 客户端连接，当前连接数: {len(_ws_clients)}")
        try:
            while True:
                # keep-alive：等待客户端发消息（ping 等），断连时抛异常
                await websocket.receive_text()
        except WebSocketDisconnect:
            with _ws_clients_lock:
                if websocket in _ws_clients:
                    _ws_clients.remove(websocket)
            logger.info(f"[ws] 客户端断开，当前连接数: {len(_ws_clients)}")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return get_dashboard_html()

    # ── Repo 管理 API ────────────────────────────────────────────────────────────

    @app.get("/api/repos")
    async def get_repos():
        cfg = load_config(_config_path)
        return [
            {
                "full_name": r.full_name,
                "display_name": r.display_name,
                "enabled": r.enabled,
            }
            for r in cfg.repos
        ]

    @app.post("/api/repos")
    async def add_repo(req: AddRepoRequest):
        parsed = _parse_github_url(req.url)
        if not parsed:
            raise HTTPException(status_code=400, detail="无效的 GitHub URL")
        owner, name = parsed
        full_name = f"{owner}/{name}"

        cfg = load_config(_config_path)
        # 检查是否已存在
        for r in cfg.repos:
            if r.full_name == full_name:
                raise HTTPException(status_code=409, detail=f"{full_name} 已存在")

        display_name = req.alias or name
        new_repo = RepoConfig(owner=owner, name=name, display_name=display_name, enabled=True)
        cfg.repos.append(new_repo)
        _save_repos_to_config(cfg.repos)

        # 写入 DB
        with get_db(_db_path) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO repos (owner, name, display_name, enabled)
                VALUES (?, ?, ?, 1)
            """, (owner, name, display_name))

        return {"full_name": full_name, "display_name": display_name, "enabled": True}

    @app.put("/api/repos/{repo_owner}/{repo_name}")
    async def update_repo(repo_owner: str, repo_name: str, req: UpdateRepoRequest):
        full_name = f"{repo_owner}/{repo_name}"
        cfg = load_config(_config_path)

        found = False
        for r in cfg.repos:
            if r.full_name == full_name:
                if req.alias is not None:
                    r.display_name = req.alias
                if req.enabled is not None:
                    r.enabled = req.enabled
                found = True
                break

        if not found:
            raise HTTPException(status_code=404, detail=f"{full_name} 不存在")

        _save_repos_to_config(cfg.repos)
        updated = next(r for r in cfg.repos if r.full_name == full_name)
        return {"full_name": full_name, "display_name": updated.display_name, "enabled": updated.enabled}

    @app.delete("/api/repos/{repo_owner}/{repo_name}")
    async def delete_repo(repo_owner: str, repo_name: str):
        full_name = f"{repo_owner}/{repo_name}"
        cfg = load_config(_config_path)

        new_repos = [r for r in cfg.repos if r.full_name != full_name]
        if len(new_repos) == len(cfg.repos):
            raise HTTPException(status_code=404, detail=f"{full_name} 不存在")

        cfg.repos = new_repos
        _save_repos_to_config(cfg.repos)
        return {"deleted": full_name}

    # ── Agents 管理 API ───────────────────────────────────────────────────────────

    # 分析师名称映射
    _ANALYST_NAMES = {
        "issues": "Issues 分析师",
        "prs": "PRs 分析师",
        "commits": "Commits 分析师",
        "synthesis": "综合分析师",
    }

    @app.get("/api/agents")
    async def get_agents():
        result = []
        project_root = Path(__file__).parent.parent.parent
        analysts_dir = project_root / ".claude" / "analysts"
        if analysts_dir.exists():
            # 按固定顺序排列
            order = ["issues", "prs", "commits", "synthesis"]
            md_files = {f.stem: f for f in analysts_dir.glob("*.md")}
            # 先按 order 排，再加上不在 order 里的文件（字母序）
            sorted_stems = [s for s in order if s in md_files] + \
                           sorted(s for s in md_files if s not in order)
            for stem in sorted_stems:
                file_path = md_files[stem]
                try:
                    content = file_path.read_text(encoding="utf-8")
                except Exception:
                    content = ""
                result.append({
                    "id": stem,
                    "name": _ANALYST_NAMES.get(stem, stem),
                    "file": f".claude/analysts/{file_path.name}",
                    "content": content,
                })
        return result

    @app.put("/api/agents/{agent_id}")
    async def update_agent(agent_id: str, req: UpdateAgentRequest):
        project_root = Path(__file__).parent.parent.parent
        # 安全检查：只允许字母数字和下划线/连字符
        import re as _re
        if not _re.match(r'^[\w\-]+$', agent_id):
            raise HTTPException(status_code=400, detail="Invalid agent id")
        file_path = project_root / ".claude" / "analysts" / f"{agent_id}.md"
        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        file_path.write_text(req.content, encoding="utf-8")
        name = _ANALYST_NAMES.get(agent_id, agent_id)
        return {"id": agent_id, "name": name, "file": f".claude/analysts/{agent_id}.md", "content": req.content}

    # ── Settings API ──────────────────────────────────────────────────────────────

    @app.get("/api/settings")
    async def get_settings():
        cfg_path = _load_config_path()
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        sched_raw = raw.get("schedule", {})
        cron = sched_raw.get("cron", "0 6 * * *")
        # 解析 cron：minute hour * * *
        parts = cron.split()
        hour = int(parts[1]) if len(parts) >= 2 else 6
        minute = int(parts[0]) if len(parts) >= 1 else 0

        notif_raw = raw.get("notification", {})
        webhooks = notif_raw.get("webhooks", []) or []
        ws_enabled = notif_raw.get("websocket", {}).get("enabled", True)

        return {
            "schedule": {"hour": hour, "minute": minute, "cron": cron},
            "webhooks": webhooks,
            "websocket_enabled": ws_enabled,
        }

    @app.put("/api/settings")
    async def update_settings(req: SettingsRequest):
        cfg_path = _load_config_path()
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if req.schedule_hour is not None and req.schedule_minute is not None:
            cron = f"{req.schedule_minute} {req.schedule_hour} * * *"
            raw.setdefault("schedule", {})["cron"] = cron
        elif req.schedule_hour is not None:
            # 只更新小时，保留分钟
            existing = raw.get("schedule", {}).get("cron", "0 6 * * *")
            parts = existing.split()
            parts[1] = str(req.schedule_hour)
            raw.setdefault("schedule", {})["cron"] = " ".join(parts)
        elif req.schedule_minute is not None:
            existing = raw.get("schedule", {}).get("cron", "0 6 * * *")
            parts = existing.split()
            parts[0] = str(req.schedule_minute)
            raw.setdefault("schedule", {})["cron"] = " ".join(parts)

        notif = raw.setdefault("notification", {})
        if req.webhooks is not None:
            notif["webhooks"] = req.webhooks
        if req.websocket_enabled is not None:
            notif.setdefault("websocket", {})["enabled"] = req.websocket_enabled

        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, allow_unicode=True, sort_keys=False)

        return await get_settings()

    @app.post("/api/settings/test-webhook")
    async def test_webhook(body: dict):
        url = body.get("url", "")
        if not url:
            raise HTTPException(status_code=400, detail="url 不能为空")
        try:
            resp = _requests.post(url, json={
                "event": "test",
                "source": "pulse",
                "message": "Pulse webhook 测试 payload",
            }, timeout=10)
            return {"status": resp.status_code, "ok": resp.ok}
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    # ── 立即执行 API ─────────────────────────────────────────────────────────────

    @app.post("/api/run")
    async def trigger_run():
        global _run_status
        with _run_lock:
            if _run_status["running"]:
                return {"status": "already_running", "started_at": _run_status["started_at"]}

            _run_status = {
                "running": True,
                "started_at": datetime.now().isoformat(),
                "finished_at": None,
                "result": None,
                "error": None,
            }

        t = threading.Thread(target=_run_full_cycle, daemon=True)
        t.start()
        return {"status": "started", "started_at": _run_status["started_at"]}

    @app.get("/api/run/status")
    async def get_run_status():
        with _run_lock:
            return dict(_run_status)

    # ── 数据读取 API ──────────────────────────────────────────────────────────────

    @app.get("/api/stats")
    async def get_stats():
        cfg = load_config(_config_path)
        result = []

        with get_db(_db_path) as conn:
            for r in cfg.enabled_repos:
                issues_open = conn.execute(
                    "SELECT COUNT(*) as c FROM issues WHERE repo_full_name=? AND state='OPEN'",
                    (r.full_name,)
                ).fetchone()["c"]

                issues_total = conn.execute(
                    "SELECT COUNT(*) as c FROM issues WHERE repo_full_name=?",
                    (r.full_name,)
                ).fetchone()["c"]

                prs_open = conn.execute(
                    "SELECT COUNT(*) as c FROM pull_requests WHERE repo_full_name=? AND state='OPEN'",
                    (r.full_name,)
                ).fetchone()["c"]

                commits_7d = conn.execute(
                    "SELECT COUNT(*) as c FROM commits WHERE repo_full_name=? AND fetched_at >= datetime('now', '-7 days')",
                    (r.full_name,)
                ).fetchone()["c"]

                merged_prs_7d = conn.execute(
                    "SELECT COUNT(*) as c FROM pull_requests WHERE repo_full_name=? AND merged_at IS NOT NULL AND merged_at >= datetime('now', '-7 days')",
                    (r.full_name,)
                ).fetchone()["c"]

                latest_release = conn.execute(
                    "SELECT tag_name, published_at FROM releases WHERE repo_full_name=? ORDER BY published_at DESC LIMIT 1",
                    (r.full_name,)
                ).fetchone()

                last_fetch = conn.execute(
                    "SELECT MAX(fetched_at) as t FROM fetch_log WHERE repo_full_name=? AND status='success'",
                    (r.full_name,)
                ).fetchone()["t"]

                result.append({
                    "full_name": r.full_name,
                    "display_name": r.display_name,
                    "issues_open": issues_open,
                    "issues_total": issues_total,
                    "prs_open": prs_open,
                    "merged_prs_7d": merged_prs_7d,
                    "commits_7d": commits_7d,
                    "latest_release": dict(latest_release) if latest_release else None,
                    "last_fetch": last_fetch,
                })

        return result

    @app.get("/api/report/{date}")
    async def get_report(date: str, repo: Optional[str] = None):
        with get_db(_db_path) as conn:
            if repo:
                row = conn.execute(
                    "SELECT content, created_at FROM reports WHERE report_date=? AND repo_full_name=? AND report_type='repo' ORDER BY created_at DESC LIMIT 1",
                    (date, repo)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT content, created_at FROM reports WHERE report_date=? AND report_type='global' ORDER BY created_at DESC LIMIT 1",
                    (date,)
                ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="报告未找到")
        return {"date": date, "content": row["content"], "created_at": row["created_at"]}

    @app.get("/api/reports")
    async def list_reports(days: int = 30):
        with get_db(_db_path) as conn:
            rows = conn.execute("""
                SELECT report_date, repo_full_name, report_type, MAX(created_at) as created_at
                FROM reports
                WHERE report_date >= date('now', ?)
                GROUP BY report_date, repo_full_name, report_type
                ORDER BY report_date DESC, report_type
            """, (f"-{days} days",)).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/issues")
    async def get_all_issues(
        limit: int = 50,
        offset: int = 0,
        state: Optional[str] = None,
        repo: Optional[str] = None,
    ):
        query = "SELECT * FROM issues WHERE 1=1"
        params: List = []
        if repo:
            query += " AND repo_full_name=?"
            params.append(repo)
        if state:
            query += " AND state=?"
            params.append(state.upper())
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        count_query = "SELECT COUNT(*) as c FROM issues WHERE 1=1"
        count_params: List = []
        if repo:
            count_query += " AND repo_full_name=?"
            count_params.append(repo)
        if state:
            count_query += " AND state=?"
            count_params.append(state.upper())

        with get_db(_db_path) as conn:
            rows = conn.execute(query, params).fetchall()
            total = conn.execute(count_query, count_params).fetchone()["c"]

        return {"total": total, "offset": offset, "limit": limit, "items": [dict(r) for r in rows]}

    @app.get("/api/issues/{repo_owner}/{repo_name}")
    async def get_issues(repo_owner: str, repo_name: str, limit: int = 50, state: Optional[str] = None):
        full_name = f"{repo_owner}/{repo_name}"
        query = "SELECT * FROM issues WHERE repo_full_name=?"
        params: List = [full_name]
        if state:
            query += " AND state=?"
            params.append(state.upper())
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with get_db(_db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/prs/{repo_owner}/{repo_name}")
    async def get_prs(repo_owner: str, repo_name: str, limit: int = 20):
        full_name = f"{repo_owner}/{repo_name}"
        with get_db(_db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM pull_requests WHERE repo_full_name=? ORDER BY updated_at DESC LIMIT ?",
                (full_name, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/commits/{repo_owner}/{repo_name}")
    async def get_commits(repo_owner: str, repo_name: str, limit: int = 30):
        full_name = f"{repo_owner}/{repo_name}"
        with get_db(_db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM commits WHERE repo_full_name=? ORDER BY committed_at DESC LIMIT ?",
                (full_name, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/trends")
    async def get_trends(days: int = 14):
        cfg = load_config(_config_path)
        result = {}
        with get_db(_db_path) as conn:
            for r in cfg.enabled_repos:
                # 全部 commits（用于趋势图）
                daily_commits = conn.execute("""
                    SELECT date(committed_at) as day, COUNT(*) as cnt
                    FROM commits
                    WHERE repo_full_name=? AND committed_at >= date('now', ?)
                    GROUP BY day ORDER BY day
                """, (r.full_name, f"-{days} days")).fetchall()

                # Main 分支 commits
                daily_main_commits = conn.execute("""
                    SELECT date(committed_at) as day, COUNT(*) as cnt
                    FROM commits
                    WHERE repo_full_name=? AND committed_at >= date('now', ?)
                      AND (branch='main' OR branch='master')
                    GROUP BY day ORDER BY day
                """, (r.full_name, f"-{days} days")).fetchall()

                # 非 main 分支 commits
                daily_branch_commits = conn.execute("""
                    SELECT date(committed_at) as day, COUNT(*) as cnt
                    FROM commits
                    WHERE repo_full_name=? AND committed_at >= date('now', ?)
                      AND branch NOT IN ('main', 'master')
                    GROUP BY day ORDER BY day
                """, (r.full_name, f"-{days} days")).fetchall()

                daily_issues = conn.execute("""
                    SELECT date(created_at) as day, COUNT(*) as cnt
                    FROM issues
                    WHERE repo_full_name=? AND created_at >= date('now', ?)
                    GROUP BY day ORDER BY day
                """, (r.full_name, f"-{days} days")).fetchall()

                daily_open_prs = conn.execute("""
                    SELECT date(created_at) as day, COUNT(*) as cnt
                    FROM pull_requests
                    WHERE repo_full_name=? AND created_at >= date('now', ?)
                      AND state = 'OPEN'
                    GROUP BY day ORDER BY day
                """, (r.full_name, f"-{days} days")).fetchall()

                daily_merged_prs = conn.execute("""
                    SELECT date(merged_at) as day, COUNT(*) as cnt
                    FROM pull_requests
                    WHERE repo_full_name=? AND merged_at >= date('now', ?)
                      AND merged_at IS NOT NULL
                    GROUP BY day ORDER BY day
                """, (r.full_name, f"-{days} days")).fetchall()

                result[r.full_name] = {
                    "display_name": r.display_name,
                    "commits": [{"day": row["day"], "count": row["cnt"]} for row in daily_commits],
                    "main_commits": [{"day": row["day"], "count": row["cnt"]} for row in daily_main_commits],
                    "branch_commits": [{"day": row["day"], "count": row["cnt"]} for row in daily_branch_commits],
                    "issues": [{"day": row["day"], "count": row["cnt"]} for row in daily_issues],
                    "open_prs": [{"day": row["day"], "count": row["cnt"]} for row in daily_open_prs],
                    "merged_prs": [{"day": row["day"], "count": row["cnt"]} for row in daily_merged_prs],
                }
        return result

    return app


def get_dashboard_html() -> str:
    """返回 Dashboard 的 HTML（TokyoNight 暗色风格）"""
    return r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pulse — AI Harness 情报系统</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans SC', 'PingFang SC', sans-serif;
            background: #1a1b26;
            color: #cdd6f4;
            min-height: 100vh;
            font-size: 14px;
            line-height: 1.6;
        }

        /* ── Header ── */
        header {
            background: #16161e;
            border-bottom: 1px solid #2a2d3e;
            padding: 14px 28px;
            display: flex;
            align-items: center;
            gap: 14px;
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .header-left { display: flex; align-items: center; gap: 14px; flex: 1; }
        header h1 { font-size: 18px; font-weight: 700; color: #7aa2f7; letter-spacing: 0.3px; }
        header .subtitle { font-size: 12px; color: #565f89; }
        .pulse-dot {
            width: 7px; height: 7px; border-radius: 50%; background: #9ece6a;
            display: inline-block; flex-shrink: 0;
            animation: blink 2.5s ease-in-out infinite;
        }
        @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }

        /* ── Header action buttons ── */
        .header-actions { display: flex; gap: 10px; align-items: center; }
        .btn {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 7px 14px; border-radius: 6px; font-size: 13px;
            cursor: pointer; border: 1px solid; font-family: inherit;
            transition: all 0.15s;
        }
        .btn-primary {
            background: rgba(122,162,247,0.12); color: #7aa2f7;
            border-color: rgba(122,162,247,0.3);
        }
        .btn-primary:hover { background: rgba(122,162,247,0.2); border-color: #7aa2f7; }
        .btn-success {
            background: rgba(158,206,106,0.12); color: #9ece6a;
            border-color: rgba(158,206,106,0.3);
        }
        .btn-success:hover { background: rgba(158,206,106,0.2); border-color: #9ece6a; }
        .btn-danger {
            background: rgba(247,118,142,0.1); color: #f7768e;
            border-color: rgba(247,118,142,0.25);
        }
        .btn-danger:hover { background: rgba(247,118,142,0.2); border-color: #f7768e; }
        .btn-sm { padding: 4px 10px; font-size: 12px; }
        .btn:disabled { opacity: 0.45; cursor: not-allowed; }

        /* ── Run status bar ── */
        #run-statusbar {
            background: rgba(158,206,106,0.08);
            border-bottom: 1px solid rgba(158,206,106,0.2);
            padding: 8px 28px;
            font-size: 12px;
            color: #9ece6a;
            display: none;
            align-items: center;
            gap: 10px;
        }
        .spin { animation: spin 1s linear infinite; display: inline-block; }
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

        /* ── Layout ── */
        .container { max-width: 1100px; margin: 0 auto; padding: 24px 28px; }

        /* ── Tabs ── */
        .tabs { display: flex; gap: 2px; margin-bottom: 24px; border-bottom: 1px solid #2a2d3e; }
        .tab {
            padding: 8px 18px; cursor: pointer; font-size: 13px; color: #565f89;
            border-bottom: 2px solid transparent; margin-bottom: -1px;
            transition: color 0.15s, border-color 0.15s;
        }
        .tab:hover { color: #a9b1d6; }
        .tab.active { color: #7aa2f7; border-bottom-color: #7aa2f7; font-weight: 500; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }

        /* ── Cards ── */
        .card { background: #16161e; border: 1px solid #2a2d3e; border-radius: 8px; padding: 18px 20px; }
        .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 22px; }
        .card h3 { font-size: 11px; color: #565f89; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.8px; text-align: left; }
        .stat-big { font-size: 30px; font-weight: 700; color: #e0e2f0; line-height: 1; }
        .stat-sub { font-size: 11px; color: #565f89; margin-top: 5px; }

        /* ── Badges ── */
        .badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px; font-weight: 500; }
        .badge-green  { background: rgba(158,206,106,0.12); color: #9ece6a; }
        .badge-blue   { background: rgba(122,162,247,0.12); color: #7aa2f7; }
        .badge-orange { background: rgba(255,158,100,0.12); color: #ff9e64; }
        .badge-purple { background: rgba(187,154,247,0.12); color: #bb9af7; }
        .badge-red    { background: rgba(247,118,142,0.12); color: #f7768e; }

        /* ── Repo cards ── */
        .repo-card { background: #16161e; border: 1px solid #2a2d3e; border-radius: 8px; margin-bottom: 14px; overflow: hidden; }
        .repo-card .delete-btn { opacity: 0; transition: opacity 0.2s; }
        .repo-card:hover .delete-btn { opacity: 1; }
        .repo-header {
            padding: 14px 18px; border-bottom: 1px solid #2a2d3e;
            display: flex; justify-content: space-between; align-items: center;
        }
        .repo-header-left { display: flex; align-items: center; gap: 12px; }
        .repo-header h2 { font-size: 15px; color: #e0e2f0; font-weight: 600; }
        .repo-header a { color: #7aa2f7; text-decoration: none; font-size: 12px; }
        .repo-header a:hover { text-decoration: underline; }
        .repo-header-right { display: flex; align-items: center; gap: 8px; }
        .repo-stats { display: flex; gap: 28px; padding: 14px 18px; }
        .repo-stat .num { font-size: 22px; font-weight: 700; color: #e0e2f0; line-height: 1; }
        .repo-stat .label { font-size: 10px; color: #565f89; margin-top: 3px; text-transform: uppercase; letter-spacing: 0.5px; }

        /* ── Section title ── */
        .section-title { font-size: 16px; color: #e0e2f0; margin: 20px 0 10px; font-weight: 600; text-align: left; }

        /* ── Report blocks ── */
        .report-block { background: #16161e; border: 1px solid #2a2d3e; border-radius: 8px; padding: 22px 24px; margin-bottom: 16px; }
        .report-block-title {
            font-size: 13px; color: #565f89; text-transform: uppercase;
            letter-spacing: 0.8px; margin-bottom: 16px; padding-bottom: 10px;
            border-bottom: 1px solid #2a2d3e; font-weight: 500;
            text-align: left;
        }

        /* ── Markdown content ── */
        .md-content { font-size: 14px; line-height: 1.75; color: #cdd6f4; text-align: left; }
        .md-content h1 { font-size: 19px; color: #e0e2f0; font-weight: 700; margin: 20px 0 10px; padding-bottom: 8px; border-bottom: 1px solid #2a2d3e; }
        .md-content h2 { font-size: 16px; color: #7aa2f7; font-weight: 600; margin: 18px 0 8px; padding-bottom: 5px; border-bottom: 1px solid #2a2d3e; }
        .md-content h3 { font-size: 14px; color: #9ece6a; font-weight: 600; margin: 14px 0 6px; }
        .md-content h4 { font-size: 13px; color: #bb9af7; font-weight: 500; margin: 12px 0 5px; }
        .md-content p { margin: 6px 0 10px; }
        .md-content ul, .md-content ol { padding-left: 18px; margin: 6px 0 10px; }
        .md-content li { margin: 3px 0; }
        .md-content li::marker { color: #7aa2f7; }
        .md-content strong { color: #e0e2f0; font-weight: 600; }
        .md-content em { color: #a9b8d4; font-style: italic; }
        .md-content code { background: #1e1e2e; color: #f7768e; padding: 1px 5px; border-radius: 4px; font-family: 'SFMono-Regular', 'JetBrains Mono', Consolas, monospace; font-size: 12.5px; border: 1px solid #2a2d3e; }
        .md-content pre { background: #1e1e2e; border: 1px solid #2a2d3e; border-radius: 6px; padding: 14px 16px; overflow-x: auto; margin: 10px 0 14px; }
        .md-content pre code { background: none; padding: 0; color: #cdd6f4; font-size: 13px; border: none; }
        .md-content blockquote { border-left: 3px solid #7aa2f7; padding: 6px 14px; margin: 8px 0; color: #7982a9; background: rgba(122,162,247,0.04); border-radius: 0 4px 4px 0; }
        .md-content table { width: 100%; border-collapse: collapse; margin: 10px 0 14px; font-size: 13px; }
        .md-content th { background: #1e1e2e; color: #e0e2f0; padding: 7px 11px; text-align: left; border: 1px solid #2a2d3e; font-weight: 600; }
        .md-content td { padding: 6px 11px; border: 1px solid #2a2d3e; color: #cdd6f4; }
        .md-content tr:nth-child(even) td { background: rgba(30,30,46,0.5); }
        .md-content hr { border: none; border-top: 1px solid #2a2d3e; margin: 14px 0; }
        .md-content a { color: #7aa2f7; text-decoration: none; }
        .md-content a:hover { text-decoration: underline; }

        /* ── Insight card ── */
        .insight-card { background: #16161e; border: 1px solid #2a2d3e; border-radius: 8px; padding: 20px 24px; margin-top: 20px; }
        .insight-card-title { font-size: 13px; color: #565f89; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid #2a2d3e; font-weight: 500; }
        .insight-badge { display: inline-block; background: rgba(158,206,106,0.12); color: #9ece6a; padding: 1px 7px; border-radius: 10px; font-size: 11px; margin-left: 8px; vertical-align: middle; text-transform: none; letter-spacing: 0; }

        /* ── Controls ── */
        .flex-between { display: flex; justify-content: space-between; align-items: center; }
        select { background: #1e1e2e; color: #cdd6f4; border: 1px solid #2a2d3e; border-radius: 6px; padding: 6px 12px; font-size: 13px; cursor: pointer; outline: none; }
        select:focus { border-color: #7aa2f7; }

        /* ── Chart ── */
        .chart-container { height: 180px; }

        /* ── Loading / Error ── */
        .loading { text-align: center; padding: 48px; color: #565f89; font-size: 13px; }
        .error-msg { color: #f7768e; padding: 10px 14px; background: rgba(247,118,142,0.08); border-radius: 6px; font-size: 13px; }

        /* ── PR toggle ── */
        .pr-toggle-group { display: flex; gap: 0; border: 1px solid #2a2d3e; border-radius: 6px; overflow: hidden; }
        .pr-toggle-btn {
            background: transparent; color: #565f89; border: none; padding: 4px 12px;
            font-size: 12px; cursor: pointer; transition: background 0.15s, color 0.15s;
            border-right: 1px solid #2a2d3e;
        }
        .pr-toggle-btn:last-child { border-right: none; }
        .pr-toggle-btn:hover { background: #1e1e2e; color: #a9b1d6; }
        .pr-toggle-btn.active { background: #2a2d3e; color: #7aa2f7; font-weight: 600; }

        /* ── Agent cards ── */
        .agent-card { background: #16161e; border: 1px solid #2a2d3e; border-radius: 8px; margin-bottom: 20px; overflow: hidden; }
        .agent-card-header {
            padding: 14px 18px; border-bottom: 1px solid #2a2d3e;
            display: flex; justify-content: space-between; align-items: center;
        }
        .agent-card-header h2 { font-size: 15px; color: #e0e2f0; font-weight: 600; }
        .agent-card-body { padding: 20px 24px; }
        .agent-card-actions { display: flex; gap: 8px; }
        .agent-textarea {
            width: 100%; min-height: 400px; background: #1e2030;
            color: #c0caf5; border: 1px solid #2a2d3e; border-radius: 6px;
            font-family: 'SFMono-Regular', 'JetBrains Mono', Consolas, monospace;
            font-size: 13px; padding: 14px 16px; resize: vertical; outline: none;
            line-height: 1.6;
        }
        .agent-textarea:focus { border-color: #7aa2f7; }

        /* ── Settings ── */
        .settings-section { background: #16161e; border: 1px solid #2a2d3e; border-radius: 8px; padding: 22px 24px; margin-bottom: 20px; }
        .settings-section h3 { font-size: 15px; color: #e0e2f0; margin-bottom: 6px; font-weight: 600; }
        .settings-section p.desc { font-size: 12px; color: #565f89; margin-bottom: 18px; }
        .settings-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
        .settings-input {
            background: #1e2030; color: #c0caf5; border: 1px solid #2a2d3e; border-radius: 6px;
            padding: 8px 12px; font-size: 13px; outline: none; font-family: inherit;
        }
        .settings-input:focus { border-color: #7aa2f7; }
        .settings-input-sm { width: 80px; }
        .settings-input-url { width: 320px; }
        .toggle-switch { position: relative; display: inline-block; width: 44px; height: 24px; }
        .toggle-switch input { opacity: 0; width: 0; height: 0; }
        .toggle-slider {
            position: absolute; cursor: pointer; inset: 0;
            background: #2a2d3e; border-radius: 24px; transition: 0.2s;
        }
        .toggle-slider::before {
            position: absolute; content: ""; height: 18px; width: 18px;
            left: 3px; bottom: 3px; background: #565f89; border-radius: 50%; transition: 0.2s;
        }
        .toggle-switch input:checked + .toggle-slider { background: rgba(158,206,106,0.3); }
        .toggle-switch input:checked + .toggle-slider::before { transform: translateX(20px); background: #9ece6a; }
        .webhook-list { margin-top: 10px; }
        .webhook-item { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
        .webhook-item span { flex: 1; font-size: 13px; color: #a9b1d6; word-break: break-all; }

        /* ── Modal ── */
        .modal-overlay {
            display: none; position: fixed; inset: 0;
            background: rgba(0,0,0,0.65); z-index: 500;
            align-items: center; justify-content: center;
        }
        .modal-overlay.open { display: flex; }
        .modal {
            background: #1e1e2e; border: 1px solid #2a2d3e; border-radius: 10px;
            padding: 24px 28px; width: 460px; max-width: 95vw;
        }
        .modal h3 { font-size: 16px; color: #e0e2f0; margin-bottom: 20px; font-weight: 600; }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; font-size: 12px; color: #565f89; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
        .form-group input {
            width: 100%; background: #16161e; border: 1px solid #2a2d3e;
            border-radius: 6px; color: #cdd6f4; padding: 9px 12px; font-size: 14px; outline: none;
            font-family: inherit;
        }
        .form-group input:focus { border-color: #7aa2f7; }
        .form-group input::placeholder { color: #565f89; }
        .modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 22px; }
    </style>
</head>
<body>
    <header>
        <div class="header-left">
            <span class="pulse-dot"></span>
            <div>
                <h1>Pulse</h1>
                <div class="subtitle">持续感知 AI agent 工具生态的前沿动态</div>
            </div>
        </div>
        <div class="header-actions">
            <button class="btn btn-primary" id="btn-run" onclick="triggerRun()">立即分析</button>
        </div>
    </header>

    <div id="run-statusbar">
        <span class="spin">↻</span>
        <span id="run-statusbar-text">正在执行采集+分析...</span>
    </div>

    <div class="container">
        <div class="tabs">
            <div class="tab active" onclick="switchTab('overview')">概览</div>
            <div class="tab" onclick="switchTab('reports')">日报</div>
            <div class="tab" onclick="switchTab('trends')">趋势</div>
            <div class="tab" onclick="switchTab('agents')">Agents</div>
            <div class="tab" onclick="switchTab('settings')">Settings</div>
        </div>

        <!-- 概览 -->
        <div id="tab-overview" class="tab-content active">
            <div id="repos-overview" class="loading">加载中...</div>
            <div id="today-insight" style="display:none;">
                <div class="insight-card">
                    <div class="insight-card-title">
                        今日洞察
                        <span class="insight-badge" id="insight-date"></span>
                    </div>
                    <div id="insight-content" class="md-content"></div>
                </div>
            </div>
        </div>

        <!-- 日报 -->
        <div id="tab-reports" class="tab-content">
            <div class="flex-between" style="margin-bottom: 18px;">
                <div class="section-title" style="margin: 0;">每日报告</div>
                <select id="report-date-select" onchange="loadReport()">
                    <option value="">加载日期中...</option>
                </select>
            </div>
            <div id="reports-content" class="loading">正在加载报告...</div>
        </div>

        <!-- 趋势 -->
        <div id="tab-trends" class="tab-content">
            <div id="trends-content" class="loading">加载中...</div>
        </div>

        <!-- Agents -->
        <div id="tab-agents" class="tab-content">
            <div id="agents-content" class="loading">加载中...</div>
        </div>

        <!-- Settings -->
        <div id="tab-settings" class="tab-content">
            <div id="settings-content" class="loading">加载中...</div>
        </div>

    </div>

    <!-- 添加项目 Modal -->
    <div id="add-repo-modal" class="modal-overlay">
        <div class="modal">
            <h3>添加 GitHub 项目</h3>
            <div class="form-group">
                <label>GitHub Repo 地址 *</label>
                <input type="text" id="add-repo-url" placeholder="https://github.com/owner/repo-name" />
            </div>
            <div class="form-group">
                <label>别名 Alias（可选）</label>
                <input type="text" id="add-repo-alias" placeholder="留空则使用 repo name" />
            </div>
            <div id="add-repo-error" class="error-msg" style="display:none; margin-top: 10px;"></div>
            <div class="modal-actions">
                <button class="btn" style="background:#1e1e2e; color:#565f89; border-color:#2a2d3e;" onclick="closeAddRepoModal()">取消</button>
                <button class="btn btn-success" onclick="submitAddRepo()">添加</button>
            </div>
        </div>
    </div>

    <script>
        let repos = [];
        const charts = {};
        let runStatusInterval = null;

        const _basePath = (() => {
            const p = window.location.pathname.replace(/\/+$/, '');
            return p || '';
        })();

        async function fetchJSON(url, options) {
            const fullUrl = url.startsWith('http') ? url : `${_basePath}/${url}`;
            const r = await fetch(fullUrl, options);
            if (!r.ok) {
                const body = await r.text();
                throw new Error(`HTTP ${r.status}: ${body}`);
            }
            return r.json();
        }

        function escapeHtml(text) {
            return (text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function switchTab(name) {
            const names = ['overview', 'reports', 'trends', 'agents', 'settings'];
            document.querySelectorAll('.tab').forEach((t, i) => t.classList.toggle('active', names[i] === name));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.getElementById(`tab-${name}`).classList.add('active');
            if (name === 'agents') loadAgents();
            if (name === 'settings') loadSettings();
        }

        // ── Repo 管理 ──────────────────────────────────────────────────────────────

        function openAddRepoModal() {
            document.getElementById('add-repo-url').value = '';
            document.getElementById('add-repo-alias').value = '';
            document.getElementById('add-repo-error').style.display = 'none';
            document.getElementById('add-repo-modal').classList.add('open');
        }

        function closeAddRepoModal() {
            document.getElementById('add-repo-modal').classList.remove('open');
        }

        async function submitAddRepo() {
            const url = document.getElementById('add-repo-url').value.trim();
            const alias = document.getElementById('add-repo-alias').value.trim();
            const errEl = document.getElementById('add-repo-error');
            errEl.style.display = 'none';

            if (!url) { errEl.textContent = '请输入 GitHub Repo 地址'; errEl.style.display = 'block'; return; }

            try {
                await fetchJSON('api/repos', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, alias: alias || null }),
                });
                closeAddRepoModal();
                await loadOverview();
                await loadReportDates();
            } catch (e) {
                errEl.textContent = `添加失败: ${e.message}`;
                errEl.style.display = 'block';
            }
        }

        async function deleteRepo(fullName) {
            if (!confirm(`确认删除 ${fullName}？\n（只从监控列表移除，历史数据保留）`)) return;
            const [owner, name] = fullName.split('/');
            try {
                await fetchJSON(`api/repos/${owner}/${name}`, { method: 'DELETE' });
                await loadOverview();
                await loadReportDates();
            } catch (e) {
                alert(`删除失败: ${e.message}`);
            }
        }

        // ── 立即执行 ───────────────────────────────────────────────────────────────

        async function triggerRun() {
            const btn = document.getElementById('btn-run');
            btn.disabled = true;
            try {
                await fetchJSON('api/run', { method: 'POST' });
                startRunPolling();
            } catch (e) {
                alert(`启动失败: ${e.message}`);
                btn.disabled = false;
            }
        }

        function startRunPolling() {
            const statusbar = document.getElementById('run-statusbar');
            const statusText = document.getElementById('run-statusbar-text');
            statusbar.style.display = 'flex';

            if (runStatusInterval) clearInterval(runStatusInterval);
            runStatusInterval = setInterval(async () => {
                try {
                    const s = await fetchJSON('api/run/status');
                    if (!s.running) {
                        clearInterval(runStatusInterval);
                        runStatusInterval = null;
                        statusbar.style.display = 'none';
                        document.getElementById('btn-run').disabled = false;
                        if (s.result) {
                            statusText.textContent = s.result;
                            // 刷新数据
                            await loadOverview();
                            await loadReportDates();
                            await loadTodayInsight();
                        }
                        if (s.error) {
                            alert(`分析出错: ${s.error}`);
                        }
                    }
                } catch {}
            }, 3000);
        }

        // 页面加载时检查是否有任务在跑
        async function checkRunStatus() {
            try {
                const s = await fetchJSON('api/run/status');
                if (s.running) {
                    document.getElementById('btn-run').disabled = true;
                    startRunPolling();
                }
            } catch {}
        }

        // ── 概览 ───────────────────────────────────────────────────────────────────

        async function loadOverview() {
            try {
                const stats = await fetchJSON('api/stats');
                repos = stats;
                const totalIssues = stats.reduce((s, r) => s + r.issues_open, 0);
                const totalPRs = stats.reduce((s, r) => s + r.prs_open, 0);
                const totalMerged = stats.reduce((s, r) => s + (r.merged_prs_7d || 0), 0);
                const totalCommits = stats.reduce((s, r) => s + r.commits_7d, 0);

                let html = `<div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:14px; margin-bottom:22px;">
                    <div class="card">
                        <h3>Open Issues</h3>
                        <div class="stat-big">${totalIssues}</div>
                        <div class="stat-sub">跨 ${stats.length} 个项目</div>
                    </div>
                    <div class="card">
                        <h3>Open PRs</h3>
                        <div class="stat-big">${totalPRs}</div>
                        <div class="stat-sub">待合并</div>
                    </div>
                    <div class="card">
                        <h3>Merged PRs</h3>
                        <div class="stat-big">${totalMerged}</div>
                        <div class="stat-sub">近 7 天已合并</div>
                    </div>
                    <div class="card">
                        <h3>7日 Commits</h3>
                        <div class="stat-big">${totalCommits}</div>
                        <div class="stat-sub">近 7 天活跃度</div>
                    </div>
                </div>`;

                // 项目列表标题行（含添加按钮）
                html += `<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                    <div class="section-title" style="margin:0;">监控项目</div>
                    <button class="btn btn-success btn-sm" onclick="openAddRepoModal()">+ 添加项目</button>
                </div>`;

                for (const r of stats) {
                    const lastFetch = r.last_fetch ? r.last_fetch.substring(0, 16).replace('T', ' ') : '从未';
                    const release = r.latest_release
                        ? `${r.latest_release.tag_name} <span style="color:#565f89;">(${(r.latest_release.published_at || '').substring(0, 10)})</span>`
                        : '<span style="color:#565f89;">-</span>';
                    html += `<div class="repo-card">
                        <div class="repo-header">
                            <div class="repo-header-left">
                                <h2>${escapeHtml(r.display_name)}</h2>
                                <a href="https://github.com/${r.full_name}" target="_blank">${r.full_name} ↗</a>
                            </div>
                            <div class="repo-header-right">
                                <button class="btn btn-danger btn-sm delete-btn" onclick="deleteRepo('${r.full_name}')">删除</button>
                            </div>
                        </div>
                        <div class="repo-stats">
                            <div class="repo-stat">
                                <div class="num">${r.issues_open}</div>
                                <div class="label">Open Issues</div>
                            </div>
                            <div class="repo-stat">
                                <div class="num">${r.prs_open}</div>
                                <div class="label">Open PRs</div>
                            </div>
                            <div class="repo-stat">
                                <div class="num">${r.merged_prs_7d || 0}</div>
                                <div class="label">Merged PRs</div>
                            </div>
                            <div class="repo-stat">
                                <div class="num">${r.commits_7d}</div>
                                <div class="label">7日 Commits</div>
                            </div>
                            <div class="repo-stat" style="text-align:left; flex:1; margin-left:16px;">
                                <div style="font-size:12px; color:#565f89; text-transform:uppercase; letter-spacing:0.5px;">Latest Release</div>
                                <div style="font-size:13px; margin-top:4px;">${release}</div>
                                <div class="stat-sub" style="margin-top:6px;">最后采集: ${lastFetch}</div>
                            </div>
                        </div>
                    </div>`;
                }

                document.getElementById('repos-overview').innerHTML = html;
            } catch (e) {
                document.getElementById('repos-overview').innerHTML = `<div class="error-msg">加载失败: ${e.message}</div>`;
            }
        }

        async function loadReportDates() {
            try {
                const reports = await fetchJSON('api/reports?days=60');
                const dates = [...new Set(reports.map(r => r.report_date))].sort().reverse();
                const select = document.getElementById('report-date-select');
                select.innerHTML = '';

                if (dates.length === 0) {
                    select.innerHTML = '<option value="">暂无报告</option>';
                    document.getElementById('reports-content').innerHTML = '<div class="loading">暂无报告数据</div>';
                    return;
                }

                dates.forEach(d => {
                    const opt = document.createElement('option');
                    opt.value = d;
                    opt.textContent = d;
                    select.appendChild(opt);
                });

                select.value = dates[0];
                await loadReport();


            } catch (e) {
                document.getElementById('report-date-select').innerHTML = '<option value="">加载失败</option>';
            }
        }

        async function loadTodayInsight() {
            try {
                const reports = await fetchJSON('api/reports?days=7');
                const dates = [...new Set(reports.map(r => r.report_date))].sort().reverse();
                if (!dates.length) return;

                const latestDate = dates[0];
                const global = await fetchJSON(`api/report/${latestDate}`);

                document.getElementById('insight-date').textContent = latestDate;
                document.getElementById('insight-content').innerHTML = marked.parse(global.content || '');
                document.getElementById('today-insight').style.display = 'block';
            } catch (e) { /* 无报告时静默 */ }
        }

        async function loadReport() {
            const date = document.getElementById('report-date-select').value;
            if (!date) return;

            document.getElementById('reports-content').innerHTML = '<div class="loading">加载中...</div>';
            try {
                let html = '';

                try {
                    const global = await fetchJSON(`api/report/${date}`);
                    html += `<div class="report-block">
                        <div class="report-block-title">综合趋势分析</div>
                        <div class="md-content">${marked.parse(global.content)}</div>
                    </div>`;
                } catch (e) {
                    html += `<div class="report-block"><div class="error-msg">暂无综合分析</div></div>`;
                }

                const repoStats = await fetchJSON('api/repos');
                for (const r of repoStats) {
                    try {
                        const rep = await fetchJSON(`api/report/${date}?repo=${encodeURIComponent(r.full_name)}`);
                        html += `<div class="report-block">
                            <div class="report-block-title">${escapeHtml(r.display_name)}</div>
                            <div class="md-content">${marked.parse(rep.content)}</div>
                        </div>`;
                    } catch (e) {
                        html += `<div class="report-block">
                            <div class="report-block-title">${escapeHtml(r.display_name)}</div>
                            <div class="error-msg">暂无报告</div>
                        </div>`;
                    }
                }

                document.getElementById('reports-content').innerHTML = html || '<div class="error-msg">暂无报告</div>';
            } catch (e) {
                document.getElementById('reports-content').innerHTML = `<div class="error-msg">加载失败: ${e.message}</div>`;
            }
        }

        // ── 趋势颜色方案 ─────────────────────────────────────────────────────────────
        const TREND_COLORS = [
            { line: '#7aa2f7', fill: 'rgba(122,162,247,0.12)' },  // blue
            { line: '#9ece6a', fill: 'rgba(158,206,106,0.12)' },  // green
            { line: '#ff9e64', fill: 'rgba(255,158,100,0.12)' },  // orange
            { line: '#bb9af7', fill: 'rgba(187,154,247,0.12)' },  // purple
        ];

        function buildDateRange(days) {
            const dates = [];
            const today = new Date();
            for (let i = days - 1; i >= 0; i--) {
                const d = new Date(today);
                d.setDate(d.getDate() - i);
                dates.push(d.toISOString().slice(0, 10));
            }
            return dates;
        }

        function fillSeries(dateRange, dataArr) {
            const map = {};
            (dataArr || []).forEach(d => { map[d.day] = d.count; });
            return dateRange.map(d => map[d] || 0);
        }

        function makeMultiLineChart(canvasId, labels, datasets, chartDays) {
            const ctx = document.getElementById(canvasId).getContext('2d');
            if (charts[canvasId]) charts[canvasId].destroy();

            // 判断是否需要双 Y 轴：最大值差距超过 10 倍
            const maxValues = datasets.map(ds => Math.max(...ds.data, 1));
            const globalMax = Math.max(...maxValues);
            const globalMin = Math.min(...maxValues);
            const needDualAxis = globalMax / globalMin > 8 && datasets.length >= 2;

            // 如果需要双轴：数量级最大的放左轴，其余放右轴
            const sortedByMax = [...datasets].sort((a, b) => Math.max(...b.data) - Math.max(...a.data));
            const leftAxisRepos = new Set([sortedByMax[0].label]);

            const processedDatasets = datasets.map((ds, i) => {
                const isLeft = !needDualAxis || leftAxisRepos.has(ds.label);
                return {
                    ...ds,
                    yAxisID: needDualAxis ? (isLeft ? 'yLeft' : 'yRight') : 'y',
                };
            });

            const scales = needDualAxis ? {
                x: {
                    ticks: { color: '#565f89', font: { size: 10 }, maxTicksLimit: 7 },
                    grid: { color: 'rgba(42,45,62,0.8)' }
                },
                yLeft: {
                    position: 'left',
                    ticks: { color: '#7aa2f7', font: { size: 10 } },
                    grid: { color: 'rgba(42,45,62,0.8)' },
                    beginAtZero: true,
                    title: { display: true, text: sortedByMax[0].label, color: '#7aa2f7', font: { size: 10 } },
                },
                yRight: {
                    position: 'right',
                    ticks: { color: '#9ece6a', font: { size: 10 } },
                    grid: { drawOnChartArea: false },
                    beginAtZero: true,
                    title: { display: true, text: '其他项目', color: '#9ece6a', font: { size: 10 } },
                },
            } : {
                x: {
                    ticks: { color: '#565f89', font: { size: 10 }, maxTicksLimit: 7 },
                    grid: { color: 'rgba(42,45,62,0.8)' }
                },
                y: {
                    ticks: { color: '#565f89', font: { size: 10 } },
                    grid: { color: 'rgba(42,45,62,0.8)' },
                    beginAtZero: true,
                },
            };

            charts[canvasId] = new Chart(ctx, {
                type: 'line',
                data: { labels, datasets: processedDatasets },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: {
                            display: false,
                        },
                        tooltip: {
                            backgroundColor: '#1e1e2e',
                            borderColor: '#2a2d3e',
                            borderWidth: 1,
                            titleColor: '#e0e2f0',
                            bodyColor: '#a9b1d6',
                        }
                    },
                    scales,
                }
            });
        }

        function buildAvatarLegend(repoList) {
            let html = '<div style="display:flex; flex-wrap:wrap; gap:12px; margin-bottom:14px; align-items:center;">';
            repoList.forEach(([fullName, data], i) => {
                const color = TREND_COLORS[i % TREND_COLORS.length];
                const owner = fullName.split('/')[0];
                html += `
                    <div style="display:flex; align-items:center; gap:6px;">
                        <img src="https://github.com/${owner}.png?size=20"
                             style="width:20px; height:20px; border-radius:50%; border:1.5px solid ${color.line};"
                             onerror="this.style.display='none'">
                        <span style="width:10px; height:3px; background:${color.line}; border-radius:2px; display:inline-block;"></span>
                        <span style="font-size:12px; color:#a9b1d6;">${escapeHtml(data.display_name)}</span>
                    </div>`;
            });
            html += '</div>';
            return html;
        }

        async function loadTrends() {
            try {
                const trends = await fetchJSON('api/trends?days=14');
                const days = 14;
                const dateRange = buildDateRange(days);
                const repoList = Object.entries(trends);

                if (repoList.length === 0) {
                    document.getElementById('trends-content').innerHTML = '<div class="loading" style="color:#565f89;">暂无趋势数据</div>';
                    return;
                }

                // 三张图：Commits趋势 / Issues趋势 / PR趋势
                const legendHtml = buildAvatarLegend(repoList);

                // PR 图表有 toggle，其他两张图直接渲染
                let html = '';
                // Commits 图
                html += `<div class="card" style="margin-bottom: 16px;">
                    <h3 style="margin-bottom: 10px;">Commits 趋势（14天）</h3>
                    ${legendHtml}
                    <div style="height: 220px;"><canvas id="chart-commits"></canvas></div>
                </div>`;
                // Issues 图
                html += `<div class="card" style="margin-bottom: 16px;">
                    <h3 style="margin-bottom: 10px;">Issues 趋势（14天）</h3>
                    ${legendHtml}
                    <div style="height: 220px;"><canvas id="chart-issues"></canvas></div>
                </div>`;
                // PR 图（带 toggle）
                html += `<div class="card" style="margin-bottom: 16px;">
                    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
                        <h3>PR 趋势（14天）</h3>
                        <div class="pr-toggle-group" id="pr-toggle-group">
                            <button class="pr-toggle-btn active" data-mode="all" onclick="switchPRMode('all')">All</button>
                            <button class="pr-toggle-btn" data-mode="open" onclick="switchPRMode('open')">Open</button>
                            <button class="pr-toggle-btn" data-mode="merged" onclick="switchPRMode('merged')">Merged</button>
                        </div>
                    </div>
                    ${legendHtml}
                    <div style="height: 220px;"><canvas id="chart-prs"></canvas></div>
                </div>`;

                document.getElementById('trends-content').innerHTML = html;

                // 缓存 trends 数据供 toggle 使用
                window._trendsRepoList = repoList;
                window._trendsDateRange = dateRange;
                window._trendsDays = days;
                window._prMode = 'all';

                // 渲染 Commits 和 Issues 图（面积图）
                ['commits', 'issues'].forEach((dataKey, idx) => {
                    const canvasId = dataKey === 'commits' ? 'chart-commits' : 'chart-issues';
                    const datasets = repoList.map(([fullName, data], i) => {
                        const color = TREND_COLORS[i % TREND_COLORS.length];
                        return {
                            label: data.display_name,
                            data: fillSeries(dateRange, data[dataKey]),
                            borderColor: color.line,
                            backgroundColor: color.fill,
                            pointBackgroundColor: color.line,
                            borderWidth: 2,
                            pointRadius: 3,
                            pointHoverRadius: 5,
                            fill: true,
                            tension: 0.3,
                        };
                    });
                    makeMultiLineChart(canvasId, dateRange, datasets, days);
                });

                // 渲染 PR 图（默认 All 模式）
                renderPRChart('all');

            } catch (e) {
                document.getElementById('trends-content').innerHTML = `<div class="error-msg">加载失败: ${e.message}</div>`;
            }
        }

        function renderPRChart(mode) {
            const repoList = window._trendsRepoList;
            const dateRange = window._trendsDateRange;
            const days = window._trendsDays;
            if (!repoList) return;

            const datasets = repoList.map(([fullName, data], i) => {
                const color = TREND_COLORS[i % TREND_COLORS.length];
                let seriesData;
                if (mode === 'open') {
                    seriesData = fillSeries(dateRange, data['open_prs']);
                } else if (mode === 'merged') {
                    seriesData = fillSeries(dateRange, data['merged_prs']);
                } else {
                    // all = open + merged combined
                    const openArr = fillSeries(dateRange, data['open_prs']);
                    const mergedArr = fillSeries(dateRange, data['merged_prs']);
                    seriesData = openArr.map((v, idx) => v + mergedArr[idx]);
                }
                return {
                    label: data.display_name,
                    data: seriesData,
                    borderColor: color.line,
                    backgroundColor: color.fill,
                    pointBackgroundColor: color.line,
                    borderWidth: 2,
                    pointRadius: 3,
                    pointHoverRadius: 5,
                    fill: true,
                    tension: 0.3,
                };
            });
            makeMultiLineChart('chart-prs', dateRange, datasets, days);
        }

        function switchPRMode(mode) {
            window._prMode = mode;
            // 更新按钮状态
            document.querySelectorAll('.pr-toggle-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.mode === mode);
            });
            renderPRChart(mode);
        }

        // ── Agents ─────────────────────────────────────────────────────────────────

        let _agentsData = [];

        async function loadAgents() {
            try {
                const agents = await fetchJSON('api/agents');
                _agentsData = agents;
                renderAgents();
            } catch (e) {
                document.getElementById('agents-content').innerHTML = `<div class="error-msg">加载失败: ${e.message}</div>`;
            }
        }

        function renderAgents() {
            if (!_agentsData.length) {
                document.getElementById('agents-content').innerHTML = '<div class="loading" style="color:#565f89;">暂无 Agent 配置</div>';
                return;
            }
            let html = '';
            _agentsData.forEach(agent => {
                html += `<div class="agent-card" id="agent-card-${agent.id}">
                    <div class="agent-card-header">
                        <h2>${escapeHtml(agent.name)}</h2>
                        <div class="agent-card-actions" id="agent-actions-${agent.id}">
                            <button class="btn btn-primary btn-sm" onclick="editAgent('${agent.id}')">编辑</button>
                        </div>
                    </div>
                    <div class="agent-card-body">
                        <div id="agent-view-${agent.id}" class="md-content">${marked.parse(agent.content || '')}</div>
                        <textarea id="agent-textarea-${agent.id}" class="agent-textarea" style="display:none;">${escapeHtml(agent.content || '')}</textarea>
                    </div>
                </div>`;
            });
            document.getElementById('agents-content').innerHTML = html;
        }

        function editAgent(agentId) {
            const viewEl = document.getElementById(`agent-view-${agentId}`);
            const textareaEl = document.getElementById(`agent-textarea-${agentId}`);
            const actionsEl = document.getElementById(`agent-actions-${agentId}`);

            viewEl.style.display = 'none';
            textareaEl.style.display = 'block';

            actionsEl.innerHTML = `
                <button class="btn btn-success btn-sm" onclick="saveAgent('${agentId}')">保存</button>
                <button class="btn btn-sm" style="background:#1e1e2e; color:#565f89; border-color:#2a2d3e;" onclick="cancelEditAgent('${agentId}')">取消</button>
            `;
        }

        function cancelEditAgent(agentId) {
            const agent = _agentsData.find(a => a.id === agentId);
            if (!agent) return;

            const textareaEl = document.getElementById(`agent-textarea-${agentId}`);
            textareaEl.value = agent.content || '';
            textareaEl.style.display = 'none';

            const viewEl = document.getElementById(`agent-view-${agentId}`);
            viewEl.style.display = 'block';

            const actionsEl = document.getElementById(`agent-actions-${agentId}`);
            actionsEl.innerHTML = `<button class="btn btn-primary btn-sm" onclick="editAgent('${agentId}')">编辑</button>`;
        }

        async function saveAgent(agentId) {
            const textareaEl = document.getElementById(`agent-textarea-${agentId}`);
            const newContent = textareaEl.value;

            const saveBtn = document.querySelector(`#agent-actions-${agentId} .btn-success`);
            if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中...'; }

            try {
                await fetchJSON(`api/agents/${agentId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content: newContent }),
                });

                // 更新本地缓存
                const agent = _agentsData.find(a => a.id === agentId);
                if (agent) agent.content = newContent;

                // 切回渲染模式
                textareaEl.style.display = 'none';
                const viewEl = document.getElementById(`agent-view-${agentId}`);
                viewEl.innerHTML = marked.parse(newContent);
                viewEl.style.display = 'block';

                const actionsEl = document.getElementById(`agent-actions-${agentId}`);
                actionsEl.innerHTML = `<button class="btn btn-primary btn-sm" onclick="editAgent('${agentId}')">编辑</button>`;
            } catch (e) {
                if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = '保存'; }
                alert(`保存失败: ${e.message}`);
            }
        }

        // ── WebSocket 实时推送 ───────────────────────────────────────────────────────

        let _ws = null;
        let _wsReconnectTimer = null;

        function connectWebSocket() {
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${proto}//${location.host}${_basePath}/ws`;
            try {
                _ws = new WebSocket(wsUrl);
                _ws.onopen = () => {
                    console.log('[ws] 已连接');
                    if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }
                };
                _ws.onmessage = (e) => {
                    try {
                        const msg = JSON.parse(e.data);
                        if (msg.type === 'report_ready') {
                            console.log('[ws] 收到 report_ready，刷新数据...');
                            // 自动刷新当前 tab 数据
                            const activeTab = document.querySelector('.tab.active');
                            if (activeTab) {
                                const tabName = activeTab.getAttribute('onclick').match(/switchTab\('(.+?)'\)/)?.[1];
                                if (tabName === 'overview' || !tabName) {
                                    loadOverview(); loadTodayInsight();
                                } else if (tabName === 'reports') {
                                    loadReportDates();
                                } else if (tabName === 'trends') {
                                    loadTrends();
                                }
                            }
                            // 无论如何都刷新概览数据（后台）
                            loadOverview();
                        }
                    } catch {}
                };
                _ws.onclose = () => {
                    console.log('[ws] 连接断开，5秒后重连...');
                    _ws = null;
                    _wsReconnectTimer = setTimeout(connectWebSocket, 5000);
                };
                _ws.onerror = () => {
                    _ws = null;
                };
            } catch (e) {
                console.log('[ws] 连接失败，5秒后重试');
                _wsReconnectTimer = setTimeout(connectWebSocket, 5000);
            }
        }

        // ── Settings ─────────────────────────────────────────────────────────────

        let _settings = {};

        async function loadSettings() {
            try {
                _settings = await fetchJSON('api/settings');
                renderSettings();
            } catch (e) {
                document.getElementById('settings-content').innerHTML = `<div class="error-msg">加载失败: ${e.message}</div>`;
            }
        }

        function renderSettings() {
            const s = _settings;
            const hour = (s.schedule || {}).hour ?? 6;
            const minute = (s.schedule || {}).minute ?? 0;
            const wsEnabled = s.websocket_enabled !== false;
            const webhooks = s.webhooks || [];

            let webhookItemsHtml = webhooks.map((url, i) => `
                <div class="webhook-item" id="webhook-item-${i}">
                    <span>${escapeHtml(url)}</span>
                    <button class="btn btn-danger btn-sm" onclick="deleteWebhook(${i})">删除</button>
                    <button class="btn btn-sm" style="background:#1e2030; color:#7aa2f7; border-color:#2a2d3e;" onclick="testWebhook('${escapeHtml(url)}')">测试</button>
                </div>`).join('');

            const html = `
                <div class="settings-section">
                    <h3>定时执行</h3>
                    <p class="desc">本地时区，每天在指定时间自动执行采集和分析</p>
                    <div class="settings-row">
                        <label style="font-size:13px; color:#a9b1d6;">执行时间</label>
                        <input type="number" id="sched-hour" class="settings-input settings-input-sm" min="0" max="23" value="${hour}" placeholder="时">
                        <span style="color:#565f89;">时</span>
                        <input type="number" id="sched-minute" class="settings-input settings-input-sm" min="0" max="59" value="${String(minute).padStart(2,'0')}" placeholder="分">
                        <span style="color:#565f89;">分</span>
                        <button class="btn btn-primary btn-sm" onclick="saveSchedule()">保存</button>
                    </div>
                    <div id="sched-feedback" style="font-size:12px; color:#9ece6a; margin-top:6px; display:none;"></div>
                </div>

                <div class="settings-section">
                    <h3>WebSocket 实时推送</h3>
                    <p class="desc">开启后，dashboard 页面会在新报告生成时自动刷新</p>
                    <div class="settings-row">
                        <label class="toggle-switch">
                            <input type="checkbox" id="ws-toggle" ${wsEnabled ? 'checked' : ''} onchange="saveWsEnabled(this.checked)">
                            <span class="toggle-slider"></span>
                        </label>
                        <span style="font-size:13px; color:#a9b1d6;" id="ws-toggle-label">${wsEnabled ? '已开启' : '已关闭'}</span>
                    </div>
                </div>

                <div class="settings-section">
                    <h3>Webhook 通知</h3>
                    <p class="desc">报告生成后，向以下 URL 发送 POST 请求</p>
                    <div class="settings-row">
                        <input type="text" id="new-webhook-url" class="settings-input settings-input-url" placeholder="https://example.com/pulse-hook">
                        <button class="btn btn-success btn-sm" onclick="addWebhook()">添加</button>
                    </div>
                    <div id="webhook-error" class="error-msg" style="display:none; margin-bottom:10px;"></div>
                    <div class="webhook-list" id="webhook-list">
                        ${webhookItemsHtml || '<div style="color:#565f89; font-size:13px;">暂无 webhook</div>'}
                    </div>
                </div>`;

            document.getElementById('settings-content').innerHTML = html;

            // 绑定 toggle label 更新
            document.getElementById('ws-toggle').addEventListener('change', function() {
                document.getElementById('ws-toggle-label').textContent = this.checked ? '已开启' : '已关闭';
            });
        }

        async function saveSchedule() {
            const hour = parseInt(document.getElementById('sched-hour').value);
            const minute = parseInt(document.getElementById('sched-minute').value);
            if (isNaN(hour) || hour < 0 || hour > 23 || isNaN(minute) || minute < 0 || minute > 59) {
                alert('时间格式有误（小时0-23，分钟0-59）');
                return;
            }
            try {
                _settings = await fetchJSON('api/settings', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ schedule_hour: hour, schedule_minute: minute }),
                });
                const fb = document.getElementById('sched-feedback');
                fb.textContent = `已保存：每天 ${String(hour).padStart(2,'0')}:${String(minute).padStart(2,'0')} 执行`;
                fb.style.display = 'block';
                setTimeout(() => { fb.style.display = 'none'; }, 3000);
            } catch (e) {
                alert(`保存失败: ${e.message}`);
            }
        }

        async function saveWsEnabled(enabled) {
            try {
                _settings = await fetchJSON('api/settings', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ websocket_enabled: enabled }),
                });
            } catch (e) {
                alert(`保存失败: ${e.message}`);
            }
        }

        async function addWebhook() {
            const url = document.getElementById('new-webhook-url').value.trim();
            const errEl = document.getElementById('webhook-error');
            errEl.style.display = 'none';
            if (!url || !url.startsWith('http')) {
                errEl.textContent = '请输入有效的 URL（以 http:// 或 https:// 开头）';
                errEl.style.display = 'block';
                return;
            }
            const newWebhooks = [...(_settings.webhooks || []), url];
            try {
                _settings = await fetchJSON('api/settings', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ webhooks: newWebhooks }),
                });
                document.getElementById('new-webhook-url').value = '';
                renderSettings();
            } catch (e) {
                errEl.textContent = `添加失败: ${e.message}`;
                errEl.style.display = 'block';
            }
        }

        async function deleteWebhook(index) {
            const newWebhooks = (_settings.webhooks || []).filter((_, i) => i !== index);
            try {
                _settings = await fetchJSON('api/settings', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ webhooks: newWebhooks }),
                });
                renderSettings();
            } catch (e) {
                alert(`删除失败: ${e.message}`);
            }
        }

        async function testWebhook(url) {
            try {
                const r = await fetchJSON('api/settings/test-webhook', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url }),
                });
                alert(`测试成功，响应状态: ${r.status}`);
            } catch (e) {
                alert(`测试失败: ${e.message}`);
            }
        }

        // 点击 modal 外部关闭
        document.getElementById('add-repo-modal').addEventListener('click', function(e) {
            if (e.target === this) closeAddRepoModal();
        });

        // 初始化
        checkRunStatus();
        loadOverview();
        loadReportDates();
        loadTodayInsight();
        loadTrends();
        connectWebSocket();
    </script>
</body>
</html>"""
