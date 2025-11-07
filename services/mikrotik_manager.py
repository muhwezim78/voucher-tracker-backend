import routeros_api
import logging
from typing import List, Dict, Any, Optional, Tuple

from config import Config

logger = logging.getLogger(__name__)

class MikroTikManager:
    def __init__(self, config: Config):
        self.host = config.MIKROTIK_CONFIG['host']
        self.username = config.MIKROTIK_CONFIG['username']
        self.password = config.MIKROTIK_CONFIG['password']

    def get_api(self) -> Tuple[Optional[routeros_api.RouterOsApiPool], Optional[Any]]:
        """Return a fresh MikroTik API connection"""
        try:
            connection = routeros_api.RouterOsApiPool(
                self.host,
                username=self.username,
                password=self.password,
                plaintext_login=True,
            )
            api = connection.get_api()
            return connection, api
        except Exception as e:
            logger.error(f"MikroTik connection failed: {e}")
            return None, None

    def get_profiles(self) -> List[Dict[str, Any]]:
        """Get all hotspot user profiles"""
        connection, api = self.get_api()
        if not api:
            return []
        try:
            profiles = api.get_resource('/ip/hotspot/user/profile')
            result = profiles.get()
            return result
        except Exception as e:
            logger.error(f"Error fetching profiles: {e}")
            return []
        finally:
            if connection:
                connection.disconnect()

    def create_voucher(self, profile_name: str, code: str, password: Optional[str] = None, 
                      comment: str = "", uptime_limit: str = "1d") -> bool:
        """Create voucher user on MikroTik"""
        connection, api = self.get_api()
        if not api:
            return False
        try:
            users = api.get_resource('/ip/hotspot/user')
            
            # Determine final password
            final_password = ""
            if password == "same":
                final_password = code
            elif password is not None:
                final_password = password
            
            users.add(
                name=code, 
                password=final_password, 
                profile=profile_name, 
                comment=comment, 
                disabled='no',
                limit_uptime=uptime_limit
            )
            logger.info(f"Voucher {code} created with profile {profile_name} and uptime limit {uptime_limit}")
            return True
        except Exception as e:
            logger.error(f"Error creating voucher: {e}")
            return False
        finally:
            if connection:
                connection.disconnect()

    def get_all_users(self) -> List[Dict[str, Any]]:
        """Get all hotspot users from MikroTik"""
        connection, api = self.get_api()
        if not api:
            return []
        try:
            users = api.get_resource('/ip/hotspot/user')
            result = users.get()
            return result
        except Exception as e:
            logger.error(f"Error fetching all users: {e}")
            return []
        finally:
            if connection:
                connection.disconnect()

    def get_active_users(self) -> List[Dict[str, Any]]:
        """Get currently active hotspot users"""
        connection, api = self.get_api()
        if not api:
            return []
        try:
            active = api.get_resource('/ip/hotspot/active')
            result = active.get()
            # Format the result to match frontend expectations
            formatted_result = []
            for user in result:
                formatted_user = {
                    'user': user.get('user', ''),
                    'profile': user.get('profile', ''),
                    'uptime': user.get('uptime', ''),
                    'bytes-in': user.get('bytes-in', '0'),
                    'bytes-out': user.get('bytes-out', '0'),
                    'server': user.get('server', '')
                }
                formatted_result.append(formatted_user)
            return formatted_result
        except Exception as e:
            logger.error(f"Error fetching active users: {e}")
            return []
        finally:
            if connection:
                connection.disconnect()

    def get_user_usage(self, username: str) -> Optional[Dict[str, Any]]:
        """Get usage statistics for a specific user"""
        connection, api = self.get_api()
        if not api:
            return None
        try:
            users = api.get_resource('/ip/hotspot/user')
            stats = users.get(name=username)
            if stats:
                return {
                    'bytes_in': int(stats[0].get('bytes-in', 0)),
                    'bytes_out': int(stats[0].get('bytes-out', 0)),
                    'uptime': stats[0].get('uptime', '0s'),
                    'limit_uptime': stats[0].get('limit-uptime', ''),
                    'disabled': stats[0].get('disabled', 'no'),
                    'comment': stats[0].get('comment', '')
                }
            return None
        except Exception as e:
            logger.error(f"Error fetching user usage: {e}")
            return None
        finally:
            if connection:
                connection.disconnect()

    def get_system_info(self) -> Dict[str, Any]:
        """Get MikroTik system information"""
        connection, api = self.get_api()
        if not api:
            return {}
        try:
            system_resource = api.get_resource('/system/resource')
            identity_resource = api.get_resource('/system/identity')
            
            system_info = system_resource.get()
            identity_info = identity_resource.get()
            
            if system_info and identity_info:
                return {
                    'router_name': identity_info[0].get('name', 'Unknown'),
                    'cpu_load': system_info[0].get('cpu-load', '0%'),
                    'uptime': system_info[0].get('uptime', '0s'),
                    'version': system_info[0].get('version', 'Unknown'),
                    'cpu_count': system_info[0].get('cpu-count', '1'),
                    'memory_usage': system_info[0].get('memory-usage', '0%')
                }
            return {}
        except Exception as e:
            logger.error(f"Error fetching system info: {e}")
            return {}
        finally:
            if connection:
                connection.disconnect()

    def remove_expired_user(self, username: str) -> bool:
        """Remove expired user from MikroTik"""
        connection, api = self.get_api()
        if not api:
            return False
        try:
            users = api.get_resource('/ip/hotspot/user')
            user_list = users.get(name=username)
            if user_list:
                user_id = user_list[0].get('id')
                users.remove(id=user_id)
                logger.info(f"Removed expired user: {username}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error removing expired user {username}: {e}")
            return False
        finally:
            if connection:
                connection.disconnect()

    def update_user_comment(self, username: str, comment: str) -> bool:
        """Update user comment in MikroTik"""
        connection, api = self.get_api()
        if not api:
            return False
        try:
            users = api.get_resource('/ip/hotspot/user')
            user_list = users.get(name=username)
            if user_list:
                user_id = user_list[0].get('id')
                users.set(id=user_id, comment=comment)
                logger.info(f"Updated comment for user: {username}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error updating user comment {username}: {e}")
            return False
        finally:
            if connection:
                connection.disconnect()