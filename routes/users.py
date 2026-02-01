from flask import Blueprint, request, jsonify, abort
from utils.helpers import check_uptime_limit
import math
import os
from flask import request, jsonify
from werkzeug.security import check_password_hash, generate_password_hash

users_bp = Blueprint("users", __name__)


def init_users_routes(app, database_service, mikrotik_manager, auth_service=None):
    """Initialize user routes"""

    def paginate_results(results, page, per_page, endpoint_name):
        """Helper function to paginate any list of results"""
        if not results:
            return {
                "data": [],
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": 0,
                    "pages": 0,
                    "has_prev": False,
                    "has_next": False,
                },
            }

        total = len(results)
        pages = math.ceil(total / per_page)

        # Validate page number
        if page < 1:
            page = 1
        if page > pages and pages > 0:
            page = pages

        # Calculate start and end indices
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page

        paginated_data = results[start_idx:end_idx]

        return {
            "data": paginated_data,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": pages,
                "has_prev": page > 1,
                "has_next": page < pages,
            },
        }

    @users_bp.route("/active-users")
    def get_active_users():
        """Get active users with pagination"""
        # Get pagination parameters with defaults
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 50, type=int)

        # Validate pagination parameters
        if page < 1:
            return jsonify({"error": "Page must be greater than 0"}), 400
        if per_page < 1 or per_page > 1000:
            return jsonify({"error": "per_page must be between 1 and 1000"}), 400

        active_users = mikrotik_manager.get_active_users()

        # Paginate the results
        paginated_result = paginate_results(
            active_users, page, per_page, "get_active_users"
        )

        return jsonify(
            {
                "active_users": paginated_result["data"],
                "pagination": paginated_result["pagination"],
            }
        )

    @users_bp.route("/all-users")
    def get_all_users():
        """Get all users from database (synced from MikroTik) efficiently with pagination"""
        # Get pagination parameters with defaults
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 100, type=int)

        # Validate pagination parameters
        if page < 1:
            return jsonify({"error": "Page must be greater than 0"}), 400
        if per_page < 1 or per_page > 500:
            return jsonify({"error": "per_page must be between 1 and 500"}), 400

        rows = database_service.get_all_users()

        # Fetch all usage data from MikroTik in bulk
        all_usage = (
            mikrotik_manager.get_all_users_usage()
        )  # returns {username: usage_dict}

        users = []
        for row in rows:
            usage = all_usage.get(row["username"], {})
            users.append(
                {
                    "username": row["username"],
                    "profile_name": row["profile_name"],
                    "is_active": bool(row["is_active"]),
                    "last_seen": row["last_seen"],
                    "uptime_limit": row["uptime_limit"],
                    "comment": row["comment"],
                    "password_type": row["password_type"],
                    "is_voucher": bool(row["is_voucher"]),
                    "mac_address": row.get("mac_address", ""),
                    "ip_address": row.get("ip_address", ""),
                    "current_uptime": usage.get("uptime", "0s"),
                    "bytes_used": (
                        usage.get("bytes_in", 0) + usage.get("bytes_out", 0)
                    ),
                }
            )

        # Paginate the results
        paginated_result = paginate_results(users, page, per_page, "get_all_users")

        return jsonify(
            {
                "all_users": paginated_result["data"],
                "pagination": paginated_result["pagination"],
            }
        )

    @users_bp.route("/users/expired")
    def get_expired_users():
        """Get all expired users efficiently with pagination"""
        # Get pagination parameters with defaults
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 100, type=int)

        # Validate pagination parameters
        if page < 1:
            return jsonify({"error": "Page must be greater than 0"}), 400
        if per_page < 1 or per_page > 500:
            return jsonify({"error": "per_page must be between 1 and 500"}), 400

        rows = database_service.get_expired_users()

        # Bulk fetch usage
        expired_usernames = [row["username"] for row in rows]
        usage_data = mikrotik_manager.get_bulk_user_usage(
            expired_usernames
        )  # returns {username: usage_dict}

        expired_users = []
        for row in rows:
            usage = usage_data.get(row["username"], {})
            expired_users.append(
                {
                    "username": row["username"],
                    "profile_name": row["profile_name"],
                    "last_seen": row["last_seen"],
                    "uptime_limit": row["uptime_limit"],
                    "comment": row["comment"],
                    "is_voucher": bool(row["is_voucher"]),
                    "current_uptime": usage.get("uptime", "0s"),
                }
            )

        # Paginate the results
        paginated_result = paginate_results(
            expired_users, page, per_page, "get_expired_users"
        )

        return jsonify(
            {
                "expired_users": paginated_result["data"],
                "pagination": paginated_result["pagination"],
            }
        )

    @users_bp.route("/users/<username>")
    def get_user_info(username):
        """Get detailed information for any user"""
        result = database_service.get_user_info(username)

        if not result:
            abort(404, description="User not found")

        usage = mikrotik_manager.get_user_usage(username)
        is_expired = (
            check_uptime_limit(usage.get("uptime", "0s"), result["uptime_limit"])
            if usage
            else False
        )

        return jsonify(
            {
                "username": result["username"],
                "profile_name": result["profile_name"],
                "is_active": bool(result["is_active"]),
                "last_seen": result["last_seen"],
                "uptime_limit": result["uptime_limit"],
                "comment": result["comment"],
                "password_type": result["password_type"],
                "is_voucher": bool(result["is_voucher"]),
                "mac_address": result.get("mac_address", ""),
                "ip_address": result.get("ip_address", ""),
                "current_usage": usage,
                "is_expired": is_expired,
            }
        )

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
            "UPDATE all_users SET comment=%s WHERE username=%s", (comment, username)
        )

        return jsonify({"message": "Comment updated successfully"})
    
    @users_bp.route("/admin/login-only", methods=["POST"])
    def special_login():
        """
    Special login route that only allows users defined in environment variables.
    Environment variables:
        ADMIN_EMAIL
        ADMIN_PASSWORD  (store hashed password for security)
        """
        data = request.json or {}
        email = data.get("email")
        password = data.get("password")

        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 400

        # Load credentials from environment
        admin_email = os.getenv("ADMIN_EMAIL")
        admin_password_hash = os.getenv("ADMIN_PASSWORD_HASH")
        admin_password_raw = os.getenv("ADMIN_PASSWORD")

        # Verify credentials
        is_valid = False
        if email == admin_email:
            if admin_password_raw and password == admin_password_raw:
                is_valid = True
            elif admin_password_hash and check_password_hash(admin_password_hash, password):
                is_valid = True

        if not is_valid:
            return jsonify({"error": "Invalid credentials"}), 401

        # Successful login - Create a temporary JWT for the admin
        token = None
        if auth_service:
            # We use a special ID "admin-env" to denote this admin came from environment variables
            token = auth_service.create_jwt(uid="admin-env", role="admin")

        return jsonify({
            "success": True,
            "message": "Login successful", 
            "user": {
                "email": email,
                "role": "admin"
            },
            "token": token
        })

    @users_bp.route("/users/<username>/traffic")
    def get_user_traffic_history(username):
        """Get traffic history for a user"""
        limit = request.args.get("limit", 100, type=int)
        
        history = database_service.get_user_traffic_history(username, limit)
        
        return jsonify({
            "username": username,
            "period": "history", # consistent with other monitoring APIs
            "history": history
        })

    pass
