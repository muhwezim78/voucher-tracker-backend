from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any

@dataclass
class Voucher:
    voucher_code: str
    profile_name: str
    created_at: datetime
    activated_at: Optional[datetime] = None
    is_used: bool = False
    customer_name: str = ""
    customer_contact: str = ""
    bytes_used: int = 0
    session_time: int = 0
    expiry_time: Optional[datetime] = None
    is_expired: bool = False
    uptime_limit: str = "1d"
    password_type: str = "blank"

@dataclass
class User:
    username: str
    profile_name: str
    created_at: datetime
    last_seen: Optional[datetime] = None
    is_active: bool = False
    bytes_used: int = 0
    uptime_limit: str = ""
    is_expired: bool = False
    comment: str = ""
    password_type: str = "blank"
    is_voucher: bool = False

@dataclass
class Profile:
    name: str
    rate_limit: str
    description: str
    price: int = 0
    time_limit: str = "24h"
    data_limit: str = "Unlimited"
    validity_period: int = 24
    uptime_limit: str = "1d"
    created_at: Optional[datetime] = None

@dataclass
class FinancialTransaction:
    voucher_code: str
    amount: int
    transaction_type: str
    transaction_date: datetime