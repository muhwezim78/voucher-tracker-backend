import psycopg2
from psycopg2.extras import RealDictCursor
import logging
from typing import List, Dict, Any, Optional
import threading
from datetime import datetime
from config import Config
from models.schemas import Voucher, User, Profile, FinancialTransaction
from .mikrotik_manager import MikroTikManager

logger = logging.getLogger(__name__)

class DatabaseService:
    def __init__(self, config: Config):
        self.config = config
        self.db_lock = threading.Lock()  # Thread-safe execution

    # ---------------------------------------------------------
    # CONNECTION & QUERY HELPERS
    # ---------------------------------------------------------
    def get_connection(self):
        """Get PostgreSQL database connection"""
        return psycopg2.connect(**self.config.DB_CONFIG)

    def execute_query(self, query: str, params: tuple = None, fetch: bool = False, fetch_one: bool = False):
        """Thread-safe helper for executing SQL queries"""
        with self.db_lock:
            conn = self.get_connection()
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(query, params)
                    result = None
                    if fetch_one:
                        result = cursor.fetchone()
                    elif fetch:
                        result = cursor.fetchall()
                    conn.commit()
                    return result
            except Exception as e:
                conn.rollback()
                logger.error(f"Database error: {e}")
                raise
            finally:
                conn.close()

    # ---------------------------------------------------------
    # DATABASE INITIALIZATION
    # ---------------------------------------------------------
    def init_db(self):
        """Initialize PostgreSQL database tables"""
        queries = [
            '''
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
            ''',
            '''
            CREATE TABLE IF NOT EXISTS financial_transactions (
                id SERIAL PRIMARY KEY,
                voucher_code TEXT,
                amount BIGINT,
                transaction_type TEXT,
                transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''',
            '''
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
            ''',
            '''
            CREATE TABLE IF NOT EXISTS pricing_rates (
                id SERIAL PRIMARY KEY,
                rate_type TEXT UNIQUE,
                amount BIGINT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''',
            '''
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
            '''
        ]

        for query in queries:
            self.execute_query(query)

        # Insert default rates if missing
        default_rates = [('day', 1000), ('week', 6000), ('month', 25000)]
        for rate_type, amount in default_rates:
            self.execute_query(
                '''
                INSERT INTO pricing_rates (rate_type, amount)
                VALUES (%s, %s)
                ON CONFLICT (rate_type) DO NOTHING
                ''',
                (rate_type, amount)
            )

    # ---------------------------------------------------------
    # PROFILES
    # ---------------------------------------------------------
    def get_profile(self, profile_name: str) -> Optional[Dict[str, Any]]:
        return self.execute_query(
            'SELECT * FROM bandwidth_profiles WHERE LOWER(name)=LOWER(%s)',
            (profile_name,),
            fetch_one=True
        )

    def get_all_profiles(self) -> List[Dict[str, Any]]:
        return self.execute_query('SELECT * FROM bandwidth_profiles', fetch=True) or []

    def add_profile(self, profile: Profile) -> bool:
        try:
            self.execute_query(
                '''
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
                ''',
                (
                    profile.name, profile.rate_limit, profile.description, profile.price,
                    profile.time_limit, profile.data_limit, profile.validity_period, profile.uptime_limit
                )
            )
            return True
        except Exception as e:
            logger.error(f"Error adding profile: {e}")
            return False

    # ---------------------------------------------------------
    # VOUCHERS
    # ---------------------------------------------------------
    def add_voucher(self, voucher: Voucher) -> bool:
        try:
            self.execute_query(
                '''
                INSERT INTO vouchers (voucher_code, profile_name, customer_name, customer_contact, 
                    expiry_time, uptime_limit, password_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''',
                (
                    voucher.voucher_code, voucher.profile_name,
                    voucher.customer_name, voucher.customer_contact,
                    voucher.expiry_time, voucher.uptime_limit, voucher.password_type
                )
            )
            return True
        except Exception as e:
            logger.error(f"Error adding voucher: {e}")
            return False

    def mark_voucher_used(self, voucher_code: str):
        self.execute_query(
            'UPDATE vouchers SET is_used=TRUE, activated_at=CURRENT_TIMESTAMP WHERE voucher_code=%s',
            (voucher_code,)
        )

    def get_voucher(self, voucher_code: str) -> Optional[Dict[str, Any]]:
        return self.execute_query(
            'SELECT * FROM vouchers WHERE voucher_code=%s',
            (voucher_code,),
            fetch_one=True
        )

    # ---------------------------------------------------------
    # USERS
    # ---------------------------------------------------------
    def get_user_info(self, username: str) -> Optional[Dict[str, Any]]:
        return self.execute_query(
            '''
            SELECT username, profile_name, activated_at, is_active, last_seen,
                   uptime_limit, comment, password_type, is_voucher
            FROM all_users WHERE username=%s
            ''',
            (username,),
            fetch_one=True
        )

    def get_all_users(self) -> List[Dict[str, Any]]:
        return self.execute_query(
            '''
            SELECT username, profile_name, is_active, last_seen,
                   uptime_limit, comment, password_type, is_voucher
            FROM all_users ORDER BY last_seen DESC
            ''',
            fetch=True
        ) or []

    # ---------------------------------------------------------
    # PRICING
    # ---------------------------------------------------------
    def get_pricing_rates(self) -> Dict[str, int]:
        rows = self.execute_query('SELECT rate_type, amount FROM pricing_rates', fetch=True) or []
        return {r['rate_type']: r['amount'] for r in rows}

    # ---------------------------------------------------------
    # FINANCIAL LOGIC
    # ---------------------------------------------------------
    def add_transaction(self, transaction: FinancialTransaction):
        self.execute_query(
            '''
            INSERT INTO financial_transactions (voucher_code, amount, transaction_type)
            VALUES (%s, %s, %s)
            ''',
            (transaction.voucher_code, transaction.amount, transaction.transaction_type)
        )

    def record_voucher_activation(self, username: str, uptime_seconds: int):
        """
        When a user's voucher starts being used (uptime > 1s), record payment and mark voucher as used.
        """
        if uptime_seconds <= 1:
            return  # Ignore idle

        user = self.get_user_info(username)
        if not user:
            logger.warning(f"User {username} not found in all_users.")
            return

        uptime_limit = user.get('uptime_limit', '1d')
        rates = self.get_pricing_rates()

        # Determine amount based on uptime limit
        if '1d' in uptime_limit or uptime_limit == '24h':
            amount = rates.get('day', 1000)
        elif '7d' in uptime_limit:
            amount = rates.get('week', 6000)
        elif '30d' in uptime_limit:
            amount = rates.get('month', 25000)
        else:
            amount = rates.get('day', 1000)

        # Avoid double-charging: only record once
        existing_tx = self.execute_query(
            '''
            SELECT id FROM financial_transactions
            WHERE voucher_code=%s AND transaction_type='SALE'
            ''',
            (username,),
            fetch_one=True
        )

        if not existing_tx:
            self.mark_voucher_used(username)
            transaction = FinancialTransaction(
                voucher_code=username,
                amount=amount,
                transaction_type='SALE'
            )
            self.add_transaction(transaction)
            logger.info(f"Recorded SALE for {username} — {amount} UGX")

    # ---------------------------------------------------------
    # USER SYNC FROM MIKROTIK
    # ---------------------------------------------------------
    def record_active_users(self, active_users: List[Dict[str, Any]]):
        """
        Record active MikroTik users in the database and trigger financial recording if needed.
        """
        for u in active_users:
            username = u.get('username') or u.get('user') or u.get('name')
            if not username:
                continue

            profile_name = u.get('profile_name', 'default')
            uptime_str = u.get('uptime', '0')
            try:
                uptime_seconds = int(uptime_str)
            except (TypeError, ValueError):
                uptime_seconds = 0

            existing = self.get_user_info(username)
            if existing:
                self.execute_query(
                    '''
                    UPDATE all_users
                    SET last_seen=CURRENT_TIMESTAMP,
                        is_active=TRUE,
                        profile_name=%s
                    WHERE username=%s
                    ''',
                    (profile_name, username)
                )
            else:
                self.execute_query(
                    '''
                    INSERT INTO all_users (username, profile_name, activated_at, is_active, uptime_limit)
                    VALUES (%s, %s, CURRENT_TIMESTAMP, TRUE, %s)
                    ''',
                    (username, profile_name, self._get_profile_uptime(profile_name))
                )

            # ✅ Trigger voucher charge if uptime > 1s
            self.record_voucher_activation(username, uptime_seconds)

    # ---------------------------------------------------------
    # SUPPORTING HELPERS
    # ---------------------------------------------------------
    def _get_profile_uptime(self, profile_name: str) -> str:
        profile = self.get_profile(profile_name)
        return profile['uptime_limit'] if profile else "1d"

    def _get_profile_price(self, profile_name: str) -> int:
        profile = self.get_profile(profile_name)
        return profile['price'] if profile else 1000

    # ---------------------------------------------------------
    # ANALYTICS
    # ---------------------------------------------------------
    def calculate_daily_revenue(self) -> int:
        result = self.execute_query(
            '''
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM financial_transactions
            WHERE transaction_type='SALE' AND transaction_date >= CURRENT_DATE
            ''',
            fetch_one=True
        )
        return result['total'] if result else 0

    def get_financial_stats(self, mikrotik_manager: Optional[MikroTikManager] = None) -> Dict[str, Any]:
        """Return summarized stats of vouchers and sales."""
        total_revenue = self.execute_query(
            "SELECT COALESCE(SUM(amount),0) as total_revenue FROM financial_transactions WHERE transaction_type='SALE'",
            fetch_one=True
        ) or {'total_revenue': 0}

        daily_revenue = self.calculate_daily_revenue()

        active_vouchers = self.execute_query(
            "SELECT COUNT(*) as active_vouchers FROM vouchers WHERE is_used=FALSE",
            fetch_one=True
        ) or {'active_vouchers': 0}

        used_today = self.execute_query(
            "SELECT COUNT(*) as used_today FROM vouchers WHERE is_used=TRUE AND DATE(activated_at)=CURRENT_DATE",
            fetch_one=True
        ) or {'used_today': 0}

        daily_activations = self.get_daily_activations()

        return {
            'total_revenue': total_revenue['total_revenue'],
            'daily_revenue': daily_revenue,
            'active_vouchers': active_vouchers['active_vouchers'],
            'used_vouchers_today': used_today['used_today'],
            'daily_activations': daily_activations
        }

    def get_daily_activations(self) -> int:
        result = self.execute_query(
            "SELECT COUNT(*) as count FROM all_users WHERE DATE(activated_at) = CURRENT_DATE",
            fetch_one=True
        )
        return result['count'] if result else 0
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
            (user.username, user.profile_name, user.uptime_limit, user.comment,
             user.password_type, user.is_voucher, user.created_at)
        )

    def update_user_active_status(self, usernames: list, is_active: bool):
        """Update is_active flag for a list of users"""
        if not usernames:
            return
        self.execute_query(
            f"UPDATE all_users SET is_active = %s WHERE username = ANY(%s)",
            (is_active, usernames)
        )
    def get_expired_users(self) -> list[dict]:
        """
        Fetch all users marked as expired from the database.
        """
        rows = self.execute_query(
            "SELECT * FROM all_users WHERE is_expired = TRUE",
            fetch=True
        )
        return rows or []
    
