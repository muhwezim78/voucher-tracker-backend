import random
import string
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

def generate_voucher_code(length: int, chars: str) -> str:
    """Generate a random voucher code"""
    return ''.join(random.choice(chars) for _ in range(length))

def uptime_to_seconds(uptime_str: str) -> int:
    """Convert MikroTik uptime string to seconds"""
    if not uptime_str:
        return 0
    
    seconds = 0
    parts = uptime_str.split(' ')
    
    for part in parts:
        if 'd' in part:
            seconds += int(part.replace('d', '')) * 24 * 3600
        elif 'h' in part:
            seconds += int(part.replace('h', '')) * 3600
        elif 'm' in part:
            seconds += int(part.replace('m', '')) * 60
        elif 's' in part:
            seconds += int(part.replace('s', ''))
    
    return seconds

def uptime_limit_to_seconds(uptime_limit: str) -> int:
    """Convert uptime limit string to seconds"""
    if not uptime_limit:
        return 0
    
    if 'd' in uptime_limit:
        return int(uptime_limit.replace('d', '')) * 24 * 3600
    elif ':' in uptime_limit:
        parts = uptime_limit.split(':')
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    else:
        try:
            return int(uptime_limit)
        except ValueError:
            return 0
    
    return 0

def check_uptime_limit(current_uptime: str, uptime_limit: str) -> bool:
    """Check if current uptime exceeds the limit"""
    if not uptime_limit or not current_uptime:
        return False
    
    current_seconds = uptime_to_seconds(current_uptime)
    limit_seconds = uptime_limit_to_seconds(uptime_limit)
    
    return current_seconds >= limit_seconds if limit_seconds > 0 else False

def calculate_expiry_time(validity_period: int) -> datetime:
    """Calculate expiry time based on validity period in hours"""
    return datetime.now() + timedelta(hours=validity_period)

def format_bytes(bytes_count: int) -> str:
    """Format bytes to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_count < 1024.0:
            return f"{bytes_count:.2f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.2f} TB"