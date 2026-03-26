"""飞书推送模块 — 通过 OpenClaw message 工具推送给 Ethan"""
import subprocess
import json
import logging
from datetime import datetime
from typing import Optional, Dict

from ..config import NotificationConfig

logger = logging.getLogger(__name__)


class FeishuNotifier:
    def __init__(self, config: NotificationConfig):
        self.config = config

    def _send_via_openclaw(self, message: str) -> bool:
        """推送接口（预留）— 当前不实际发送，后续通过 OpenClaw skill/plugin 接入"""
        # NOTE: 飞书直推已禁用。推送层保留接口，后续走 OpenClaw plugin 接入。
        # 原 openclaw message CLI 调用代码已注释掉：
        #
        # result = subprocess.run(
        #     ["openclaw", "message", "send", "--channel", "feishu",
        #      "--target", f"user:{self.config.feishu.user_open_id}",
        #      "--account", "xinge", "--text", message],
        #     capture_output=True, text=True, timeout=30,
        # )
        logger.info(f"[Feishu notifier] push skipped (disabled). Message length: {len(message)}")
        return True  # 返回 True 避免 daemon 报错，视为静默成功

    def send_daily_report(self, repo_reports: Dict[str, str], global_report: str) -> bool:
        """推送每日日报"""
        if not self.config.feishu.enabled:
            logger.info("Feishu notification disabled")
            return False

        today = datetime.now().strftime("%Y-%m-%d")
        weekday_map = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日"}
        weekday = weekday_map[datetime.now().weekday()]

        # 构建日报消息
        parts = [
            f"# Pulse 日报 · {today} {weekday}",
            f"AI Harness 情报系统 | 监控 {len(repo_reports)} 个项目",
            "",
        ]

        # 各 repo 摘要
        for repo_name, report in repo_reports.items():
            parts.append(f"---")
            parts.append(f"## {repo_name}")
            # 截取报告的关键部分（前1500字）
            parts.append(report[:1500] if len(report) > 1500 else report)
            parts.append("")

        # 全局总结
        if global_report:
            parts.append("---")
            parts.append("## 综合趋势分析")
            parts.append(global_report[:1500] if len(global_report) > 1500 else global_report)

        message = "\n".join(parts)
        return self._send_via_openclaw(message)

    def send_alert(self, title: str, content: str) -> bool:
        """推送即时告警"""
        message = f"**[Pulse 告警]** {title}\n\n{content}"
        return self._send_via_openclaw(message)

    def send_fetch_summary(self, results: Dict) -> bool:
        """推送采集摘要"""
        lines = [f"**Pulse 采集完成** ({datetime.now().strftime('%H:%M')})"]
        for repo, counts in results.items():
            line = f"• {repo}: issues={counts.get('issues', 0)}, prs={counts.get('prs', 0)}, commits={counts.get('commits', 0)}"
            lines.append(line)
        message = "\n".join(lines)
        return self._send_via_openclaw(message)
