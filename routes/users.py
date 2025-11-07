from flask import Blueprint, request, jsonify, abort

from utils.helpers import check_uptime_limit

users_bp = Blueprint('users', __name__)

def init_users_routes(app, database_service, mikrotik_manager):
    """Initialize user routes"""
    
    @users_bp.route("/active-users")
    def get_active_users():
        active_users = mikrotik_manager.get_active_users()
        return jsonify({"active_users": active_users})

    @users_bp.route("/all-users")
    def get_all_users():
        """Get all users from database (synced from MikroTik)"""
        rows = database_service.get_all_users()
        
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

    @users_bp.route("/users/expired")
    def get_expired_users():
        """Get all expired users (both vouchers and regular users)"""
        rows = database_service.execute_query(
            '''
            SELECT username, profile_name, last_seen, uptime_limit, comment, is_voucher
            FROM all_users 
            WHERE is_expired = TRUE
            ORDER BY last_seen DESC
            ''',
            fetch=True
        ) or []
        
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

    @users_bp.route("/users/<username>")
    def get_user_info(username):
        """Get detailed information for any user"""
        result = database_service.execute_query(
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

    @users_bp.route("/users/<username>/comment", methods=["PUT"])
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
        database_service.execute_query(
            'UPDATE all_users SET comment=%s WHERE username=%s',
            (comment, username)
        )
        
        return jsonify({"message": "Comment updated successfully"})

    # Register blueprint
    app.register_blueprint(users_bp)