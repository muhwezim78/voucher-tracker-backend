import threading
import time
import datetime
import logging
from typing import List, Dict, Any

from models.schemas import User, FinancialTransaction
from utils.helpers import check_uptime_limit

logger = logging.getLogger(__name__)

class MonitoringService:
    def __init__(self, database_service, mikrotik_manager, voucher_service):
        self.db = database_service
        self.mikrotik = mikrotik_manager
        self.voucher_service = voucher_service
        self._running = False
        self._thread = None

    def start_monitoring(self):
        """Start the background monitoring thread"""
        if self._running:
            return
            
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("Monitoring service started")

    def stop_monitoring(self):
        """Stop the background monitoring thread"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Monitoring service stopped")

    def _monitor_loop(self):
        """Main monitoring loop"""
        while self._running:
            try:
                self.sync_all_users()
                self.monitor_active_users()
                self.check_expired_users()
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
            time.sleep(30)

    def sync_all_users(self):
        """Sync all MikroTik users to database"""
        try:
            all_users = self.mikrotik.get_all_users()
            
            for user in all_users:
                username = user.get('name', '')
                profile_name = user.get('profile', '')
                uptime_limit = user.get('limit-uptime', '')
                comment = user.get('comment', '')
                
                # Determine if it's a voucher and password type
                result = self.db.get_voucher(username)
                is_voucher = result is not None
                
                if is_voucher:
                    password_type = result.get('password_type', 'blank') if result else 'blank'
                else:
                    # For non-voucher users, determine password type from comment
                    if 'password=same' in comment.lower():
                        password_type = 'same'
                    elif 'password=blank' in comment.lower() or 'blank password' in comment.lower():
                        password_type = 'blank'
                    else:
                        password_type = 'custom'

                user_obj = User(
                    username=username,
                    profile_name=profile_name,
                    uptime_limit=uptime_limit,
                    comment=comment,
                    password_type=password_type,
                    is_voucher=is_voucher,
                    created_at=datetime.datetime.now() 
                )
                
                self.db.sync_user(user_obj)
                
        except Exception as e:
            logger.error(f"Error in sync_all_users: {e}")

    def monitor_active_users(self):
        """Monitor active users and update their status"""
        try:
            active_users = self.mikrotik.get_active_users()
            active_usernames = [u.get('user', '') for u in active_users if u.get('user')]
            
            all_users = self.db.execute_query('SELECT username FROM all_users', fetch=True) or []
            all_usernames = [u['username'] for u in all_users]
            
            inactive_usernames = list(set(all_usernames) - set(active_usernames))
            
            # Update active status
            if active_usernames:
                self.db.update_user_active_status(active_usernames, True)
                
                # Handle voucher-specific tracking
                for username in active_usernames:
                    self._handle_voucher_activation(username)
                    
                # Mark inactive users
            if inactive_usernames:
                self.db.update_user_active_status(inactive_usernames, False)
                
        except Exception as e:
            logger.error(f"Error in monitor_active_users: {e}")

    def _handle_voucher_activation(self, username: str):
        """Handle voucher activation and usage tracking"""
        try:
            # Check if it's a voucher
            voucher = self.db.get_voucher(username)
            if not voucher:
                return
                
            # Mark as used if not already
            if not voucher.get('is_used'):
                self.db.mark_voucher_used(username)
                
                # Record transaction
                profile_name = voucher.get('profile_name')
                profile_info = self.db.get_profile(profile_name)
                price = profile_info.get('price', 1000) if profile_info else 1000
                
                transaction = FinancialTransaction(
                    voucher_code=username,
                    amount=price,
                    transaction_type="SALE",
                    transaction_date=datetime.now()
                )
                self.db.add_transaction(transaction)
            
            # Update usage statistics
            usage = self.mikrotik.get_user_usage(username)
            if usage:
                bytes_used = usage.get('bytes_in', 0) + usage.get('bytes_out', 0)
                self.db.update_voucher_usage(username, bytes_used)
                
        except Exception as e:
            logger.error(f"Error handling voucher activation for {username}: {e}")

    def check_expired_users(self):
        """Check and mark expired users based on uptime limits"""
        try:
            users = self.db.execute_query(
                'SELECT username, uptime_limit, is_expired FROM all_users',
                fetch=True
            ) or []
            
            active_users = self.mikrotik.get_active_users()
            active_dict = {u.get('user'): u for u in active_users if u.get('user')}
            
            for user in users:
                username = user['username']
                uptime_limit = user.get('uptime_limit', '0s') or '0s'
                is_expired = user.get('is_expired', False)
                
                
                usage = self.mikrotik.get_user_usage(username)
                
                current_uptime = usage.get('uptime', '0s') if usage else '0s'

                    # Check if uptime limit is reached
                if check_uptime_limit(current_uptime, uptime_limit):
                    if username in active_dict:
                        logger.info(f"Removing expired user {username} from router")
                        try:
                            self.mikrotik.remove_active_user(username)
                        except Exception as e:
                            logger.warning(f"Error removing user {username} from router: {e}")                       
                        
                    if not is_expired:
                        logger.info(f"Marking user {username} as expired in database")    
                        self.db.execute_query(
                            'UPDATE all_users SET is_expired=TRUE, is_active=FALSE WHERE username=%s',
                            (username,)
                        )
                        
                        # If it's a voucher, mark it as expired
                        voucher = self.db.get_voucher(username)
                        if voucher and not voucher.get('is_expired', False):
                            self.db.execute_query(
                                'UPDATE vouchers SET is_expired=TRUE WHERE voucher_code=%s',
                                (username,)
                            )
                            
        except Exception as e:
            logger.error(f"Error in check_expired_users: {e}")