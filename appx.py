# app.py
from flask import Flask, jsonify, request, abort
from flask_cors import CORS
from datetime import datetime, timedelta
import sqlite3
import routeros_api
import threading
import time
import logging
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173"])

DB_PATH = "vouchers.db"

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

    def create_voucher(self, profile_name, code, comment=""):
        connection, api = self.get_api()
        if not api:
            return False
        try:
            users = api.get_resource('/ip/hotspot/user')
            users.add(name=code, password=code, profile=profile_name, comment=comment, disabled='no')
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
            is_expired BOOLEAN DEFAULT 0
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
    
    if "1day" in profile.get('name', '').lower():
        price = 1000
    elif "1week" in profile.get('name', '').lower():
        price = 6000
        time_limit = "7 days"
        validity_period = 168
    elif "1month" in profile.get('name', '').lower():
        price = 25000
        time_limit = "30 days"
        validity_period = 720
    
    c.execute('''
        INSERT OR REPLACE INTO bandwidth_profiles 
        (name, rate_limit, description, price, time_limit, data_limit, validity_period)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        profile.get('name'), 
        profile.get('rate-limit', 'unlimited'), 
        f"Profile {profile.get('name')}",
        price,
        time_limit,
        data_limit,
        validity_period
    ))
    conn.commit()
    conn.close()

def add_voucher_to_db(voucher_code, profile_name, customer_name="", customer_contact=""):
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
        INSERT INTO vouchers (voucher_code, profile_name, customer_name, customer_contact, expiry_time)
        VALUES (?, ?, ?, ?, ?)
    ''', (voucher_code, profile_name, customer_name, customer_contact, expiry_time))
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
# Global MikroTik Manager
# ----------------------------
mikrotik_manager = MikroTikManager(host="192.168.88.1", username="admin", password="kaumelinen8")
init_db()

# ----------------------------
# Background Monitoring Thread
# ----------------------------
def monitor_active_users():
    while True:
        try:
            active_users = mikrotik_manager.get_active_users()
            for user in active_users:
                username = user.get('user', '')
                if username.startswith("VOUCHER"):
                    mark_voucher_used(username)
                    usage = mikrotik_manager.get_user_usage(username)
                    if usage:
                        bytes_used = usage['bytes_in'] + usage['bytes_out']
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute('UPDATE vouchers SET bytes_used=?, session_time=? WHERE voucher_code=?',
                                  (bytes_used, 0, username))
                        conn.commit()
                        conn.close()
                        price = calculate_price(usage['uptime'])
                        add_transaction(username, price)
                        
            # Check for expired vouchers
            check_expired_vouchers()
            
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

threading.Thread(target=monitor_active_users, daemon=True).start()

# ----------------------------
# API Endpoints
# ----------------------------
@app.route("/")
def root():
    return jsonify({"message": "MikroTik Voucher Tracker API"})

@app.route("/profiles")
def get_profiles():
    profiles = mikrotik_manager.get_profiles()
    
    # Enhance profiles with pricing information
    enhanced_profiles = []
    for profile in profiles:
        profile_name = profile.get('name', '')
        
        # Get pricing from database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT price, time_limit, data_limit, validity_period FROM bandwidth_profiles WHERE name=?', (profile_name,))
        db_profile = c.fetchone()
        conn.close()
        
        if db_profile:
            profile['price'] = db_profile[0]
            profile['time_limit'] = db_profile[1]
            profile['data_limit'] = db_profile[2]
            profile['validity_period'] = db_profile[3]
        else:
            # Default values if not in database
            profile['price'] = 1000
            profile['time_limit'] = "24h"
            profile['data_limit'] = "Unlimited"
            profile['validity_period'] = 24
        
        enhanced_profiles.append(profile)
    
    return jsonify({"profiles": enhanced_profiles})

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

    if not profile_name:
        return jsonify({"error": "profile_name is required"}), 400

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
        add_voucher_to_db(voucher_code, profile_name, customer_name, customer_contact)
        mikrotik_manager.create_voucher(profile_name, voucher_code, f"Customer: {customer_name}")
        vouchers.append(voucher_code)
        
        # Calculate price for this voucher
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT price FROM bandwidth_profiles WHERE name=?", (profile_name,))
        price_row = c.fetchone()
        conn.close()
        
        if price_row:
            total_price += price_row[0]

    return jsonify({
        "vouchers": vouchers, 
        "message": f"Generated {len(vouchers)} vouchers",
        "total_price": total_price
    })

@app.route("/vouchers/<voucher_code>")
def get_voucher_info(voucher_code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT voucher_code, profile_name, created_at, activated_at, is_used, bytes_used, session_time,
               customer_name, customer_contact
        FROM vouchers WHERE voucher_code=?
    ''', (voucher_code,))
    row = c.fetchone()
    conn.close()
    if not row:
        abort(404, description="Voucher not found")

    usage = mikrotik_manager.get_user_usage(voucher_code)
    price = calculate_price(usage['uptime'] if usage else None)

    return jsonify({
        'code': row[0],
        'profile_name': row[1],
        'created_at': row[2],
        'activated_at': row[3],
        'is_used': bool(row[4]),
        'bytes_used': row[5],
        'session_time': row[6],
        'customer_name': row[7],
        'customer_contact': row[8],
        'current_usage': usage,
        'price': price
    })

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

@app.route("/active-users")
def get_active_users():
    active_users = mikrotik_manager.get_active_users()
    return jsonify({"active_users": active_users})

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
        LEFT JOIN financial_transactions ft
        ON v.voucher_code = ft.voucher_code
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

# ----------------------------
# New API Endpoints for React Frontend
# ----------------------------

@app.route("/system/info")
def get_system_info():
    """Get MikroTik system information"""
    system_info = mikrotik_manager.get_system_info()
    return jsonify({"system_info": system_info})

@app.route("/vouchers/expired")
def get_expired_vouchers_endpoint():
    """Get list of expired vouchers"""
    expired_vouchers = get_expired_vouchers()
    return jsonify({"expired_vouchers": expired_vouchers})

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
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT price, time_limit, data_limit, validity_period FROM bandwidth_profiles WHERE name=?', (profile_name,))
        db_profile = c.fetchone()
        conn.close()
        
        enhanced_profile = {
            'name': profile_name,
            'rate_limit': profile.get('rate-limit', 'unlimited'),
            'price': db_profile[0] if db_profile else 1000,
            'time_limit': db_profile[1] if db_profile else "24h",
            'data_limit': db_profile[2] if db_profile else "Unlimited",
            'validity_period': db_profile[3] if db_profile else 24
        }
        enhanced_profiles.append(enhanced_profile)
    
    return jsonify({"profiles": enhanced_profiles})

# ----------------------------
# Run Flask App
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)