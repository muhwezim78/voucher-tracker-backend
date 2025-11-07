import random
import string
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

from config import Config
from models.schemas import Voucher
from utils.helpers import generate_voucher_code, calculate_expiry_time
from utils.validators import validate_voucher_code, validate_profile_name, validate_quantity, validate_customer_info

logger = logging.getLogger(__name__)

class VoucherService:
    def __init__(self, config: Config, database_service, mikrotik_manager):
        self.config = config
        self.db = database_service
        self.mikrotik = mikrotik_manager

    def generate_voucher_code(self, uptime_limit: str) -> str:
        """Generate unique voucher code based on uptime limit"""
        config = self.config.VOUCHER_CONFIG.get(uptime_limit, self.config.VOUCHER_CONFIG['1d'])
        
        while True:
            code = generate_voucher_code(config['length'], config['chars'])
            
            # Check if code already exists in database
            result = self.db.get_voucher(code)
            if not result:
                return code

    def create_vouchers(self, profile_name: str, quantity: int, customer_name: str = "", 
                       customer_contact: str = "", password_type: str = "blank") -> Tuple[bool, List[Dict[str, Any]], str]:
        """Create multiple vouchers"""
        # Validate inputs
        is_valid, error = validate_profile_name(profile_name)
        if not is_valid:
            return False, [], error
            
        is_valid, error = validate_quantity(quantity)
        if not is_valid:
            return False, [], error
            
        is_valid, error = validate_customer_info(customer_name, customer_contact)
        if not is_valid:
            return False, [], error

        # Get profile information
        db_profile = self.db.get_profile(profile_name)
        if not db_profile:
            return False, [], "Profile not found"

        uptime_limit = db_profile.get('uptime_limit', '1d')
        price_per_voucher = db_profile.get('price', 1000)
        validity_period = db_profile.get('validity_period', 24)

        vouchers = []
        total_price = 0
        successful_creations = 0

        for i in range(quantity):
            try:
                voucher_code = self.generate_voucher_code(uptime_limit)
                
                # Create voucher in database
                voucher = Voucher(
                    voucher_code=voucher_code,
                    profile_name=profile_name,
                    customer_name=customer_name,
                    customer_contact=customer_contact,
                    expiry_time=calculate_expiry_time(validity_period),
                    uptime_limit=uptime_limit,
                    password_type=password_type
                )
                
                if not self.db.add_voucher(voucher):
                    continue

                # Create voucher on MikroTik
                password = self._determine_password(password_type, voucher_code)
                comment = self._create_user_comment(customer_name, customer_contact, password_type)
                
                success = self.mikrotik.create_voucher(
                    profile_name, voucher_code, password, comment, uptime_limit
                )
                
                if success:
                    password_display = self._get_password_display(password_type, password)
                    vouchers.append({
                        'code': voucher_code,
                        'password': password_display,
                        'profile': profile_name,
                        'uptime_limit': uptime_limit
                    })
                    total_price += price_per_voucher
                    successful_creations += 1
                else:
                    logger.error(f"Failed to create voucher {voucher_code} on MikroTik")
                    
            except Exception as e:
                logger.error(f"Error creating voucher {i+1}: {e}")
                continue

        if successful_creations == 0:
            return False, [], "Failed to create any vouchers"
            
        message = f"Successfully created {successful_creations} out of {quantity} vouchers"
        if successful_creations < quantity:
            message += f". {quantity - successful_creations} failed."

        return True, vouchers, message

    def _determine_password(self, password_type: str, voucher_code: str) -> Optional[str]:
        """Determine password based on password type"""
        if password_type == "same":
            return "same"
        elif password_type == "custom":
            return generate_voucher_code(8, string.ascii_uppercase + string.digits)
        else:  # blank
            return None

    def _get_password_display(self, password_type: str, password: Optional[str]) -> str:
        """Get password display for response"""
        if password_type == "custom" and password:
            return password
        elif password_type == "same":
            return "same as username"
        else:
            return "blank"

    def _create_user_comment(self, customer_name: str, customer_contact: str, password_type: str) -> str:
        """Create comment for MikroTik user"""
        comment_parts = ["Type: voucher"]
        if customer_name:
            comment_parts.append(f"Customer: {customer_name}")
        if customer_contact:
            comment_parts.append(f"Contact: {customer_contact}")
        if password_type != "blank":
            comment_parts.append(f"Password: {password_type}")
            
        return " | ".join(comment_parts)

    def get_voucher_info(self, voucher_code: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """Get detailed voucher information"""
        is_valid, error = validate_voucher_code(voucher_code)
        if not is_valid:
            return False, None, error

        result = self.db.get_voucher(voucher_code)
        if not result:
            return False, None, "Voucher not found"

        usage = self.mikrotik.get_user_usage(voucher_code)
        profile_info = self.db.get_profile(result['profile_name'])
        price = profile_info.get('price', 1000) if profile_info else 1000

        voucher_info = {
            'code': result['voucher_code'],
            'profile_name': result['profile_name'],
            'created_at': result['created_at'],
            'activated_at': result['activated_at'],
            'is_used': bool(result['is_used']),
            'bytes_used': result['bytes_used'],
            'session_time': result['session_time'],
            'customer_name': result['customer_name'],
            'customer_contact': result['customer_contact'],
            'uptime_limit': result['uptime_limit'],
            'password_type': result['password_type'],
            'current_usage': usage,
            'price': price
        }

        return True, voucher_info, "Voucher found"

    def get_expired_vouchers(self) -> List[Dict[str, Any]]:
        """Get vouchers that have reached their uptime limit"""
        rows = self.db.execute_query(
            '''
            SELECT voucher_code, profile_name, activated_at, uptime_limit, is_expired
            FROM vouchers 
            WHERE is_used = TRUE
            ORDER BY activated_at DESC
            LIMIT 50
            ''',
            fetch=True
        ) or []
        
        expired_vouchers = []
        for row in rows:
            voucher_code = row['voucher_code']
            uptime_limit = row['uptime_limit']
            
            # Get current usage from MikroTik
            usage = self.mikrotik.get_user_usage(voucher_code)
            current_uptime = usage.get('uptime', '0s') if usage else '0s'
            
            # Check if uptime limit is reached
            from utils.helpers import check_uptime_limit
            is_expired = check_uptime_limit(current_uptime, uptime_limit)
            
            expired_vouchers.append({
                'voucher_code': voucher_code,
                'profile_name': row['profile_name'],
                'activated_at': row['activated_at'],
                'uptime_limit': uptime_limit,
                'current_uptime': current_uptime,
                'is_expired': is_expired or bool(row['is_expired'])
            })
        
        return expired_vouchers