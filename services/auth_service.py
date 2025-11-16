import os
import time
import random
import bcrypt
import jwt
import logging
from functools import wraps
from flask import request, jsonify, g
from dotenv import load_dotenv
from twilio.rest import Client
from flask_limiter.util import get_remote_address
from typing import Dict, Any, Optional, List

load_dotenv()

logger = logging.getLogger(__name__)

required_env_vars = [
    "APP_SECRET_KEY",
    "JWT_SECRET_KEY",
    "ENCRYPTION_KEY",
    "TWILIO_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
]

for var in required_env_vars:
    if not os.getenv(var):
        raise ValueError(f"Missing required environment variable: {var}")

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", APP_SECRET_KEY)
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Security settings
BCRYPT_ROUNDS = int(os.getenv("BCRYPT_ROUNDS", "12"))
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "24"))
SESSION_TIMEOUT_HOURS = int(os.getenv("SESSION_TIMEOUT_HOURS", "1"))

# Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


class AuthService:
    """Enhanced authentication service for multi-user system with router management."""

    @staticmethod
    def create_jwt(uid: str, role: str = "user", expire_hours: int = None) -> str:
        """Create JWT token with enhanced payload"""
        if expire_hours is None:
            expire_hours = JWT_EXPIRY_HOURS

        now = int(time.time())
        expire_seconds = expire_hours * 3600
        payload = {
            "sub": uid,
            "role": role,
            "iat": now,
            "exp": now + expire_seconds,
            "iss": "mikrotik-auth-service",
            "aud": "mikrotik-management-app",
        }
        token = jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")
        return token if isinstance(token, str) else token.decode("utf-8")

    @staticmethod
    def verify_jwt(token: str) -> Dict[str, Any]:
        """Verify JWT token with enhanced error handling"""
        try:
            payload = jwt.decode(
                token,
                JWT_SECRET_KEY,
                algorithms=["HS256"],
                audience="mikrotik-management-app",
                issuer="mikrotik-auth-service",
            )
            return payload
        except jwt.ExpiredSignatureError:
            logger.warning(f"JWT token expired: {token[:20]}...")
            return {"error": "Token expired", "code": "TOKEN_EXPIRED"}
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {str(e)}")
            return {"error": "Invalid token", "code": "INVALID_TOKEN"}
        except Exception as e:
            logger.error(f"JWT verification error: {str(e)}")
            return {"error": "Token verification failed", "code": "VERIFICATION_FAILED"}

    @staticmethod
    def auth_required(fn):
        """Enhanced decorator to protect routes with JWT and session validation"""

        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = request.headers.get("Authorization", "").replace("Bearer ", "")
            if not token:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Missing authentication token",
                            "code": "MISSING_TOKEN",
                        }
                    ),
                    401,
                )

            # Validate JWT
            payload = AuthService.verify_jwt(token)
            if "error" in payload:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": payload["error"],
                            "code": payload.get("code", "AUTH_ERROR"),
                        }
                    ),
                    401,
                )

            # Validate session in database (if database_service available)
            if hasattr(g, "database_service"):
                session = g.database_service.validate_session(token)
                if not session:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": "Invalid or expired session",
                                "code": "INVALID_SESSION",
                            }
                        ),
                        401,
                    )

            # Set user context
            g.uid = payload["sub"]
            g.role = payload.get("role", "user")
            g.token = token

            return fn(*args, **kwargs)

        return wrapper

    @staticmethod
    def admin_required(fn):
        """Decorator to require admin role"""

        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not hasattr(g, "role") or g.role != "admin":
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Administrator access required",
                            "code": "ADMIN_REQUIRED",
                        }
                    ),
                    403,
                )
            return fn(*args, **kwargs)

        return wrapper

    @staticmethod
    def router_access_required(fn):
        """Decorator to validate router access for the user"""

        @wraps(fn)
        def wrapper(*args, **kwargs):
            router_name = (
                request.args.get("router_name") or request.json.get("router_name")
                if request.json
                else None
            )
            if not router_name:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Router name is required",
                            "code": "ROUTER_NAME_REQUIRED",
                        }
                    ),
                    400,
                )

            # Check if user has access to this router
            if hasattr(g, "database_service") and hasattr(g, "uid"):
                router = g.database_service.get_router_credentials(g.uid, router_name)
                if not router:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": "Router not found or access denied",
                                "code": "ROUTER_ACCESS_DENIED",
                            }
                        ),
                        403,
                    )
                g.current_router = router

            return fn(*args, **kwargs)

        return wrapper

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt with configurable rounds"""
        return bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
        ).decode("utf-8")

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """Verify bcrypt password"""
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
        except Exception as e:
            logger.error(f"Password verification error: {str(e)}")
            return False

    @staticmethod
    def register_user(database_service, user_data: dict):
        """Enhanced user registration with validation"""
        # Validate required fields
        required_fields = ["email", "password", "full_name"]
        missing_fields = [
            field for field in required_fields if not user_data.get(field)
        ]
        if missing_fields:
            return (
                AuthService.standard_response(
                    False, f"Missing required fields: {', '.join(missing_fields)}"
                ),
                400,
            )

        # Validate email format
        email = user_data["email"]
        if not AuthService._validate_email(email):
            return AuthService.standard_response(False, "Invalid email format"), 400

        # Validate password strength
        password_error = AuthService._validate_password_strength(user_data["password"])
        if password_error:
            return AuthService.standard_response(False, password_error), 400

        # Check if user already exists
        existing_user = database_service.get_user_by_email(email)
        if existing_user:
            return (
                AuthService.standard_response(
                    False, "User with this email already exists"
                ),
                400,
            )

        # Prepare user data
        user_data["password"] = AuthService.hash_password(user_data["password"])
        user_data["userId"] = f"BTU{random.randint(10000000, 99999999)}"
        user_data["is_verified"] = user_data.get("is_verified", False)
        user_data["role"] = user_data.get("role", "user")
        user_data["is_active"] = user_data.get("is_active", True)

        # Register user
        success = database_service.register_user(user_data)
        if success:
            logger.info(f"User registered successfully: {email}")

            # Send verification (in production, send email/SMS)
            if not user_data["is_verified"]:
                AuthService._send_verification_code(email, user_data["userId"])

            return AuthService.standard_response(
                True,
                "User registered successfully",
                {
                    "userId": user_data["userId"],
                    "email": email,
                    "requires_verification": not user_data["is_verified"],
                },
            )
        else:
            logger.error(f"User registration failed: {email}")
            return AuthService.standard_response(False, "Registration failed"), 500

    @staticmethod
    def login_user(
        database_service, email: str, password: str, device_info: str = None
    ):
        """Enhanced login with session management"""
        if not email or not password:
            return (
                AuthService.standard_response(False, "Email and password required"),
                400,
            )

        # Rate limiting check (basic implementation)
        if hasattr(g, "limiter"):
            identity = f"login_{email}"
            if not g.limiter.test(identity):
                return (
                    AuthService.standard_response(False, "Too many login attempts"),
                    429,
                )

        user_data = database_service.verify_login({"email": email})
        if not user_data:
            logger.warning(f"Failed login attempt - user not found: {email}")
            return AuthService.standard_response(False, "Invalid credentials"), 401

        if not user_data.get("is_verified", False):
            return AuthService.standard_response(False, "Account not verified"), 403

        if not user_data.get("is_active", True):
            return AuthService.standard_response(False, "Account deactivated"), 403

        if not AuthService.verify_password(password, user_data["password"]):
            logger.warning(f"Failed login attempt - invalid password: {email}")
            return AuthService.standard_response(False, "Invalid credentials"), 401

        # Create JWT token
        token = AuthService.create_jwt(user_data["id"], user_data.get("role", "user"))

        # Create session in database
        ip_address = request.remote_addr
        device_info = device_info or request.headers.get("User-Agent", "Unknown")
        database_service.create_session(user_data["id"], token, device_info, ip_address)

        # Return user data without sensitive information
        user_response = {
            "userId": user_data["id"],
            "email": user_data["email"],
            "full_name": user_data.get("full_name"),
            "role": user_data.get("role", "user"),
            "company_name": user_data.get("company_name"),
            "is_verified": user_data.get("is_verified", False),
        }

        logger.info(f"User logged in successfully: {email}")
        return AuthService.standard_response(
            True,
            "Login successful",
            {
                "token": token,
                "user": user_response,
                "expires_in": JWT_EXPIRY_HOURS * 3600,
            },
        )

    @staticmethod
    def logout_user(database_service, token: str):
        """Logout user by invalidating session"""
        success = database_service.invalidate_session(token)
        if success:
            logger.info("User logged out successfully")
            return AuthService.standard_response(True, "Logged out successfully")
        else:
            return AuthService.standard_response(False, "Logout failed"), 500

    @staticmethod
    def connect_mikrotik(
        database_service, mikrotik_manager, user_id: str, router_data: dict
    ):
        """Connect a router and save info for specific user"""
        required_fields = ["router_name", "host", "username", "password"]
        missing_fields = [
            field for field in required_fields if not router_data.get(field)
        ]
        if missing_fields:
            return (
                AuthService.standard_response(
                    False, f"Missing required fields: {', '.join(missing_fields)}"
                ),
                400,
            )

        # Test router connection
        try:
            mikrotik_manager.connect_router(
                router_data["host"], router_data["username"], router_data["password"]
            )
            logger.info(f"Router connection successful: {router_data['router_name']}")
        except Exception as e:
            logger.error(
                f"Router connection failed: {router_data['router_name']} - {str(e)}"
            )
            return (
                AuthService.standard_response(
                    False, f"Router connection failed: {str(e)}"
                ),
                400,
            )

        # Save router info to database
        router_data["user_id"] = user_id
        success = database_service.save_router_info(router_data)

        if success:
            logger.info(f"Router saved successfully: {router_data['router_name']}")
            return AuthService.standard_response(
                True,
                "Router connected and saved successfully",
                {"router_name": router_data["router_name"]},
            )
        else:
            logger.error(f"Failed to save router: {router_data['router_name']}")
            return (
                AuthService.standard_response(
                    False, "Failed to save router information"
                ),
                500,
            )

    @staticmethod
    def get_user_routers(database_service, user_id: str):
        """Get all routers for a user"""
        routers = database_service.get_user_routers(user_id)
        return AuthService.standard_response(
            True,
            "Routers retrieved successfully",
            {"routers": routers, "count": len(routers)},
        )

    @staticmethod
    def get_router_credentials(database_service, user_id: str, router_name: str):
        """Get router credentials for connection"""
        router = database_service.get_router_credentials(user_id, router_name)
        if router:
            return AuthService.standard_response(
                True, "Router credentials retrieved", {"router": router}
            )
        else:
            return AuthService.standard_response(False, "Router not found"), 404

    @staticmethod
    def initiate_password_reset(database_service, email: str):
        """Initiate password reset process"""
        user = database_service.get_user_by_email(email)
        if not user:
            # Don't reveal whether user exists for security
            logger.info(f"Password reset requested for non-existent email: {email}")
            return AuthService.standard_response(
                True, "If the email exists, a reset link has been sent"
            )

        # Generate reset token
        reset_token = f"RESET{random.randint(10000000, 99999999)}"

        # Save token to database
        success = database_service.create_password_reset_token(
            user["user_id"], reset_token
        )

        if success:
            # In production, send SMS/email with reset token
            AuthService._send_sms_reset_code(user.get("phone"), reset_token)

            logger.info(f"Password reset initiated for: {email}")
            return AuthService.standard_response(
                True, "Password reset code sent to your registered phone number"
            )
        else:
            logger.error(f"Failed to create reset token for: {email}")
            return (
                AuthService.standard_response(
                    False, "Failed to initiate password reset"
                ),
                500,
            )

    @staticmethod
    def reset_password(database_service, token: str, new_password: str):
        """Reset password using reset token"""
        if not token or not new_password:
            return (
                AuthService.standard_response(
                    False, "Token and new password are required"
                ),
                400,
            )

        # Validate password strength
        password_error = AuthService._validate_password_strength(new_password)
        if password_error:
            return AuthService.standard_response(False, password_error), 400

        # Validate reset token
        reset_data = database_service.validate_password_reset_token(token)
        if not reset_data:
            return (
                AuthService.standard_response(False, "Invalid or expired reset token"),
                400,
            )

        # Update password
        hashed_password = AuthService.hash_password(new_password)
        success = database_service.update_user_password(
            reset_data["user_id"], hashed_password
        )

        if success:
            # Mark token as used and invalidate all sessions
            database_service.use_password_reset_token(token)
            database_service.invalidate_all_user_sessions(reset_data["user_id"])

            logger.info(f"Password reset successful for user: {reset_data['user_id']}")
            return AuthService.standard_response(True, "Password reset successfully")
        else:
            logger.error(f"Password reset failed for user: {reset_data['user_id']}")
            return AuthService.standard_response(False, "Failed to reset password"), 500

    @staticmethod
    def standard_response(
        success: bool, message: str = None, data: dict = None, code: str = None
    ):
        """Enhanced standard JSON response"""
        res = {"success": success}
        if message:
            res["message"] = message
        if data:
            res["data"] = data
        if code:
            res["code"] = code
        return jsonify(res)

    @staticmethod
    def _validate_email(email: str) -> bool:
        """Basic email validation"""
        import re

        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        return bool(re.match(pattern, email))

    @staticmethod
    def _validate_password_strength(password: str) -> Optional[str]:
        """Validate password strength"""
        if len(password) < 8:
            return "Password must be at least 8 characters long"
        if not any(char.isdigit() for char in password):
            return "Password must contain at least one number"
        if not any(char.isupper() for char in password):
            return "Password must contain at least one uppercase letter"
        if not any(char.islower() for char in password):
            return "Password must contain at least one lowercase letter"
        return None

    @staticmethod
    def _send_verification_code(email: str, user_id: str):
        """Send verification code (SMS/Email)"""
        try:
            # Generate verification code
            verification_code = f"VRFY{random.randint(1000, 9999)}"

            # In production, store code in database and send via email/SMS
            logger.info(f"Verification code for {email}: {verification_code}")

            # Example SMS sending (uncomment in production)
            # twilio_client.messages.create(
            #  body=f"Your verification code: {verification_code}",
            #   from_=TWILIO_PHONE_NUMBER,
            #    to=user_phone,
        # )

        except Exception as e:
            logger.error(f"Failed to send verification code: {str(e)}")

    @staticmethod
    def _send_sms_reset_code(phone: str, reset_code: str):
        """Send password reset code via SMS"""
        if not phone:
            return

        try:
            # In production, uncomment to send actual SMS
            twilio_client.messages.create(
                body=f"Your password reset code: {reset_code}",
                from_=TWILIO_PHONE_NUMBER,
                to=phone,
            )
            logger.info(f"Password reset code for {phone}: {reset_code}")
        except Exception as e:
            logger.error(f"Failed to send SMS reset code: {str(e)}")

    @staticmethod
    def get_current_user():
        """Get current user from request context"""
        if hasattr(g, "uid"):
            return {
                "user_id": g.uid,
                "role": getattr(g, "role", "user"),
                "token": getattr(g, "token", None),
            }
        return None


class SubscriptionService:
    """Enhanced subscription service with user-based management."""

    @staticmethod
    def generate_code(
        database_service,
        duration: int,
        package_type: str,
        quantity: int = 1,
        user_id: str = None,
    ):
        """Generate subscription codes for specific user"""
        if not (duration and package_type and quantity):
            return (
                AuthService.standard_response(
                    False, "Duration, package type, and quantity are required"
                ),
                400,
            )

        codes = []
        for _ in range(quantity):
            code = f"SUB{random.randint(10000000, 99999999)}"
            success = database_service.store_voucher(
                {
                    "code": code,
                    "duration": duration,
                    "package_type": package_type,
                    "used": False,
                    "created_by": user_id,  # Track which user created the code
                }
            )
            if success:
                codes.append(code)

        return AuthService.standard_response(
            True,
            "Subscription codes generated successfully",
            {
                "codes": codes,
                "count": len(codes),
                "package_type": package_type,
                "duration": duration,
            },
        )

    @staticmethod
    def verify_code(database_service, user_id: str, code: str):
        """Verify a subscription code for a user"""
        if not code:
            return (
                AuthService.standard_response(False, "Subscription code is required"),
                400,
            )

        result = database_service.verify_code(user_id, code)
        if not result:
            return (
                AuthService.standard_response(
                    False, "Invalid or already used subscription code"
                ),
                404,
            )

        return AuthService.standard_response(
            True, "Subscription verified successfully", {"details": result}
        )

    @staticmethod
    def check_status(database_service, user_id: str):
        """Check the subscription status of a user"""
        status = database_service.check_subscription_status(user_id)
        return AuthService.standard_response(
            True, "Subscription status retrieved", {"status": status}
        )

    @staticmethod
    @AuthService.auth_required
    def get_user_subscriptions(database_service, user_id: str):
        """Get all subscriptions for a user"""
        subscriptions = database_service.get_user_subscriptions(user_id)
        return AuthService.standard_response(
            True,
            "Subscriptions retrieved successfully",
            {"subscriptions": subscriptions, "count": len(subscriptions)},
        )
