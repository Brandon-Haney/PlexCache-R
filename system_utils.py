"""
System utilities for PlexCache.
Handles OS detection, system-specific operations, and path conversions.
"""

import os
import platform
import shutil
import subprocess
import atexit
import fcntl
from typing import Tuple, Optional
import logging


# ============================================================================
# Unraid Path Utilities
# ============================================================================

# ZFS-backed path prefixes that should NOT be converted to /mnt/user0/.
# For ZFS pool-only shares (shareUseCache=only), files never appear at /mnt/user0/
# because that path only shows standard array disks. Using /mnt/user/ is safe for
# these paths since there is no cache/array split — no FUSE ambiguity exists.
# Populated at startup by detect_zfs() checks on each path_mapping's real_path.
_zfs_user_prefixes: set = set()


def set_zfs_prefixes(prefixes: set) -> None:
    """Set the ZFS-backed path prefixes (called once at startup)."""
    global _zfs_user_prefixes
    _zfs_user_prefixes = prefixes


def get_array_direct_path(user_share_path: str) -> str:
    """Convert a user share path to array-direct path for existence checks.

    On Unraid, /mnt/user/ is a FUSE virtual filesystem that merges cache + array.
    When checking if a file exists ONLY on the array (not on cache), we need to
    use /mnt/user0/ which provides direct access to the array only.

    This is critical for eviction: we must verify a backup truly exists on the
    array before deleting the cache copy. Using /mnt/user/ would incorrectly
    return True if the file only exists on cache.

    Exception: ZFS pool-backed shares (shareUseCache=only) never have files at
    /mnt/user0/ — their files live on a ZFS pool, not array disks. For these
    paths, we skip the conversion and keep /mnt/user/ which is safe because
    there is no cache/array FUSE ambiguity.

    Args:
        user_share_path: A path potentially starting with /mnt/user/

    Returns:
        The /mnt/user0/ equivalent path if input is /mnt/user/ and not ZFS-backed,
        otherwise unchanged.
    """
    if user_share_path.startswith('/mnt/user/'):
        for prefix in _zfs_user_prefixes:
            if user_share_path.startswith(prefix):
                return user_share_path  # ZFS pool — no user0 conversion
        return '/mnt/user0/' + user_share_path[len('/mnt/user/'):]
    return user_share_path


def detect_zfs(path: str) -> bool:
    """Detect if a path is on a ZFS filesystem.

    First tries df -T on the exact path. If that reports a non-ZFS type
    AND the path is under /mnt/user/ (Unraid FUSE), falls back to checking
    /proc/mounts for ZFS datasets mounted with the same share name.

    This fallback is needed because Unraid's FUSE layer (/mnt/user/) reports
    filesystem type as 'shfs' even when the underlying storage is ZFS.

    Args:
        path: Path to check.

    Returns:
        True if the path is on ZFS, False otherwise.
    """
    try:
        result = subprocess.run(
            ['df', '-T', path],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and 'zfs' in result.stdout.lower():
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Fallback: For Unraid FUSE paths like /mnt/user/<share>/, df -T reports
    # 'shfs' instead of the underlying filesystem. Check /proc/mounts for
    # ZFS datasets with a mountpoint matching the share name.
    if path.startswith('/mnt/user/'):
        parts = path.rstrip('/').split('/')
        if len(parts) >= 4:
            share_name = parts[3]  # e.g., 'plex_media' from /mnt/user/plex_media/...
            return _check_zfs_mount_for_share(share_name)

    return False


def _check_zfs_mount_for_share(share_name: str) -> bool:
    """Check if a ZFS dataset is mounted with a matching share name.

    Reads /proc/mounts to find ZFS mounts where the mountpoint's last
    path component matches the Unraid share name. This detects ZFS-backed
    shares that are hidden behind Unraid's FUSE layer at /mnt/user/.

    Example /proc/mounts line:
        plex/plex_media /mnt/plex/plex_media zfs rw,xattr,posixacl ...

    Args:
        share_name: The Unraid share name (e.g., 'plex_media').

    Returns:
        True if a ZFS mount with a matching share name is found.
    """
    try:
        with open('/proc/mounts', 'r') as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 3:
                    mountpoint = fields[1]
                    fs_type = fields[2]
                    if fs_type == 'zfs' and mountpoint.rstrip('/').endswith('/' + share_name):
                        logging.debug(f"ZFS mount detected via /proc/mounts: {mountpoint} (share: {share_name})")
                        return True
    except (OSError, IOError):
        pass
    return False


class SingleInstanceLock:
    """
    Prevent multiple instances of PlexCache from running simultaneously.

    Uses flock to ensure only one instance can run at a time.
    The lock is automatically released when the process exits or crashes.
    """

    def __init__(self, lock_file: str):
        self.lock_file = lock_file
        self.lock_fd = None
        self.locked = False

    def acquire(self) -> bool:
        """
        Acquire the lock.

        Returns:
            True if lock acquired successfully, False if another instance is running.
        """
        try:
            self.lock_fd = open(self.lock_file, 'w')
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Write PID for debugging
            self.lock_fd.write(str(os.getpid()))
            self.lock_fd.flush()
            self.locked = True

            # Register cleanup on exit
            atexit.register(self.release)

            return True

        except (IOError, OSError):
            # Lock is held by another process
            if self.lock_fd:
                self.lock_fd.close()
                self.lock_fd = None
            return False

    def release(self):
        """Release the lock and clean up."""
        if not self.locked:
            return

        try:
            if self.lock_fd:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                self.lock_fd.close()
                self.lock_fd = None

            if os.path.exists(self.lock_file):
                os.remove(self.lock_file)

            self.locked = False
        except Exception:
            pass  # Best effort cleanup


class SystemDetector:
    """Detects and provides information about the current system."""
    
    def __init__(self):
        self.os_name = platform.system()
        self.is_linux = self.os_name != 'Windows'
        self.is_unraid = self._detect_unraid()
        self.is_docker = self._detect_docker()
        
    def _detect_unraid(self) -> bool:
        """Detect if running on Unraid system."""
        os_info = {
            'Linux': '/mnt/user0/',
            'Darwin': None,
            'Windows': None
        }
        
        unraid_path = os_info.get(self.os_name)
        return os.path.exists(unraid_path) if unraid_path else False
    
    def _detect_docker(self) -> bool:
        """Detect if running inside a Docker container."""
        return os.path.exists('/.dockerenv')
    
class FileUtils:
    """Utility functions for file operations."""
    
    def __init__(self, is_linux: bool, permissions: int = 0o777):
        self.is_linux = is_linux
        self.permissions = permissions
    
    def check_path_exists(self, path: str) -> None:
        """Check if path exists, is a directory, and is writable."""
        logging.debug(f"Checking path: {path}")
        
        if not os.path.exists(path):
            logging.error(f"Path does not exist: {path}")
            raise FileNotFoundError(f"Path {path} does not exist.")
        
        if not os.path.isdir(path):
            logging.error(f"Path is not a directory: {path}")
            raise NotADirectoryError(f"Path {path} is not a directory.")
        
        if not os.access(path, os.W_OK):
            logging.error(f"Path is not writable: {path}")
            raise PermissionError(f"Path {path} is not writable.")
        
        logging.debug(f"Path validation successful: {path}")
    
    def get_free_space(self, directory: str) -> Tuple[float, str]:
        """Get free space in a human-readable format."""
        if not os.path.exists(directory):
            raise FileNotFoundError(f"Invalid path, unable to calculate free space for: {directory}.")

        stat = os.statvfs(directory)
        free_space_bytes = stat.f_bfree * stat.f_frsize
        return self._convert_bytes_to_readable_size(free_space_bytes)

    def get_total_drive_size(self, directory: str) -> int:
        """Get total size of the drive in bytes."""
        if not os.path.exists(directory):
            raise FileNotFoundError(f"Invalid path, unable to calculate drive size for: {directory}.")

        stat = os.statvfs(directory)
        return stat.f_blocks * stat.f_frsize

    def get_total_size_of_files(self, files: list) -> Tuple[float, str]:
        """Calculate total size of files in human-readable format."""
        total_size_bytes = 0
        skipped_files = []
        for file in files:
            try:
                total_size_bytes += os.path.getsize(file)
            except (OSError, FileNotFoundError):
                skipped_files.append(file)

        if skipped_files:
            logging.warning(f"Could not get size for {len(skipped_files)} files (will skip during move)")
            for f in skipped_files:
                logging.debug(f"  Skipping inaccessible file: {f}")

        return self._convert_bytes_to_readable_size(total_size_bytes)
    
    def _convert_bytes_to_readable_size(self, size_bytes: int) -> Tuple[float, str]:
        """Convert bytes to human-readable format."""
        if size_bytes >= (1024 ** 4):
            size = size_bytes / (1024 ** 4)
            unit = 'TB'
        elif size_bytes >= (1024 ** 3):
            size = size_bytes / (1024 ** 3)
            unit = 'GB'
        elif size_bytes >= (1024 ** 2):
            size = size_bytes / (1024 ** 2)
            unit = 'MB'
        else:
            size = size_bytes / 1024
            unit = 'KB'
        
        return size, unit
    
    def copy_file_with_permissions(self, src: str, dest: str, verbose: bool = False) -> int:
        """Copy a file preserving original ownership and permissions (Linux only)."""
        logging.debug(f"Copying file from {src} to {dest}")

        try:
            if self.is_linux:
                # Get source file ownership before copy
                stat_info = os.stat(src)
                src_uid = stat_info.st_uid
                src_gid = stat_info.st_gid
                src_mode = stat_info.st_mode

                # Copy the file (preserves metadata like timestamps)
                shutil.copy2(src, dest)

                # Restore original ownership (shutil.copy2 doesn't preserve uid/gid)
                os.chown(dest, src_uid, src_gid)
                original_umask = os.umask(0)
                os.chmod(dest, self.permissions)
                os.umask(original_umask)

                if verbose:
                    # Log ownership details for debugging
                    dest_stat = os.stat(dest)
                    logging.debug(f"File copied: {src} -> {dest}")
                    logging.debug(f"  Preserved ownership: uid={dest_stat.st_uid}, gid={dest_stat.st_gid}")
                    logging.debug(f"  Mode: {oct(dest_stat.st_mode)}")
                else:
                    logging.debug(f"File copied with permissions preserved: {dest}")
            else:  # Windows logic
                shutil.copy2(src, dest)
                logging.debug(f"File copied (Windows): {src} -> {dest}")

            return 0
        except (FileNotFoundError, PermissionError, Exception) as e:
            logging.error(f"Error copying file from {src} to {dest}: {str(e)}")
            raise RuntimeError(f"Error copying file: {str(e)}")

    def create_directory_with_permissions(self, path: str, src_file_for_permissions: str) -> None:
        """Create directory with proper permissions."""
        logging.debug(f"Creating directory with permissions: {path}")
        
        if not os.path.exists(path):
            if self.is_linux:
                # Get the permissions of the source file
                stat_info = os.stat(src_file_for_permissions)
                uid = stat_info.st_uid
                gid = stat_info.st_gid
                original_umask = os.umask(0)
                os.makedirs(path, exist_ok=True)
                os.chown(path, uid, gid)
                os.chmod(path, self.permissions)
                os.umask(original_umask)
                logging.debug(f"Directory created with permissions (Linux): {path}")
            else:  # Windows platform
                os.makedirs(path, exist_ok=True)
                logging.debug(f"Directory created (Windows): {path}")
        else:
            logging.debug(f"Directory already exists: {path}") 