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

        self._usage_update_min_delta = usage_update_min_delta
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

        for t in self._threads.values():
            t.start()

        logger.info(
            "Monitoring service started (sync=%ss active=%ss expiry=%ss)",
            self.sync_interval,
            self.active_interval,
            self.expiry_interval,
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
                self.sync_all_users()
            except Exception as e:
                logger.exception("Error in sync_all_users: %s", e)
            self._wait_or_stop(self.sync_interval)

    def _active_worker(self):
        while not self._stop_event.is_set():
            try:
                self.monitor_active_users()
            except Exception as e:
                logger.exception("Error in monitor_active_users: %s", e)
            self._wait_or_stop(self.active_interval)

    def _expiry_worker(self):
        while not self._stop_event.is_set():
            try:
                self.check_expired_users()
            except Exception as e:
                logger.exception("Error in check_expired_users: %s", e)
            self._wait_or_stop(self.expiry_interval)

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

    def monitor_active_users(self):
        """Monitor active users and handle voucher activations."""
        try:
            active_entries = self.mikrotik.get_active_users() or []
            active_usernames = set()
            active_map = {}
            for e in active_entries:
                uname = e.get("user") or e.get("name") or e.get("username")
                if not uname:
                    continue
                active_usernames.add(uname)
                active_map[uname] = e

            # Update active status in DB
            if active_usernames:
                db_active_rows = (
                    self.db.execute_query(
                        "SELECT username, is_active FROM all_users WHERE username = ANY(%s)",
                        (list(active_usernames),),
                        fetch=True,
                    )
                    or []
                )
                db_active_set = {
                    r["username"] for r in db_active_rows if r.get("is_active")
                }
                to_mark_active = [u for u in active_usernames if u not in db_active_set]
                if to_mark_active:
                    self.db.update_user_active_status(to_mark_active, True)
            else:
                db_active_set = set()

            db_currently_active = (
                self.db.execute_query(
                    "SELECT username FROM all_users WHERE is_active = TRUE", fetch=True
                )
                or []
            )
            db_currently_active_set = {r["username"] for r in db_currently_active}
            to_mark_inactive = [
                u for u in db_currently_active_set if u not in active_usernames
            ]
            if to_mark_inactive:
                self.db.update_user_active_status(to_mark_inactive, False)

            for username in active_usernames:
                voucher = self.db.get_voucher(username)
                if voucher and not voucher.get("is_used"):
                    self._handle_voucher_activation(username, active_map.get(username))
                self._maybe_update_usage(username)

        except Exception:
            logger.exception("monitor_active_users failed")

    def _handle_voucher_activation(
        self, username: str, active_entry: Optional[Dict[str, Any]] = None
    ):
        """Mark voucher used and create SALE transaction safely."""
        try:
            voucher = self.db.get_voucher(username)
            if not voucher or voucher.get("is_used"):
                return

            profile_name = voucher.get("profile_name")
            profile_info = self.db.get_profile(profile_name)
            price = profile_info.get("price", 1000) if profile_info else 1000

            with self._db_lock:
                existing_tx = self.db.execute_query(
                    "SELECT id FROM financial_transactions WHERE voucher_code=%s AND transaction_type='SALE'",
                    (username,),
                    fetch_one=True,
                )
                if existing_tx:
                    if not voucher.get("is_used"):
                        self.db.mark_voucher_used(username)
                    return

                self.db.mark_voucher_used(username)
                tx = FinancialTransaction(
                    voucher_code=username,
                    amount=price,
                    transaction_type="SALE",
                    transaction_date=datetime.datetime.now(),
                )
                self.db.add_transaction(tx)
                logger.info("Recorded SALE for voucher %s amount=%s", username, price)

        except Exception:
            logger.exception("_handle_voucher_activation failed for %s", username)

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
                        self.db.execute_query(
                            "UPDATE vouchers SET bytes_used = %s WHERE voucher_code = %s",
                            (total, username),
                        )
                self._usage_cache[username] = {"bytes": total, "ts": now}

        except Exception:
            logger.exception("_maybe_update_usage failed for %s", username)

    def check_expired_users(self):
        """Expire users exceeding uptime limit."""
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

            active_entries = self.mikrotik.get_active_users() or []
            active_map = {
                (e.get("user") or e.get("name") or e.get("username")): e
                for e in active_entries
                if (e.get("user") or e.get("name") or e.get("username"))
            }

            for r in rows:
                username = r["username"]
                uptime_limit = r.get("uptime_limit") or "0s"

                usage = self.mikrotik.get_user_usage(username) or {}
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
                            self.mikrotik.remove_active_user(username)
                        except Exception:
                            logger.exception(
                                "Failed to remove %s from router", username
                            )

                    self.db.execute_query(
                        "UPDATE all_users SET is_expired = TRUE, is_active = FALSE WHERE username = %s",
                        (username,),
                    )

                    voucher = self.db.get_voucher(username)
                    if voucher and not voucher.get("is_expired", False):
                        self.db.execute_query(
                            "UPDATE vouchers SET is_expired = TRUE WHERE voucher_code = %s",
                            (username,),
                        )

        except Exception:
            logger.exception("check_expired_users failed")
