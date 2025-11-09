# app.py
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from datetime import datetime, timedelta
import sqlite3
import routeros_api
import threading
import time
import logging
import json
import eventlet
eventlet.monkey_patch()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, 
     origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
     allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
     supports_credentials=True)

# Socket.IO configuration
socketio = SocketIO(app, 
                   cors_allowed_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
                   async_mode='eventlet',
                   logger=True,
                   engineio_logger=True)

DB_PATH = "vouchers.db"

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', 'http://localhost:5173')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response

# Handle preflight requests
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = jsonify({"status": "success"})
        response.headers.add('Access-Control-Allow-Origin', 'http://localhost:5173')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response


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

    def create_voucher(self, profile_name, code, password="", comment=""):
        connection, api = self.get_api()
        if not api:
            return False
        try:
            users = api.get_resource('/ip/hotspot/user')
            users.add(
                name=code, 
                password=password or code, 
                profile=profile_name, 
                comment=comment, 
                disabled='no'
            )
            logger.info(f"Voucher {code} created with profile {profile_name}")
            return True
        except Exception as e:
            logger.error(f"Error creating voucher: {e}")
            return False
        finally:
            if connection:
                connection.disconnect()

    def get_active_users(self):
        connection, api = self.get_api()
        if not api:
            return []
        try:
            active = api.get_resource('/ip/hotspot/active')
            return active.get()
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
                    'uptime': stats[0].get('uptime', '0s')
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

    def get_all_users(self):
        """Get all hotspot users"""
        connection, api = self.get_api()
        if not api:
            return []
        try:
            users = api.get_resource('/ip/hotspot/user')
            return users.get()
        except Exception as e:
            logger.error(f"Error fetching all users: {e}")
            return []
        finally:
            if connection:
                connection.disconnect()

# ----------------------------
# Database Functions
# ----------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_code TEXT UNIQUE,
            profile_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            activated_at TIMESTAMP,
            is_used BOOLEAN DEFAULT 0,
            customer_name TEXT,
            customer_contact TEXT,
            bytes_used INTEGER DEFAULT 0,
            session_time INTEGER DEFAULT 0,
            expiry_time TIMESTAMP,
            is_expired BOOLEAN DEFAULT 0,
            password_type TEXT DEFAULT 'blank',
            password TEXT DEFAULT ''
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS financial_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_code TEXT,
            amount INTEGER,
            transaction_type TEXT,
            transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS bandwidth_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            rate_limit TEXT,
            description TEXT,
            price INTEGER DEFAULT 0,
            time_limit TEXT,
            data_limit TEXT,
            validity_period INTEGER,
            uptime_limit TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS pricing_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rate_type TEXT UNIQUE,
            amount INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Initialize default pricing rates
    default_rates = [('day', 1000), ('week', 6000), ('month', 25000)]
    for rate_type, amount in default_rates:
        c.execute('''
            INSERT OR IGNORE INTO pricing_rates (rate_type, amount)
            VALUES (?, ?)
        ''', (rate_type, amount))
    
    conn.commit()
    conn.close()

def add_profile_to_db(profile):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Calculate price based on profile name or other criteria
    price = 0
    time_limit = "24h"
    data_limit = "Unlimited"
    validity_period = 24
    uptime_limit = "24h"
    
    if "1day" in profile.get('name', '').lower() or "daily" in profile.get('name', '').lower():
        price = 1000
    elif "1week" in profile.get('name', '').lower() or "weekly" in profile.get('name', '').lower():
        price = 6000
        time_limit = "7 days"
        validity_period = 168
        uptime_limit = "168h"
    elif "1month" in profile.get('name', '').lower() or "monthly" in profile.get('name', '').lower():
        price = 25000
        time_limit = "30 days"
        validity_period = 720
        uptime_limit = "720h"
    
    c.execute('''
        INSERT OR REPLACE INTO bandwidth_profiles 
        (name, rate_limit, description, price, time_limit, data_limit, validity_period, uptime_limit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        profile.get('name'), 
        profile.get('rate-limit', 'unlimited'), 
        f"Profile {profile.get('name')}",
        price,
        time_limit,
        data_limit,
        validity_period,
        uptime_limit
    ))
    conn.commit()
    conn.close()

def add_voucher_to_db(voucher_code, profile_name, password="", password_type="blank", customer_name="", customer_contact=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Calculate expiry time based on profile
    c.execute('SELECT validity_period FROM bandwidth_profiles WHERE name=?', (profile_name,))
    profile = c.fetchone()
    expiry_time = None
    
    if profile and profile[0]:
        expiry_hours = profile[0]
        expiry_time = datetime.now() + timedelta(hours=expiry_hours)
    
    c.execute('''
        INSERT INTO vouchers (voucher_code, profile_name, password, password_type, customer_name, customer_contact, expiry_time)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (voucher_code, profile_name, password, password_type, customer_name, customer_contact, expiry_time))
    conn.commit()
    conn.close()

def mark_voucher_used(voucher_code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE vouchers SET is_used=1, activated_at=CURRENT_TIMESTAMP WHERE voucher_code=?
    ''', (voucher_code,))
    conn.commit()
    conn.close()

def add_transaction(voucher_code, amount, transaction_type="SALE"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO financial_transactions (voucher_code, amount, transaction_type)
        VALUES (?, ?, ?)
    ''', (voucher_code, amount, transaction_type))
    conn.commit()
    conn.close()

def get_pricing_rates():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT rate_type, amount FROM pricing_rates')
    rows = c.fetchall()
    conn.close()
    
    rates = {}
    for row in rows:
        rates[row[0]] = row[1]
    return rates

def update_pricing_rates(rates):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    for rate_type, amount in rates.items():
        c.execute('''
            UPDATE pricing_rates SET amount=?, updated_at=CURRENT_TIMESTAMP 
            WHERE rate_type=?
        ''', (amount, rate_type))
    
    conn.commit()
    conn.close()

def get_expired_vouchers():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT voucher_code, profile_name, activated_at, expiry_time, is_expired
        FROM vouchers 
        WHERE expiry_time < CURRENT_TIMESTAMP OR is_expired = 1
        ORDER BY activated_at DESC
        LIMIT 50
    ''')
    rows = c.fetchall()
    conn.close()
    
    expired_vouchers = []
    for row in rows:
        expired_vouchers.append({
            'voucher_code': row[0],
            'profile_name': row[1],
            'activated_at': row[2],
            'expiry_time': row[3],
            'is_expired': bool(row[4])
        })
    
    return expired_vouchers

def get_all_users():
    """Get all users from database with enhanced information"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT v.voucher_code as username, v.profile_name, v.is_used as is_active, 
               v.activated_at as last_seen, v.password_type, v.is_expired,
               p.uptime_limit, v.created_at
        FROM vouchers v
        LEFT JOIN bandwidth_profiles p ON v.profile_name = p.name
        UNION
        SELECT u.name as username, u.profile as profile_name, 
               CASE WHEN a.user IS NOT NULL THEN 1 ELSE 0 END as is_active,
               NULL as last_seen, 'regular' as password_type, 0 as is_expired,
               u.uptime_limit, u.creation_date as created_at
        FROM (
            SELECT DISTINCT name, profile, uptime_limit, creation_date 
            FROM ip_hotspot_users_cache
        ) u
        LEFT JOIN (
            SELECT DISTINCT user FROM ip_hotspot_active_cache
        ) a ON u.name = a.user
    ''')
    rows = c.fetchall()
    conn.close()
    
    users = []
    for row in rows:
        users.append({
            'username': row[0],
            'profile_name': row[1],
            'is_active': bool(row[2]),
            'last_seen': row[3],
            'password_type': row[4],
            'is_expired': bool(row[5]),
            'uptime_limit': row[6],
            'is_voucher': row[0].startswith('VOUCHER') if row[0] else False,
            'created_at': row[7]
        })
    
    return users

def get_expired_users():
    """Get expired users"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT voucher_code as username, profile_name, activated_at as last_seen, 
               is_voucher = 1 as is_voucher
        FROM vouchers 
        WHERE is_expired = 1 OR expiry_time < CURRENT_TIMESTAMP
    ''')
    rows = c.fetchall()
    conn.close()
    
    expired_users = []
    for row in rows:
        expired_users.append({
            'username': row[0],
            'profile_name': row[1],
            'last_seen': row[2],
            'is_voucher': bool(row[3])
        })
    
    return expired_users

def get_user_details(username):
    """Get detailed user information"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Check if it's a voucher user
    c.execute('''
        SELECT v.voucher_code, v.profile_name, v.is_used, v.is_expired, 
               v.password_type, v.created_at, v.activated_at, v.customer_name,
               v.customer_contact, p.uptime_limit, p.price
        FROM vouchers v
        LEFT JOIN bandwidth_profiles p ON v.profile_name = p.name
        WHERE v.voucher_code = ?
    ''', (username,))
    
    row = c.fetchone()
    if row:
        user_info = {
            'username': row[0],
            'profile_name': row[1],
            'is_active': bool(row[2]),
            'is_expired': bool(row[3]),
            'password_type': row[4],
            'created_at': row[5],
            'activated_at': row[6],
            'customer_name': row[7],
            'customer_contact': row[8],
            'uptime_limit': row[9],
            'price': row[10],
            'is_voucher': True
        }
    else:
        # Regular user
        user_info = {
            'username': username,
            'profile_name': 'Unknown',
            'is_active': False,
            'is_expired': False,
            'password_type': 'regular',
            'is_voucher': False
        }
    
    conn.close()
    return user_info

def update_user_comment(username, comment):
    """Update user comment - for now we'll store in a separate table"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_comments (
            username TEXT PRIMARY KEY,
            comment TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        INSERT OR REPLACE INTO user_comments (username, comment)
        VALUES (?, ?)
    ''', (username, comment))
    
    conn.commit()
    conn.close()

def get_voucher_details(voucher_code):
    """Get detailed voucher information"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        SELECT v.voucher_code, v.profile_name, v.created_at, v.activated_at, 
               v.is_used, v.bytes_used, v.customer_name, v.customer_contact,
               v.password_type, v.password, p.price, p.uptime_limit
        FROM vouchers v
        LEFT JOIN bandwidth_profiles p ON v.profile_name = p.name
        WHERE v.voucher_code = ?
    ''', (voucher_code,))
    
    row = c.fetchone()
    conn.close()
    
    if not row:
        return None
    
    return {
        'code': row[0],
        'profile_name': row[1],
        'created_at': row[2],
        'activated_at': row[3],
        'is_used': bool(row[4]),
        'bytes_used': row[5],
        'customer_name': row[6],
        'customer_contact': row[7],
        'password_type': row[8],
        'password': row[9],
        'price': row[10],
        'uptime_limit': row[11]
    }

# ----------------------------
# Global MikroTik Manager
# ----------------------------
mikrotik_manager = MikroTikManager(host="192.168.88.1", username="admin", password="kaumelinen8")
init_db()

# ----------------------------
# Socket.IO Event Handlers
# ----------------------------
@socketio.on('connect')
def handle_connect():
    logger.info(f"Client connected: {request.sid}")
    emit('connection_status', {'status': 'connected', 'message': 'Successfully connected to server'})

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"Client disconnected: {request.sid}")

@socketio.on('request_update')
def handle_request_update(data):
    """Handle client requests for data updates"""
    try:
        update_type = data.get('type', 'all')
        
        if update_type in ['financial', 'all']:
            financial_stats = get_financial_stats()
            emit('financial_update', financial_stats)
        
        if update_type in ['users', 'all']:
            active_users = mikrotik_manager.get_active_users()
            emit('users_update', {'active_users': active_users})
        
        if update_type in ['system', 'all']:
            system_info = mikrotik_manager.get_system_info()
            emit('system_update', {'system_info': system_info})
            
    except Exception as e:
        logger.error(f"Error handling update request: {e}")
        emit('error', {'message': 'Failed to fetch updated data'})

# ----------------------------
# Background Monitoring Thread
# ----------------------------
def monitor_active_users():
    while True:
        try:
            active_users = mikrotik_manager.get_active_users()
            
            # Update active users cache
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('DELETE FROM ip_hotspot_active_cache')
            for user in active_users:
                c.execute('''
                    INSERT INTO ip_hotspot_active_cache (user, uptime, server, bytes_in, bytes_out)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    user.get('user'), 
                    user.get('uptime'), 
                    user.get('server'),
                    user.get('bytes-in', 0),
                    user.get('bytes-out', 0)
                ))
            conn.commit()
            conn.close()
            
            # Update voucher usage
            for user in active_users:
                username = user.get('user', '')
                if username.startswith("VOUCHER"):
                    mark_voucher_used(username)
                    usage = mikrotik_manager.get_user_usage(username)
                    if usage:
                        bytes_used = usage['bytes_in'] + usage['bytes_out']
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute('UPDATE vouchers SET bytes_used=? WHERE voucher_code=?',
                                  (bytes_used, username))
                        conn.commit()
                        conn.close()
                        price = calculate_price(usage['uptime'])
                        add_transaction(username, price)
            
            # Check for expired vouchers
            check_expired_vouchers()
            
            # Broadcast live data to all connected clients
            financial_stats = get_financial_stats()
            system_info = mikrotik_manager.get_system_info()
            
            socketio.emit('live_data', {
                'financial': financial_stats,
                'revenue': get_revenue_data().get('revenue_data', []),
                'active_users': active_users,
                'system_info': system_info
            })
            
        except Exception as e:
            logger.error(f"Error in monitor_active_users: {e}")
        time.sleep(30)

def check_expired_vouchers():
    """Mark vouchers as expired based on expiry time"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE vouchers 
        SET is_expired = 1 
        WHERE expiry_time < CURRENT_TIMESTAMP AND is_expired = 0
    ''')
    conn.commit()
    conn.close()

# Initialize cache tables
def init_cache_tables():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS ip_hotspot_active_cache (
            user TEXT PRIMARY KEY,
            uptime TEXT,
            server TEXT,
            bytes_in INTEGER,
            bytes_out INTEGER,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS ip_hotspot_users_cache (
            name TEXT PRIMARY KEY,
            profile TEXT,
            uptime_limit TEXT,
            creation_date TIMESTAMP,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_cache_tables()
threading.Thread(target=monitor_active_users, daemon=True).start()

# ----------------------------
# Pricing Helper
# ----------------------------
def calculate_price(uptime_str):
    if not uptime_str:
        return 0
    if 'd' in uptime_str:
        days = int(uptime_str.replace('d','').strip())
        if days >= 30:
            return 25000
        elif days >= 7:
            return 6000
        else:
            return 1000
    return 0

# ----------------------------
# API Endpoints - Aligned with Frontend
# ----------------------------
@app.route("/")
def root():
    return jsonify({"message": "MikroTik Voucher Tracker API", "version": "1.0.0"})

@app.route("/health")
def health_check():
    return jsonify({"status": "healthy"})

# Financial endpoints
@app.route("/financial/stats")
def get_financial_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(amount),0) FROM financial_transactions WHERE transaction_type='SALE'")
    total_revenue = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(amount),0) FROM financial_transactions WHERE transaction_type='SALE' AND DATE(transaction_date)=DATE('now')")
    daily_revenue = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM vouchers WHERE is_used=0")
    active_vouchers = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM vouchers WHERE is_used=1 AND DATE(activated_at)=DATE('now')")
    used_today = c.fetchone()[0]
    conn.close()
    return jsonify({
        'total_revenue': total_revenue,
        'daily_revenue': daily_revenue,
        'active_vouchers': active_vouchers,
        'used_vouchers_today': used_today
    })

@app.route("/financial/revenue-data")
def get_revenue_data():
    days = int(request.args.get("days", 30))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT DATE(transaction_date) as date, SUM(amount) as revenue, COUNT(*) as voucher_count
        FROM financial_transactions
        WHERE transaction_type='SALE' AND transaction_date >= DATE('now', ?)
        GROUP BY DATE(transaction_date)
        ORDER BY date
    ''', (f'-{days} days',))
    rows = c.fetchall()
    conn.close()
    data = [{'date': r[0], 'revenue': r[1], 'voucher_count': r[2]} for r in rows]
    return jsonify({"revenue_data": data})

@app.route("/financial/profile-stats")
def get_profile_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT v.profile_name,
               COUNT(*) as total_sold,
               SUM(ft.amount) as total_revenue,
               SUM(CASE WHEN v.is_used=1 THEN 1 ELSE 0 END) as used_count
        FROM vouchers v
        LEFT JOIN financial_transactions ft ON v.voucher_code = ft.voucher_code
        GROUP BY v.profile_name
    ''')
    rows = c.fetchall()
    conn.close()

    profile_stats = []
    for row in rows:
        profile_stats.append({
            'profile_name': row[0],
            'total_sold': row[1],
            'total_revenue': row[2] or 0,
            'used_count': row[3]
        })

    return jsonify({"profile_stats": profile_stats})

# Voucher endpoints
@app.route("/vouchers", methods=["POST"])
def generate_vouchers():
    data = request.json
    profile_name = data.get("profile_name")
    quantity = data.get("quantity", 1)
    customer_name = data.get("customer_name", "")
    customer_contact = data.get("customer_contact", "")
    password_type = data.get("password_type", "blank")

    if not profile_name:
        return jsonify({"error": "profile_name is required"}), 400

    # Verify profile exists
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM bandwidth_profiles WHERE name=?", (profile_name,))
    db_profile = c.fetchone()
    conn.close()

    if not db_profile:
        profiles = mikrotik_manager.get_profiles()
        profile = next((p for p in profiles if p.get("name") == profile_name), None)
        if not profile:
            return jsonify({"error": "Profile not found on MikroTik"}), 404
        add_profile_to_db(profile)

    vouchers = []
    total_price = 0
    
    for i in range(quantity):
        voucher_code = f"VOUCHER{datetime.now().strftime('%Y%m%d%H%M%S')}{i}"
        
        # Determine password based on type
        password = ""
        if password_type == "same":
            password = voucher_code
        elif password_type == "blank":
            password = ""
        else:
            password = f"pass{datetime.now().strftime('%H%M%S')}{i}"
        
        # Add to database
        add_voucher_to_db(voucher_code, profile_name, password, password_type, customer_name, customer_contact)
        
        # Create on MikroTik
        mikrotik_manager.create_voucher(profile_name, voucher_code, password, f"Customer: {customer_name}")
        
        vouchers.append({
            'code': voucher_code,
            'password': password,
            'profile_name': profile_name
        })
        
        # Calculate price for this voucher
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT price FROM bandwidth_profiles WHERE name=?", (profile_name,))
        price_row = c.fetchone()
        conn.close()
        
        if price_row:
            total_price += price_row[0]

    # Broadcast voucher generation event
    socketio.emit('vouchers_generated', {
        'vouchers': vouchers,
        'total_price': total_price,
        'message': f'Generated {len(vouchers)} vouchers'
    })

    return jsonify({
        "vouchers": vouchers, 
        "message": f"Generated {len(vouchers)} vouchers",
        "total_price": total_price
    })

@app.route("/vouchers/expired")
def get_expired_vouchers_endpoint():
    expired_vouchers = get_expired_vouchers()
    return jsonify({"expired_vouchers": expired_vouchers})

@app.route("/vouchers/<voucher_code>")
def get_voucher_info(voucher_code):
    voucher_details = get_voucher_details(voucher_code)
    if not voucher_details:
        return jsonify({"error": "Voucher not found"}), 404

    usage = mikrotik_manager.get_user_usage(voucher_code)
    voucher_details['current_usage'] = usage

    return jsonify(voucher_details)

# User endpoints
@app.route("/all-users")
def get_all_users_endpoint():
    users = get_all_users()
    return jsonify({"all_users": users})

@app.route("/active-users")
def get_active_users_endpoint():
    active_users = mikrotik_manager.get_active_users()
    return jsonify({"active_users": active_users})

@app.route("/users/expired")
def get_expired_users_endpoint():
    expired_users = get_expired_users()
    return jsonify({"expired_users": expired_users})

@app.route("/users/<username>")
def get_user_details_endpoint(username):
    user_details = get_user_details(username)
    if not user_details:
        return jsonify({"error": "User not found"}), 404

    # Add current usage information
    usage = mikrotik_manager.get_user_usage(username)
    user_details['current_usage'] = usage

    return jsonify(user_details)

@app.route("/users/<username>/comments", methods=["PUT"])
def update_user_comment_endpoint(username):
    data = request.json
    comment = data.get('comment', '')
    
    update_user_comment(username, comment)
    return jsonify({"success": True, "message": "Comment updated successfully"})

# Profile endpoints
@app.route("/profiles/enhanced")
def get_enhanced_profiles():
    profiles = mikrotik_manager.get_profiles()
    enhanced_profiles = []
    
    for profile in profiles:
        profile_name = profile.get('name', '')
        
        # Get additional info from database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT price, time_limit, data_limit, validity_period, uptime_limit FROM bandwidth_profiles WHERE name=?', (profile_name,))
        db_profile = c.fetchone()
        conn.close()
        
        enhanced_profile = {
            'name': profile_name,
            'rate_limit': profile.get('rate-limit', 'unlimited'),
            'price': db_profile[0] if db_profile else 1000,
            'time_limit': db_profile[1] if db_profile else "24h",
            'data_limit': db_profile[2] if db_profile else "Unlimited",
            'validity_period': db_profile[3] if db_profile else 24,
            'uptime_limit': db_profile[4] if db_profile else "24h"
        }
        enhanced_profiles.append(enhanced_profile)
    
    return jsonify({"profiles": enhanced_profiles})

# System endpoints
@app.route("/system/info")
def get_system_info_endpoint():
    system_info = mikrotik_manager.get_system_info()
    return jsonify({"system_info": system_info})

# Pricing endpoints
@app.route("/pricing/rates", methods=["GET", "PUT"])
def handle_pricing_rates():
    if request.method == "GET":
        rates = get_pricing_rates()
        return jsonify({"base_rates": rates})
    elif request.method == "PUT":
        data = request.json
        if 'rates' not in data:
            return jsonify({"error": "rates is required"}), 400
        
        update_pricing_rates(data['rates'])
        return jsonify({"message": "Pricing rates updated successfully"})

# ----------------------------
# Run Flask App with Socket.IO
# ----------------------------
if __name__ == "__main__":
    logger.info("Starting MikroTik Voucher Tracker API with Socket.IO support")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)