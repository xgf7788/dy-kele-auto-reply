"""Logging configuration using loguru."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger as _logger


def _cleanup_old_logs(log_dir: Path, pattern: str, days: int):
    """Remove log files older than specified days."""
    cutoff = datetime.now() - timedelta(days=days)
    for log_file in log_dir.glob(pattern):
        try:
            # Check file modification time
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
            if mtime < cutoff:
                log_file.unlink(missing_ok=True)
        except Exception:
            pass


def setup_logger(log_level: str = "INFO", log_dir: Path = None, log_to_file: bool = True):
    """Configure loguru logger.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_dir: Directory for log files
        log_to_file: Whether to log to file
    """
    # Remove default handler
    _logger.remove()

    # Console handler with colored output
    _logger.add(
        sys.stdout,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
        colorize=True,
    )

    # File handler
    if log_to_file and log_dir:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

        # Use date-based filenames to avoid os.rename (rotation) on Windows
        # which fails with PermissionError when another process holds the file handle.
        today = datetime.now().strftime("%Y-%m-%d")

        # Main log file
        _logger.add(
            log_dir / f"app_{today}.log",
            rotation=None,          # Disable rotation (no os.rename)
            retention=None,         # Manual cleanup below
            level=log_level,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            encoding="utf-8",
            enqueue=True,
            delay=True,
        )

        # Error log file (errors only)
        _logger.add(
            log_dir / f"error_{today}.log",
            rotation=None,          # Disable rotation (no os.rename)
            retention=None,         # Manual cleanup below
            level="ERROR",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}\n{exception}",
            encoding="utf-8",
            enqueue=True,
            delay=True,
        )

        # Clean up old log files (retain last 7 days)
        _cleanup_old_logs(log_dir, "app_*.log", days=7)
        _cleanup_old_logs(log_dir, "error_*.log", days=30)

    return _logger


def get_logger(name: str = None):
    """Get logger instance."""
    if name:
        return _logger.bind(name=name)
    return _logger
