import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
import logging
from typing import List, Dict, Any, Optional, Tuple
import threading
from datetime import datetime, timedelta
from contextlib import contextmanager
import time
import os
import random
import json
import subprocess
from cryptography.fernet import Fernet
import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import re

logger = logging.getLogger(__name__)

class DatabaseService:
    """Main database service with integrated user, router, and subscription management"""
    
    def __init__(self, config):
        self.config = config
        self._connection_pool = []
        self._max_pool_size = 10
        self._pool_lock = threading.Lock()
        self._stats = {
            'queries_executed': 0,
            'connection_creates': 0,
            'connection_returns': 0,
            'errors': 0
        }
        self._stats_lock = threading.Lock()
        
        self.user_service = UserService(self)
        self.router_service = RouterService(self)
        self.subscription_service = SubscriptionService(self)
        self.monitoring_service = MonitoringService(self)
        
        # Initialize connection pool
        self._initialize_pool()

    def _initialize_pool(self):
        """Initialize connection pool with minimum connections"""
        for _ in range(3):
            conn = self._create_connection()
            if conn:
                self._connection_pool.append(conn)

    def _create_connection(self) -> Optional[psycopg2.extensions.connection]:
        """Create new database connection with health check"""
        try:
            conn = psycopg2.connect(**self.config.DB_CONFIG)
            conn.autocommit = False
            
            # Set optimal connection parameters
            with conn.cursor() as cursor:
                cursor.execute("SET statement_timeout = 30000")
                cursor.execute("SET idle_in_transaction_session_timeout = 60000")
            
            with self._stats_lock:
                self._stats['connection_creates'] += 1
                
            return conn
        except Exception as e:
            logger.error(f"Failed to create database connection: {e}")
            return None

    def _health_check(self, conn: psycopg2.extensions.connection) -> bool:
        """Check if connection is healthy"""
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
            return True
        except Exception:
            return False

    @contextmanager
    def get_connection(self):
        """Get database connection from pool with context manager"""
        conn = None
        start_time = time.time()
        
        try:
            with self._pool_lock:
                while self._connection_pool:
                    conn = self._connection_pool.pop()
                    if self._health_check(conn):
                        break
                    else:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None
                
                if not conn:
                    conn = self._create_connection()
            
            if not conn:
                raise Exception("Failed to obtain database connection")
            
            yield conn
            
        except Exception as e:
            logger.error(f"Error in connection context: {e}")
            with self._stats_lock:
                self._stats['errors'] += 1
            raise
        finally:
            if conn:
                try:
                    conn.rollback()
                    
                    with self._pool_lock:
                        if (len(self._connection_pool) < self._max_pool_size and 
                            self._health_check(conn)):
                            self._connection_pool.append(conn)
                            with self._stats_lock:
                                self._stats['connection_returns'] += 1
                        else:
                            conn.close()
                except Exception as e:
                    logger.warning(f"Error returning connection to pool: {e}")
                    try:
                        conn.close()
                    except Exception:
                        pass

    def execute_query(
        self,
        query: str,
        params: tuple = None,
        fetch: bool = False,
        fetch_one: bool = False,
        batch_data: list = None,
        retries: int = 2
    ) -> Any:
        """Robust query execution with retry logic"""
        last_exception = None
        
        for attempt in range(retries + 1):
            with self.get_connection() as conn:
                try:
                    start_time = time.time()
                    
                    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                        if batch_data:
                            execute_batch(cursor, query, batch_data, page_size=100)
                        else:
                            cursor.execute(query, params or ())
                        
                        result = None
                        if fetch_one:
                            result = cursor.fetchone()
                        elif fetch:
                            result = cursor.fetchall()
                        
                        conn.commit()
                        
                        execution_time = time.time() - start_time
                        with self._stats_lock:
                            self._stats['queries_executed'] += 1
                        
                        if execution_time > 1.0:
                            logger.warning(
                                f"Slow query ({execution_time:.2f}s, attempt {attempt + 1}): {query[:100]}..."
                            )
                        
                        return result
                        
                except psycopg2.InterfaceError as e:
                    last_exception = e
                    logger.warning(f"Database connection error (attempt {attempt + 1}): {e}")
                    if attempt < retries:
                        time.sleep(0.1 * (attempt + 1))
                        continue
                    else:
                        break
                except psycopg2.Error as e:
                    conn.rollback()
                    last_exception = e
                    logger.error(f"Database error in query '{query[:50]}...': {e}")
                    raise
                except Exception as e:
                    conn.rollback()
                    last_exception = e
                    logger.error(f"Unexpected error in query execution: {e}")
                    raise
        
        raise last_exception or Exception("Query execution failed after retries")

    def init_db(self):
        """Initialize PostgreSQL database tables with all required schemas"""
        tables_queries = [
            # Core tables from original implementation
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
            
            # New subscription and router tables
            """
            CREATE TABLE IF NOT EXISTS subscription_packages (
                id SERIAL PRIMARY KEY,
                package_name TEXT UNIQUE NOT NULL,
                router_limit INTEGER NOT NULL,
                price BIGINT NOT NULL,
                features JSONB,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                voucher_code TEXT REFERENCES vouchers(voucher_code),
                package_type TEXT NOT NULL DEFAULT 'Basic',
                router_limit INTEGER NOT NULL DEFAULT 1,
                start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_date TIMESTAMP NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                auto_renew BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS enhanced_user_routers (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                router_name TEXT NOT NULL,
                host TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                model TEXT,
                location TEXT,
                is_online BOOLEAN DEFAULT FALSE,
                last_seen TIMESTAMP,
                wireguard_interface TEXT,
                wireguard_public_key TEXT,
                wireguard_private_key TEXT,
                wireguard_port INTEGER DEFAULT 51820,
                is_wireguard_setup BOOLEAN DEFAULT FALSE,
                description TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, router_name)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS router_monitoring (
                id SERIAL PRIMARY KEY,
                router_id INTEGER REFERENCES enhanced_user_routers(id) ON DELETE CASCADE,
                interface_name TEXT,
                interface_status TEXT,
                last_status_change TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_activity (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                action_type TEXT NOT NULL,
                description TEXT,
                ip_address TEXT,
                user_agent TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ]
        
        # Create tables
        for query in tables_queries:
            try:
                self.execute_query(query)
            except Exception as e:
                logger.error(f"Error creating table: {e}")

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
            
            # New indexes
            "CREATE INDEX IF NOT EXISTS idx_user_subscriptions_user_id ON user_subscriptions(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_user_subscriptions_end_date ON user_subscriptions(end_date)",
            "CREATE INDEX IF NOT EXISTS idx_enhanced_user_routers_user_id ON enhanced_user_routers(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_enhanced_user_routers_last_seen ON enhanced_user_routers(last_seen)",
            "CREATE INDEX IF NOT EXISTS idx_router_monitoring_router_id ON router_monitoring(router_id)",
            "CREATE INDEX IF NOT EXISTS idx_user_activity_user_id ON user_activity(user_id)",
        ]

        for query in index_queries:
            try:
                self.execute_query(query)
            except Exception as e:
                logger.warning(f"Could not create index: {e}")

        # Insert default data
        self._insert_default_data()

    def _insert_default_data(self):
        """Insert default packages and pricing rates"""
        try:
            # Default packages
            default_packages = [
                ('Basic', 1, 20000, '{"wireguard": true, "basic_monitoring": true}'),
                ('Premium', 3, 45000, '{"wireguard": true, "advanced_monitoring": true, "interface_alerts": true}'),
                ('Professional', 5, 80000, '{"wireguard": true, "advanced_monitoring": true, "interface_alerts": true, "offline_notifications": true, "sms_alerts": true}')
            ]
            
            self.execute_query(
                """
                INSERT INTO subscription_packages (package_name, router_limit, price, features)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (package_name) DO UPDATE SET
                    router_limit = EXCLUDED.router_limit,
                    price = EXCLUDED.price,
                    features = EXCLUDED.features
                """,
                batch_data=default_packages
            )
            
            # Default pricing rates
            default_rates = [("day", 1000), ("week", 6000), ("month", 25000)]
            self.execute_query(
                """
                INSERT INTO pricing_rates (rate_type, amount)
                VALUES (%s, %s)
                ON CONFLICT (rate_type) DO NOTHING
                """,
                batch_data=default_rates
            )
            
        except Exception as e:
            logger.error(f"Error inserting default data: {e}")

    def _encrypt_password(self, password: str) -> str:
        """Encrypt password with proper key management"""
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            raise ValueError("ENCRYPTION_KEY environment variable not set")
        
        salt = b'fixed_salt_change_in_production'
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(key.encode()))
        fernet = Fernet(key)
        return fernet.encrypt(password.encode()).decode()

    def _decrypt_password(self, encrypted_password: str) -> str:
        """Decrypt password"""
        try:
            key = os.getenv("ENCRYPTION_KEY")
            if not key:
                raise ValueError("ENCRYPTION_KEY environment variable not set")
            
            salt = b'fixed_salt_change_in_production'
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(key.encode()))
            fernet = Fernet(key)
            return fernet.decrypt(encrypted_password.encode()).decode()
        except Exception as e:
            logger.error(f"Error decrypting password: {e}")
            return ""

    def get_stats(self) -> Dict[str, Any]:
        """Get database performance statistics"""
        with self._stats_lock:
            stats = self._stats.copy()
        
        stats['pool_size'] = len(self._connection_pool)
        stats['pool_max_size'] = self._max_pool_size
        
        return stats

    # Original methods maintained for backward compatibility
    def get_voucher(self, voucher_code: str) -> Optional[Dict[str, Any]]:
        return self.execute_query(
            "SELECT * FROM vouchers WHERE voucher_code=%s",
            (voucher_code,),
            fetch_one=True
        )

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        return self.execute_query(
            "SELECT * FROM users WHERE LOWER(email)=LOWER(%s)", 
            (email,), 
            fetch_one=True
        )

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        return self.execute_query(
            "SELECT * FROM users WHERE user_id=%s", 
            (user_id,), 
            fetch_one=True
        )

    def _get_user_db_id(self, user_id: str) -> int:
        """Get database internal ID from user_id"""
        user = self.get_user_by_id(user_id)
        return user["id"] if user else None

    # Additional original methods...
    def add_voucher(self, voucher_data: dict) -> bool:
        try:
            self.execute_query(
                """
                INSERT INTO vouchers (voucher_code, profile_name, customer_name, customer_contact, 
                    expiry_time, uptime_limit, password_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    voucher_data["voucher_code"],
                    voucher_data["profile_name"],
                    voucher_data.get("customer_name"),
                    voucher_data.get("customer_contact"),
                    voucher_data.get("expiry_time"),
                    voucher_data.get("uptime_limit", "1d"),
                    voucher_data.get("password_type", "blank")
                ),
            )
            return True
        except Exception as e:
            logger.error(f"Error adding voucher: {e}")
            return False

class BaseService:
    """Base service class with common functionality"""
    
    def __init__(self, db_service: DatabaseService):
        self.db = db_service
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._cache_ttl = 300
    
    def _clean_cache(self, key: str = None):
        """Clean cache entries"""
        current_time = time.time()
        with self._cache_lock:
            if key:
                if key in self._cache and current_time - self._cache[key]['timestamp'] > self._cache_ttl:
                    del self._cache[key]
            else:
                expired_keys = [
                    k for k, v in self._cache.items() 
                    if current_time - v['timestamp'] > self._cache_ttl
                ]
                for k in expired_keys:
                    del self._cache[k]
    
    def _get_cached(self, key: str) -> Any:
        """Get cached value"""
        self._clean_cache(key)
        with self._cache_lock:
            return self._cache.get(key, {}).get('value')
    
    def _set_cached(self, key: str, value: Any):
        """Set cached value"""
        with self._cache_lock:
            self._cache[key] = {
                'value': value,
                'timestamp': time.time()
            }

class UserService(BaseService):
    """User management service integrated with DatabaseService"""
    
    def register_user(self, user_data: dict) -> Dict[str, Any]:
        try:
            # Validate input
            if not user_data.get("email") or not user_data.get("password"):
                return {"success": False, "message": "Email and password are required"}
            
            # Check existing user
            existing_user = self.db.get_user_by_email(user_data["email"])
            if existing_user:
                return {"success": False, "message": "User with this email already exists"}
            
            # Generate user ID
            user_id = f"usr_{int(time.time())}_{random.randint(1000, 9999)}"
            
            # Create user
            self.db.execute_query(
                """
                INSERT INTO users (user_id, email, password, full_name, phone, company_name, role, is_verified)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    user_data["email"],
                    user_data["password"],
                    user_data.get("full_name", ""),
                    user_data.get("phone", ""),
                    user_data.get("company_name", ""),
                    "user",
                    False
                )
            )
            
            # Log activity
            self.db.execute_query(
                """
                INSERT INTO user_activity (user_id, action_type, description)
                VALUES (%s, %s, %s)
                """,
                (self.db._get_user_db_id(user_id), "REGISTRATION", "User registered successfully")
            )
            
            return {
                "success": True,
                "user_id": user_id,
                "message": "Registration successful. Await subscription activation."
            }
            
        except Exception as e:
            logger.error(f"User registration failed: {e}")
            return {"success": False, "message": "Registration failed"}

    def activate_subscription(self, user_id: str, voucher_code: str) -> Dict[str, Any]:
        try:
            user = self.db.get_user_by_id(user_id)
            if not user:
                return {"success": False, "message": "User not found"}
            
            voucher = self.db.get_voucher(voucher_code)
            if not voucher:
                return {"success": False, "message": "Invalid voucher code"}
            
            if voucher.get('is_used'):
                return {"success": False, "message": "Voucher already used"}
            
            # Map voucher to package
            package_type = self._map_voucher_to_package(voucher)
            package = self._get_package_details(package_type)
            
            if not package:
                return {"success": False, "message": f"Invalid package: {package_type}"}
            
            # Calculate dates
            start_date = datetime.now()
            validity_days = voucher.get('validity_period', 30)
            end_date = start_date + timedelta(days=validity_days)
            
            # Activate subscription
            with self.db.get_connection() as conn:
                with conn.cursor() as cursor:
                    # Create subscription
                    cursor.execute(
                        """
                        INSERT INTO user_subscriptions 
                        (user_id, voucher_code, package_type, router_limit, start_date, end_date, is_active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            self.db._get_user_db_id(user_id),
                            voucher_code,
                            package_type,
                            package['router_limit'],
                            start_date,
                            end_date,
                            True
                        )
                    )
                    
                    # Mark voucher used
                    cursor.execute(
                        "UPDATE vouchers SET is_used = TRUE, activated_at = %s WHERE voucher_code = %s",
                        (start_date, voucher_code)
                    )
                    
                    # Activate user
                    cursor.execute(
                        "UPDATE users SET is_verified = TRUE, is_active = TRUE WHERE user_id = %s",
                        (user_id,)
                    )
                    
                    conn.commit()
            
            # Clear cache
            self._clean_cache(f"subscription_{user_id}")
            
            return {
                "success": True,
                "message": f"Subscription activated! {package_type} package active until {end_date.strftime('%Y-%m-%d')}",
                "package": package_type,
                "router_limit": package['router_limit'],
                "end_date": end_date
            }
            
        except Exception as e:
            logger.error(f"Subscription activation failed: {e}")
            return {"success": False, "message": "Activation failed"}

    def _map_voucher_to_package(self, voucher: Dict[str, Any]) -> str:
        profile_name = voucher.get('profile_name', 'Basic').lower()
        if 'professional' in profile_name:
            return 'Professional'
        elif 'premium' in profile_name:
            return 'Premium'
        else:
            return 'Basic'

    def _get_package_details(self, package_name: str) -> Optional[Dict[str, Any]]:
        cache_key = f"package_{package_name}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached
        
        package = self.db.execute_query(
            "SELECT * FROM subscription_packages WHERE package_name = %s AND is_active = TRUE",
            (package_name,),
            fetch_one=True
        )
        
        if package and package.get('features'):
            try:
                package['features'] = json.loads(package['features'])
            except (json.JSONDecodeError, TypeError):
                package['features'] = {}
        
        self._set_cached(cache_key, package)
        return package

    def get_user_subscription(self, user_id: str) -> Optional[Dict[str, Any]]:
        cache_key = f"subscription_{user_id}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached
        
        subscription = self.db.execute_query(
            """
            SELECT us.*, sp.features, sp.price,
                   EXTRACT(DAYS FROM (us.end_date - CURRENT_TIMESTAMP)) as days_remaining
            FROM user_subscriptions us
            JOIN subscription_packages sp ON us.package_type = sp.package_name
            WHERE us.user_id = %s AND us.is_active = TRUE AND us.end_date > CURRENT_TIMESTAMP
            ORDER BY us.end_date DESC
            LIMIT 1
            """,
            (self.db._get_user_db_id(user_id),),
            fetch_one=True
        )
        
        if subscription and subscription.get('features'):
            try:
                subscription['features'] = json.loads(subscription['features'])
            except (json.JSONDecodeError, TypeError):
                subscription['features'] = {}
        
        self._set_cached(cache_key, subscription)
        return subscription

    def validate_user_access(self, user_id: str) -> Dict[str, Any]:
        user = self.db.get_user_by_id(user_id)
        if not user or not user.get('is_active'):
            return {"has_access": False, "reason": "User account inactive"}
        
        subscription = self.get_user_subscription(user_id)
        if not subscription:
            return {"has_access": False, "reason": "No active subscription"}
        
        if subscription['end_date'] < datetime.now():
            return {"has_access": False, "reason": "Subscription expired"}
        
        return {
            "has_access": True,
            "reason": "Active subscription",
            "subscription": subscription,
            "days_remaining": subscription.get('days_remaining', 0)
        }

    def can_add_router(self, user_id: str) -> Dict[str, Any]:
        access_check = self.validate_user_access(user_id)
        if not access_check["has_access"]:
            return {"can_add": False, "reason": access_check["reason"]}
        
        router_count = self.db.execute_query(
            "SELECT COUNT(*) as count FROM enhanced_user_routers WHERE user_id = %s AND is_active = TRUE",
            (self.db._get_user_db_id(user_id),),
            fetch_one=True
        )
        
        current_count = router_count['count'] if router_count else 0
        subscription = access_check["subscription"]
        router_limit = subscription['router_limit']
        
        if current_count >= router_limit:
            return {
                "can_add": False,
                "reason": f"Router limit reached ({router_limit})",
                "current": current_count,
                "limit": router_limit
            }
        
        return {
            "can_add": True,
            "current": current_count,
            "limit": router_limit
        }

class RouterService(BaseService):
    """Router management service integrated with DatabaseService"""
    
    def __init__(self, db_service: DatabaseService):
        super().__init__(db_service)
        # MikroTik manager would be initialized here
        self.mikrotik_manager = None
    
    def add_user_router(self, user_id: str, router_data: dict) -> Dict[str, Any]:
        try:
            user_service = UserService(self.db)
            
            # Validate access
            access_check = user_service.validate_user_access(user_id)
            if not access_check["has_access"]:
                return {"success": False, "message": access_check["reason"]}
            
            # Check router limit
            can_add = user_service.can_add_router(user_id)
            if not can_add["can_add"]:
                return {"success": False, "message": can_add["reason"]}
            
            # Validate router data
            if not self._validate_router_data(router_data):
                return {"success": False, "message": "Invalid router data"}
            
            # Test connectivity (simplified - in real implementation, use MikroTikManager)
            connectivity_check = self._test_router_connectivity(router_data)
            if not connectivity_check["success"]:
                return connectivity_check
            
            # Generate WireGuard keys (simplified)
            wg_keys = self._generate_wireguard_keys()
            
            # Encrypt password
            encrypted_password = self.db._encrypt_password(router_data["password"])
            
            # Save router
            router_id = self.db.execute_query(
                """
                INSERT INTO enhanced_user_routers 
                (user_id, router_name, host, username, password, model, location, 
                 wireguard_public_key, wireguard_private_key, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    self.db._get_user_db_id(user_id),
                    router_data["router_name"],
                    router_data["host"],
                    router_data["username"],
                    encrypted_password,
                    connectivity_check.get("model", "Unknown"),
                    router_data.get("location", ""),
                    wg_keys["public_key"],
                    wg_keys["private_key"],
                    router_data.get("description", "")
                ),
                fetch_one=True
            )["id"]
            
            # Setup WireGuard (simplified - in real implementation, configure on router)
            setup_result = self._setup_wireguard(router_id, router_data, wg_keys)
            
            if not setup_result["success"]:
                # Rollback
                self.db.execute_query("DELETE FROM enhanced_user_routers WHERE id = %s", (router_id,))
                return setup_result
            
            # Log activity
            self.db.execute_query(
                """
                INSERT INTO user_activity (user_id, action_type, description)
                VALUES (%s, %s, %s)
                """,
                (self.db._get_user_db_id(user_id), "ROUTER_ADD", f"Added router: {router_data['router_name']}")
            )
            
            return {
                "success": True,
                "message": "Router added successfully",
                "router_id": router_id,
                "wireguard_public_key": wg_keys["public_key"]
            }
            
        except Exception as e:
            logger.error(f"Failed to add router: {e}")
            return {"success": False, "message": f"Failed to add router: {str(e)}"}

    def _validate_router_data(self, router_data: dict) -> bool:
        required = ["router_name", "host", "username", "password"]
        return all(field in router_data and router_data[field] for field in required)

    def _test_router_connectivity(self, router_data: dict) -> Dict[str, Any]:
        # Simplified connectivity test
        # In real implementation, use MikroTikManager to test connection
        return {
            "success": True,
            "model": "Simulated Router",
            "version": "6.0"
        }

    def _generate_wireguard_keys(self) -> Dict[str, str]:
        # Simplified key generation
        # In real implementation, use proper WireGuard key generation
        timestamp = str(int(time.time()))
        return {
            "public_key": f"pub_{timestamp}",
            "private_key": f"priv_{timestamp}"
        }

    def _setup_wireguard(self, router_id: int, router_data: dict, wg_keys: dict) -> Dict[str, Any]:
        # Simplified WireGuard setup
        # In real implementation, configure WireGuard on the actual router
        try:
            self.db.execute_query(
                """
                UPDATE enhanced_user_routers 
                SET wireguard_interface = %s, wireguard_port = %s, is_wireguard_setup = %s
                WHERE id = %s
                """,
                (f"wg-{router_id}", 51820, True, router_id)
            )
            return {"success": True, "interface_name": f"wg-{router_id}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_user_routers(self, user_id: str) -> List[Dict[str, Any]]:
        routers = self.db.execute_query(
            """
            SELECT id, router_name, host, model, location, is_online, last_seen, 
                   is_wireguard_setup, created_at
            FROM enhanced_user_routers 
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY created_at DESC
            """,
            (self.db._get_user_db_id(user_id),),
            fetch=True
        ) or []
        
        return routers

class SubscriptionService(BaseService):
    """Subscription management service"""
    
    def get_subscription_summary(self, user_id: str) -> Dict[str, Any]:
        user_service = UserService(self.db)
        access_check = user_service.validate_user_access(user_id)
        
        if not access_check["has_access"]:
            return access_check
        
        subscription = access_check["subscription"]
        
        # Get router count
        router_count = self.db.execute_query(
            "SELECT COUNT(*) as count FROM enhanced_user_routers WHERE user_id = %s AND is_active = TRUE",
            (self.db._get_user_db_id(user_id),),
            fetch_one=True
        )
        
        return {
            "has_access": True,
            "subscription": subscription,
            "router_usage": {
                "current": router_count['count'] if router_count else 0,
                "limit": subscription['router_limit'],
                "remaining": max(0, subscription['router_limit'] - (router_count['count'] if router_count else 0))
            },
            "time_remaining": {
                "days": subscription.get('days_remaining', 0),
                "end_date": subscription['end_date'],
                "renewal_date": subscription['end_date']
            }
        }

class MonitoringService(BaseService):
    """Monitoring service for subscriptions and routers"""
    
    def __init__(self, db_service: DatabaseService):
        super().__init__(db_service)
        self._last_checks = {}
    
    def check_subscription_expiry(self):
        """Check for expiring subscriptions"""
        try:
            # Subscriptions expiring in 3 days
            expiring_soon = self.db.execute_query(
                """
                SELECT us.*, u.email, u.full_name, u.phone
                FROM user_subscriptions us
                JOIN users u ON us.user_id = u.id
                WHERE us.is_active = TRUE 
                AND us.end_date BETWEEN CURRENT_TIMESTAMP AND CURRENT_TIMESTAMP + INTERVAL '3 days'
                AND u.is_active = TRUE
                """,
                fetch=True
            ) or []
            
            for subscription in expiring_soon:
                self._send_expiry_notification(subscription, 'expiring_soon')
            
            self._last_checks['subscriptions'] = datetime.now()
            
            return len(expiring_soon)
            
        except Exception as e:
            logger.error(f"Subscription expiry check failed: {e}")
            return 0
    
    def check_router_health(self):
        """Check router connectivity"""
        try:
            routers = self.db.execute_query(
                "SELECT id, router_name, host, user_id FROM enhanced_user_routers WHERE is_active = TRUE",
                fetch=True
            ) or []
            
            offline_count = 0
            for router in routers:
                # Simplified health check - in real implementation, test connectivity
                is_online = random.choice([True, False])  # Simulated
                
                self.db.execute_query(
                    "UPDATE enhanced_user_routers SET is_online = %s, last_seen = %s WHERE id = %s",
                    (is_online, datetime.now() if is_online else router.get('last_seen'), router['id'])
                )
                
                if not is_online:
                    offline_count += 1
            
            self._last_checks['routers'] = datetime.now()
            return offline_count
            
        except Exception as e:
            logger.error(f"Router health check failed: {e}")
            return 0
    
    def _send_expiry_notification(self, subscription: Dict[str, Any], notification_type: str):
        """Send notification (simplified - log instead of actual email/SMS)"""
        days_remaining = (subscription['end_date'] - datetime.now()).days
        
        if notification_type == 'expiring_soon':
            message = f"Subscription for {subscription['full_name']} expires in {days_remaining} days"
        else:
            message = f"Subscription for {subscription['full_name']} has expired"
        
        logger.info(f"NOTIFICATION: {message}")
    
    def run_health_checks(self) -> Dict[str, Any]:
        """Run all health checks"""
        return {
            'subscriptions_expiring': self.check_subscription_expiry(),
            'routers_offline': self.check_router_health(),
            'timestamp': datetime.now()
        }

# Service Coordinator for managing all services
class ServiceCoordinator:
    """Main coordinator for all services"""
    
    def __init__(self, config):
        self.config = config
        self.db = DatabaseService(config)
        self._running = False
        self._monitoring_thread = None
    
    def start_services(self):
        """Start all background services"""
        try:
            # Initialize database
            self.db.init_db()
            
            # Start monitoring service
            self._running = True
            self._monitoring_thread = threading.Thread(target=self._monitoring_worker, daemon=True)
            self._monitoring_thread.start()
            
            logger.info("All services started successfully")
            
        except Exception as e:
            logger.error(f"Failed to start services: {e}")
            raise
    
    def stop_services(self):
        """Stop all background services"""
        self._running = False
        if self._monitoring_thread:
            self._monitoring_thread.join(timeout=10)
        
        logger.info("All services stopped")
    
    def _monitoring_worker(self):
        """Background monitoring worker"""
        while self._running:
            try:
                # Run health checks every 5 minutes
                self.db.monitoring_service.run_health_checks()
                time.sleep(300)  # 5 minutes
            except Exception as e:
                logger.error(f"Monitoring worker error: {e}")
                time.sleep(60)
    
    def get_system_status(self) -> Dict[str, Any]:
        """Get comprehensive system status"""
        return {
            "database": self.db.get_stats(),
            "services_running": self._running,
            "timestamp": datetime.now()
        }

# Example usage
if __name__ == "__main__":
    # Example config - replace with your actual config
    class Config:
        DB_CONFIG = {
            'dbname': 'your_db',
            'user': 'your_user', 
            'password': 'your_password',
            'host': 'localhost',
            'port': 5432
        }
    
    config = Config()
    
    # Initialize and use the system
    coordinator = ServiceCoordinator(config)
    coordinator.start_services()
    
    # Example: User registration
    user_service = coordinator.db.user_service
    result = user_service.register_user({
        "email": "test@example.com",
        "password": "hashed_password",
        "full_name": "Test User"
    })
    
    print(f"Registration result: {result}")
    
    # Keep running
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        coordinator.stop_services()