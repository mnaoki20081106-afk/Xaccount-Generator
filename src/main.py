"""
エントリーポイント。
"""
import asyncio
from pathlib import Path
from loguru import logger

from .config import load_config
from .db import Database
from .proxy.manager import ProxyManager
from .auth.twofa import TwoFAManager
from .factory.account_factory import AccountFactory
from .utils.browser import BrowserManager


async def main():
    config = load_config("config.yaml")

    Path(config.logging.file).parent.mkdir(parents=True, exist_ok=True)
    logger.add(config.logging.file, rotation="10 MB", level=config.logging.level)

    db = Database(config.db.path)
    await db.connect()

    proxy_manager = ProxyManager(config.proxy)
    twofa_manager = TwoFAManager(config.twofa.encryption_key)
    browser_manager = BrowserManager(
        headless=config.browser.headless,
        slow_mo=config.browser.slow_mo,
        timeout=config.browser.default_timeout,
    )

    await browser_manager.start()

    factory = AccountFactory(
        config, proxy_manager, twofa_manager, browser_manager
    )

    logger.info("Xaccount-Generator started")

    try:
        account = await factory.create_one()
        if account:
            account_id = await db.add_account(account)
            logger.info(f"Saved account id={account_id} username={account.username}")
        else:
            logger.warning("No account created this run")
    finally:
        await browser_manager.stop()
        await db.close()

    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
