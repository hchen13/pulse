"""配置加载模块"""
import os
from pathlib import Path
from typing import List, Optional
import yaml
from dataclasses import dataclass, field


@dataclass
class RepoConfig:
    owner: str
    name: str
    display_name: str
    enabled: bool = True

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class CollectionConfig:
    max_issues: int = 50
    max_prs: int = 30
    max_commits_per_branch: int = 20
    max_releases: int = 10
    watch_branches: List[str] = field(default_factory=list)


@dataclass
class ScheduleConfig:
    cron: str = "0 8 * * *"


@dataclass
class AnalysisConfig:
    claude_bin: str = "claude"
    model: str = "claude-haiku-4-5"
    max_tokens: int = 2000


@dataclass
class FeishuConfig:
    enabled: bool = True
    user_open_id: str = ""
    webhook_url: str = ""


@dataclass
class NotificationConfig:
    feishu: FeishuConfig = field(default_factory=FeishuConfig)


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8765
    debug: bool = False


@dataclass
class StorageConfig:
    db_path: str = "./data/pulse.db"
    log_dir: str = "./logs"


@dataclass
class PulseConfig:
    repos: List[RepoConfig] = field(default_factory=list)
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    web: WebConfig = field(default_factory=WebConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    @property
    def enabled_repos(self) -> List[RepoConfig]:
        return [r for r in self.repos if r.enabled]


def load_config(config_path: Optional[str] = None) -> PulseConfig:
    """加载配置文件，返回 PulseConfig 对象"""
    if config_path is None:
        # 默认在项目根目录找 config.yaml
        config_path = Path(__file__).parent.parent / "config.yaml"

    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # 解析 repos
    repos = []
    for r in raw.get("repos", []):
        repos.append(RepoConfig(
            owner=r["owner"],
            name=r["name"],
            display_name=r.get("display_name", f"{r['owner']}/{r['name']}"),
            enabled=r.get("enabled", True),
        ))

    # 解析 collection
    col_raw = raw.get("collection", {})
    collection = CollectionConfig(
        max_issues=col_raw.get("max_issues", 50),
        max_prs=col_raw.get("max_prs", 30),
        max_commits_per_branch=col_raw.get("max_commits_per_branch", 20),
        max_releases=col_raw.get("max_releases", 10),
        watch_branches=col_raw.get("watch_branches", []),
    )

    # 解析 schedule
    sched_raw = raw.get("schedule", {})
    schedule = ScheduleConfig(cron=sched_raw.get("cron", "0 8 * * *"))

    # 解析 analysis
    anal_raw = raw.get("analysis", {})
    analysis = AnalysisConfig(
        claude_bin=anal_raw.get("claude_bin", "claude"),
        model=anal_raw.get("model", "claude-haiku-4-5"),
        max_tokens=anal_raw.get("max_tokens", 2000),
    )

    # 解析 notification
    notif_raw = raw.get("notification", {})
    feishu_raw = notif_raw.get("feishu", {})
    feishu = FeishuConfig(
        enabled=feishu_raw.get("enabled", True),
        user_open_id=feishu_raw.get("user_open_id", ""),
        webhook_url=feishu_raw.get("webhook_url", ""),
    )
    notification = NotificationConfig(feishu=feishu)

    # 解析 web
    web_raw = raw.get("web", {})
    web = WebConfig(
        host=web_raw.get("host", "0.0.0.0"),
        port=web_raw.get("port", 8765),
        debug=web_raw.get("debug", False),
    )

    # 解析 storage
    stor_raw = raw.get("storage", {})
    storage = StorageConfig(
        db_path=stor_raw.get("db_path", "./data/pulse.db"),
        log_dir=stor_raw.get("log_dir", "./logs"),
    )

    return PulseConfig(
        repos=repos,
        collection=collection,
        schedule=schedule,
        analysis=analysis,
        notification=notification,
        web=web,
        storage=storage,
    )
