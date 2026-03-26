"""LLM 分析层 — 调用 claude CLI 分析采集到的数据（四维度拆分）"""
import subprocess
import logging
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Callable

from ..config import AnalysisConfig, RepoConfig
from ..db.models import get_db

logger = logging.getLogger(__name__)


# 分析师 system prompt 目录
_ANALYSTS_DIR = Path(__file__).parent.parent.parent / ".claude" / "analysts"

def _load_analyst_prompt(analyst: str) -> str:
    """读取指定分析师的 system prompt
    analyst: "issues" | "prs" | "commits" | "synthesis"
    """
    path = _ANALYSTS_DIR / f"{analyst}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ── 维度 1：用户痛点与需求（Issues） ──────────────────────────────────────────────
ISSUES_PROMPT = """【严格要求】输出不得包含任何 emoji 字符。纯文字输出。

以下是 **{repo_name}** 过去 {days} 天的 Issues 数据（共 {issue_count} 条），代表用户真实声音。

{issues_text}

---

你是一名 AI agent 赛道分析师，请分析上述 issues，中文输出，**直接以下面标题开头，不要加前言**：

## 用户痛点与需求

基于 new issues 和 feature requests，分析用户真实的痛点和需求方向。要求：
- 提炼共性模式，不要罗列 issue 标题或编号
- 4-6 条 bullet，每条有洞察，并用具体数字或比例支撑（如"X 个 issue 反映了相同诉求"）
- 引用关键 issue 编号作为证据（如"如 #123 所示"），但不要罗列编号作为内容
- 关注：哪些需求被反复提出？哪些障碍影响核心工作流？用户期望产品向哪里延伸？

【输出规范】不要 emoji，第一行必须是"## 用户痛点与需求"，不要加前言或结论。
"""


# ── 维度 2：社区在解决什么（PRs） ──────────────────────────────────────────────
PRS_PROMPT = """【严格要求】输出不得包含任何 emoji 字符。纯文字输出。

以下是 **{repo_name}** 过去 {days} 天的 Pull Requests 数据（共 {pr_count} 条 open PR）。

{prs_text}

---

你是一名 AI agent 赛道分析师，请分析上述 PRs，中文输出，**直接以下面标题开头，不要加前言**：

## 社区在解决什么

基于 PRs 和 PR reviews，分析社区（尤其是非核心团队贡献者）在尝试解决什么问题。要求：
- 4-6 条 bullet，聚焦社区关注的问题方向和动机，带具体数量支撑
- 有多人同时贡献的方向特别值得关注（如"X 个 PR 都指向同一方向"）
- 引用关键 PR 编号作为证据（如"如 #456 所示"），但不要罗列编号作为内容
- 关注：社区在修什么（官方还没修的）？社区在补充什么能力（路线图之外的）？PR 停留时间说明了什么？

【输出规范】不要 emoji，第一行必须是"## 社区在解决什么"，不要加前言或结论。
"""


# ── 维度 3：官方工作重心与方向（非 main 分支 commits + merged PRs） ─────────────
BRANCH_COMMITS_PROMPT = """【严格要求】输出不得包含任何 emoji 字符。纯文字输出。

以下是 **{repo_name}** 过去 {days} 天的官方工作信号：

### Merged PRs（共 {merged_pr_count} 条）
（官方选择合并的 PR 是最直接的优先级声明）
{merged_prs_text}

### 非 main 分支 Commits（共 {branch_commit_count} 条）
（代表官方正在推进但尚未发布的工作方向）
{branch_commits_text}

---

你是一名 AI agent 赛道分析师，请分析官方工作重心，中文输出，**直接以下面标题开头，不要加前言**：

## 官方工作重心与方向

基于 merged PRs 和非 main 分支的 commits，分析官方真正在投入什么方向。要求：
- 4-6 条 bullet，聚焦投入方向和战略信号，带具体数量支撑
- 指出合并的主要方向分类（功能开发/bug 修复/性能优化等）及大致比例
- 引用关键 PR 编号作为证据（如"如 #789 所示"），但不要罗列编号作为内容
- 关注：官方在把资源往哪里押注？commit 集中在哪个模块或方向？非 main 分支说明未来什么方向将要落地？

【输出规范】不要 emoji，第一行必须是"## 官方工作重心与方向"，不要加前言或结论。
"""


# ── 维度 4：版本进度与节奏（main 分支 commits + releases） ────────────────────
MAIN_PROGRESS_PROMPT = """【严格要求】输出不得包含任何 emoji 字符。纯文字输出。

以下是 **{repo_name}** 过去 {days} 天的版本进度数据：

### Main 分支 Commits（共 {main_commit_count} 条）
（代表已经稳定、准备或已经发布的工作）
{main_commits_text}

### Releases（共 {release_count} 条）
{releases_text}

---

你是一名 AI agent 赛道分析师，请分析版本进度，中文输出，**直接以下面标题开头，不要加前言**：

## 版本进度与节奏

基于 main 分支 commits 和 releases，判断项目当前走到哪一步了。要求：
- 4-5 条 bullet，聚焦节奏和阶段判断，带具体数字支撑
- 明确给出区间内版本数或 commit 数
- 关注：发版频率如何？每次 release 的重量级？项目处于快速迭代 / 稳定维护 / 大版本冲刺 哪个阶段？

【输出规范】不要 emoji，第一行必须是"## 版本进度与节奏"，不要加前言或结论。
"""


# ── 全局综合分析 ────────────────────────────────────────────────────────────────
GLOBAL_SUMMARY_PROMPT = """【严格要求】输出不得包含任何 emoji 字符。不得使用表情符号、图标、符号标记（如 🔍📊🎯🚨📈 等）。纯文字 + Markdown 标题 + bullet points 输出。

以下是多个 AI agent 工具项目在 {date} 的分析报告（每个项目包含四个维度）：

{repo_summaries}

---

请输出跨项目综合观察，中文，**直接用以下结构输出，不要加前言或标题前缀**：

## 用户都在头疼什么

多个项目用户共同反映的痛点。哪些抱怨反复出现？这说明现阶段 AI 工具在哪里还没做好？用口语化方式描述。（3-4 条 bullet）

## 社区在往哪发力

社区贡献者在自发解决什么问题？哪些方向上有多个项目的社区同时在推进？这说明什么？（3-4 条 bullet）

## 官方重点做什么

各项目官方团队在把资源押在哪里？有什么共同的方向，有什么明显的分歧？这对行业走向意味着什么？（3-4 条 bullet）

## 值得关注的信号

这期最值得记住的 2-3 个观察。要有立场：不是陈述事实，而是说清楚"这意味着什么，对开发者或用户有什么影响"。（3-4 条 bullet，必须有观点）

【输出规范】禁止 emoji，禁止图标字符，禁止"折射出"、"其本质在于"等学术化表达，禁止重复各项目内容，要用平易近人的语言讲赛道故事。
"""


class LLMAnalyzer:
    def __init__(self, config: AnalysisConfig, db_path: str, broadcast_fn: Optional[Callable] = None):
        self.config = config
        self.db_path = db_path
        self.broadcast_fn = broadcast_fn  # optional: broadcast_event(type, data)

    def _broadcast(self, event_type: str, data: dict):
        """安全地广播事件（broadcast_fn 可能为 None）"""
        if self.broadcast_fn:
            try:
                self.broadcast_fn(event_type, data)
            except Exception as e:
                logger.warning(f"[ws] broadcast failed: {e}")

    def _call_claude(self, prompt: str, analyst: str = "synthesis") -> Optional[str]:
        """调用 claude CLI 进行分析（支持工具调用，允许读取临时文件）
        analyst: "issues" | "prs" | "commits" | "synthesis"
        """
        # 根据 analyst 类型选择模型
        if analyst in ("issues", "prs", "commits"):
            model = self.config.get_dimension_model()
        else:
            model = self.config.get_synthesis_model()

        try:
            system_prompt = _load_analyst_prompt(analyst)
            cmd = [
                self.config.claude_bin,
                "--model", model,
                "--allowedTools", "Read",
            ]
            if system_prompt:
                cmd += ["--system-prompt", system_prompt]

            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                logger.error(f"claude CLI error: {result.stderr}")
                return None
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error("claude CLI timeout")
            return None
        except FileNotFoundError:
            logger.error(f"claude CLI not found at: {self.config.claude_bin}")
            return None

    def _save_step(self, date: str, repo_full_name: str, step_name: str, analyst: str,
                   content: str, duration_s: float):
        """把分析步骤的中间结果存入 analysis_steps 表"""
        # 根据 analyst 类型获取实际使用的模型
        if analyst in ("issues", "prs", "commits"):
            model = self.config.get_dimension_model()
        else:
            model = self.config.get_synthesis_model()

        try:
            with get_db(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO analysis_steps
                    (report_date, repo_full_name, step_name, analyst, model, content, duration_s)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (date, repo_full_name, step_name, analyst, model, content, duration_s))
        except Exception as e:
            logger.warning(f"Failed to save analysis step {step_name}: {e}")

    def _write_tmp_file(self, repo_full_name: str, dimension: str, data: List[Dict]) -> str:
        """将全量数据写入临时 JSON 文件，返回文件路径"""
        today = datetime.now().strftime("%Y-%m-%d")
        safe_repo = repo_full_name.replace("/", "-")
        tmp_dir = Path("/tmp/pulse")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        filepath = tmp_dir / f"{safe_repo}-{dimension}-{today}.json"
        filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(filepath)

    def _get_recent_data(self, repo_full_name: str, days: int = 7) -> Dict:
        """从数据库获取数据：优先 24h 增量，无数据时回退到 7 天窗口"""
        # 先尝试 24h 数据
        window_hours = 24
        window_days = 1

        with get_db(self.db_path) as conn:
            count_24h = conn.execute("""
                SELECT COUNT(*) as c FROM issues
                WHERE repo_full_name = ? AND fetched_at >= datetime('now', '-1 day')
            """, (repo_full_name,)).fetchone()["c"]

            pr_count_24h = conn.execute("""
                SELECT COUNT(*) as c FROM pull_requests
                WHERE repo_full_name = ? AND fetched_at >= datetime('now', '-1 day')
            """, (repo_full_name,)).fetchone()["c"]

            commit_count_24h = conn.execute("""
                SELECT COUNT(*) as c FROM commits
                WHERE repo_full_name = ? AND fetched_at >= datetime('now', '-1 day')
            """, (repo_full_name,)).fetchone()["c"]

        total_24h = count_24h + pr_count_24h + commit_count_24h

        # 冷启动回退：24h 无数据时用 7 天
        if total_24h == 0:
            logger.info(f"  24h data empty for {repo_full_name}, falling back to 7-day window")
            window_days = 7
            window_label = "-7 days"
        else:
            logger.info(f"  24h data: {total_24h} items for {repo_full_name}")
            window_label = "-1 day"

        since = (datetime.now() - timedelta(days=window_days)).isoformat()

        with get_db(self.db_path) as conn:
            # Issues — 全量写文件，prompt 用前 50 条
            all_issues = conn.execute("""
                SELECT issue_number, title, state, author, labels, comments, created_at, url
                FROM issues
                WHERE repo_full_name = ?
                  AND fetched_at >= datetime('now', ?)
                ORDER BY updated_at DESC
            """, (repo_full_name, window_label)).fetchall()

            # Open PRs — 社区贡献（PRs 分析师用）
            all_open_prs = conn.execute("""
                SELECT pr_number, title, state, author, base_branch, head_branch, created_at, merged_at, url
                FROM pull_requests
                WHERE repo_full_name = ?
                  AND state = 'OPEN'
                  AND fetched_at >= datetime('now', ?)
                ORDER BY updated_at DESC
            """, (repo_full_name, window_label)).fetchall()

            # Merged PRs — 官方优先级（Commits 分析师用）
            all_merged_prs = conn.execute("""
                SELECT pr_number, title, state, author, base_branch, head_branch, created_at, merged_at, url
                FROM pull_requests
                WHERE repo_full_name = ?
                  AND merged_at IS NOT NULL
                  AND fetched_at >= datetime('now', ?)
                ORDER BY merged_at DESC
            """, (repo_full_name, window_label)).fetchall()

            # Main 分支 commits
            all_main_commits = conn.execute("""
                SELECT sha, branch, author, message, committed_at
                FROM commits
                WHERE repo_full_name = ?
                  AND (branch = 'main' OR branch = 'master')
                  AND fetched_at >= datetime('now', ?)
                ORDER BY committed_at DESC
            """, (repo_full_name, window_label)).fetchall()

            # 非 main 分支 commits
            all_branch_commits = conn.execute("""
                SELECT sha, branch, author, message, committed_at
                FROM commits
                WHERE repo_full_name = ?
                  AND branch NOT IN ('main', 'master')
                  AND fetched_at >= datetime('now', ?)
                ORDER BY committed_at DESC
            """, (repo_full_name, window_label)).fetchall()

            # Releases
            all_releases = conn.execute("""
                SELECT tag_name, name, is_prerelease, published_at, url
                FROM releases
                WHERE repo_full_name = ?
                  AND (published_at >= ? OR fetched_at >= datetime('now', ?))
                ORDER BY published_at DESC
                LIMIT 10
            """, (repo_full_name, since, window_label)).fetchall()

        issues_list = [dict(r) for r in all_issues]
        open_prs_list = [dict(r) for r in all_open_prs]
        merged_prs_list = [dict(r) for r in all_merged_prs]
        main_commits_list = [dict(r) for r in all_main_commits]
        branch_commits_list = [dict(r) for r in all_branch_commits]
        releases_list = [dict(r) for r in all_releases]

        # 写全量数据到临时文件
        issues_file = self._write_tmp_file(repo_full_name, "issues", issues_list)
        open_prs_file = self._write_tmp_file(repo_full_name, "open_prs", open_prs_list)
        merged_prs_file = self._write_tmp_file(repo_full_name, "merged_prs", merged_prs_list)
        main_commits_file = self._write_tmp_file(repo_full_name, "main_commits", main_commits_list)
        branch_commits_file = self._write_tmp_file(repo_full_name, "branch_commits", branch_commits_list)

        return {
            "window_days": window_days,
            "issues": issues_list,
            "issues_file": issues_file,
            "open_prs": open_prs_list,
            "open_prs_file": open_prs_file,
            "merged_prs": merged_prs_list,
            "merged_prs_file": merged_prs_file,
            "main_commits": main_commits_list,
            "main_commits_file": main_commits_file,
            "branch_commits": branch_commits_list,
            "branch_commits_file": branch_commits_file,
            "releases": releases_list,
        }

    def _format_issues(self, issues: List[Dict]) -> str:
        if not issues:
            return "（无数据）"
        lines = []
        for i in issues:
            labels = i.get("labels", "[]")
            try:
                label_list = json.loads(labels) if labels else []
                label_str = f" [{', '.join(label_list)}]" if label_list else ""
            except:
                label_str = ""
            lines.append(
                f"- #{i['issue_number']} [{i['state']}] {i['title']}{label_str}"
                f" (by {i['author']}, {i['comments']} comments)"
            )
        return "\n".join(lines)

    def _format_prs(self, prs: List[Dict], merged_only: bool = False) -> str:
        if not prs:
            return "（无数据）"
        lines = []
        for p in prs:
            is_merged = bool(p.get("merged_at"))
            if merged_only and not is_merged:
                continue
            status = "merged" if is_merged else p.get("state", "")
            lines.append(
                f"- #{p['pr_number']} [{status}] {p['title']}"
                f" ({p['head_branch']} → {p['base_branch']}, by {p['author']})"
            )
        return "\n".join(lines) if lines else "（无数据）"

    def _format_commits(self, commits: List[Dict]) -> str:
        if not commits:
            return "（无数据）"
        lines = []
        for c in commits:
            msg = (c.get("message") or "").split("\n")[0][:100]
            lines.append(f"- [{c['branch']}] {c['sha'][:8]} {msg} (by {c['author']})")
        return "\n".join(lines)

    def _format_releases(self, releases: List[Dict]) -> str:
        if not releases:
            return "（无数据）"
        lines = []
        for r in releases:
            prerelease = " [prerelease]" if r.get("is_prerelease") else ""
            lines.append(f"- {r['tag_name']}{prerelease}: {r['name']} ({r['published_at'][:10]})")
        return "\n".join(lines)

    def _build_data_section(self, data: List[Dict], format_fn, filepath: str, shown: int = 50) -> str:
        """构建数据段落：前 N 条 + 文件读取提示"""
        total = len(data)
        preview = data[:shown]
        preview_text = format_fn(preview)
        if total <= shown:
            return preview_text
        return (
            f"{preview_text}\n\n"
            f"（以上为前 {shown} 条，共 {total} 条。"
            f"如需查看全部数据，请读取文件：{filepath}）"
        )

    def _analyze_issues(self, repo_name: str, repo_full_name: str, data: Dict, days: int,
                        run_id: str = "") -> Optional[str]:
        """维度 1：用户痛点与需求（Issues）"""
        step_key = f"{repo_name}/issues"
        self._broadcast("step_start", {
            "run_id": run_id,
            "step": step_key,
            "analyst": "用户研究分析师",
            "repo": repo_name,
        })
        t0 = time.time()

        issues = data["issues"]
        issues_section = self._build_data_section(
            issues, self._format_issues, data["issues_file"]
        )
        prompt = ISSUES_PROMPT.format(
            repo_name=repo_name,
            days=days,
            issue_count=len(issues),
            issues_text=issues_section,
        )
        result = self._call_claude(prompt, analyst="issues")

        duration = time.time() - t0
        if result:
            today = datetime.now().strftime("%Y-%m-%d")
            self._save_step(today, repo_full_name, "issues", "issues", result, duration)

        self._broadcast("step_done", {
            "run_id": run_id,
            "step": step_key,
            "duration_s": round(duration, 1),
            "success": result is not None,
        })
        return result

    def _analyze_prs(self, repo_name: str, repo_full_name: str, data: Dict, days: int,
                     run_id: str = "") -> Optional[str]:
        """维度 2：社区在解决什么（Open PRs only）"""
        step_key = f"{repo_name}/prs"
        self._broadcast("step_start", {
            "run_id": run_id,
            "step": step_key,
            "analyst": "社区生态分析师",
            "repo": repo_name,
        })
        t0 = time.time()

        open_prs = data["open_prs"]
        prs_section = self._build_data_section(
            open_prs, self._format_prs, data["open_prs_file"]
        )
        prompt = PRS_PROMPT.format(
            repo_name=repo_name,
            days=days,
            pr_count=len(open_prs),
            prs_text=prs_section,
        )
        result = self._call_claude(prompt, analyst="prs")

        duration = time.time() - t0
        if result:
            today = datetime.now().strftime("%Y-%m-%d")
            self._save_step(today, repo_full_name, "prs", "prs", result, duration)

        self._broadcast("step_done", {
            "run_id": run_id,
            "step": step_key,
            "duration_s": round(duration, 1),
            "success": result is not None,
        })
        return result

    def _analyze_branch_commits(
        self,
        repo_name: str,
        repo_full_name: str,
        data: Dict,
        days: int,
        run_id: str = "",
    ) -> Optional[str]:
        """维度 3：官方工作重心与方向（merged PRs + 非 main 分支 commits）"""
        step_key = f"{repo_name}/commits"
        self._broadcast("step_start", {
            "run_id": run_id,
            "step": step_key,
            "analyst": "工程方向分析师",
            "repo": repo_name,
        })
        t0 = time.time()

        merged_prs = data["merged_prs"]
        branch_commits = data["branch_commits"]
        merged_prs_section = self._build_data_section(
            merged_prs, self._format_prs, data["merged_prs_file"]
        )
        branch_commits_section = self._build_data_section(
            branch_commits, self._format_commits, data["branch_commits_file"]
        )
        prompt = BRANCH_COMMITS_PROMPT.format(
            repo_name=repo_name,
            days=days,
            merged_pr_count=len(merged_prs),
            merged_prs_text=merged_prs_section,
            branch_commit_count=len(branch_commits),
            branch_commits_text=branch_commits_section,
        )
        result = self._call_claude(prompt, analyst="commits")

        duration = time.time() - t0
        if result:
            today = datetime.now().strftime("%Y-%m-%d")
            self._save_step(today, repo_full_name, "commits", "commits", result, duration)

        self._broadcast("step_done", {
            "run_id": run_id,
            "step": step_key,
            "duration_s": round(duration, 1),
            "success": result is not None,
        })
        return result

    def _analyze_main_progress(
        self,
        repo_name: str,
        repo_full_name: str,
        data: Dict,
        days: int,
        run_id: str = "",
    ) -> Optional[str]:
        """维度 4：版本进度与节奏（main 分支 commits + releases）"""
        step_key = f"{repo_name}/main"
        self._broadcast("step_start", {
            "run_id": run_id,
            "step": step_key,
            "analyst": "版本节奏分析师",
            "repo": repo_name,
        })
        t0 = time.time()

        main_commits = data["main_commits"]
        releases = data["releases"]
        main_commits_section = self._build_data_section(
            main_commits, self._format_commits, data["main_commits_file"]
        )
        prompt = MAIN_PROGRESS_PROMPT.format(
            repo_name=repo_name,
            days=days,
            main_commit_count=len(main_commits),
            main_commits_text=main_commits_section,
            release_count=len(releases),
            releases_text=self._format_releases(releases),
        )
        result = self._call_claude(prompt, analyst="commits")

        duration = time.time() - t0
        if result:
            today = datetime.now().strftime("%Y-%m-%d")
            self._save_step(today, repo_full_name, "main", "commits", result, duration)

        self._broadcast("step_done", {
            "run_id": run_id,
            "step": step_key,
            "duration_s": round(duration, 1),
            "success": result is not None,
        })
        return result

    def analyze_repo(self, repo: RepoConfig, days: int = 7, run_id: str = "") -> Optional[str]:
        """分析单个 repo：4个维度分析，返回四角度结构 Markdown 报告"""
        logger.info(f"Analyzing {repo.full_name} (4-angle mode)")
        data = self._get_recent_data(repo.full_name, days)
        effective_days = data["window_days"]

        # 4 个维度并行分析
        logger.info(f"  [parallel] Starting 4-dimension analysis for {repo.display_name} ({effective_days}d window)...")
        dim_results = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(self._analyze_issues, repo.display_name, repo.full_name, data, effective_days, run_id): 'issues',
                executor.submit(self._analyze_prs, repo.display_name, repo.full_name, data, effective_days, run_id): 'prs',
                executor.submit(self._analyze_branch_commits, repo.display_name, repo.full_name, data, effective_days, run_id): 'branch',
                executor.submit(self._analyze_main_progress, repo.display_name, repo.full_name, data, effective_days, run_id): 'main',
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    dim_results[key] = future.result()
                    logger.info(f"  [done] {key} analysis for {repo.display_name}")
                except Exception as e:
                    logger.error(f"  [error] {key} analysis for {repo.display_name}: {e}")
                    dim_results[key] = None

        issues_analysis = dim_results.get('issues')
        prs_analysis = dim_results.get('prs')
        branch_analysis = dim_results.get('branch')
        main_analysis = dim_results.get('main')

        # 合并四个维度（直接拼接，每个维度已包含标题）
        sections = [s for s in [issues_analysis, prs_analysis, branch_analysis, main_analysis] if s]
        analysis = "\n\n".join(sections) if sections else None

        if analysis:
            today = datetime.now().strftime("%Y-%m-%d")
            with get_db(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO reports
                    (report_date, repo_full_name, report_type, content)
                    VALUES (?, ?, 'repo', ?)
                """, (today, repo.full_name, analysis))
            logger.info(f"  Saved 4-angle report for {repo.full_name}")

        return analysis

    def analyze_global(self, repo_summaries: Dict[str, str], run_id: str = "") -> Optional[str]:
        """生成跨 repo 的四角度综合分析"""
        logger.info("Generating global 4-angle analysis")

        step_key = "global/synthesis"
        self._broadcast("step_start", {
            "run_id": run_id,
            "step": step_key,
            "analyst": "综合分析师",
        })
        t0 = time.time()

        summaries_text = ""
        for repo_name, summary in repo_summaries.items():
            summaries_text += f"\n## {repo_name}\n{summary}\n\n"

        today = datetime.now().strftime("%Y-%m-%d")
        prompt = GLOBAL_SUMMARY_PROMPT.format(
            date=today,
            repo_summaries=summaries_text,
        )

        analysis = self._call_claude(prompt, analyst="synthesis")
        duration = time.time() - t0

        if analysis:
            with get_db(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO reports
                    (report_date, repo_full_name, report_type, content)
                    VALUES (?, NULL, 'global', ?)
                """, (today, analysis))
            # save step (repo_full_name = '__global__' for global synthesis)
            self._save_step(today, "__global__", "synthesis", "synthesis", analysis, duration)

        self._broadcast("step_done", {
            "run_id": run_id,
            "step": step_key,
            "duration_s": round(duration, 1),
            "success": analysis is not None,
        })

        return analysis

    def cleanup_old_data(self, days: int = 40):
        """清理超过 N 天的旧数据（滚动清理）"""
        logger.info(f"Cleaning up data older than {days} days...")
        with get_db(self.db_path) as conn:
            results = {}
            for table, col in [
                ("issues", "fetched_at"),
                ("pull_requests", "fetched_at"),
                ("commits", "fetched_at"),
                ("releases", "fetched_at"),
                ("fetch_log", "fetched_at"),
            ]:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE {col} < datetime('now', ?)",
                    (f"-{days} days",)
                )
                results[table] = cur.rowcount

            cur = conn.execute(
                "DELETE FROM reports WHERE report_date < date('now', ?)",
                (f"-{days} days",)
            )
            results["reports"] = cur.rowcount

            cur = conn.execute(
                "DELETE FROM analysis_steps WHERE report_date < date('now', ?)",
                (f"-{days} days",)
            )
            results["analysis_steps"] = cur.rowcount

        logger.info(f"Cleanup done: {results}")

    def get_report(self, date: str, repo_full_name: Optional[str] = None) -> Optional[str]:
        """从数据库获取历史报告"""
        report_type = "repo" if repo_full_name else "global"
        with get_db(self.db_path) as conn:
            row = conn.execute("""
                SELECT content FROM reports
                WHERE report_date = ? AND report_type = ?
                  AND (repo_full_name = ? OR repo_full_name IS NULL)
                LIMIT 1
            """, (date, report_type, repo_full_name)).fetchone()
        return row["content"] if row else None
