"""GitHub 数据采集层 — 使用 gh CLI"""
import json
import subprocess
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from ..config import RepoConfig, CollectionConfig
from ..db.models import get_db

logger = logging.getLogger(__name__)


def _run_gh(args: List[str]) -> Optional[Any]:
    """运行 gh CLI 命令，返回解析后的 JSON"""
    cmd = ["gh"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.error(f"gh command failed: {' '.join(cmd)}\n{result.stderr}")
            return None
        if result.stdout.strip():
            return json.loads(result.stdout)
        return []
    except subprocess.TimeoutExpired:
        logger.error(f"gh command timeout: {' '.join(cmd)}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}\nOutput: {result.stdout[:500]}")
        return None


class GitHubCollector:
    def __init__(self, config: CollectionConfig, db_path: str):
        self.config = config
        self.db_path = db_path

    def fetch_all(self, repo: RepoConfig) -> Dict[str, int]:
        """采集所有维度数据，返回各维度的数量"""
        results = {}
        results["issues"] = self.fetch_issues(repo)
        results["prs"] = self.fetch_prs(repo)
        results["commits"] = self.fetch_commits(repo)
        results["releases"] = self.fetch_releases(repo)
        return results

    def fetch_issues(self, repo: RepoConfig) -> int:
        """采集 Issues（open + 最近关闭）"""
        full_name = repo.full_name
        logger.info(f"Fetching issues for {full_name}")

        # 采集 open issues
        data = _run_gh([
            "issue", "list",
            "--repo", full_name,
            "--state", "open",
            "--limit", str(self.config.max_issues),
            "--json", "number,title,body,state,author,labels,createdAt,updatedAt,closedAt,url",
        ])

        # 也采集最近关闭的（最近7天）
        closed_data = _run_gh([
            "issue", "list",
            "--repo", full_name,
            "--state", "closed",
            "--limit", str(self.config.max_issues // 2),
            "--json", "number,title,body,state,author,labels,createdAt,updatedAt,closedAt,url",
        ])

        all_issues = (data or []) + (closed_data or [])
        count = 0

        with get_db(self.db_path) as conn:
            for issue in all_issues:
                try:
                    labels = json.dumps([l.get("name", "") for l in issue.get("labels", [])])
                    author = issue.get("author", {})
                    author_login = author.get("login", "") if isinstance(author, dict) else str(author)

                    # comments 字段在 gh CLI 返回的是对象列表，取长度
                    comments_val = issue.get("comments", 0)
                    if isinstance(comments_val, list):
                        comments_val = len(comments_val)

                    conn.execute("""
                        INSERT OR REPLACE INTO issues
                        (repo_full_name, issue_number, title, body, state, author, labels,
                         created_at, updated_at, closed_at, comments, url, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """, (
                        full_name,
                        issue["number"],
                        issue.get("title", ""),
                        (issue.get("body") or "")[:5000],  # 截断过长的 body
                        issue.get("state", ""),
                        author_login,
                        labels,
                        issue.get("createdAt", ""),
                        issue.get("updatedAt", ""),
                        issue.get("closedAt", ""),
                        comments_val,
                        issue.get("url", ""),
                    ))
                    count += 1
                except Exception as e:
                    logger.warning(f"Failed to insert issue #{issue.get('number')}: {e}")

            self._log_fetch(conn, full_name, "issues", "success", count)

        logger.info(f"Fetched {count} issues for {full_name}")
        return count

    def fetch_prs(self, repo: RepoConfig) -> int:
        """采集 PRs（open + 最近 merged）"""
        full_name = repo.full_name
        logger.info(f"Fetching PRs for {full_name}")

        open_data = _run_gh([
            "pr", "list",
            "--repo", full_name,
            "--state", "open",
            "--limit", str(self.config.max_prs),
            "--json", "number,title,body,state,author,labels,baseRefName,headRefName,createdAt,updatedAt,mergedAt,url",
        ])

        merged_data = _run_gh([
            "pr", "list",
            "--repo", full_name,
            "--state", "merged",
            "--limit", str(self.config.max_prs // 2),
            "--json", "number,title,body,state,author,labels,baseRefName,headRefName,createdAt,updatedAt,mergedAt,url",
        ])

        all_prs = (open_data or []) + (merged_data or [])
        count = 0

        with get_db(self.db_path) as conn:
            for pr in all_prs:
                try:
                    labels = json.dumps([l.get("name", "") for l in pr.get("labels", [])])
                    author = pr.get("author", {})
                    author_login = author.get("login", "") if isinstance(author, dict) else str(author)

                    conn.execute("""
                        INSERT OR REPLACE INTO pull_requests
                        (repo_full_name, pr_number, title, body, state, author, labels,
                         base_branch, head_branch, created_at, updated_at, merged_at, url, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """, (
                        full_name,
                        pr["number"],
                        pr.get("title", ""),
                        (pr.get("body") or "")[:5000],
                        pr.get("state", ""),
                        author_login,
                        labels,
                        pr.get("baseRefName", ""),
                        pr.get("headRefName", ""),
                        pr.get("createdAt", ""),
                        pr.get("updatedAt", ""),
                        pr.get("mergedAt", ""),
                        pr.get("url", ""),
                    ))
                    count += 1
                except Exception as e:
                    logger.warning(f"Failed to insert PR #{pr.get('number')}: {e}")

            self._log_fetch(conn, full_name, "prs", "success", count)

        logger.info(f"Fetched {count} PRs for {full_name}")
        return count

    def fetch_commits(self, repo: RepoConfig) -> int:
        """采集所有分支的最新 commits（自动发现）"""
        full_name = repo.full_name
        logger.info(f"Fetching commits for {full_name}")

        # 自动拉取所有分支
        branches_data = _run_gh([
            "api",
            f"repos/{full_name}/branches",
            "--method", "GET",
            "--field", "per_page=100",
        ])

        branches_to_check = []
        if branches_data and isinstance(branches_data, list):
            branches_to_check = [b["name"] for b in branches_data if isinstance(b, dict) and "name" in b]
            logger.info(f"Auto-discovered {len(branches_to_check)} branches for {full_name}")
        else:
            # fallback: 只拉默认分支
            repo_info = _run_gh([
                "repo", "view", full_name,
                "--json", "defaultBranchRef",
            ])
            if repo_info:
                default_branch = repo_info.get("defaultBranchRef", {}).get("name", "main")
                branches_to_check = [default_branch]
            else:
                branches_to_check = ["main"]

        count = 0
        with get_db(self.db_path) as conn:
            for branch in branches_to_check:
                data = _run_gh([
                    "api",
                    f"repos/{full_name}/commits",
                    "--method", "GET",
                    "--field", f"sha={branch}",
                    "--field", f"per_page={self.config.max_commits_per_branch}",
                ])

                if not data:
                    continue

                for commit in data:
                    try:
                        sha = commit.get("sha", "")
                        commit_data = commit.get("commit", {})
                        author_data = commit_data.get("author", {})
                        gh_author = commit.get("author") or {}

                        conn.execute("""
                            INSERT OR IGNORE INTO commits
                            (repo_full_name, branch, sha, author, message, committed_at, url, fetched_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        """, (
                            full_name,
                            branch,
                            sha,
                            gh_author.get("login", author_data.get("name", "")),
                            commit_data.get("message", "")[:2000],
                            author_data.get("date", ""),
                            commit.get("html_url", ""),
                        ))
                        count += 1
                    except Exception as e:
                        logger.warning(f"Failed to insert commit {commit.get('sha', '')[:8]}: {e}")

            self._log_fetch(conn, full_name, "commits", "success", count)

        logger.info(f"Fetched {count} commits for {full_name}")
        return count

    def fetch_releases(self, repo: RepoConfig) -> int:
        """采集最新 Releases"""
        full_name = repo.full_name
        logger.info(f"Fetching releases for {full_name}")

        data = _run_gh([
            "release", "list",
            "--repo", full_name,
            "--limit", str(self.config.max_releases),
            "--json", "tagName,name,isPrerelease,publishedAt",
        ])

        count = 0
        if data:
            with get_db(self.db_path) as conn:
                for release in data:
                    try:
                        # 获取 release body（notes）
                        rel_detail = _run_gh([
                            "release", "view",
                            release["tagName"],
                            "--repo", full_name,
                            "--json", "body",
                        ])
                        body = rel_detail.get("body", "") if rel_detail else ""

                        release_url = f"https://github.com/{full_name}/releases/tag/{release['tagName']}"
                        conn.execute("""
                            INSERT OR REPLACE INTO releases
                            (repo_full_name, tag_name, name, body, is_prerelease, published_at, url, fetched_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        """, (
                            full_name,
                            release["tagName"],
                            release.get("name", ""),
                            (body or "")[:5000],
                            1 if release.get("isPrerelease") else 0,
                            release.get("publishedAt", ""),
                            release_url,
                        ))
                        count += 1
                    except Exception as e:
                        logger.warning(f"Failed to insert release {release.get('tagName')}: {e}")

                self._log_fetch(conn, full_name, "releases", "success", count)

        logger.info(f"Fetched {count} releases for {full_name}")
        return count

    def _log_fetch(self, conn, repo_full_name: str, fetch_type: str, status: str,
                   count: int, error_msg: str = None):
        """记录采集日志"""
        conn.execute("""
            INSERT INTO fetch_log (repo_full_name, fetch_type, status, items_count, error_msg)
            VALUES (?, ?, ?, ?, ?)
        """, (repo_full_name, fetch_type, status, count, error_msg))
