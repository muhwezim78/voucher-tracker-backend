from .vouchers import init_vouchers_routes
from .profiles import init_profiles_routes
from .users import init_users_routes
from .financial import init_financial_routes
from .system import init_system_routes
from .pricing import init_pricing_routes
from .auth import init_auth_routes

__all__ = [
    "init_vouchers_routes",
    "init_profiles_routes",
    "init_users_routes",
    "init_financial_routes",
    "init_system_routes",
    "init_pricing_routes",
    "init_auth_routes",
]
