import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
import logging
from typing import List, Dict, Any, Optional
import threading
from datetime import datetime
from contextlib import contextmanager
import time
from config import Config
from models.schemas import Voucher, User, Profile, FinancialTransaction
from .mikrotik_manager import MikroTikManager
import os
from datetime import datetime, timedelta, timezone
import random


logger = logging.getLogger(__name__)


class DatabaseService:
    def __init__(self, config: Config):
        self.config = config
        self.db_lock = threading.Lock()
        self._connection_pool = []
        self._max_pool_size = 5
        self._pool_lock = threading.Lock()

    @contextmanager
    def get_connection(self):
        """Get database connection from pool with context manager"""
        conn = None
        with self._pool_lock:
            if self._connection_pool:
                conn = self._connection_pool.pop()
            else:
                conn = self._create_connection()

        try:
            yield conn
        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                # Return to pool if healthy, otherwise close
                try:
                    with self._pool_lock:
                        if (
                            len(self._connection_pool) < self._max_pool_size
                            and conn.closed == 0
                        ):
                            self._connection_pool.append(conn)
                        else:
                            conn.close()
                except Exception as e:
                    logger.warning(f"Error returning connection to pool: {e}")
                    conn.close()

    def _create_connection(self):
        """Create new database connection with optimized settings"""
        conn = psycopg2.connect(**self.config.DB_CONFIG)
        # Optimize connection settings
        conn.autocommit = False
        return conn

    def execute_query(
        self,
        query: str,
        params: tuple = None,
        fetch: bool = False,
        fetch_one: bool = False,
        batch_data: list = None,
    ):
        """Thread-safe optimized query execution with connection pooling"""
        start_time = time.time()

        with self.get_connection() as conn:
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    if batch_data:
                        execute_batch(cursor, query, batch_data)
                    else:
                        cursor.execute(query, params)

                    result = None
                    if fetch_one:
                        result = cursor.fetchone()
                    elif fetch:
                        result = cursor.fetchall()

                    conn.commit()

                    # Log slow queries for optimization
                    execution_time = time.time() - start_time
                    if execution_time > 1.0:  # Log queries taking more than 1 second
                        logger.warning(
                            f"Slow query detected ({execution_time:.2f}s): {query[:100]}..."
                        )

                    return result
            except Exception as e:
                conn.rollback()
                logger.error(f"Database error in query '{query[:50]}...': {e}")
                raise

    def init_db(self):
        """Initialize PostgreSQL database tables with indexes for performance"""
        tables_queries = [
            """
            CREATE TABLE IF NOT EXISTS vouchers (
                id SERIAL PRIMARY KEY,
                voucher_code TEXT UNIQUE,
                profile_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                activated_at TIMESTAMP,
                is_used BOOLEAN DEFAULT FALSE,
                customer_name TEXT,
                customer_contact TEXT,
                bytes_used BIGINT DEFAULT 0,
                session_time INTEGER DEFAULT 0,
                expiry_time TIMESTAMP,
                is_expired BOOLEAN DEFAULT FALSE,
                uptime_limit TEXT DEFAULT '1d',
                password_type TEXT DEFAULT 'blank'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS financial_transactions (
                id SERIAL PRIMARY KEY,
                voucher_code TEXT,
                amount BIGINT,
                transaction_type TEXT,
                transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS bandwidth_profiles (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                rate_limit TEXT,
                description TEXT,
                price BIGINT DEFAULT 0,
                time_limit TEXT,
                data_limit TEXT,
                validity_period INTEGER,
                uptime_limit TEXT DEFAULT '1d',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pricing_rates (
                id SERIAL PRIMARY KEY,
                rate_type TEXT UNIQUE,
                amount BIGINT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS all_users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                profile_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                activated_at TIMESTAMP,
                last_seen TIMESTAMP,
                is_active BOOLEAN DEFAULT FALSE,
                bytes_used BIGINT DEFAULT 0,
                uptime_limit TEXT,
                is_expired BOOLEAN DEFAULT FALSE,
                comment TEXT,
                password_type TEXT,
                is_voucher BOOLEAN DEFAULT FALSE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                user_id TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                full_name TEXT,
                phone TEXT,
                company_name TEXT,
                is_verified BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                role TEXT DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS user_routers (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                router_name TEXT NOT NULL,
                host TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL, -- Encrypted
                description TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, router_name)
            )""",
            """CREATE TABLE IF NOT EXISTS user_sessions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                token TEXT UNIQUE NOT NULL,
                device_info TEXT,
                ip_address TEXT,
                expires_at TIMESTAMP NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS password_resets (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                token TEXT UNIQUE NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS user_verifications (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                code TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'EMAIL',  -- or SMS
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
        ]

        # Create tables
        for query in tables_queries:
            self.execute_query(query)

        # Create performance indexes
        index_queries = [
            "CREATE INDEX IF NOT EXISTS idx_vouchers_code ON vouchers(voucher_code)",
            "CREATE INDEX IF NOT EXISTS idx_vouchers_used ON vouchers(is_used)",
            "CREATE INDEX IF NOT EXISTS idx_vouchers_activated ON vouchers(activated_at)",
            "CREATE INDEX IF NOT EXISTS idx_transactions_type_date ON financial_transactions(transaction_type, transaction_date)",
            "CREATE INDEX IF NOT EXISTS idx_transactions_voucher ON financial_transactions(voucher_code)",
            "CREATE INDEX IF NOT EXISTS idx_users_username ON all_users(username)",
            "CREATE INDEX IF NOT EXISTS idx_users_active ON all_users(is_active)",
            "CREATE INDEX IF NOT EXISTS idx_users_last_seen ON all_users(last_seen)",
            "CREATE INDEX IF NOT EXISTS idx_users_activated ON all_users(activated_at)",
            "CREATE INDEX IF NOT EXISTS idx_profiles_name ON bandwidth_profiles(name)",
            "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
            "CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_user_routers_user_id ON user_routers(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_token ON user_sessions(token)",
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token)",
            "CREATE INDEX IF NOT EXISTS idx_user_verifications_user_id ON user_verifications(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_user_verifications_code ON user_verifications(code)",
        ]

        for query in index_queries:
            try:
                self.execute_query(query)
            except Exception as e:
                logger.warning(f"Could not create index: {e}")

        default_rates = [("day", 1000), ("week", 6000), ("month", 25000)]
        self.execute_query(
            """
            INSERT INTO pricing_rates (rate_type, amount)
            VALUES (%s, %s)
            ON CONFLICT (rate_type) DO NOTHING
            """,
            batch_data=default_rates,
        )

    # ---------------------------------------------------------
    # PROFILES (OPTIMIZED WITH CACHING)
    # ---------------------------------------------------------
    def __init__(self, config: Config):
        self.config = config
        self.db_lock = threading.Lock()
        self._connection_pool = []
        self._max_pool_size = 5
        self._pool_lock = threading.Lock()
        self._profile_cache = {}
        self._cache_lock = threading.Lock()
        self._cache_ttl = 300  # 5 minutes cache TTL
        self._last_cache_cleanup = time.time()

    def _clean_cache_if_needed(self):
        """Clean cache periodically"""
        current_time = time.time()
        if current_time - self._last_cache_cleanup > self._cache_ttl:
            with self._cache_lock:
                self._profile_cache.clear()
                self._last_cache_cleanup = current_time

    def get_profile(self, profile_name: str) -> Optional[Dict[str, Any]]:
        """Get profile with caching"""
        self._clean_cache_if_needed()

        cache_key = f"profile_{profile_name.lower()}"
        with self._cache_lock:
            if cache_key in self._profile_cache:
                return self._profile_cache[cache_key]

        result = self.execute_query(
            "SELECT * FROM bandwidth_profiles WHERE LOWER(name)=LOWER(%s)",
            (profile_name,),
            fetch_one=True,
        )

        if result:
            with self._cache_lock:
                self._profile_cache[cache_key] = result

        return result

    def get_all_profiles(self) -> List[Dict[str, Any]]:
        """Get all profiles with single query"""
        return (
            self.execute_query(
                "SELECT * FROM bandwidth_profiles ORDER BY name", fetch=True
            )
            or []
        )

    def add_profile(self, profile: Profile) -> bool:
        """Add profile with cache invalidation"""
        try:
            self.execute_query(
                """
                INSERT INTO bandwidth_profiles 
                (name, rate_limit, description, price, time_limit, data_limit, validity_period, uptime_limit)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    rate_limit = EXCLUDED.rate_limit,
                    description = EXCLUDED.description,
                    price = EXCLUDED.price,
                    time_limit = EXCLUDED.time_limit,
                    data_limit = EXCLUDED.data_limit,
                    validity_period = EXCLUDED.validity_period,
                    uptime_limit = EXCLUDED.uptime_limit
                """,
                (
                    profile.name,
                    profile.rate_limit,
                    profile.description,
                    profile.price,
                    profile.time_limit,
                    profile.data_limit,
                    profile.validity_period,
                    profile.uptime_limit,
                ),
            )
            # Invalidate cache
            with self._cache_lock:
                cache_key = f"profile_{profile.name.lower()}"
                if cache_key in self._profile_cache:
                    del self._profile_cache[cache_key]
            return True
        except Exception as e:
            logger.error(f"Error adding profile: {e}")
            return False

    # ---------------------------------------------------------
    # VOUCHER OPERATIONS (OPTIMIZED)
    # ---------------------------------------------------------
    def add_voucher(self, voucher: Voucher) -> bool:
        """Add single voucher"""
        try:
            self.execute_query(
                """
                INSERT INTO vouchers (voucher_code, profile_name, customer_name, customer_contact, 
                    expiry_time, uptime_limit, password_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    voucher.voucher_code,
                    voucher.profile_name,
                    voucher.customer_name,
                    voucher.customer_contact,
                    voucher.expiry_time,
                    voucher.uptime_limit,
                    voucher.password_type,
                ),
            )
            return True
        except Exception as e:
            logger.error(f"Error adding voucher: {e}")
            return False

    def add_vouchers_batch(self, vouchers: List[Voucher]) -> bool:
        """Add multiple vouchers in batch for better performance"""
        if not vouchers:
            return True

        batch_data = []
        for voucher in vouchers:
            batch_data.append(
                (
                    voucher.voucher_code,
                    voucher.profile_name,
                    voucher.customer_name,
                    voucher.customer_contact,
                    voucher.expiry_time,
                    voucher.uptime_limit,
                    voucher.password_type,
                )
            )

        try:
            self.execute_query(
                """
                INSERT INTO vouchers (voucher_code, profile_name, customer_name, customer_contact, 
                    expiry_time, uptime_limit, password_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                batch_data=batch_data,
            )
            return True
        except Exception as e:
            logger.error(f"Error adding vouchers batch: {e}")
            return False

    def mark_voucher_used(self, voucher_code: str):
        """Mark voucher as used"""
        self.execute_query(
            "UPDATE vouchers SET is_used=TRUE, activated_at=CURRENT_TIMESTAMP WHERE voucher_code=%s",
            (voucher_code,),
        )

    def mark_vouchers_used_batch(self, voucher_codes: List[str]):
        """Mark multiple vouchers as used in batch"""
        if not voucher_codes:
            return

        batch_data = [(code,) for code in voucher_codes]
        self.execute_query(
            "UPDATE vouchers SET is_used=TRUE, activated_at=CURRENT_TIMESTAMP WHERE voucher_code=%s",
            batch_data=batch_data,
        )

    def get_voucher(self, voucher_code: str) -> Optional[Dict[str, Any]]:
        """Get single voucher"""
        return self.execute_query(
            "SELECT * FROM vouchers WHERE voucher_code=%s",
            (voucher_code,),
            fetch_one=True,
        )

    # ---------------------------------------------------------
    # USER OPERATIONS (OPTIMIZED WITH BATCHING)
    # ---------------------------------------------------------
    def get_user_info(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user information"""
        return self.execute_query(
            """
            SELECT username, profile_name, activated_at, is_active, last_seen,
                   uptime_limit, comment, password_type, is_voucher
            FROM all_users WHERE username=%s
            """,
            (username,),
            fetch_one=True,
        )

    def get_all_users(self) -> List[Dict[str, Any]]:
        """Get all users with pagination-ready query"""
        return (
            self.execute_query(
                """
            SELECT username, profile_name, is_active, last_seen,
                   uptime_limit, comment, password_type, is_voucher
            FROM all_users 
            ORDER BY last_seen DESC NULLS LAST, username
            """,
                fetch=True,
            )
            or []
        )

    def get_users_paginated(self, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        """Get users with pagination for better performance with large datasets"""
        offset = (page - 1) * page_size

        users = (
            self.execute_query(
                """
            SELECT username, profile_name, is_active, last_seen,
                   uptime_limit, comment, password_type, is_voucher
            FROM all_users 
            ORDER BY last_seen DESC NULLS LAST, username
            LIMIT %s OFFSET %s
            """,
                (page_size, offset),
                fetch=True,
            )
            or []
        )

        total_result = self.execute_query(
            "SELECT COUNT(*) as total FROM all_users", fetch_one=True
        )
        total = total_result["total"] if total_result else 0

        return {
            "users": users,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": (total + page_size - 1) // page_size,
            },
        }

    def get_pricing_rates(self) -> Dict[str, int]:
        """Get pricing rates with caching"""
        self._clean_cache_if_needed()

        with self._cache_lock:
            if "pricing_rates" in self._profile_cache:
                return self._profile_cache["pricing_rates"]

        rows = (
            self.execute_query(
                "SELECT rate_type, amount FROM pricing_rates", fetch=True
            )
            or []
        )
        rates = {r["rate_type"]: r["amount"] for r in rows}

        with self._cache_lock:
            self._profile_cache["pricing_rates"] = rates

        return rates

    def add_transaction(self, transaction: FinancialTransaction):
        """Add single transaction"""
        self.execute_query(
            """
            INSERT INTO financial_transactions (voucher_code, amount, transaction_type)
            VALUES (%s, %s, %s)
            """,
            (
                transaction.voucher_code,
                transaction.amount,
                transaction.transaction_type,
            ),
        )

    def add_transactions_batch(self, transactions: List[FinancialTransaction]):
        """Add multiple transactions in batch"""
        if not transactions:
            return

        batch_data = []
        for transaction in transactions:
            batch_data.append(
                (
                    transaction.voucher_code,
                    transaction.amount,
                    transaction.transaction_type,
                )
            )

        self.execute_query(
            """
            INSERT INTO financial_transactions (voucher_code, amount, transaction_type)
            VALUES (%s, %s, %s)
            """,
            batch_data=batch_data,
        )

    def record_voucher_activation(self, username: str, uptime_seconds: int):
        """
        When a user's voucher starts being used (uptime > 1s), record payment and mark voucher as used.
        Optimized to check conditions early.
        """
        if uptime_seconds <= 1:
            return  # Ignore idle

        user = self.get_user_info(username)
        if not user:
            logger.warning(f"User {username} not found in all_users.")
            return

        # Check if transaction already exists
        existing_tx = self.execute_query(
            """
            SELECT id FROM financial_transactions
            WHERE voucher_code=%s AND transaction_type='SALE'
            """,
            (username,),
            fetch_one=True,
        )

        if existing_tx:
            return  # Already processed

        uptime_limit = user.get("uptime_limit", "1d")
        rates = self.get_pricing_rates()

        # Determine amount based on uptime limit
        if "1d" in uptime_limit or uptime_limit == "24h":
            amount = rates.get("day", 1000)
        elif "7d" in uptime_limit:
            amount = rates.get("week", 6000)
        elif "30d" in uptime_limit:
            amount = rates.get("month", 25000)
        else:
            amount = rates.get("day", 1000)

        # Use batch operations for related updates
        self.mark_voucher_used(username)

        transaction = FinancialTransaction(
            voucher_code=username, amount=amount, transaction_type="SALE"
        )
        self.add_transaction(transaction)
        logger.info(f"Recorded SALE for {username} — {amount} UGX")

    def record_active_users(self, active_users: List[Dict[str, Any]]):
        """
        Record active MikroTik users in the database with batch operations.
        """
        if not active_users:
            return

        # Prepare batch data for upsert
        upsert_data = []
        activation_data = []

        # Get profile uptimes in batch
        profile_names = list(
            set(u.get("profile_name", "default") for u in active_users)
        )
        profile_uptimes = {}
        for profile_name in profile_names:
            profile_uptimes[profile_name] = self._get_profile_uptime(profile_name)

        for u in active_users:
            username = u.get("username") or u.get("user") or u.get("name")
            if not username:
                continue

            profile_name = u.get("profile_name", "default")
            uptime_str = u.get("uptime", "0")
            try:
                uptime_seconds = int(uptime_str)
            except (TypeError, ValueError):
                uptime_seconds = 0

            uptime_limit = profile_uptimes.get(profile_name, "1d")

            upsert_data.append((username, profile_name, True, uptime_limit))

            if uptime_seconds > 1:
                activation_data.append((username, uptime_seconds))

        # Batch upsert users
        if upsert_data:
            self.execute_query(
                """
                INSERT INTO all_users (username, profile_name, is_active, uptime_limit, last_seen)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (username) DO UPDATE SET
                    profile_name = EXCLUDED.profile_name,
                    is_active = EXCLUDED.is_active,
                    uptime_limit = EXCLUDED.uptime_limit,
                    last_seen = CURRENT_TIMESTAMP
                """,
                batch_data=upsert_data,
            )

        # Process activations
        for username, uptime_seconds in activation_data:
            self.record_voucher_activation(username, uptime_seconds)

    def _get_profile_uptime(self, profile_name: str) -> str:
        """Get profile uptime with caching"""
        profile = self.get_profile(profile_name)
        return profile["uptime_limit"] if profile else "1d"

    def _get_profile_price(self, profile_name: str) -> int:
        """Get profile price with caching"""
        profile = self.get_profile(profile_name)
        return profile["price"] if profile else 1000

    # ---------------------------------------------------------
    # FINANCIAL OPERATIONS (OPTIMIZED)
    # ---------------------------------------------------------
    def calculate_daily_revenue(self) -> int:
        """Calculate daily revenue with optimized query"""
        result = self.execute_query(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM financial_transactions
            WHERE transaction_type = 'SALE'
            AND DATE(transaction_date) = CURRENT_DATE
            """,
            fetch_one=True,
        )
        return result["total"] if result else 0

    def get_financial_stats(
        self, mikrotik_manager: Optional[MikroTikManager] = None
    ) -> Dict[str, Any]:
        """Return summarized stats of vouchers and sales with single query execution"""

        # Use single query to get multiple stats
        stats_query = """
        SELECT 
            (SELECT COALESCE(SUM(amount),0) FROM financial_transactions WHERE transaction_type='SALE') as total_revenue,
            (SELECT COALESCE(SUM(amount),0) FROM financial_transactions WHERE transaction_type='SALE' AND DATE(transaction_date)=CURRENT_DATE) as daily_revenue,
            (SELECT COUNT(*) FROM vouchers WHERE is_used=FALSE) as active_vouchers,
            (SELECT COUNT(*) FROM vouchers WHERE is_used=TRUE AND DATE(activated_at)=CURRENT_DATE) as used_today,
            (SELECT COUNT(*) FROM all_users WHERE DATE(activated_at)=CURRENT_DATE) as daily_activations
        """

        result = self.execute_query(stats_query, fetch_one=True) or {}

        return {
            "total_revenue": result.get("total_revenue", 0),
            "daily_revenue": result.get("daily_revenue", 0),
            "active_vouchers": result.get("active_vouchers", 0),
            "used_vouchers_today": result.get("used_today", 0),
            "daily_activations": result.get("daily_activations", 0),
        }

    def get_daily_activations(self) -> int:
        """Get daily activations count"""
        result = self.execute_query(
            "SELECT COUNT(*) as count FROM all_users WHERE DATE(activated_at) = CURRENT_DATE",
            fetch_one=True,
        )
        return result["count"] if result else 0

    def sync_user(self, user: User):
        """Insert or update user in the database"""
        self.execute_query(
            """
            INSERT INTO all_users (username, profile_name, uptime_limit, comment, password_type, is_voucher, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (username) DO UPDATE
            SET profile_name = EXCLUDED.profile_name,
                uptime_limit = EXCLUDED.uptime_limit,
                comment = EXCLUDED.comment,
                password_type = EXCLUDED.password_type,
                is_voucher = EXCLUDED.is_voucher,
                created_at = EXCLUDED.created_at
            """,
            (
                user.username,
                user.profile_name,
                user.uptime_limit,
                user.comment,
                user.password_type,
                user.is_voucher,
                user.created_at,
            ),
        )

    def update_user_active_status(self, usernames: list, is_active: bool):
        """Update is_active flag for a list of users with batch operation"""
        if not usernames:
            return

        batch_data = [(is_active, username) for username in usernames]
        self.execute_query(
            "UPDATE all_users SET is_active = %s WHERE username = %s",
            batch_data=batch_data,
        )

    def get_expired_users(self) -> list[dict]:
        """
        Fetch all users marked as expired from the database.
        """
        rows = self.execute_query(
            "SELECT * FROM all_users WHERE is_expired = TRUE", fetch=True
        )
        return rows or []

    # ---------------------------------------------------------
    # PERFORMANCE MONITORING
    # ---------------------------------------------------------
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get database performance statistics"""
        query_stats = """
        SELECT 
            schemaname, tablename, seq_scan, seq_tup_read,
            idx_scan, idx_tup_fetch, n_tup_ins, n_tup_upd, n_tup_del
        FROM pg_stat_user_tables 
        WHERE tablename IN ('vouchers', 'all_users', 'financial_transactions', 'bandwidth_profiles')
        """

        index_stats = """
        SELECT 
            tablename, indexname, idx_scan, idx_tup_read, idx_tup_fetch
        FROM pg_stat_user_indexes 
        WHERE tablename IN ('vouchers', 'all_users', 'financial_transactions', 'bandwidth_profiles')
        """

        table_stats = self.execute_query(query_stats, fetch=True) or []
        index_stats = self.execute_query(index_stats, fetch=True) or []

        return {
            "table_statistics": table_stats,
            "index_statistics": index_stats,
            "connection_pool_size": len(self._connection_pool),
        }

    def register_user(self, user_data: dict) -> bool:
        """Register new user with enhanced data"""
        try:
            self.execute_query(
                """
                INSERT INTO users (user_id, email, password, full_name, phone, company_name, role)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_data["userId"],
                    user_data["email"],
                    user_data["password"],
                    user_data.get("full_name"),
                    user_data.get("phone"),
                    user_data.get("company_name"),
                    user_data.get("role", "user"),
                ),
            )
            return True
        except Exception as e:
            logger.error(f"Error registering user: {e}")
            return False

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get user by email"""
        return self.execute_query(
            "SELECT * FROM users WHERE LOWER(email)=LOWER(%s)", (email,), fetch_one=True
        )

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user by user_id"""
        return self.execute_query(
            "SELECT * FROM users WHERE user_id=%s", (user_id,), fetch_one=True
        )

    def verify_login(self, credentials: dict) -> Optional[Dict[str, Any]]:
        """Verify user login credentials"""
        user = self.get_user_by_email(credentials["email"])
        if user and user.get("is_active") and user.get("is_verified"):
            # Return user data without password for security
            return {
                "id": user["user_id"],
                "email": user["email"],
                "full_name": user["full_name"],
                "role": user["role"],
                "company_name": user["company_name"],
                "password": user["password"],  # For verification in auth service
            }
        return None

    def update_user_verification(self, email: str, verified: bool = True) -> bool:
        """Update user verification status"""
        try:
            self.execute_query(
                "UPDATE users SET is_verified=%s, updated_at=CURRENT_TIMESTAMP WHERE email=%s",
                (verified, email),
            )
            return True
        except Exception as e:
            logger.error(f"Error updating user verification: {e}")
            return False

    def save_router_info(self, router_data: dict) -> bool:
        """Save router information for a user"""
        try:
            encrypted_password = self._encrypt_password(router_data["password"])

            self.execute_query(
                """
                INSERT INTO user_routers (user_id, router_name, host, username, password, description)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, router_name) DO UPDATE SET
                    host = EXCLUDED.host,
                    username = EXCLUDED.username,
                    password = EXCLUDED.password,
                    description = EXCLUDED.description,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    router_data["user_id"],
                    router_data["router_name"],
                    router_data["host"],
                    router_data["username"],
                    encrypted_password,
                    router_data.get("description"),
                ),
            )
            return True
        except Exception as e:
            logger.error(f"Error saving router info: {e}")
            return False

    def get_user_routers(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all routers for a user"""
        user = self.get_user_by_id(user_id)
        if not user:
            return []

        return (
            self.execute_query(
                """
            SELECT id, router_name, host, username, description, is_active, created_at
            FROM user_routers 
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY router_name
            """,
                (user["id"],),
                fetch=True,
            )
            or []
        )

    def get_router_credentials(
        self, user_id: str, router_name: str
    ) -> Optional[Dict[str, Any]]:
        """Get router credentials for connection (with decryption)"""
        user = self.get_user_by_id(user_id)
        if not user:
            return None

        router = self.execute_query(
            "SELECT * FROM user_routers WHERE user_id=%s AND router_name=%s AND is_active=TRUE",
            (user["id"], router_name),
            fetch_one=True,
        )

        if router:
            # Decrypt the password
            router["password"] = self._decrypt_password(router["password"])
            return router
        return None

    def create_session(
        self,
        user_id: str,
        token: str,
        device_info: str = None,
        ip_address: str = None,
        expire_seconds: int = 3600,
    ) -> bool:
        """Create user session"""
        try:
            expires_at = datetime.now() + timedelta(seconds=expire_seconds)
            self.execute_query(
                """
                INSERT INTO user_sessions (user_id, token, device_info, ip_address, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    self._get_user_db_id(user_id),
                    token,
                    device_info,
                    ip_address,
                    expires_at,
                ),
            )
            return True
        except Exception as e:
            logger.error(f"Error creating session: {e}")
            return False

    def validate_session(self, token: str) -> Optional[Dict[str, Any]]:
        """Validate user session"""
        session = self.execute_query(
            """
            SELECT us.*, u.user_id, u.email, u.role, u.full_name
            FROM user_sessions us
            JOIN users u ON us.user_id = u.id
            WHERE us.token = %s AND us.is_active = TRUE AND us.expires_at > CURRENT_TIMESTAMP
            AND u.is_active = TRUE AND u.is_verified = TRUE
            """,
            (token,),
            fetch_one=True,
        )
        return session

    def invalidate_session(self, token: str) -> bool:
        """Invalidate user session (logout)"""
        try:
            self.execute_query(
                "UPDATE user_sessions SET is_active=FALSE WHERE token=%s", (token,)
            )
            return True
        except Exception as e:
            logger.error(f"Error invalidating session: {e}")
            return False

    def invalidate_all_user_sessions(self, user_id: str) -> bool:
        """Invalidate all sessions for a user"""
        try:
            self.execute_query(
                "UPDATE user_sessions SET is_active=FALSE WHERE user_id=%s",
                (self._get_user_db_id(user_id),),
            )
            return True
        except Exception as e:
            logger.error(f"Error invalidating user sessions: {e}")
            return False

    def create_password_reset_token(
        self, user_id: str, token: str, expire_seconds: int = 3600
    ) -> bool:
        """Create password reset token"""
        try:
            expires_at = datetime.now() + timedelta(seconds=expire_seconds)
            self.execute_query(
                "INSERT INTO password_resets (user_id, token, expires_at) VALUES (%s, %s, %s)",
                (self._get_user_db_id(user_id), token, expires_at),
            )
            return True
        except Exception as e:
            logger.error(f"Error creating password reset token: {e}")
            return False

    def validate_password_reset_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Validate password reset token"""
        return self.execute_query(
            """
            SELECT pr.*, u.user_id, u.email
            FROM password_resets pr
            JOIN users u ON pr.user_id = u.id
            WHERE pr.token = %s AND pr.used = FALSE AND pr.expires_at > CURRENT_TIMESTAMP
            AND u.is_active = TRUE
            """,
            (token,),
            fetch_one=True,
        )

    def use_password_reset_token(self, token: str) -> bool:
        """Mark password reset token as used"""
        try:
            self.execute_query(
                "UPDATE password_resets SET used=TRUE WHERE token=%s", (token,)
            )
            return True
        except Exception as e:
            logger.error(f"Error using password reset token: {e}")
            return False

    def _get_user_db_id(self, user_id: str) -> int:
        """Get database internal ID from user_id"""
        user = self.get_user_by_id(user_id)
        return user["id"] if user else None

    def _encrypt_password(self, password: str) -> str:
        """Encrypt router passwords (use a proper encryption method)"""
        # In production, use proper encryption like Fernet or similar
        import base64
        from cryptography.fernet import Fernet

        # You should store this key securely in environment variables
        key = os.getenv("ENCRYPTION_KEY", "default_key_change_in_production")
        fernet = Fernet(base64.urlsafe_b64encode(key.ljust(32)[:32].encode()))
        return fernet.encrypt(password.encode()).decode()

    def _decrypt_password(self, encrypted_password: str) -> str:
        """Decrypt router passwords"""
        try:
            import base64
            from cryptography.fernet import Fernet

            key = os.getenv("ENCRYPTION_KEY", "default_key_change_in_production")
            fernet = Fernet(base64.urlsafe_b64encode(key.ljust(32)[:32].encode()))
            return fernet.decrypt(encrypted_password.encode()).decode()
        except Exception as e:
            logger.error(f"Error decrypting password: {e}")
            return ""

    def update_user_password(self, user_id: str, new_password: str) -> bool:
        """Update user password"""
        try:
            self.execute_query(
                "UPDATE users SET password=%s, updated_at=CURRENT_TIMESTAMP WHERE user_id=%s",
                (new_password, user_id),
            )
            self.invalidate_all_user_sessions(user_id)
            return True
        except Exception as e:
            logger.error(f"Error updating user password: {e}")
            return False
        
    def send_verification_code(self, email: str, user_id: str, code_type: str = "EMAIL", expire_seconds: int = 600) -> Optional[str]:
        """
    Generate and store a verification code for a user.
    Returns the code (for logging/testing, not in production).
        """
        try:
            code = f"VRFY{random.randint(1000, 9999)}"
            expires_at = datetime.now() + timedelta(seconds=expire_seconds)

            self.execute_query(
            '''
            INSERT INTO user_verifications (user_id, code, type, expires_at)
            VALUES (%s, %s, %s, %s)
            ''',
            (self._get_user_db_id(user_id), code, code_type, expires_at)
            )

            logger.info(f"Verification code for {email}: {code}")

        # TODO: send via email/SMS in production
           # send_email(to=email, subject="Verification Code", body=f"Your code is {code}")
           # if code_type == "SMS":
            #    twilio_client.messages.create(body=f"Your code: {code}", from_=TWILIO_PHONE_NUMBER, to=user_phone)

            return code
        except Exception as e:
            logger.error(f"Failed to generate/send verification code for {email}: {e}")
            return None
        
        
    def validate_verification_code(self, user_id: str, code: str) -> bool:
        """Validate a verification code"""
        row = self.execute_query(
        '''
        SELECT * FROM user_verifications
        WHERE user_id=%s AND code=%s AND used=FALSE AND expires_at > CURRENT_TIMESTAMP
        ''',
        (self._get_user_db_id(user_id), code),
        fetch_one=True
        )

        if row:
            self.execute_query(
            'UPDATE user_verifications SET used=TRUE WHERE id=%s',
            (row['id'],)
            )
            return True

        return False