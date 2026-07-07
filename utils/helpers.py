"""Utility helper functions."""
import asyncio
import random
from typing import Dict, Optional, Callable, Any
from functools import wraps


def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """Parse cookie string into dictionary.

    Args:
        cookie_str: Cookie string in format "key1=value1; key2=value2"

    Returns:
        Dictionary of cookie name to value
    """
    cookies = {}
    if not cookie_str:
        return cookies

    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()

    return cookies


def format_cookie(cookies: Dict[str, str]) -> str:
    """Format cookie dictionary to string.

    Args:
        cookies: Dictionary of cookie name to value

    Returns:
        Cookie string
    """
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def retry_with_backoff(max_retries: int = 3, initial_delay: float = 1.0, max_delay: float = 30.0):
    """Decorator to retry function with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (0 or None for unlimited retries for 24/7 operation)
        initial_delay: Initial delay between retries in seconds
        max_delay: Maximum delay between retries in seconds
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args, **kwargs) -> Any:
            delay = initial_delay
            last_exception = None
            attempt = 0

            while True:  # Infinite loop for 24/7 operation
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    # If max_retries is set (not 0/None), check limit
                    if max_retries and attempt >= max_retries:
                        raise last_exception

                    attempt += 1
                    # Add jitter to avoid thundering herd
                    jitter = random.uniform(0, 0.1 * delay)
                    await asyncio.sleep(delay + jitter)
                    delay = min(delay * 2, max_delay)

        @wraps(func)
        def sync_wrapper(*args, **kwargs) -> Any:
            delay = initial_delay
            last_exception = None
            attempt = 0

            while True:  # Infinite loop for 24/7 operation
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    # If max_retries is set (not 0/None), check limit
                    if max_retries and attempt >= max_retries:
                        raise last_exception

                    attempt += 1
                    # Add jitter to avoid thundering herd
                    jitter = random.uniform(0, 0.1 * delay)
                    asyncio.sleep(delay + jitter)
                    delay = min(delay * 2, max_delay)

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator


def truncate_string(s: str, max_length: int = 100, suffix: str = "...") -> str:
    """Truncate string to max length.

    Args:
        s: Input string
        max_length: Maximum length
        suffix: Suffix to add if truncated

    Returns:
        Truncated string
    """
    if len(s) <= max_length:
        return s
    return s[:max_length - len(suffix)] + suffix


def sanitize_filename(filename: str, replacement: str = "_") -> str:
    """Sanitize filename by removing/replacing invalid characters.

    Args:
        filename: Original filename
        replacement: Replacement character for invalid chars

    Returns:
        Sanitized filename
    """
    import re
    # Remove invalid characters for Windows/Unix filenames
    invalid_chars = r'[<>:"/\\|?*\x00-\x1f]'
    sanitized = re.sub(invalid_chars, replacement, filename)
    # Limit length
    if len(sanitized) > 200:
        sanitized = sanitized[:200]
    return sanitized.strip()


def format_time_delta(seconds: float) -> str:
    """Format seconds to human readable time delta.

    Args:
        seconds: Number of seconds

    Returns:
        Formatted string like "1h 30m 45s"
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)
