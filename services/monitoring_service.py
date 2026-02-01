import threading
import time
import datetime
import logging
from typing import List, Dict, Any, Optional

from models.schemas import User, FinancialTransaction
from utils.helpers import check_uptime_limit

logger = logging.getLogger(__name__)


class MonitoringService:
    """
    Improved monitoring service:
    - Splits sync / active monitoring / expiry checks into separate timers
    - Minimizes DB writes by change-detection and thresholds
    - Uses a stop_event for clean shutdown
    """

    def __init__(
        self,
        database_service,
        mikrotik_manager,
        voucher_service=None,
        socketio=None,  # optional Flask-SocketIO for emitting events
        sync_interval: int = 300,  # seconds: sync all users
        active_interval: int = 30,  # seconds: monitor active users
        expiry_interval: int = 60,  # seconds: check expirations
        usage_update_min_delta: int = 1024 * 10,  # only update usage if changed > 10KB
        usage_update_max_age: int = 300,  # force update at least every N seconds
    ):
        self.db = database_service
        self.mikrotik = mikrotik_manager
        self.voucher_service = voucher_service
        self.socketio = socketio

        # Intervals
        self.sync_interval = sync_interval
        self.active_interval = active_interval
        self.expiry_interval = expiry_interval
        self.traffic_interval = 5 # Snapshot traffic every 5 seconds for live updates

        self._usage_update_min_delta = usage_update_min_delta
        self._usage_update_max_age = usage_update_max_age
        self._usage_update_max_age = usage_update_max_age

        # usage cache: { username: { "bytes": int, "ts": datetime } }
        self._usage_cache: Dict[str, Dict[str, Any]] = {}

        self._stop_event = threading.Event()

        # worker threads
        self._threads: Dict[str, threading.Thread] = {}

        # lock for DB operations from background threads
        self._db_lock = threading.Lock()

    def start_monitoring(self):
        """Start worker threads (idempotent)."""
        if self._threads:
            return  # already started

        self._stop_event.clear()

        # create and start threads
        self._threads["sync"] = threading.Thread(target=self._sync_worker, daemon=True)
        self._threads["active"] = threading.Thread(
            target=self._active_worker, daemon=True
        )
        self._threads["expiry"] = threading.Thread(
            target=self._expiry_worker, daemon=True
        )
        self._threads["traffic"] = threading.Thread(
            target=self._traffic_worker, daemon=True
        )

        for t in self._threads.values():
            t.start()

        logger.info(
            "Monitoring service started (sync=%ss active=%ss expiry=%ss traffic=%ss)",
            self.sync_interval,
            self.active_interval,
            self.expiry_interval,
            self.traffic_interval,
        )

    def stop_monitoring(self):
        """Stop all workers and wait briefly for join."""
        self._stop_event.set()

        for t in self._threads.values():
            t.join(timeout=5)

        self._threads.clear()
        logger.info("Monitoring service stopped")

    # ----------------------
    # Worker Threads
    # ----------------------
    def _sync_worker(self):
        while not self._stop_event.is_set():
            try:
                # 1. Sync Bandwidth Profiles with Smart Pricing
                logger.info("Starting periodic profile sync...")
                success, msg = self.db.sync_profiles_from_mikrotik(self.mikrotik)
                if success:
                    logger.info(f"Profile sync successful: {msg}")
                
                # 2. Sync Static Users
                self.sync_all_users()
            except Exception as e:
                logger.exception("Error in _sync_worker: %s", e)
            self._wait_or_stop(self.sync_interval)

    def _active_worker(self):
        while not self._stop_event.is_set():
            try:
                # Use the optimized record_active_users logic
                active_entries = self.mikrotik.get_active_users() or []
                if active_entries:
                    self.db.record_active_users(active_entries)
                    
                    # Still need to update statuses for those who went offline
                    active_usernames = {e.get("user") or e.get("name") or e.get("username") for e in active_entries}
                    active_usernames.discard(None)
                    
                    # Update is_active=False for those not in the list
                    self.db.execute_query(
                        "UPDATE all_users SET is_active = FALSE WHERE is_active = TRUE AND username != ALL(%s)",
                        (list(active_usernames),)
                    )
            except Exception as e:
                logger.exception("Error in _active_worker: %s", e)
            self._wait_or_stop(self.active_interval)


    def _expiry_worker(self):
        while not self._stop_event.is_set():
            try:
                self.check_expired_users()
            except Exception as e:
                logger.exception("Error in check_expired_users: %s", e)
            self._wait_or_stop(self.expiry_interval)

    def _traffic_worker(self):
        while not self._stop_event.is_set():
            try:
                self.snapshot_traffic()
            except Exception as e:
                logger.exception("Error in snapshot_traffic: %s", e)
            self._wait_or_stop(self.traffic_interval)

    def _wait_or_stop(self, seconds: int):
        """Wait but exit early if stop event set."""
        if isinstance(seconds, int) and seconds > 0:
            self._stop_event.wait(timeout=seconds)
        else:
            logger.warning("Invalid interval passed to _wait_or_stop: %s", seconds)

    # ----------------------
    # Sync / Active / Expiry
    # ----------------------
    def sync_all_users(self):
        """Sync static users from MikroTik to DB."""
        try:
            all_users = self.mikrotik.get_all_users() or []
            if not all_users:
                return

            existing_rows = (
                self.db.execute_query(
                    "SELECT username, created_at, comment, password_type FROM all_users",
                    fetch=True,
                )
                or []
            )
            existing_map = {r["username"]: r for r in existing_rows}

            for user in all_users:
                username = user.get("name") or user.get("user") or user.get("username")
                if not username:
                    continue

                profile_name = (
                    user.get("profile")
                    or user.get("profile_name")
                    or user.get("limit-profile")
                    or "default"
                )
                uptime_limit = (
                    user.get("limit-uptime") or user.get("uptime-limit") or None
                )
                comment = user.get("comment") or ""

                voucher_row = self.db.get_voucher(username)
                is_voucher = voucher_row is not None

                password_type = "custom"
                if is_voucher:
                    password_type = voucher_row.get("password_type", "blank")
                else:
                    c = comment.lower()
                    if "password=same" in c:
                        password_type = "same"
                    elif "password=blank" in c or "blank password" in c:
                        password_type = "blank"

                created_at = (
                    existing_map[username]["created_at"]
                    if username in existing_map
                    and existing_map[username].get("created_at")
                    else datetime.datetime.now()
                )

                user_obj = User(
                    username=username,
                    profile_name=profile_name,
                    uptime_limit=uptime_limit,
                    comment=comment,
                    password_type=password_type,
                    is_voucher=is_voucher,
                    created_at=created_at,
                )

                try:
                    self.db.sync_user(user_obj)
                except Exception:
                    try:
                        self.db.execute_query(
                            """
                            INSERT INTO all_users
                              (username, profile_name, uptime_limit, comment, password_type, is_voucher, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (username) DO UPDATE SET
                              profile_name = EXCLUDED.profile_name,
                              uptime_limit = EXCLUDED.uptime_limit,
                              comment = EXCLUDED.comment,
                              password_type = EXCLUDED.password_type,
                              is_voucher = EXCLUDED.is_voucher
                            """,
                            (
                                user_obj.username,
                                user_obj.profile_name,
                                user_obj.uptime_limit,
                                user_obj.comment,
                                user_obj.password_type,
                                user_obj.is_voucher,
                                user_obj.created_at,
                            ),
                        )
                    except Exception as e:
                        logger.exception(
                            "Failed fallback upsert for user %s: %s", username, e
                        )

        except Exception:
            logger.exception("sync_all_users failed")

    def _maybe_update_usage(self, username: str):
        """Update voucher usage only when threshold exceeded or max_age exceeded."""
        try:
            voucher = self.db.get_voucher(username)
            if not voucher:
                return

            usage = self.mikrotik.get_user_usage(username) or {}
            bytes_in = int(usage.get("bytes_in", 0) or 0)
            bytes_out = int(usage.get("bytes_out", 0) or 0)
            total = bytes_in + bytes_out
            now = datetime.datetime.now()
            cache = self._usage_cache.get(username)

            should_update = False
            if cache is None:
                should_update = True
            else:
                prev_bytes = cache["bytes"]
                prev_ts = cache["ts"]
                if total < prev_bytes:  # router counter reset
                    should_update = True
                else:
                    delta = total - prev_bytes
                    age = (now - prev_ts).total_seconds()
                    should_update = (
                        delta >= self._usage_update_min_delta
                        or age >= self._usage_update_max_age
                    )

            if should_update:
                with self._db_lock:
                    if hasattr(self.db, "update_voucher_usage"):
                        self.db.update_voucher_usage(username, total)
                    else:
                         # Fallback update if method missing
                        self.db.execute_query(
                            "UPDATE vouchers SET bytes_used = %s WHERE voucher_code = %s",
                            (total, username),
                        )
                    
                    # Also update all_users bytes_used
                    self.db.execute_query(
                        "UPDATE all_users SET bytes_used = %s WHERE username = %s",
                        (total, username)
                    )

                self._usage_cache[username] = {"bytes": total, "ts": now}
                
                # Emit update if socketio available
                if self.socketio:
                   self.socketio.emit('user_update', {
                       'username': username,
                       'bytes_used': total,
                       'active': True
                   })

        except Exception:
            logger.exception("_maybe_update_usage failed for %s", username)

    def check_expired_users(self):
        """Expire users exceeding uptime limit - optimized with bulk fetch."""
        try:
            rows = (
                self.db.execute_query(
                    "SELECT username, uptime_limit, is_expired FROM all_users WHERE is_expired = FALSE",
                    fetch=True,
                )
                or []
            )
            if not rows:
                return

            # Bulk fetch all usage at once to avoid N+1 queries
            usernames = [r["username"] for r in rows]
            all_usage = self.mikrotik.get_bulk_user_usage(usernames)

            active_entries = self.mikrotik.get_active_users() or []
            active_map = {
                (e.get("user") or e.get("name") or e.get("username")): e
                for e in active_entries
                if (e.get("user") or e.get("name") or e.get("username"))
            }

            expired_users = []
            expired_vouchers = []

            for r in rows:
                username = r["username"]
                uptime_limit = r.get("uptime_limit") or "0s"

                usage = all_usage.get(username, {})
                current_uptime = usage.get("uptime", "0s")

                try:
                    expired = check_uptime_limit(current_uptime, uptime_limit)
                except Exception as e:
                    logger.warning(
                        "Failed to check uptime limit for %s: %s", username, e
                    )
                    expired = False

                if expired:
                    if username in active_map:
                        try:
                            self.mikrotik.remove_expired_user(username)
                        except Exception:
                            logger.exception(
                                "Failed to remove %s from router", username
                            )

                    expired_users.append(username)

                    voucher = self.db.get_voucher(username)
                    if voucher and not voucher.get("is_expired", False):
                        expired_vouchers.append(username)

            # Batch update expired users in DB
            if expired_users:
                placeholders = ','.join(['%s'] * len(expired_users))
                self.db.execute_query(
                    f"UPDATE all_users SET is_expired = TRUE, is_active = FALSE WHERE username IN ({placeholders})",
                    tuple(expired_users),
                )
                logger.info(f"Marked {len(expired_users)} users as expired")

            # Batch update expired vouchers
            if expired_vouchers:
                placeholders = ','.join(['%s'] * len(expired_vouchers))
                self.db.execute_query(
                    f"UPDATE vouchers SET is_expired = TRUE WHERE voucher_code IN ({placeholders})",
                    tuple(expired_vouchers),
                )
                logger.info(f"Marked {len(expired_vouchers)} vouchers as expired")

        except Exception:
            logger.exception("check_expired_users failed")

    def snapshot_traffic(self):
        """Snapshot traffic for all active users"""
        try:
            active_entries = self.mikrotik.get_active_users() or []
            if not active_entries:
                return

            snapshots = []
            timestamp = datetime.datetime.now()
            
            # Emit live traffic event
            live_traffic_data = []

            for entry in active_entries:
                username = entry.get("user") or entry.get("name")
                if not username: continue
                
                try:
                    bytes_in = int(entry.get("bytes-in", 0))
                    bytes_out = int(entry.get("bytes-out", 0))
                except (ValueError, TypeError):
                    continue

                snapshots.append({
                    "username": username,
                    "timestamp": timestamp,
                    "bytes_in": bytes_in,
                    "bytes_out": bytes_out
                })
                
                live_traffic_data.append({
                    "username": username,
                    "bytes_in": bytes_in,
                    "bytes_out": bytes_out,
                    "ip_address": entry.get("address", ""),
                    "mac_address": entry.get("mac-address", "")
                })

            # Save history to DB
            if hasattr(self.db, 'record_traffic_snapshot'):
                self.db.record_traffic_snapshot(snapshots)
                
            # Emit WebSocket event for live graph
            if self.socketio:
                self.socketio.emit('traffic_update', {
                    'timestamp': timestamp.isoformat(),
                    'data': live_traffic_data
                })

        except Exception:
            logger.exception("snapshot_traffic failed")
