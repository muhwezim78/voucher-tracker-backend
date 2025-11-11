from flask import Flask, request, jsonify, g, Blueprint
import logging

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
logger = logging.getLogger(__name__)


def init_auth_routes(app, mikrotik_manager, database_service, auth_service):
    """Initialize authentication, router connection, and subscription routes"""

    db = database_service
    mm = mikrotik_manager
    AuthService = auth_service
    SubscriptionService = auth_service

    @auth_bp.route("/register", methods=["POST"])
    def register():
        try:
            data = request.get_json(silent=True) or {}
            if not data:
                return jsonify({"success": False, "error": "Invalid JSON body"}), 400

            return AuthService.register_user(db, data)

        except Exception as e:
            logger.exception("Error in /auth/register")
            return jsonify({"success": False, "error": str(e)}), 500

    @auth_bp.route("/login", methods=["POST"])
    def login():
        try:
            data = request.get_json(silent=True) or {}

            email = data.get("email")
            password = data.get("password")

            if not email or not password:
                return jsonify({"success": False, "error": "Email and password required"}), 400

            return AuthService.login_user(db, email, password)

        except Exception as e:
            logger.exception("Error in /auth/login")
            return jsonify({"success": False, "error": str(e)}), 500


    @auth_bp.route("/mikrotik/connect", methods=["POST"])
    @AuthService.auth_required
    def connect_router():
        try:
            data = request.get_json(silent=True) or {}

            host = data.get("host")
            username = data.get("username")
            password = data.get("password")

            if not host or not username or not password:
                return jsonify({
                    "success": False,
                    "error": "host, username, and password are required"
                }), 400

            return AuthService.connect_mikrotik(db, mm, host, username, password)

        except Exception as e:
            logger.exception("Error in /auth/mikrotik/connect")
            return jsonify({"success": False, "error": str(e)}), 500


    @auth_bp.route("/subscription/generate", methods=["POST"])
    def generate_subscription():
        try:
            data = request.get_json(silent=True) or {}
            duration = data.get("duration")
            package_type = data.get("package")
            quantity = data.get("quantity", 1)

            if not duration or not package_type:
                return jsonify({
                    "success": False,
                    "error": "duration and package type required"
                }), 400

            return SubscriptionService.generate_code(
                db=db,
                duration=duration,
                package_type=package_type,
                quantity=quantity
            )

        except Exception as e:
            logger.exception("Error in /auth/subscription/generate")
            return jsonify({"success": False, "error": str(e)}), 500

    @auth_bp.route("/subscription/verify", methods=["POST"])
    def verify_subscription():
        try:
            data = request.get_json(silent=True) or {}
            email = data.get("email")
            code = data.get("code")

            if not email or not code:
                return jsonify({
                    "success": False,
                    "error": "email and code required"
                }), 400

            return SubscriptionService.verify_code(
                db=db,
                email=email,
                code=code
            )

        except Exception as e:
            logger.exception("Error in /auth/subscription/verify")
            return jsonify({"success": False, "error": str(e)}), 500

    @auth_bp.route("/subscription/status", methods=["POST"])
    def check_subscription_status():
        try:
            data = request.get_json(silent=True) or {}
            email = data.get("email")

            if not email:
                return jsonify({"success": False, "error": "email required"}), 400

            return SubscriptionService.check_status(
                db=db,
                email=email
            )

        except Exception as e:
            logger.exception("Error in /auth/subscription/status")
            return jsonify({"success": False, "error": str(e)}), 500

    app.register_blueprint(auth_bp)
