"""Pulse 主入口 — CLI + Daemon + Web server"""
import click
import logging
import sys
import uvicorn
from pathlib import Path
from typing import Optional

from .cli import cli
from .daemon import PulseDaemon
from .config import load_config
from .db.models import init_db


@cli.command("serve")
@click.option("--host", default=None, help="监听地址（覆盖 config）")
@click.option("--port", "-p", default=None, type=int, help="监听端口（覆盖 config）")
@click.option("--reload", is_flag=True, help="热重载（开发用）")
@click.pass_context
def serve(ctx, host, port, reload):
    """启动 Web Dashboard"""
    from .config import load_config
    cfg = load_config(ctx.obj.get("config_path"))
    init_db(cfg.storage.db_path)

    actual_host = host or cfg.web.host
    actual_port = port or cfg.web.port

    click.echo(f"启动 Pulse Web Dashboard: http://{actual_host}:{actual_port}")

    # 设置 uvicorn 运行
    config_path = ctx.obj.get("config_path")

    # 动态创建 app
    from .web.app import create_app
    app = create_app(config_path)

    uvicorn.run(
        app,
        host=actual_host,
        port=actual_port,
        log_level="info",
        reload=reload,
    )


@cli.command("daemon")
@click.option("--no-push", is_flag=True, help="不推送飞书")
@click.pass_context
def daemon_start(ctx, no_push):
    """启动定时 Daemon（阻塞运行）"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ]
    )

    # 也写日志文件
    try:
        cfg = load_config(ctx.obj.get("config_path"))
        init_db(cfg.storage.db_path)
        from pathlib import Path
        log_dir = Path(cfg.storage.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "daemon.log")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    except Exception:
        pass

    daemon = PulseDaemon(ctx.obj.get("config_path"))
    daemon.start(push=not no_push)


# 将命令注册到 cli group（serve 和 daemon 已经通过装饰器注册）
