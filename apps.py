# app.py
from flask import Flask, jsonify, request, abort
from flask_cors import CORS
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
import routeros_api
import threading
import time
import logging
import json
import random
import string
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# CORS configuration from environment
cors_origins = os.getenv('CORS_ORIGINS', 'http://localhost:5173,http://localhost:3000').split(',')
CORS(app, origins=cors_origins)

# Database configuration from environment
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': os.getenv('DB_PORT', '5432'),
    'database': os.getenv('DB_NAME', 'voucher_system'),
    'user': os.getenv('DB_USER', 'postgres'),
    'password': os.getenv('DB_PASSWORD', '')
}

# MikroTik configuration from environment
MIKROTIK_CONFIG = {
    'host': os.getenv('MIKROTIK_HOST', '192.168.88.1'),
    'username': os.getenv('MIKROTIK_USERNAME', 'admin'),
    'password': os.getenv('MIKROTIK_PASSWORD', 'kaumelinen8')
}

db_lock = threading.Lock()

def get_connection():
    """Get PostgreSQL database connection"""
    return psycopg2.connect(**DB_CONFIG)

def execute_query(query, params=None, fetch=False, fetch_one=False):
    """Helper function to execute database queries"""
    conn = get_connection()
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

# ----------------------------
# Configuration
# ----------------------------
VOUCHER_CONFIG = {
    '1d': {'length': 5, 'chars': string.ascii_uppercase + string.digits},
    '7d': {'length': 6, 'chars': string.ascii_uppercase + string.digits},
    '30d': {'length': 7, 'chars': string.ascii_uppercase + string.digits}
}

# ----------------------------
# MikroTik Manager
# ----------------------------
class MikroTikManager:
    def __init__(self, host, username, password):
        self.host = host
        self.username = username
        self.password = password

    def get_api(self):
        """Return a fresh MikroTik API connection"""
        try:
            connection = routeros_api.RouterOsApiPool(
                self.host,
                username=self.username,
                password=self.password,
                plaintext_login=True,
            )
            api = connection.get_api()
            return connection, api
        except Exception as e:
            logger.error(f"MikroTik connection failed: {e}")
            return None, None

    def get_profiles(self):
        connection, api = self.get_api()
        if not api:
            return []
        try:
            profiles = api.get_resource('/ip/hotspot/user/profile')
            result = profiles.get()
            return result
        except Exception as e:
            logger.error(f"Error fetching profiles: {e}")
            return []
        finally:
            if connection:
                connection.disconnect()

    def create_voucher(self, profile_name, code, password=None, comment="", uptime_limit="1d"):
        connection, api = self.get_api()
        if not api:
            return False
        try:
            users = api.get_resource('/ip/hotspot/user')
            
            # If password is None, use blank password
            # If password is "same", use same as username
            final_password = ""
            if password == "same":
                final_password = code
            elif password is not None:
                final_password = password
            # else remains blank
            
            users.add(
                name=code, 
                password=final_password, 
                profile=profile_name, 
                comment=comment, 
                disabled='no',
                limit_uptime=uptime_limit
            )
            logger.info(f"Voucher {code} created with profile {profile_name} and uptime limit {uptime_limit}")
            return True
        except Exception as e:
            logger.error(f"Error creating voucher: {e}")
            return False
        finally:
            if connection:
                connection.disconnect()

    def get_all_users(self):
        """Get all hotspot users from MikroTik"""
        connection, api = self.get_api()
        if not api:
            return []
        try:
            users = api.get_resource('/ip/hotspot/user')
            result = users.get()
            return result
        except Exception as e:
            logger.error(f"Error fetching all users: {e}")
            return []
        finally:
            if connection:
                connection.disconnect()

    def get_active_users(self):
        connection, api = self.get_api()
        if not api:
            return []
        try:
            active = api.get_resource('/ip/hotspot/active')
            result = active.get()
            # Format the result to match frontend expectations
            formatted_result = []
            for user in result:
                formatted_user = {
                    'user': user.get('user', ''),
                    'profile': user.get('profile', ''),
                    'uptime': user.get('uptime', ''),
                    'bytes-in': user.get('bytes-in', '0'),
                    'bytes-out': user.get('bytes-out', '0'),
                    'server': user.get('server', '')
                }
                formatted_result.append(formatted_user)
            return formatted_result
        except Exception as e:
            logger.error(f"Error fetching active users: {e}")
            return []
        finally:
            if connection:
                connection.disconnect()

    def get_user_usage(self, username):
        connection, api = self.get_api()
        if not api:
            return None
        try:
            users = api.get_resource('/ip/hotspot/user')
            stats = users.get(name=username)
            if stats:
                return {
                    'bytes_in': int(stats[0].get('bytes-in', 0)),
                    'bytes_out': int(stats[0].get('bytes-out', 0)),
                    'uptime': stats[0].get('uptime', '0s'),
                    'limit_uptime': stats[0].get('limit-uptime', ''),
                    'disabled': stats[0].get('disabled', 'no'),
                    'comment': stats[0].get('comment', '')
                }
            return None
        except Exception as e:
            logger.error(f"Error fetching user usage: {e}")
            return None
        finally:
            if connection:
                connection.disconnect()

    def get_system_info(self):
        connection, api = self.get_api()
        if not api:
            return {}
        try:
            system_resource = api.get_resource('/system/resource')
            identity_resource = api.get_resource('/system/identity')
            
            system_info = system_resource.get()
            identity_info = identity_resource.get()
            
            if system_info and identity_info:
                return {
                    'router_name': identity_info[0].get('name', 'Unknown'),
                    'cpu_load': system_info[0].get('cpu-load', '0%'),
                    'uptime': system_info[0].get('uptime', '0s'),
                    'version': system_info[0].get('version', 'Unknown'),
                    'cpu_count': system_info[0].get('cpu-count', '1'),
                    'memory_usage': system_info[0].get('memory-usage', '0%')
                }
            return {}
        except Exception as e:
            logger.error(f"Error fetching system info: {e}")
            return {}
        finally:
            if connection:
                connection.disconnect()

    def remove_expired_user(self, username):
        """Remove expired user from MikroTik"""
        connection, api = self.get_api()
        if not api:
            return False
        try:
            users = api.get_resource('/ip/hotspot/user')
            # We need to find the user by name first to get its ID
            user_list = users.get(name=username)
            if user_list:
                user_id = user_list[0].get('id')
                users.remove(id=user_id)
                logger.info(f"Removed expired user: {username}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error removing expired user {username}: {e}")
            return False
        finally:
            if connection:
                connection.disconnect()

    def update_user_comment(self, username, comment):
        """Update user comment in MikroTik"""
        connection, api = self.get_api()
        if not api:
            return False
        try:
            users = api.get_resource('/ip/hotspot/user')
            user_list = users.get(name=username)
            if user_list:
                user_id = user_list[0].get('id')
                users.set(id=user_id, comment=comment)
                logger.info(f"Updated comment for user: {username}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error updating user comment {username}: {e}")
            return False
        finally:
            if connection:
                connection.disconnect()

# ----------------------------
# Database Functions
# ----------------------------
def init_db():
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
        execute_query(query)
    
    # Initialize default pricing rates
    default_rates = [('day', 1000), ('week', 6000), ('month', 25000)]
    for rate_type, amount in default_rates:
        execute_query(
            '''
            INSERT INTO pricing_rates (rate_type, amount)
            VALUES (%s, %s)
            ON CONFLICT (rate_type) DO NOTHING
            ''',
            (rate_type, amount)
        )
    
    # Initialize default profiles if they don't exist
    default_profiles = [
        ('1DAY', 'unlimited', 'Daily Profile', 1000, '24h', 'Unlimited', 24, '1d'),
        ('1WEEK', 'unlimited', 'Weekly Profile', 6000, '7 days', 'Unlimited', 168, '7d'),
        ('1MONTH', 'unlimited', 'Monthly Profile', 25000, '30 days', 'Unlimited', 720, '30d')
    ]
    
    for profile in default_profiles:
        execute_query(
            '''
            INSERT INTO bandwidth_profiles 
            (name, rate_limit, description, price, time_limit, data_limit, validity_period, uptime_limit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO NOTHING
            ''',
            profile
        )

def add_profile_to_db(profile):
    """Add profile to database"""
    # Calculate price and uptime limit based on profile name
    price = 0
    time_limit = "24h"
    data_limit = "Unlimited"
    validity_period = 24
    uptime_limit = "1d"
    
    profile_name = profile.get('name', '').lower()
    
    if "1day" in profile_name or "daily" in profile_name:
        price = 1000
        uptime_limit = "1d"
    elif "1week" in profile_name or "weekly" in profile_name:
        price = 6000
        time_limit = "7 days"
        validity_period = 168
        uptime_limit = "7d"
    elif "1month" in profile_name or "monthly" in profile_name:
        price = 25000
        time_limit = "30 days"
        validity_period = 720
        uptime_limit = "30d"
    
    execute_query(
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
            profile.get('name'), 
            profile.get('rate-limit', 'unlimited'), 
            f"Profile {profile.get('name')}",
            price,
            time_limit,
            data_limit,
            validity_period,
            uptime_limit
        )
    )

def generate_voucher_code(uptime_limit):
    """Generate voucher code based on uptime limit"""
    config = VOUCHER_CONFIG.get(uptime_limit, VOUCHER_CONFIG['1d'])
    length = config['length']
    chars = config['chars']
    
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        
        # Check if code already exists in database
        result = execute_query(
            'SELECT COUNT(*) as count FROM vouchers WHERE voucher_code=%s',
            (code,),
            fetch_one=True
        )
        exists = result['count'] > 0 if result else False
        
        if not exists:
            return code

def add_voucher_to_db(voucher_code, profile_name, customer_name="", customer_contact="", uptime_limit="1d", password_type="blank"):
    """Add voucher to database"""
    # Calculate expiry time based on profile
    result = execute_query(
        'SELECT validity_period FROM bandwidth_profiles WHERE name=%s',
        (profile_name,),
        fetch_one=True
    )
    expiry_time = None
    
    if result and result['validity_period']:
        expiry_hours = result['validity_period']
        expiry_time = datetime.now() + timedelta(hours=expiry_hours)
    
    execute_query(
        '''
        INSERT INTO vouchers (voucher_code, profile_name, customer_name, customer_contact, expiry_time, uptime_limit, password_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''',
        (voucher_code, profile_name, customer_name, customer_contact, expiry_time, uptime_limit, password_type)
    )

def mark_voucher_used(voucher_code):
    """Mark voucher as used"""
    execute_query(
        'UPDATE vouchers SET is_used=TRUE, activated_at=CURRENT_TIMESTAMP WHERE voucher_code=%s',
        (voucher_code,)
    )

def add_transaction(voucher_code, amount, transaction_type="SALE"):
    """Add financial transaction"""
    execute_query(
        'INSERT INTO financial_transactions (voucher_code, amount, transaction_type) VALUES (%s, %s, %s)',
        (voucher_code, amount, transaction_type)
    )

def get_pricing_rates():
    """Get pricing rates from database"""
    rows = execute_query('SELECT rate_type, amount FROM pricing_rates', fetch=True)
    
    rates = {}
    for row in rows:
        rates[row['rate_type']] = row['amount']
    return rates

def update_pricing_rates(rates):
    """Update pricing rates in database"""
    for rate_type, amount in rates.items():
        execute_query(
            'UPDATE pricing_rates SET amount=%s, updated_at=CURRENT_TIMESTAMP WHERE rate_type=%s',
            (amount, rate_type)
        )

def sync_all_users():
    """Sync all MikroTik users to database"""
    try:
        all_users = mikrotik_manager.get_all_users()
        
        for user in all_users:
            username = user.get('name', '')
            profile_name = user.get('profile', '')
            uptime_limit = user.get('limit-uptime', '')
            comment = user.get('comment', '')
            
            # Determine if it's a voucher and password type
            result = execute_query(
                'SELECT COUNT(*) as count FROM vouchers WHERE voucher_code=%s',
                (username,),
                fetch_one=True
            )
            is_voucher = result['count'] > 0 if result else False
            
            if is_voucher:
                result = execute_query(
                    'SELECT password_type FROM vouchers WHERE voucher_code=%s',
                    (username,),
                    fetch_one=True
                )
                password_type = result['password_type'] if result else 'blank'
            else:
                # For non-voucher users, try to determine password type from comment or other means
                if 'password=same' in comment.lower():
                    password_type = 'same'
                elif 'password=blank' in comment.lower() or 'blank password' in comment.lower():
                    password_type = 'blank'
                else:
                    password_type = 'custom'
            
            execute_query(
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
                (username, profile_name, uptime_limit, comment, password_type, is_voucher)
            )
    except Exception as e:
        logger.error(f"Error in sync_all_users: {e}")

def get_expired_vouchers():
    """Get vouchers that have reached their uptime limit"""
    rows = execute_query(
        '''
        SELECT voucher_code, profile_name, activated_at, uptime_limit, is_expired
        FROM vouchers 
        WHERE is_used = TRUE
        ORDER BY activated_at DESC
        LIMIT 50
        ''',
        fetch=True
    )
    
    expired_vouchers = []
    for row in rows:
        voucher_code = row['voucher_code']
        uptime_limit = row['uptime_limit']
        
        # Get current usage from MikroTik
        usage = mikrotik_manager.get_user_usage(voucher_code)
        current_uptime = usage.get('uptime', '0s') if usage else '0s'
        
        # Check if uptime limit is reached
        is_expired = check_uptime_limit(current_uptime, uptime_limit)
        
        expired_vouchers.append({
            'voucher_code': voucher_code,
            'profile_name': row['profile_name'],
            'activated_at': row['activated_at'],
            'uptime_limit': uptime_limit,
            'current_uptime': current_uptime,
            'is_expired': is_expired or bool(row['is_expired'])  # Also check database flag
        })
    
    return expired_vouchers

def check_uptime_limit(current_uptime, uptime_limit):
    """Check if current uptime exceeds the limit"""
    if not uptime_limit or not current_uptime:
        return False
    
    # Convert current uptime to seconds
    current_seconds = uptime_to_seconds(current_uptime)
    limit_seconds = uptime_limit_to_seconds(uptime_limit)
    
    return current_seconds >= limit_seconds if limit_seconds > 0 else False

def uptime_to_seconds(uptime_str):
    """Convert MikroTik uptime string to seconds"""
    if not uptime_str:
        return 0
    
    seconds = 0
    parts = uptime_str.split(' ')
    
    for part in parts:
        if 'd' in part:
            seconds += int(part.replace('d', '')) * 24 * 3600
        elif 'h' in part:
            seconds += int(part.replace('h', '')) * 3600
        elif 'm' in part:
            seconds += int(part.replace('m', '')) * 60
        elif 's' in part:
            seconds += int(part.replace('s', ''))
    
    return seconds

def uptime_limit_to_seconds(uptime_limit):
    """Convert uptime limit string to seconds"""
    if not uptime_limit:
        return 0
    
    # Handle common formats: "1d", "7d", "30d", "24:00:00", etc.
    if 'd' in uptime_limit:
        return int(uptime_limit.replace('d', '')) * 24 * 3600
    elif ':' in uptime_limit:
        # Format: "HH:MM:SS"
        parts = uptime_limit.split(':')
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    else:
        # Assume it's in seconds
        try:
            return int(uptime_limit)
        except:
            return 0
    
    return 0

# ----------------------------
# Pricing Helper
# ----------------------------
def calculate_price(profile_name):
    """Calculate price based on profile name"""
    if not profile_name:
        return 0
    
    profile_name_lower = profile_name.lower()
    
    if "1day" in profile_name_lower or "daily" in profile_name_lower:
        return 1000
    elif "1week" in profile_name_lower or "weekly" in profile_name_lower:
        return 6000
    elif "1month" in profile_name_lower or "monthly" in profile_name_lower:
        return 25000
    else:
        # Try to get price from database
        result = execute_query(
            'SELECT price FROM bandwidth_profiles WHERE name=%s',
            (profile_name,),
            fetch_one=True
        )
        return result['price'] if result else 1000

# ----------------------------
# Global MikroTik Manager
# ----------------------------
mikrotik_manager = MikroTikManager(**MIKROTIK_CONFIG)
init_db()

# ----------------------------
# Background Monitoring Thread
# ----------------------------
def monitor_all_users():
    while True:
        try:
            # Sync all users from MikroTik to database
            sync_all_users()
            
            # Monitor active users and update their status
            active_users = mikrotik_manager.get_active_users()
            active_usernames = [user.get('user', '') for user in active_users]
            
            if active_usernames:
                # Update active status for all users
                placeholders = ','.join(['%s'] * len(active_usernames))
                query = f'UPDATE all_users SET is_active=TRUE, last_seen=CURRENT_TIMESTAMP WHERE username IN ({placeholders})'
                execute_query(query, active_usernames)
                
                # Handle voucher-specific tracking
                for username in active_usernames:
                    result = execute_query(
                        'SELECT COUNT(*) as count FROM vouchers WHERE voucher_code=%s',
                        (username,),
                        fetch_one=True
                    )
                    is_voucher = result['count'] > 0 if result else False
                    
                    if is_voucher:
                        mark_voucher_used(username)
                        usage = mikrotik_manager.get_user_usage(username)
                        if usage:
                            bytes_used = usage['bytes_in'] + usage['bytes_out']
                            execute_query(
                                'UPDATE vouchers SET bytes_used=%s WHERE voucher_code=%s',
                                (bytes_used, username)
                            )
                            
                            # Get profile name to calculate price
                            result = execute_query(
                                'SELECT profile_name FROM vouchers WHERE voucher_code=%s',
                                (username,),
                                fetch_one=True
                            )
                            if result:
                                profile_name = result['profile_name']
                                price = calculate_price(profile_name)
                                add_transaction(username, price)
            
            # Mark inactive users
            if active_usernames:
                placeholders = ','.join(['%s'] * len(active_usernames))
                query = f'UPDATE all_users SET is_active=FALSE WHERE username NOT IN ({placeholders})'
                execute_query(query, active_usernames)
            else:
                execute_query('UPDATE all_users SET is_active=FALSE')
            
            # Check for expired users (both vouchers and regular users)
            check_expired_users()
            
        except Exception as e:
            logger.error(f"Error in monitor_all_users: {e}")
        time.sleep(30)

def check_expired_users():
    """Check and mark expired users based on uptime limits for ALL users"""
    active_users = execute_query(
        'SELECT username, uptime_limit FROM all_users WHERE is_active=TRUE',
        fetch=True
    )
    
    for user in active_users:
        username = user['username']
        uptime_limit = user['uptime_limit']
        usage = mikrotik_manager.get_user_usage(username)
        if usage:
            current_uptime = usage.get('uptime', '0s')
            
            # Check if uptime limit is reached
            if check_uptime_limit(current_uptime, uptime_limit):
                logger.info(f"User {username} has reached uptime limit")
                
                # Mark as expired in database
                execute_query(
                    'UPDATE all_users SET is_expired=TRUE, is_active=FALSE WHERE username=%s',
                    (username,)
                )
                
                # If it's a voucher, mark it as expired
                result = execute_query(
                    'SELECT COUNT(*) as count FROM vouchers WHERE voucher_code=%s',
                    (username,),
                    fetch_one=True
                )
                is_voucher = result['count'] > 0 if result else False
                if is_voucher:
                    execute_query(
                        'UPDATE vouchers SET is_expired=TRUE WHERE voucher_code=%s',
                        (username,)
                    )

# Start background thread
monitor_thread = threading.Thread(target=monitor_all_users, daemon=True)
monitor_thread.start()

# ----------------------------
# API Endpoints
# ----------------------------
@app.route("/")
def root():
    return jsonify({"message": "MikroTik Voucher Tracker API"})

@app.route("/profiles")
def get_profiles():
    try:
        profiles = mikrotik_manager.get_profiles()
        
        # If no profiles from MikroTik, use default profiles from database
        if not profiles:
            db_profiles = execute_query(
                'SELECT name, rate_limit, price, time_limit, data_limit, uptime_limit FROM bandwidth_profiles',
                fetch=True
            )
            
            enhanced_profiles = []
            for profile in db_profiles:
                enhanced_profiles.append({
                    'name': profile['name'],
                    'rate-limit': profile['rate_limit'],
                    'price': profile['price'],
                    'time_limit': profile['time_limit'],
                    'data_limit': profile['data_limit'],
                    'uptime_limit': profile['uptime_limit']
                })
            return jsonify({"profiles": enhanced_profiles})
        
        # Enhance profiles with pricing information
        enhanced_profiles = []
        for profile in profiles:
            profile_name = profile.get('name', '')
            
            # Get pricing from database
            db_profile = execute_query(
                'SELECT price, time_limit, data_limit, validity_period, uptime_limit FROM bandwidth_profiles WHERE name=%s',
                (profile_name,),
                fetch_one=True
            )
            
            if db_profile:
                profile['price'] = db_profile['price']
                profile['time_limit'] = db_profile['time_limit']
                profile['data_limit'] = db_profile['data_limit']
                profile['validity_period'] = db_profile['validity_period']
                profile['uptime_limit'] = db_profile['uptime_limit']
            else:
                # Default values if not in database
                profile['price'] = calculate_price(profile_name)
                profile['time_limit'] = "24h"
                profile['data_limit'] = "Unlimited"
                profile['validity_period'] = 24
                profile['uptime_limit'] = "1d"
            
            enhanced_profiles.append(profile)
        
        return jsonify({"profiles": enhanced_profiles})
    except Exception as e:
        logger.error(f"Error in get_profiles: {e}")
        return jsonify({"profiles": []})

@app.route("/profiles/add", methods=["POST"])
def add_profile():
    data = request.json
    profile_name = data.get("profile_name")
    if not profile_name:
        return jsonify({"error": "profile_name is required"}), 400

    profiles = mikrotik_manager.get_profiles()
    profile = next((p for p in profiles if p.get("name") == profile_name), None)
    if not profile:
        return jsonify({"error": "Profile not found on MikroTik"}), 404

    add_profile_to_db(profile)
    return jsonify({"message": f"Profile '{profile_name}' added to database successfully"})

@app.route("/vouchers/generate", methods=["POST"])
def generate_vouchers():
    data = request.json
    profile_name = data.get("profile_name")
    quantity = data.get("quantity", 1)
    customer_name = data.get("customer_name", "")
    customer_contact = data.get("customer_contact", "")
    password_type = data.get("password_type", "blank")  # "blank", "same", or "custom"

    if not profile_name:
        return jsonify({"error": "profile_name is required"}), 400

    db_profile = execute_query(
        "SELECT uptime_limit, price FROM bandwidth_profiles WHERE name=%s",
        (profile_name,),
        fetch_one=True
    )

    uptime_limit = "1d"  # default
    price_per_voucher = 1000
    
    if db_profile:
        uptime_limit = db_profile['uptime_limit'] or "1d"
        price_per_voucher = db_profile['price'] or 1000
    else:
        # Try to get profile from MikroTik
        profiles = mikrotik_manager.get_profiles()
        profile = next((p for p in profiles if p.get("name") == profile_name), None)
        if not profile:
            return jsonify({"error": "Profile not found"}), 404
        add_profile_to_db(profile)
        price_per_voucher = calculate_price(profile_name)

    vouchers = []
    total_price = 0
    
    for i in range(quantity):
        voucher_code = generate_voucher_code(uptime_limit)
        
        # Determine password for MikroTik
        password = None
        if password_type == "same":
            password = "same"  # Special flag for MikroTik manager
        elif password_type == "custom":
            # Generate a random password
            password = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        
        add_voucher_to_db(voucher_code, profile_name, customer_name, customer_contact, uptime_limit, password_type)
        
        # Create comment for the user
        comment = f"Customer: {customer_name} | Contact: {customer_contact} | Type: voucher"
        if password_type != "blank":
            comment += f" | Password: {password_type}"
        
        success = mikrotik_manager.create_voucher(profile_name, voucher_code, password, comment, uptime_limit)
        
        if success:
            password_display = ""
            if password_type == "custom":
                password_display = password
            elif password_type == "same":
                password_display = "same as username"
            else:
                password_display = "blank"
                
            vouchers.append({
                'code': voucher_code,
                'password': password_display
            })
            total_price += price_per_voucher
        else:
            logger.error(f"Failed to create voucher {voucher_code} on MikroTik")

    return jsonify({
        "vouchers": vouchers, 
        "message": f"Generated {len(vouchers)} vouchers",
        "total_price": total_price
    })

@app.route("/vouchers/<voucher_code>")
def get_voucher_info(voucher_code):
    result = execute_query(
        '''
        SELECT voucher_code, profile_name, created_at, activated_at, is_used, bytes_used, session_time,
               customer_name, customer_contact, uptime_limit, password_type
        FROM vouchers WHERE voucher_code=%s
        ''',
        (voucher_code,),
        fetch_one=True
    )
    if not result:
        abort(404, description="Voucher not found")

    usage = mikrotik_manager.get_user_usage(voucher_code)
    price = calculate_price(result['profile_name'])

    return jsonify({
        'code': result['voucher_code'],
        'profile_name': result['profile_name'],
        'created_at': result['created_at'],
        'activated_at': result['activated_at'],
        'is_used': bool(result['is_used']),
        'bytes_used': result['bytes_used'],
        'session_time': result['session_time'],
        'customer_name': result['customer_name'],
        'customer_contact': result['customer_contact'],
        'uptime_limit': result['uptime_limit'],
        'password_type': result['password_type'],
        'current_usage': usage,
        'price': price
    })

@app.route("/financial/stats")
def get_financial_stats():
    result = execute_query(
        "SELECT COALESCE(SUM(amount),0) as total_revenue FROM financial_transactions WHERE transaction_type='SALE'",
        fetch_one=True
    )
    total_revenue = result['total_revenue'] if result else 0
    
    result = execute_query(
        "SELECT COALESCE(SUM(amount),0) as daily_revenue FROM financial_transactions WHERE transaction_type='SALE' AND DATE(transaction_date)=CURRENT_DATE",
        fetch_one=True
    )
    daily_revenue = result['daily_revenue'] if result else 0
    
    result = execute_query(
        "SELECT COUNT(*) as active_vouchers FROM vouchers WHERE is_used=FALSE",
        fetch_one=True
    )
    active_vouchers = result['active_vouchers'] if result else 0
    
    result = execute_query(
        "SELECT COUNT(*) as used_today FROM vouchers WHERE is_used=TRUE AND DATE(activated_at)=CURRENT_DATE",
        fetch_one=True
    )
    used_today = result['used_today'] if result else 0
    
    return jsonify({
        'total_revenue': total_revenue,
        'daily_revenue': daily_revenue,
        'active_vouchers': active_vouchers,
        'used_vouchers_today': used_today
    })

@app.route("/financial/revenue-data")
def get_revenue_data():
    days = int(request.args.get("days", 30))
    
    rows = execute_query(
        '''
        SELECT DATE(transaction_date) as date, SUM(amount) as revenue, COUNT(*) as voucher_count
        FROM financial_transactions
        WHERE transaction_type='SALE' AND transaction_date >= CURRENT_DATE - INTERVAL '%s days'
        GROUP BY DATE(transaction_date)
        ORDER BY date
        ''',
        (days,),
        fetch=True
    )
    
    # Generate data for all days, even if no transactions
    data = []
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        revenue = 0
        voucher_count = 0
        
        for row in rows:
            if row['date'].strftime('%Y-%m-%d') == date:
                revenue = row['revenue']
                voucher_count = row['voucher_count']
                break
                
        data.append({'date': date, 'revenue': revenue, 'voucher_count': voucher_count})
    
    data.reverse()  # Sort chronologically
    return jsonify({"revenue_data": data})

@app.route("/active-users")
def get_active_users():
    active_users = mikrotik_manager.get_active_users()
    return jsonify({"active_users": active_users})

@app.route("/all-users")
def get_all_users():
    """Get all users from database (synced from MikroTik)"""
    rows = execute_query(
        '''
        SELECT username, profile_name, is_active, last_seen, uptime_limit, comment, password_type, is_voucher
        FROM all_users 
        ORDER BY last_seen DESC
        ''',
        fetch=True
    )
    
    users = []
    for row in rows:
        usage = mikrotik_manager.get_user_usage(row['username'])
        users.append({
            'username': row['username'],
            'profile_name': row['profile_name'],
            'is_active': bool(row['is_active']),
            'last_seen': row['last_seen'],
            'uptime_limit': row['uptime_limit'],
            'comment': row['comment'],
            'password_type': row['password_type'],
            'is_voucher': bool(row['is_voucher']),
            'current_uptime': usage.get('uptime', '0s') if usage else '0s',
            'bytes_used': (usage.get('bytes_in', 0) + usage.get('bytes_out', 0)) if usage else 0
        })
    
    return jsonify({"all_users": users})

@app.route("/financial/profile-stats")
def get_profile_stats():
    rows = execute_query(
        '''
        SELECT v.profile_name,
               COUNT(*) as total_sold,
               SUM(CASE WHEN ft.amount IS NOT NULL THEN ft.amount ELSE 0 END) as total_revenue,
               SUM(CASE WHEN v.is_used=TRUE THEN 1 ELSE 0 END) as used_count
        FROM vouchers v
        LEFT JOIN financial_transactions ft ON v.voucher_code = ft.voucher_code
        GROUP BY v.profile_name
        ''',
        fetch=True
    )

    profile_stats = []
    for row in rows:
        profile_stats.append({
            'profile_name': row['profile_name'],
            'total_sold': row['total_sold'],
            'total_revenue': row['total_revenue'] or 0,
            'used_count': row['used_count']
        })

    return jsonify({"profile_stats": profile_stats})

@app.route("/system/info")
def get_system_info():
    """Get MikroTik system information"""
    system_info = mikrotik_manager.get_system_info()
    return jsonify({"system_info": system_info})

@app.route("/vouchers/expired")
def get_expired_vouchers_endpoint():
    """Get list of expired vouchers based on uptime limits"""
    try:
        expired_vouchers = get_expired_vouchers()
        return jsonify({"expired_vouchers": expired_vouchers})
    except Exception as e:
        logger.error(f"Error in get_expired_vouchers_endpoint: {e}")
        return jsonify({"expired_vouchers": [], "error": str(e)}), 500

@app.route("/pricing/rates", methods=["GET", "PUT"])
def handle_pricing_rates():
    """Get or update pricing rates"""
    if request.method == "GET":
        rates = get_pricing_rates()
        return jsonify({"base_rates": rates})
    elif request.method == "PUT":
        data = request.json
        if 'base_rates' not in data:
            return jsonify({"error": "base_rates is required"}), 400
        
        update_pricing_rates(data['base_rates'])
        return jsonify({"message": "Pricing rates updated successfully"})

@app.route("/profiles/enhanced")
def get_enhanced_profiles():
    """Get profiles with enhanced information for the frontend"""
    profiles = mikrotik_manager.get_profiles()
    enhanced_profiles = []
    
    for profile in profiles:
        profile_name = profile.get('name', '')
        
        # Get additional info from database
        db_profile = execute_query(
            'SELECT price, time_limit, data_limit, validity_period, uptime_limit FROM bandwidth_profiles WHERE name=%s',
            (profile_name,),
            fetch_one=True
        )
        
        enhanced_profile = {
            'name': profile_name,
            'rate_limit': profile.get('rate-limit', 'unlimited'),
            'price': db_profile['price'] if db_profile else calculate_price(profile_name),
            'time_limit': db_profile['time_limit'] if db_profile else "24h",
            'data_limit': db_profile['data_limit'] if db_profile else "Unlimited",
            'validity_period': db_profile['validity_period'] if db_profile else 24,
            'uptime_limit': db_profile['uptime_limit'] if db_profile else "1d"
        }
        enhanced_profiles.append(enhanced_profile)
    
    return jsonify({"profiles": enhanced_profiles})

@app.route("/users/expired")
def get_expired_users():
    """Get all expired users (both vouchers and regular users)"""
    rows = execute_query(
        '''
        SELECT username, profile_name, last_seen, uptime_limit, comment, is_voucher
        FROM all_users 
        WHERE is_expired = TRUE
        ORDER BY last_seen DESC
        ''',
        fetch=True
    )
    
    expired_users = []
    for row in rows:
        usage = mikrotik_manager.get_user_usage(row['username'])
        expired_users.append({
            'username': row['username'],
            'profile_name': row['profile_name'],
            'last_seen': row['last_seen'],
            'uptime_limit': row['uptime_limit'],
            'comment': row['comment'],
            'is_voucher': bool(row['is_voucher']),
            'current_uptime': usage.get('uptime', '0s') if usage else '0s'
        })
    
    return jsonify({"expired_users": expired_users})

@app.route("/users/<username>")
def get_user_info(username):
    """Get detailed information for any user"""
    result = execute_query(
        '''
        SELECT username, profile_name, is_active, last_seen, uptime_limit, comment, password_type, is_voucher
        FROM all_users WHERE username=%s
        ''',
        (username,),
        fetch_one=True
    )
    
    if not result:
        abort(404, description="User not found")
    
    usage = mikrotik_manager.get_user_usage(username)
    is_expired = check_uptime_limit(usage.get('uptime', '0s'), result['uptime_limit']) if usage else False
    
    return jsonify({
        'username': result['username'],
        'profile_name': result['profile_name'],
        'is_active': bool(result['is_active']),
        'last_seen': result['last_seen'],
        'uptime_limit': result['uptime_limit'],
        'comment': result['comment'],
        'password_type': result['password_type'],
        'is_voucher': bool(result['is_voucher']),
        'current_usage': usage,
        'is_expired': is_expired
    })

@app.route("/users/<username>/comment", methods=["PUT"])
def update_user_comment(username):
    """Update user comment in both MikroTik and database"""
    data = request.json
    comment = data.get("comment", "")
    
    if not comment:
        return jsonify({"error": "comment is required"}), 400
    
    # Update in MikroTik
    success = mikrotik_manager.update_user_comment(username, comment)
    if not success:
        return jsonify({"error": "Failed to update comment in MikroTik"}), 500
    
    # Update in database
    execute_query(
        'UPDATE all_users SET comment=%s WHERE username=%s',
        (comment, username)
    )
    
    return jsonify({"message": "Comment updated successfully"})

# ----------------------------
# Run Flask App
# ----------------------------
if __name__ == "__main__":
    host = os.getenv('FLASK_HOST', '0.0.0.0')
    port = int(os.getenv('FLASK_PORT', 8000))
    debug = os.getenv('FLASK_DEBUG', 'True').lower() == 'true'
    
    app.run(host=host, port=port, debug=debug)