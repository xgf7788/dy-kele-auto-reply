#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Douyin Kele (抖音来客) IM Auto-Reply Bot

This script automatically monitors IM messages from multiple Douyin Kele stores
and replies using a custom API.

Usage:
    python main.py                          # Run all stores
    python main.py --store store_001        # Run a single store
    python main.py --store "店铺A"          # Run by store name
    python main.py --store store_001 --port 8900  # Custom health port
"""
import argparse
import asyncio
import signal
import sys
import io
from pathlib import Path

# Fix Windows encoding issues
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from core.store_manager import StoreManager
from core.message_listener import MessageListener
from core.message_handler import MessageHandler
from core.health_server import HealthServer
from storage import get_db
from utils.logger import setup_logger, get_logger

# ---- CLI argument parsing ----
parser = argparse.ArgumentParser(
    description="Douyin Kele (抖音来客) IM Auto-Reply Bot",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=(
        "Examples:\n"
        "  python main.py                          Run all stores\n"
        "  python main.py --store store_001          Run a single store by ID\n"
        '  python main.py --store "店铺A"            Run a single store by name\n'
        "  python main.py --store store_001 --port 8900   Custom health port\n"
    ),
)
parser.add_argument(
    "--store", type=str, default=None,
    help="Run only the specified store (by store_id or name). Omit to run all stores."
)
parser.add_argument(
    "--port", type=int, default=8899,
    help="Health check HTTP server port (default: 8899)."
)
_args = parser.parse_args()

# Filter stores BEFORE anything else uses settings.stores
settings.filter_stores(_args.store)

# Setup logging
logger = setup_logger(
    log_level=settings.log_level,
    log_dir=Path("storage/logs"),
    log_to_file=True
)


class AutoReplyBot:
    """Main bot orchestrating store management, message listening, and handling."""

    def __init__(self, health_port: int = 8899):
        self.store_manager = StoreManager()
        self.message_handler: MessageHandler | None = None
        self.message_listener: MessageListener | None = None
        self.health_server: HealthServer | None = None
        self.db = get_db()
        self._shutdown_event = asyncio.Event()
        self.health_port = health_port

    async def initialize(self) -> None:
        """Initialize all components."""
        logger.info(f"Starting {settings.app_name}...")

        # Initialize database
        await self.db.connect()
        logger.info("Database connected")

        # Initialize store manager
        await self.store_manager.initialize()

        # Load and initialize stores
        if not settings.stores:
            logger.warning("No stores configured! Please check config/accounts.yaml")
            return

        for store_config in settings.stores:
            if store_config.enabled:
                logger.info(f"Adding store: {store_config.name} ({store_config.store_id})")
                await self.store_manager.add_store(store_config)
                await asyncio.sleep(2)  # Stagger store initialization

        # Wait for stores to be ready
        online_stores = self.store_manager.get_online_stores()
        logger.info(f"Online stores: {len(online_stores)}/{len(settings.stores)}")

        if not online_stores:
            logger.error("No stores are online! Please check login credentials.")
            raise RuntimeError("No stores are online - cannot start bot")

        # Initialize message listener first
        self.message_listener = MessageListener(
            self.store_manager,
            message_callback=self._on_message
        )

        # Initialize message handler with callback to record sent messages
        # 将 message_listener 传递给 handler，以便设置处理状态
        self.message_handler = MessageHandler(
            self.store_manager,
            on_reply_sent=self._on_reply_sent,
            message_listener=self.message_listener
        )
        await self.message_handler.start()
        await self.message_listener.start()

        # Start health check HTTP server
        self.health_server = HealthServer(self, port=self.health_port)
        await self.health_server.start()

        # Log running configuration
        store_names = [s.config.name for s in online_stores]
        logger.info(
            f"Bot running with {len(online_stores)} store(s): {', '.join(store_names)}"
            f" | Health: http://0.0.0.0:{self.health_port}"
        )
        logger.info("Bot initialization complete!")

    async def _on_message(self, message) -> None:
        """Callback for new messages."""
        logger.info(f"_on_message called: ID={message.message_id}, User={message.user.nickname}, Content={message.content[:30]}...")
        if self.message_handler:
            result = await self.message_handler.enqueue(message)
            logger.info(f"enqueue result: {result}")
        else:
            logger.error("message_handler is None!")

    def _on_reply_sent(self, store_id: str, reply_text: str) -> None:
        """Callback when a reply is sent. Records the message to distinguish from user messages."""
        if self.message_listener:
            self.message_listener.record_sent_message(store_id, reply_text)

    async def run(self) -> None:
        """Main run loop."""
        try:
            await self.initialize()

            # Wait for shutdown signal
            await self._shutdown_event.wait()

        except asyncio.CancelledError:
            logger.info("Run loop cancelled")
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down...")

        # Stop listener
        if self.message_listener:
            await self.message_listener.stop()

        # Stop handler
        if self.message_handler:
            await self.message_handler.stop()

        # Stop health server
        if self.health_server:
            await self.health_server.stop()

        # Shutdown store manager
        await self.store_manager.shutdown()

        # Close database
        await self.db.close()

        # Close shared API session
        from core.api_client import close_session
        await close_session()

        logger.info("Shutdown complete")

    def signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}")
        self._shutdown_event.set()

    async def print_stats(self) -> None:
        """Periodically print statistics."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=60
                )
            except asyncio.TimeoutError:
                pass

            if self._shutdown_event.is_set():
                break

            # Print stats
            print("\n" + "="*60)
            print(" SYSTEM STATISTICS ")
            print("="*60)

            # Store stats
            store_health = await self.store_manager.health_check()
            print(f"\nStores: {store_health['online']} online, "
                  f"{store_health['offline']} offline, "
                  f"{store_health['error']} error")

            # Handler stats
            if self.message_handler:
                handler_stats = self.message_handler.get_stats()
                print(f"\nMessages:")
                print(f"  Received: {handler_stats['total_received']}")
                print(f"  Processed: {handler_stats['total_processed']}")
                print(f"  Replied: {handler_stats['total_replied']}")
                print(f"  Failed: {handler_stats['total_failed']}")
                print(f"  Skipped: {handler_stats['total_skipped']}")
                print(f"  Avg Response: {handler_stats['avg_response_time_ms']}ms")
                print(f"  Queue Size: {handler_stats['queue_size']}")

            # Database stats
            db_stats = await self.db.get_stats()
            print(f"\nDatabase:")
            print(f"  Total Messages: {db_stats['total']}")
            print(f"  Replied: {db_stats['replied']}")
            print(f"  Failed: {db_stats['failed']}")
            print(f"  Pending: {db_stats['pending']}")

            print("="*60 + "\n")


async def main():
    """Main entry point."""
    bot = AutoReplyBot(health_port=_args.port)

    # Setup signal handlers
    signal.signal(signal.SIGINT, bot.signal_handler)
    signal.signal(signal.SIGTERM, bot.signal_handler)

    # Start stats printer
    stats_task = asyncio.create_task(bot.print_stats())

    try:
        await bot.run()
    finally:
        stats_task.cancel()
        try:
            await stats_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(0)
