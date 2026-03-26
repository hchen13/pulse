"""Pulse Daemon — 定时调度采集+分析+推送"""
import logging
import signal
import sys
import time
from datetime import datetime
from typing import Optional

from .config import load_config, PulseConfig
from .db.models import init_db
from .collectors.github import GitHubCollector
from .analyzers.llm import LLMAnalyzer
from .notifiers.feishu import FeishuNotifier

logger = logging.getLogger("pulse.daemon")


class PulseDaemon:
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self.running = False

        # 注册信号处理
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def _reload_config(self) -> PulseConfig:
        """每次执行前重新加载配置（支持热更新）"""
        return load_config(self.config_path)

    def run_once(self, push: bool = True) -> dict:
        """执行一次完整的采集+分析+推送流程"""
        logger.info(f"=== Pulse 开始执行 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
        cfg = self._reload_config()

        collector = GitHubCollector(cfg.collection, cfg.storage.db_path)
        analyzer = LLMAnalyzer(cfg.analysis, cfg.storage.db_path)
        notifier = FeishuNotifier(cfg.notification)

        fetch_results = {}
        repo_reports = {}
        errors = []

        # 1. 采集数据
        for repo in cfg.enabled_repos:
            logger.info(f"采集 {repo.full_name} ...")
            try:
                results = collector.fetch_all(repo)
                fetch_results[repo.full_name] = results
                logger.info(f"✓ {repo.full_name}: {results}")
            except Exception as e:
                logger.error(f"采集 {repo.full_name} 失败: {e}")
                errors.append(f"采集失败: {repo.full_name} — {e}")

        # 2. LLM 分析
        for repo in cfg.enabled_repos:
            logger.info(f"分析 {repo.full_name} ...")
            try:
                analysis = analyzer.analyze_repo(repo)
                if analysis:
                    repo_reports[repo.display_name] = analysis
                    logger.info(f"✓ 分析完成: {repo.full_name}")
                else:
                    logger.warning(f"分析返回空: {repo.full_name}")
            except Exception as e:
                logger.error(f"分析 {repo.full_name} 失败: {e}")
                errors.append(f"分析失败: {repo.full_name} — {e}")

        # 3. 生成全局分析
        global_report = None
        if len(repo_reports) > 1:
            logger.info("生成综合分析...")
            try:
                global_report = analyzer.analyze_global(repo_reports)
                logger.info("✓ 综合分析完成")
            except Exception as e:
                logger.error(f"综合分析失败: {e}")

        # 3.5 清理旧数据（40天滚动）
        try:
            analyzer.cleanup_old_data(days=40)
        except Exception as e:
            logger.warning(f"数据清理失败（非致命）: {e}")

        # 4. 飞书推送
        if push and cfg.notification.feishu.enabled:
            logger.info("推送飞书...")
            try:
                ok = notifier.send_daily_report(repo_reports, global_report or "")
                if ok:
                    logger.info("✓ 飞书推送成功")
                else:
                    logger.warning("飞书推送失败")
            except Exception as e:
                logger.error(f"飞书推送异常: {e}")

        result = {
            "fetch_results": fetch_results,
            "repo_reports": list(repo_reports.keys()),
            "has_global": global_report is not None,
            "errors": errors,
        }
        logger.info(f"=== Pulse 执行完成 ===")
        return result

    def start(self, push: bool = True):
        """启动 daemon，按 cron 定时执行"""
        try:
            from croniter import croniter
        except ImportError:
            logger.error("缺少依赖: pip install croniter")
            sys.exit(1)

        cfg = self._reload_config()
        cron_expr = cfg.schedule.cron
        logger.info(f"Pulse Daemon 启动，cron: {cron_expr}")

        self.running = True
        cron = croniter(cron_expr, datetime.now())
        next_run = cron.get_next(datetime)
        logger.info(f"下次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")

        while self.running:
            now = datetime.now()
            if now >= next_run:
                try:
                    self.run_once(push=push)
                except Exception as e:
                    logger.error(f"执行异常: {e}")

                # 计算下一次执行时间
                cfg = self._reload_config()  # 重新加载（可能 cron 改了）
                cron = croniter(cfg.schedule.cron, datetime.now())
                next_run = cron.get_next(datetime)
                logger.info(f"下次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")

            # 每 30 秒检查一次
            time.sleep(30)

        logger.info("Pulse Daemon 已停止")
