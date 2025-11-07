import psycopg2
from psycopg2.extras import RealDictCursor
import logging
from typing import List, Dict, Any, Optional
import threading

from config import Config
from models.schemas import Voucher, User, Profile, FinancialTransaction

logger = logging.getLogger(__name__)

class DatabaseService:
    def __init__(self, config: Config):
        self.config = config
        self.db_lock = threading.Lock()

    def get_connection(self):
        """Get PostgreSQL database connection"""
        return psycopg2.connect(**self.config.DB_CONFIG)

    def execute_query(self, query: str, params: tuple = None, fetch: bool = False, fetch_one: bool = False):
        """Helper function to execute database queries"""
        conn = self.get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, params)
                if fetch:
                    result = cursor.fetchall()
                elif fetch_one:
                    result = cursor.fetchone()
                else:
                    result = None
                conn.commit()
                return result
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise e
        finally:
            conn.close()

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
                bytes_used INTEGER DEFAULT 0,
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
                amount INTEGER,
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
                price INTEGER DEFAULT 0,
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
                amount INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS all_users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                profile_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP,
                is_active BOOLEAN DEFAULT FALSE,
                bytes_used INTEGER DEFAULT 0,
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
        
        self._initialize_default_data()

    def _initialize_default_data(self):
        """Initialize default pricing rates and profiles"""
        # Default pricing rates
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
        
        # Default profiles
        default_profiles = [
            ('1DAY', 'unlimited', 'Daily Profile', 1000, '24h', 'Unlimited', 24, '1d'),
            ('1WEEK', 'unlimited', 'Weekly Profile', 6000, '7 days', 'Unlimited', 168, '7d'),
            ('1MONTH', 'unlimited', 'Monthly Profile', 25000, '30 days', 'Unlimited', 720, '30d')
        ]
        
        for profile in default_profiles:
            self.execute_query(
                '''
                INSERT INTO bandwidth_profiles 
                (name, rate_limit, description, price, time_limit, data_limit, validity_period, uptime_limit)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO NOTHING
                ''',
                profile
            )

    # Voucher methods
    def add_voucher(self, voucher: Voucher) -> bool:
        """Add voucher to database"""
        try:
            self.execute_query(
                '''
                INSERT INTO vouchers (voucher_code, profile_name, customer_name, customer_contact, 
                                   expiry_time, uptime_limit, password_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''',
                (voucher.voucher_code, voucher.profile_name, voucher.customer_name, 
                 voucher.customer_contact, voucher.expiry_time, voucher.uptime_limit, 
                 voucher.password_type)
            )
            return True
        except Exception as e:
            logger.error(f"Error adding voucher: {e}")
            return False

    def get_voucher(self, voucher_code: str) -> Optional[Dict[str, Any]]:
        """Get voucher by code"""
        return self.execute_query(
            '''
            SELECT voucher_code, profile_name, created_at, activated_at, is_used, 
                   bytes_used, session_time, customer_name, customer_contact, 
                   uptime_limit, password_type
            FROM vouchers WHERE voucher_code=%s
            ''',
            (voucher_code,),
            fetch_one=True
        )

    def mark_voucher_used(self, voucher_code: str):
        """Mark voucher as used"""
        self.execute_query(
            'UPDATE vouchers SET is_used=TRUE, activated_at=CURRENT_TIMESTAMP WHERE voucher_code=%s',
            (voucher_code,)
        )

    def update_voucher_usage(self, voucher_code: str, bytes_used: int):
        """Update voucher bytes used"""
        self.execute_query(
            'UPDATE vouchers SET bytes_used=%s WHERE voucher_code=%s',
            (bytes_used, voucher_code)
        )

    # User methods
    def sync_user(self, user: User):
        """Sync user to database"""
        self.execute_query(
            '''
            INSERT INTO all_users 
            (username, profile_name, uptime_limit, last_seen, comment, password_type, is_voucher)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s, %s, %s)
            ON CONFLICT (username) DO UPDATE SET
                profile_name = EXCLUDED.profile_name,
                uptime_limit = EXCLUDED.uptime_limit,
                last_seen = EXCLUDED.last_seen,
                comment = EXCLUDED.comment,
                password_type = EXCLUDED.password_type,
                is_voucher = EXCLUDED.is_voucher
            ''',
            (user.username, user.profile_name, user.uptime_limit, user.comment, 
             user.password_type, user.is_voucher)
        )

    def update_user_active_status(self, usernames: List[str], is_active: bool = True):
        """Update active status for users"""
        if not usernames:
            return
        
        placeholders = ','.join(['%s'] * len(usernames))
        query = f'UPDATE all_users SET is_active=%s, last_seen=CURRENT_TIMESTAMP WHERE username IN ({placeholders})'
        self.execute_query(query, (is_active, *usernames))

    def get_all_users(self) -> List[Dict[str, Any]]:
        """Get all users from database"""
        return self.execute_query(
            '''
            SELECT username, profile_name, is_active, last_seen, uptime_limit, 
                   comment, password_type, is_voucher
            FROM all_users 
            ORDER BY last_seen DESC
            ''',
            fetch=True
        ) or []

    # Profile methods
    def add_profile(self, profile: Profile) -> bool:
        """Add profile to database"""
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

    def get_profile(self, profile_name: str) -> Optional[Dict[str, Any]]:
        """Get profile by name"""
        return self.execute_query(
            'SELECT name, rate_limit, price, time_limit, data_limit, validity_period, uptime_limit FROM bandwidth_profiles WHERE name=%s',
            (profile_name,),
            fetch_one=True
        )

    def get_all_profiles(self) -> List[Dict[str, Any]]:
        """Get all profiles from database"""
        return self.execute_query(
            'SELECT name, rate_limit, price, time_limit, data_limit, validity_period, uptime_limit FROM bandwidth_profiles',
            fetch=True
        ) or []

    # Financial methods
    def add_transaction(self, transaction: FinancialTransaction):
        """Add financial transaction"""
        self.execute_query(
            'INSERT INTO financial_transactions (voucher_code, amount, transaction_type) VALUES (%s, %s, %s)',
            (transaction.voucher_code, transaction.amount, transaction.transaction_type)
        )

    def get_financial_stats(self) -> Dict[str, Any]:
        """Get financial statistics"""
        total_revenue = self.execute_query(
            "SELECT COALESCE(SUM(amount),0) as total_revenue FROM financial_transactions WHERE transaction_type='SALE'",
            fetch_one=True
        ) or {'total_revenue': 0}
        
        daily_revenue = self.execute_query(
            "SELECT COALESCE(SUM(amount),0) as daily_revenue FROM financial_transactions WHERE transaction_type='SALE' AND DATE(transaction_date)=CURRENT_DATE",
            fetch_one=True
        ) or {'daily_revenue': 0}
        
        active_vouchers = self.execute_query(
            "SELECT COUNT(*) as active_vouchers FROM vouchers WHERE is_used=FALSE",
            fetch_one=True
        ) or {'active_vouchers': 0}
        
        used_today = self.execute_query(
            "SELECT COUNT(*) as used_today FROM vouchers WHERE is_used=TRUE AND DATE(activated_at)=CURRENT_DATE",
            fetch_one=True
        ) or {'used_today': 0}
        
        return {
            'total_revenue': total_revenue['total_revenue'],
            'daily_revenue': daily_revenue['daily_revenue'],
            'active_vouchers': active_vouchers['active_vouchers'],
            'used_vouchers_today': used_today['used_today']
        }

    def get_revenue_data(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get revenue data for specified number of days"""
        rows = self.execute_query(
            '''
            SELECT DATE(transaction_date) as date, SUM(amount) as revenue, COUNT(*) as voucher_count
            FROM financial_transactions
            WHERE transaction_type='SALE' AND transaction_date >= CURRENT_DATE - INTERVAL '%s days'
            GROUP BY DATE(transaction_date)
            ORDER BY date
            ''',
            (days,),
            fetch=True
        ) or []
        
        return rows

    # Pricing methods
    def get_pricing_rates(self) -> Dict[str, int]:
        """Get pricing rates from database"""
        rows = self.execute_query('SELECT rate_type, amount FROM pricing_rates', fetch=True) or []
        
        rates = {}
        for row in rows:
            rates[row['rate_type']] = row['amount']
        return rates

    def update_pricing_rates(self, rates: Dict[str, int]):
        """Update pricing rates in database"""
        for rate_type, amount in rates.items():
            self.execute_query(
                'UPDATE pricing_rates SET amount=%s, updated_at=CURRENT_TIMESTAMP WHERE rate_type=%s',
                (amount, rate_type)
            )