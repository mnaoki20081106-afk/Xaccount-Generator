"""
CLIエントリーポイント。

使い方:
  python -m src.main generate --count 10
  python -m src.main warmup
  python -m src.main check
  python -m src.main run --target 100    # フルデーモン
  python -m src.main list
  python -m src.main status
"""
import asyncio
import sys
from pathlib import Path
from loguru import logger

from .config import load_config
from .orchestrator import Orchestrator


def setup_logging(config):
    Path(config.logging.file).parent.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level=config.logging.level, colorize=True)
    logger.add(
        config.logging.file,
        rotation="10 MB",
        retention="30 days",
        level=config.logging.level,
    )


async def cmd_generate(config, count: int, concurrency: int):
    orc = Orchestrator(config)
    await orc.start()
    try:
        await orc.generate_accounts(count, concurrency=concurrency)
    finally:
        await orc.stop()


async def cmd_warmup(config, concurrency: int):
    orc = Orchestrator(config)
    await orc.start()
    try:
        await orc.run_warmup_cycle(concurrency=concurrency)
    finally:
        await orc.stop()


async def cmd_check(config):
    orc = Orchestrator(config)
    await orc.start()
    try:
        await orc.run_shadowban_check()
    finally:
        await orc.stop()


async def cmd_run(config, target: int, gen_concurrency: int, warmup_concurrency: int, interval_hours: float):
    orc = Orchestrator(config)
    await orc.start()
    try:
        await orc.run_forever(
            target_count=target,
            generation_concurrency=gen_concurrency,
            warmup_concurrency=warmup_concurrency,
            check_interval_hours=interval_hours,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        await orc.stop()


async def cmd_list(config):
    from .db import Database
    from .models import AccountStatus
    db = Database(config.db.path)
    await db.connect()
    accounts = await db.list_accounts(limit=200)
    await db.close()

    status_counts: dict[str, int] = {}
    for a in accounts:
        status_counts[a.status.value] = status_counts.get(a.status.value, 0) + 1

    print(f"\n{'ID':>4}  {'username':<20}  {'status':<14}  {'shadow':>6}  created")
    print("-" * 72)
    for a in accounts:
        created = a.created_at.strftime("%m-%d %H:%M") if a.created_at else "-"
        print(f"{a.id:>4}  {a.username:<20}  {a.status.value:<14}  {a.shadow_score:>6.2f}  {created}")

    print(f"\nTotal: {len(accounts)}")
    for st, cnt in sorted(status_counts.items()):
        print(f"  {st:<14}: {cnt}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Xaccount-Generator")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルパス")
    sub = parser.add_subparsers(dest="command")

    p_gen = sub.add_parser("generate", help="垢を生成する")
    p_gen.add_argument("--count", type=int, default=10, help="生成する垢数")
    p_gen.add_argument("--concurrency", type=int, default=2, help="同時生成数")

    p_warm = sub.add_parser("warmup", help="ウォームアップサイクルを1回実行")
    p_warm.add_argument("--concurrency", type=int, default=5)

    sub.add_parser("check", help="シャドウバン確認")

    p_run = sub.add_parser("run", help="フルデーモン起動")
    p_run.add_argument("--target", type=int, default=100, help="維持する垢数")
    p_run.add_argument("--gen-concurrency", type=int, default=2)
    p_run.add_argument("--warmup-concurrency", type=int, default=5)
    p_run.add_argument("--interval-hours", type=float, default=6.0)

    sub.add_parser("list", help="垢一覧を表示")
    sub.add_parser("status", help="垢一覧を表示（listの別名）")

    p_serve = sub.add_parser("serve", help="HTTP API サーバーを起動")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    config = load_config(args.config)
    setup_logging(config)

    if args.command == "generate":
        asyncio.run(cmd_generate(config, args.count, args.concurrency))
    elif args.command == "warmup":
        asyncio.run(cmd_warmup(config, args.concurrency))
    elif args.command == "check":
        asyncio.run(cmd_check(config))
    elif args.command == "run":
        asyncio.run(cmd_run(
            config,
            args.target,
            args.gen_concurrency,
            args.warmup_concurrency,
            args.interval_hours,
        ))
    elif args.command in ("list", "status"):
        asyncio.run(cmd_list(config))
    elif args.command == "serve":
        import os
        import uvicorn
        os.environ.setdefault("GENERATOR_CONFIG", args.config)
        from .api.server import app
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
