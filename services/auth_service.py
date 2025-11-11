# auth_service.py
import os
import time
import random
import bcrypt
import jwt
from functools import wraps
from flask import request, jsonify, g
from dotenv import load_dotenv
from twilio.rest import Client
from flask_limiter.util import get_remote_address

load_dotenv()

# Ensure required env vars
required_env_vars = [
    "APP_SECRET_KEY",
    "TWILIO_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
]

for var in required_env_vars:
    if not os.getenv(var):
        raise ValueError(f"Missing required environment variable: {var}")

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


class AuthService:
    """Authentication service for JWT, passwords, and user validation."""

    @staticmethod
    def create_jwt(uid: str, expire_seconds: int = 3600) -> str:
        """Create JWT token"""
        now = int(time.time())
        payload = {"sub": uid, "iat": now, "exp": now + expire_seconds}
        token = jwt.encode(payload, APP_SECRET_KEY, algorithm="HS256")

        return token if isinstance(token, str) else token.decode("utf-8")

    @staticmethod
    def verify_jwt(token: str):
        """Verify JWT token"""
        try:
            return jwt.decode(token, APP_SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return {"error": "Token expired"}
        except jwt.InvalidTokenError:
            return {"error": "Invalid token"}

    @staticmethod
    def auth_required(fn):
        """Decorator to protect routes with JWT"""

        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = request.headers.get("Authorization", "").replace("Bearer ", "")
            if not token:
                return jsonify({"success": False, "error": "Missing token"}), 401

            payload = AuthService.verify_jwt(token)
            if "error" in payload:
                return jsonify({"success": False, "error": payload["error"]}), 401

            g.uid = payload["sub"]
            g.role = payload.get("role")
            return fn(*args, **kwargs)

        return wrapper

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt"""
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """Verify bcrypt password"""
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))

    @staticmethod
    def standard_response(success: bool, message: str = None, data: dict = None):
        """Standard JSON response"""
        res = {"success": success}
        if message:
            res["message"] = message
        if data:
            res["data"] = data
        return jsonify(res)

    @staticmethod
    def register_user(database_service, user_data: dict):
        """Register new user"""
        password = user_data.get("password")
        if not password:
            return AuthService.standard_response(False, "Password is required"), 400

        user_data["password"] = AuthService.hash_password(password)
        user_data["is_verified"] = user_data.get("is_verified", False)
        user_data["userId"] = f"BTU{random.randint(10000000, 99999999)}"

        database_service.register_user(user_data)
        return AuthService.standard_response(
            True, "User registered", {"userId": user_data["userId"]}
        )

    @staticmethod
    def login_user(database_service, email: str, password: str):
        """Login user and return JWT"""
        if not email or not password:
            return (
                AuthService.standard_response(False, "Email and password required"),
                400,
            )

        user_data = database_service.verify_login({"email": email})
        if not user_data:
            return AuthService.standard_response(False, "Invalid credentials"), 401

        if not user_data.get("is_verified"):
            return AuthService.standard_response(False, "Account not verified"), 403

        if not AuthService.verify_password(password, user_data["password"]):
            return AuthService.standard_response(False, "Invalid credentials"), 401

        token = AuthService.create_jwt(user_data["id"], user_data.get("role", "user"))
        return AuthService.standard_response(
            True, "Login successful", {"token": token, "user": user_data}
        )

    @staticmethod
    def connect_mikrotik(
        database_service, mikrotik_manager, host: str, username: str, password: str
    ):
        """Connect a router and save info"""
        if not (host and username and password):
            return (
                AuthService.standard_response(
                    False, "Host, username, and password required"
                ),
                400,
            )

        mikrotik_manager.connect_router(host, username, password)
        database_service.save_router_info({"host": host, "username": username})
        return AuthService.standard_response(True, "Router connected successfully")


class SubscriptionService:
    """Handles subscription codes, verification, and status."""

    @staticmethod
    def generate_code(
        database_service, duration: int, package_type: str, quantity: int = 1
    ):
        """Generate subscription codes and store in database"""
        if not (duration and package_type and quantity):
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "Duration, package type, and quantity are required",
                    }
                ),
                400,
            )

        codes = []
        for _ in range(quantity):
            code = f"SUB{random.randint(10000000, 99999999)}"
            database_service.store_voucher(
                {
                    "code": code,
                    "duration": duration,
                    "package_type": package_type,
                    "used": False,
                }
            )
            codes.append(code)

        return jsonify(
            {"success": True, "message": "Codes generated successfully", "codes": codes}
        )

    @staticmethod
    def verify_code(database_service, email: str, code: str):
        """Verify a subscription code for a user"""
        if not email or not code:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "Email and subscription code are required",
                    }
                ),
                400,
            )

        result = database_service.verify_code(email, code)
        if not result:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "Invalid or already used subscription code",
                    }
                ),
                404,
            )

        return jsonify(
            {"success": True, "message": "Subscription verified", "details": result}
        )

    @staticmethod
    def check_status(database_service, email: str):
        """Check the subscription status of a user"""
        if not email:
            return (
                jsonify(
                    {"success": False, "message": "Email is required to check status"}
                ),
                400,
            )

        status = database_service.check_subscription_status(email)
        return jsonify({"success": True, "status": status})
