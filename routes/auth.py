from flask import Flask, request, jsonify, g, Blueprint
import logging
import time

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
logger = logging.getLogger(__name__)


def init_auth_routes(app, database_service, mikrotik_manager, auth_service, subscription_service):
    """Initialize authentication, router connection, and subscription routes"""

    db = database_service
    mm = mikrotik_manager
    auth_service_instance = auth_service
    SubscriptionService = subscription_service

    @auth_bp.route("/register", methods=["POST"])
    def register():
        try:
            data = request.get_json(silent=True) or {}
            if not data:
                return jsonify({"success": False, "error": "Invalid JSON body"}), 400

            return auth_service_instance.register_user(db, data)

        except Exception as e:
            logger.exception("Error in /auth/register")
            return jsonify({"success": False, "error": "Registration failed"}), 500

    @auth_bp.route("/login", methods=["POST"])
    def login():
        try:
            data = request.get_json(silent=True) or {}
            email = data.get("email")
            password = data.get("password")
            device_info = request.headers.get('User-Agent', 'Unknown')

            if not email or not password:
                return jsonify({"success": False, "error": "Email and password required"}), 400

            return auth_service_instance.login_user(db, email, password, device_info)

        except Exception as e:
            logger.exception("Error in /auth/login")
            return jsonify({"success": False, "error": "Login failed"}), 500

    @auth_bp.route("/logout", methods=["POST"])
    @auth_service_instance.auth_required
    def logout():
        try:
            token = request.headers.get("Authorization", "").replace("Bearer ", "")
            return auth_service_instance.logout_user(db, token)
        except Exception as e:
            logger.exception("Error in /auth/logout")
            return jsonify({"success": False, "error": "Logout failed"}), 500

    @auth_bp.route("/profile", methods=["GET"])
    @auth_service_instance.auth_required
    def get_profile():
        try:
            user_data = db.get_user_by_id(g.uid)
            if user_data:
                # Remove sensitive data
                user_data.pop('password', None)
                return auth_service_instance.standard_response(
                    True, 
                    "Profile retrieved successfully", 
                    {"user": user_data}
                )
            return auth_service_instance.standard_response(False, "User not found"), 404
        except Exception as e:
            logger.exception("Error in /auth/profile")
            return jsonify({"success": False, "error": "Failed to retrieve profile"}), 500

    @auth_bp.route("/password/reset/initiate", methods=["POST"])
    def initiate_password_reset():
        try:
            data = request.get_json(silent=True) or {}
            email = data.get("email")
            
            if not email:
                return jsonify({"success": False, "error": "Email is required"}), 400

            return auth_service_instance.initiate_password_reset(db, email)
        except Exception as e:
            logger.exception("Error in /auth/password/reset/initiate")
            return jsonify({"success": False, "error": "Password reset initiation failed"}), 500

    @auth_bp.route("/password/reset/confirm", methods=["POST"])
    def confirm_password_reset():
        try:
            data = request.get_json(silent=True) or {}
            token = data.get("token")
            new_password = data.get("new_password")
            
            if not token or not new_password:
                return jsonify({"success": False, "error": "Token and new password are required"}), 400

            return auth_service_instance.reset_password(db, token, new_password)
        except Exception as e:
            logger.exception("Error in /auth/password/reset/confirm")
            return jsonify({"success": False, "error": "Password reset failed"}), 500

    # Router Management Routes
    @auth_bp.route("/router/connect", methods=["POST"])
    @auth_service_instance.auth_required
    def connect_router():
        try:
            data = request.get_json(silent=True) or {}
            
            # Add user_id to router data
            data['user_id'] = g.uid
            
            return auth_service_instance.connect_mikrotik(db, mm, g.uid, data)

        except Exception as e:
            logger.exception("Error in /auth/router/connect")
            return jsonify({"success": False, "error": "Router connection failed"}), 500

    @auth_bp.route("/routers", methods=["GET"])
    @auth_service_instance.auth_required
    def get_user_routers():
        try:
            return auth_service_instance.get_user_routers(db, g.uid)
        except Exception as e:
            logger.exception("Error in /auth/routers")
            return jsonify({"success": False, "error": "Failed to retrieve routers"}), 500

    @auth_bp.route("/router/<router_name>/credentials", methods=["GET"])
    @auth_service_instance.auth_required
    def get_router_credentials(router_name):
        try:
            return auth_service_instance.get_router_credentials(db, g.uid, router_name)
        except Exception as e:
            logger.exception(f"Error in /auth/router/{router_name}/credentials")
            return jsonify({"success": False, "error": "Failed to retrieve router credentials"}), 500

    @auth_bp.route("/router/<router_name>/test", methods=["POST"])
    @auth_service_instance.auth_required
    def test_router_connection(router_name):
        try:
            router = db.get_router_credentials(g.uid, router_name)
            if not router:
                return jsonify({"success": False, "error": "Router not found"}), 404

            # Test connection using mikrotik manager
            mm.connect_router(router['host'], router['username'], router['password'])
            return auth_service_instance.standard_response(True, "Router connection test successful")
        except Exception as e:
            logger.exception(f"Error testing router connection: {router_name}")
            return jsonify({"success": False, "error": f"Connection test failed: {str(e)}"}), 400

    # Enhanced Subscription Routes with User Context
    @auth_bp.route("/subscription/generate", methods=["POST"])
    @auth_service_instance.auth_required
    def generate_subscription():
        try:
            data = request.get_json(silent=True) or {}
            duration = data.get("duration")
            package_type = data.get("package_type")
            quantity = data.get("quantity", 1)

            if not duration or not package_type:
                return jsonify({
                    "success": False,
                    "error": "duration and package_type are required"
                }), 400

            return SubscriptionService.generate_code(
                database_service=db,
                duration=duration,
                package_type=package_type,
                quantity=quantity,
                user_id=g.uid
            )

        except Exception as e:
            logger.exception("Error in /auth/subscription/generate")
            return jsonify({"success": False, "error": "Failed to generate subscription codes"}), 500

    @auth_bp.route("/subscription/verify", methods=["POST"])
    @auth_service_instance.auth_required
    def verify_subscription():
        try:
            data = request.get_json(silent=True) or {}
            code = data.get("code")

            if not code:
                return jsonify({
                    "success": False,
                    "error": "Subscription code is required"
                }), 400

            return SubscriptionService.verify_code(
                database_service=db,
                user_id=g.uid,
                code=code
            )

        except Exception as e:
            logger.exception("Error in /auth/subscription/verify")
            return jsonify({"success": False, "error": "Failed to verify subscription"}), 500

    @auth_bp.route("/subscription/status", methods=["GET"])
    @auth_service_instance.auth_required
    def check_subscription_status():
        try:
            return SubscriptionService.check_status(
                database_service=db,
                user_id=g.uid
            )
        except Exception as e:
            logger.exception("Error in /auth/subscription/status")
            return jsonify({"success": False, "error": "Failed to check subscription status"}), 500

    @auth_bp.route("/subscriptions", methods=["GET"])
    @auth_service_instance.auth_required
    def get_user_subscriptions():
        try:
            return SubscriptionService.get_user_subscriptions(db, g.uid)
        except Exception as e:
            logger.exception("Error in /auth/subscriptions")
            return jsonify({"success": False, "error": "Failed to retrieve subscriptions"}), 500

    # Admin-only routes
    @auth_bp.route("/admin/users", methods=["GET"])
    @auth_service_instance.auth_required
    @auth_service_instance.admin_required
    def get_all_users():
        try:
            users = db.get_all_users()
            return auth_service_instance.standard_response(
                True,
                "Users retrieved successfully",
                {"users": users, "count": len(users)}
            )
        except Exception as e:
            logger.exception("Error in /auth/admin/users")
            return jsonify({"success": False, "error": "Failed to retrieve users"}), 500

    @auth_bp.route("/admin/user/<user_id>/deactivate", methods=["POST"])
    @auth_service_instance.auth_required
    @auth_service_instance.admin_required
    def deactivate_user(user_id):
        try:
            success = db.deactivate_user(user_id)
            if success:
                return auth_service_instance.standard_response(True, "User deactivated successfully")
            else:
                return auth_service_instance.standard_response(False, "Failed to deactivate user"), 500
        except Exception as e:
            logger.exception(f"Error deactivating user {user_id}")
            return jsonify({"success": False, "error": "Failed to deactivate user"}), 500

    # Health check endpoint
    @auth_bp.route("/health", methods=["GET"])
    def health_check():
        try:
            # Test database connection
            db_status = "healthy" if db.execute_query("SELECT 1", fetch_one=True) else "unhealthy"
            
            return jsonify({
                "success": True,
                "status": "service is running",
                "database": db_status,
                "timestamp": time.time()
            })
        except Exception as e:
            logger.exception("Error in /auth/health")
            return jsonify({
                "success": False,
                "status": "service error",
                "error": str(e)
            }), 500

    # Add database service to app context for use in auth decorators
    @app.before_request
    def before_request():
        g.database_service = db

    app.register_blueprint(auth_bp)
    logger.info("Authentication routes initialized successfully")