from .vouchers import init_vouchers_routes, vouchers_bp
from .profiles import init_profiles_routes, profiles_bp
from .users import init_users_routes, users_bp
from .financial import init_financial_routes, financial_bp
from .system import init_system_routes, system_bp
from .pricing import init_pricing_routes, pricing_bp
from .auth import init_auth_routes, auth_bp

__all__ = [
    "init_vouchers_routes",
    "vouchers_bp",
    "init_profiles_routes",
    "profiles_bp",
    "init_users_routes",
    "users_bp",
    "init_financial_routes",
    "financial_bp",
    "init_system_routes",
    "system_bp",
    "init_pricing_routes",
    "pricing_bp",
    "init_auth_routes",
    "auth_bp",
]
