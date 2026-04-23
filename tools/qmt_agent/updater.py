"""
QMT Agent update checker.

Checks for updates from the same COS bucket as the Electron app.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import threading
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("qmt_agent.updater")

# Default update server URL (same as Electron)
DEFAULT_UPDATE_BASE_URL = "https://cos.quantmind.cloud/update/qmt-agent"


@dataclass
class UpdateInfo:
    """Information about an available update."""
    version: str
    release_date: Optional[str] = None
    release_notes: Optional[str] = None
    download_url: Optional[str] = None
    file_size: Optional[int] = None
    file_hash: Optional[str] = None


@dataclass
class UpdateCheckResult:
    """Result of an update check."""
    available: bool
    current_version: str
    latest_version: Optional[str] = None
    update_info: Optional[UpdateInfo] = None
    error: Optional[str] = None


def get_current_version() -> str:
    """Get the current application version."""
    version_file = Path(__file__).resolve().parent / "version.json"
    if not version_file.exists():
        return "1.0.0"
    try:
        data = json.loads(version_file.read_text(encoding="utf-8"))
        return str(data.get("version") or "1.0.0")
    except Exception:
        return "1.0.0"


def get_update_base_url() -> str:
    """Get the update server base URL."""
    version_file = Path(__file__).resolve().parent / "version.json"
    if version_file.exists():
        try:
            data = json.loads(version_file.read_text(encoding="utf-8"))
            url = data.get("update_base_url")
            if url:
                return str(url).rstrip("/")
        except Exception:
            pass
    return DEFAULT_UPDATE_BASE_URL


def get_platform_suffix() -> str:
    """Get the platform-specific suffix for update files."""
    if sys.platform == "win32":
        if platform.machine().endswith("64"):
            return "win-x64"
        return "win-ia32"
    elif sys.platform == "darwin":
        if platform.machine() == "arm64":
            return "mac-arm64"
        return "mac-x64"
    elif sys.platform.startswith("linux"):
        return "linux-x64"
    return "unknown"


def parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a version string into a tuple of integers for comparison."""
    parts = []
    for part in version_str.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            # Handle pre-release versions like "1.0.0-beta"
            num_part = "".join(c for c in part if c.isdigit())
            parts.append(int(num_part) if num_part else 0)
    return tuple(parts)


def is_newer_version(current: str, latest: str) -> bool:
    """Check if the latest version is newer than the current version."""
    try:
        current_tuple = parse_version(current)
        latest_tuple = parse_version(latest)
        return latest_tuple > current_tuple
    except Exception:
        return False


class UpdateChecker:
    """Handles checking for application updates."""

    def __init__(
        self,
        current_version: Optional[str] = None,
        update_base_url: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.current_version = current_version or get_current_version()
        self.update_base_url = update_base_url or get_update_base_url()
        self.timeout = timeout
        self._last_check_result: Optional[UpdateCheckResult] = None
        self._last_check_time: Optional[float] = None
        self._check_lock = threading.Lock()

    def _fetch_latest_version(self) -> dict[str, Any]:
        """Fetch the latest version info from the update server."""
        platform_suffix = get_platform_suffix()

        # Try platform-specific latest.json first
        urls_to_try = [
            f"{self.update_base_url}/{platform_suffix}/latest.json",
            f"{self.update_base_url}/latest.json",
        ]

        last_error: Optional[Exception] = None
        for url in urls_to_try:
            try:
                logger.debug(f"Checking for updates at: {url}")
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": f"QuantMind-QMT-Agent/{self.current_version}",
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    logger.debug(f"Update info received: {data}")
                    return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    logger.debug(f"No update info at {url} (404)")
                    continue
                last_error = e
            except Exception as e:
                last_error = e
                logger.debug(f"Failed to fetch from {url}: {e}")

        if last_error:
            raise last_error
        raise FileNotFoundError("No update info found")

    def check_for_updates(
        self,
        force: bool = False,
    ) -> UpdateCheckResult:
        """
        Check if an update is available.

        Args:
            force: Force a new check even if recently checked.

        Returns:
            UpdateCheckResult with update availability and info.
        """
        import time

        with self._check_lock:
            # Return cached result if recently checked (within 1 hour)
            if not force and self._last_check_result and self._last_check_time:
                if time.time() - self._last_check_time < 3600:
                    return self._last_check_result

            try:
                data = self._fetch_latest_version()
                latest_version = str(data.get("version") or "")

                if not latest_version:
                    return UpdateCheckResult(
                        available=False,
                        current_version=self.current_version,
                        error="Invalid version info from server",
                    )

                # Build update info
                platform_suffix = get_platform_suffix()
                download_url = data.get("download_url") or data.get("url")
                if download_url and not download_url.startswith("http"):
                    download_url = f"{self.update_base_url}/{platform_suffix}/{download_url}"

                update_info = UpdateInfo(
                    version=latest_version,
                    release_date=data.get("release_date") or data.get("date"),
                    release_notes=data.get("release_notes") or data.get("notes"),
                    download_url=download_url,
                    file_size=data.get("file_size") or data.get("size"),
                    file_hash=data.get("file_hash") or data.get("sha256") or data.get("hash"),
                )

                available = is_newer_version(self.current_version, latest_version)

                result = UpdateCheckResult(
                    available=available,
                    current_version=self.current_version,
                    latest_version=latest_version,
                    update_info=update_info if available else None,
                )

                self._last_check_result = result
                self._last_check_time = time.time()

                if available:
                    logger.info(
                        f"Update available: {self.current_version} -> {latest_version}"
                    )
                else:
                    logger.info(f"No update available (current: {self.current_version})")

                return result

            except Exception as e:
                logger.error(f"Failed to check for updates: {e}")
                return UpdateCheckResult(
                    available=False,
                    current_version=self.current_version,
                    error=str(e),
                )

    def check_async(
        self,
        callback: Callable[[UpdateCheckResult], None],
        force: bool = False,
    ) -> threading.Thread:
        """
        Check for updates asynchronously.

        Args:
            callback: Function to call with the result.
            force: Force a new check even if recently checked.

        Returns:
            The thread running the check.
        """

        def _check():
            try:
                result = self.check_for_updates(force=force)
                callback(result)
            except Exception as e:
                logger.exception("Async update check failed")
                callback(UpdateCheckResult(
                    available=False,
                    current_version=self.current_version,
                    error=str(e),
                ))

        thread = threading.Thread(target=_check, name="update-checker", daemon=True)
        thread.start()
        return thread

    @property
    def last_check_result(self) -> Optional[UpdateCheckResult]:
        """Get the last check result, if any."""
        return self._last_check_result


# Global instance
_update_checker: Optional[UpdateChecker] = None


def get_update_checker() -> UpdateChecker:
    """Get the global update checker instance."""
    global _update_checker
    if _update_checker is None:
        _update_checker = UpdateChecker()
    return _update_checker
