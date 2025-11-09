import random
import string
import logging
from datetime import datetime, timedelta
from typing import Optional
import re

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

def parse_uptime_to_seconds(uptime_str):
    """
    Convert uptime strings like '123m51s', '2h30m', '1d5h30m' to seconds
    """
    if not uptime_str or uptime_str == '0s':
        return 0
    
    try:
        # If it's already a number, return it
        return int(uptime_str)
    except ValueError:
        pass
    
    # Parse time string
    seconds = 0
    current_value = ''
    
    for char in uptime_str:
        if char.isdigit():
            current_value += char
        else:
            if current_value:
                if char == 'd':  # days
                    seconds += int(current_value) * 24 * 60 * 60
                elif char == 'h':  # hours
                    seconds += int(current_value) * 60 * 60
                elif char == 'm':  # minutes
                    seconds += int(current_value) * 60
                elif char == 's':  # seconds
                    seconds += int(current_value)
                current_value = ''
    
    return seconds


def check_uptime_limit(current_uptime: str, uptime_limit: str) -> bool:
    """Check if current uptime exceeds the limit"""
    if not uptime_limit or not current_uptime:
        return False
    
    try:
        # USE parse_uptime_to_seconds for BOTH values - this handles '123m51s' format
        current_seconds = parse_uptime_to_seconds(current_uptime)
        limit_seconds = parse_uptime_to_seconds(uptime_limit)
        
        logger.debug(f"Uptime check: {current_uptime} -> {current_seconds}s, Limit: {uptime_limit} -> {limit_seconds}s")
        
        # If limit is 0, it means no limit (never expires)
        if limit_seconds == 0:
            return False
            
        # Check if current uptime has reached or exceeded the limit
        return current_seconds >= limit_seconds
        
    except Exception as e:
        logger.error(f"Error in check_uptime_limit: {e}")
        return False
    
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