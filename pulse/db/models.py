"""SQLite 数据库模型和初始化"""
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Generator

_db_path: str = "./data/pulse.db"


def set_db_path(path: str):
    global _db_path
    _db_path = path


def init_db(db_path: str = None):
    """初始化数据库，创建表结构"""
    if db_path:
        set_db_path(db_path)

    Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(_db_path)
    c = conn.cursor()

    # Repos 表
    c.execute("""
        CREATE TABLE IF NOT EXISTS repos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT,
            enabled INTEGER DEFAULT 1,
            added_at TEXT DEFAULT (datetime('now')),
            UNIQUE(owner, name)
        )
    """)

    # Issues 表
    c.execute("""
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_full_name TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            title TEXT,
            body TEXT,
            state TEXT,
            author TEXT,
            labels TEXT,
            created_at TEXT,
            updated_at TEXT,
            closed_at TEXT,
            comments INTEGER DEFAULT 0,
            url TEXT,
            fetched_at TEXT DEFAULT (datetime('now')),
            UNIQUE(repo_full_name, issue_number)
        )
    """)

    # Pull Requests 表
    c.execute("""
        CREATE TABLE IF NOT EXISTS pull_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_full_name TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            title TEXT,
            body TEXT,
            state TEXT,
            author TEXT,
            labels TEXT,
            base_branch TEXT,
            head_branch TEXT,
            created_at TEXT,
            updated_at TEXT,
            merged_at TEXT,
            url TEXT,
            fetched_at TEXT DEFAULT (datetime('now')),
            UNIQUE(repo_full_name, pr_number)
        )
    """)

    # Commits 表
    c.execute("""
        CREATE TABLE IF NOT EXISTS commits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_full_name TEXT NOT NULL,
            branch TEXT NOT NULL,
            sha TEXT NOT NULL,
            author TEXT,
            message TEXT,
            committed_at TEXT,
            url TEXT,
            fetched_at TEXT DEFAULT (datetime('now')),
            UNIQUE(repo_full_name, sha)
        )
    """)

    # Releases 表
    c.execute("""
        CREATE TABLE IF NOT EXISTS releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_full_name TEXT NOT NULL,
            tag_name TEXT NOT NULL,
            name TEXT,
            body TEXT,
            is_prerelease INTEGER DEFAULT 0,
            published_at TEXT,
            url TEXT,
            fetched_at TEXT DEFAULT (datetime('now')),
            UNIQUE(repo_full_name, tag_name)
        )
    """)

    # 分析报告表
    c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            repo_full_name TEXT,
            report_type TEXT NOT NULL,  -- 'repo' or 'global'
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(report_date, repo_full_name, report_type)
        )
    """)

    # Fetch 历史表（记录每次采集的时间和状态）
    c.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_full_name TEXT NOT NULL,
            fetch_type TEXT NOT NULL,  -- 'issues','prs','commits','releases'
            status TEXT NOT NULL,       -- 'success','error'
            items_count INTEGER DEFAULT 0,
            error_msg TEXT,
            fetched_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()


@contextmanager
def get_db(db_path: str = None) -> Generator[sqlite3.Connection, None, None]:
    """获取数据库连接（context manager）"""
    path = db_path or _db_path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
