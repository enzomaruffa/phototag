"""SSH tunnel and Immich integration."""

import subprocess
import time
import requests
from pathlib import Path
from typing import List, Optional, Callable
import logging


class ImmichUploader:
    """Manages SSH tunnel and Immich uploads."""

    def __init__(
        self,
        server_host: Optional[str] = None,
        server_user: Optional[str] = None,
        ssh_config_name: Optional[str] = None,
        local_port: int = 2283,
    ):
        if ssh_config_name:
            self.ssh_target = ssh_config_name
        elif server_host and server_user:
            self.ssh_target = f"{server_user}@{server_host}"
        else:
            raise ValueError(
                "Either ssh_config_name or both server_host and server_user must be provided"
            )

        self.local_port = local_port
        self.immich_url = f"http://localhost:{local_port}"
        self.tunnel_was_existing = False

    def start_tunnel(self) -> bool:
        """Start SSH tunnel to Immich server."""
        try:
            # First check if tunnel is already working
            if self.test_connection():
                logging.info("Tunnel already exists and is working")
                self.tunnel_was_existing = True
                return True

            # If we get here, no working tunnel exists, so create one
            # SSH tunnel command: local_port -> server:2283
            cmd = [
                "ssh",
                "-L",
                f"{self.local_port}:localhost:2283",
                self.ssh_target,
                "-N",
                "-f",
            ]

            logging.info(f"Starting SSH tunnel with command: {' '.join(cmd)}")
            # With -f, ssh authenticates, forks the tunnel into the background,
            # and the parent exits 0. A non-zero exit is the only real failure -
            # treating the parent's exit as a crash breaks on fast connections.
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                logging.error(
                    f"SSH tunnel failed (exit {result.returncode}): {result.stderr.strip()}"
                )
                return False

            self.tunnel_was_existing = False

            # Wait for the forwarded port to become usable
            for _ in range(10):
                if self.test_connection():
                    logging.info(f"SSH tunnel established to {self.ssh_target}")
                    return True
                time.sleep(1)

            logging.error("SSH tunnel forked but Immich is not responding through it")
            self.stop_tunnel()
            return False

        except Exception as e:
            logging.error(f"Failed to start SSH tunnel: {e}")
            return False

    def stop_tunnel(self):
        """Stop SSH tunnel (only if we created it)."""
        # Don't stop tunnel if it was already existing when we started
        if self.tunnel_was_existing:
            logging.info("Not stopping tunnel - it was already running when we started")
            return

        # ssh -f forks away from us, so kill by port-forward pattern
        try:
            subprocess.run(
                ["pkill", "-f", f"ssh.*{self.local_port}:localhost:2283"], check=False
            )
        except Exception:
            pass

    def test_connection(self) -> bool:
        """Test if Immich is accessible through tunnel."""
        try:
            # Just test if we can reach the server (root path should return HTML)
            response = requests.get(f"{self.immich_url}/", timeout=5)
            return response.status_code == 200
        except Exception:
            return False

    def ensure_connection(self) -> bool:
        """Ensure tunnel is active, restart if needed."""
        if not self.test_connection():
            logging.info("Tunnel connection lost, reconnecting...")
            self.stop_tunnel()
            time.sleep(2)
            return self.start_tunnel()
        return True

    def get_existing_tags(self, api_key: str) -> List[str]:
        """Fetch existing tags from Immich."""
        try:
            if not self.ensure_connection():
                return []

            headers = {"Authorization": f"Bearer {api_key}"}

            # Try different endpoints based on Immich version
            endpoints = [f"{self.immich_url}/api/tags", f"{self.immich_url}/api/tag"]

            for endpoint in endpoints:
                try:
                    response = requests.get(endpoint, headers=headers, timeout=10)
                    if response.status_code == 200:
                        tags_data = response.json()
                        if isinstance(tags_data, list):
                            return [
                                (
                                    tag.get("name", tag)
                                    if isinstance(tag, dict)
                                    else str(tag)
                                )
                                for tag in tags_data
                            ]
                        return []
                except Exception:
                    continue

            logging.warning("Could not fetch tags from Immich")
            return []

        except Exception as e:
            logging.error(f"Failed to fetch tags: {e}")
            return []

    def upload_photos(
        self,
        photo_dir: Path,
        album_name: Optional[str] = None,
        retry_callback: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Upload photos using Immich CLI with tunnel monitoring."""
        max_retries = 3
        retry_count = 0

        while retry_count < max_retries:
            try:
                if not self.ensure_connection():
                    if retry_count < max_retries - 1 and retry_callback:
                        logging.warning("Connection lost. Attempting to reconnect...")
                        if not retry_callback():
                            logging.info("User chose not to retry")
                            return False
                        retry_count += 1
                        continue
                    return False

                # Build immich command
                cmd = ["immich", "upload", str(photo_dir)]

                if album_name:
                    cmd.extend(["--album", album_name])

                logging.info(
                    f"Starting upload (attempt {retry_count + 1}/{max_retries})"
                )

                # Run upload with monitoring
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=3600  # 1 hour timeout
                )

                if result.returncode == 0:
                    logging.info(f"Successfully uploaded photos from {photo_dir}")
                    return True
                else:
                    # Check if failure was due to connection issue
                    error_msg = result.stderr.lower()
                    if any(
                        keyword in error_msg
                        for keyword in ["connection", "network", "timeout", "refused"]
                    ):
                        logging.warning(
                            f"Upload failed due to connection issue: {result.stderr}"
                        )
                        if retry_count < max_retries - 1 and retry_callback:
                            if retry_callback():
                                retry_count += 1
                                continue
                            else:
                                logging.info("User chose not to retry")
                                return False

                    logging.error(f"Upload failed: {result.stderr}")
                    return False

            except subprocess.TimeoutExpired:
                logging.error("Upload timed out")
                if retry_count < max_retries - 1 and retry_callback:
                    if retry_callback():
                        retry_count += 1
                        continue
                return False
            except Exception as e:
                logging.error(f"Upload failed: {e}")
                if retry_count < max_retries - 1 and retry_callback:
                    if retry_callback():
                        retry_count += 1
                        continue
                return False

        logging.error(f"Upload failed after {max_retries} attempts")
        return False

    def __enter__(self):
        """Context manager entry."""
        if self.start_tunnel():
            return self
        else:
            raise ConnectionError("Failed to establish SSH tunnel")

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop_tunnel()
