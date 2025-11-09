import re
from typing import Optional, Tuple

def validate_voucher_code(code: str) -> Tuple[bool, Optional[str]]:
    """Validate voucher code format"""
    if not code or len(code) < 5:
        return False, "Voucher code must be at least 5 characters long"
    
    if not re.match(r'^[A-Z0-9]+$', code):
        return False, "Voucher code can only contain uppercase letters and numbers"
    
    return True, None

def validate_profile_name(profile_name: str) -> Tuple[bool, Optional[str]]:
    """Validate profile name"""
    if not profile_name or len(profile_name.strip()) == 0:
        return False, "Profile name is required"
    
    if len(profile_name) > 100:
        return False, "Profile name too long"
    
    return True, None

def validate_quantity(quantity: int) -> Tuple[bool, Optional[str]]:
    """Validate voucher quantity"""
    if quantity < 1:
        return False, "Quantity must be at least 1"
    
    if quantity > 200:
        return False, "Quantity cannot exceed 200"
    
    return True, None

def validate_customer_info(customer_name: str, customer_contact: str) -> Tuple[bool, Optional[str]]:
    """Validate customer information"""
    if customer_name and len(customer_name) > 100:
        return False, "Customer name too long"
    
    if customer_contact and len(customer_contact) > 100:
        return False, "Customer contact too long"
    
    return True, None