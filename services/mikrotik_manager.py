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

    # -------------------- Profiles --------------------
    def get_profiles(self) -> List[Dict[str, Any]]:
        """Get all hotspot user profiles"""
        connection, api = self.get_api()
        if not api:
            return []
        try:
            profiles = api.get_resource('/ip/hotspot/user/profile')
            return profiles.get()
        except Exception as e:
            logger.error(f"Error fetching profiles: {e}")
            return []
        finally:
            if connection:
                connection.disconnect()

    # -------------------- Voucher/User management --------------------
    def create_voucher(self, profile_name: str, code: str, password: Optional[str] = None,
                       comment: str = "", uptime_limit: str = "1d") -> bool:
        """Create voucher user on MikroTik"""
        connection, api = self.get_api()
        if not api:
            return False
        try:
            users = api.get_resource('/ip/hotspot/user')
            
            # Determine password
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
            logger.info(f"Voucher {code} created with profile {profile_name} and uptime {uptime_limit}")
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
            return users.get()
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
            formatted_result = []
            for user in result:
                formatted_result.append({
                    'user': user.get('user', ''),
                    'profile': user.get('profile', ''),
                    'uptime': user.get('uptime', ''),
                    'bytes-in': user.get('bytes-in', '0'),
                    'bytes-out': user.get('bytes-out', '0'),
                    'server': user.get('server', '')
                })
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

    # -------------------- Bulk Usage (Optimization) --------------------
    def get_all_users_usage(self) -> Dict[str, Dict[str, Any]]:
        """
        Fetch all hotspot users and their usage stats in a single API call.
        Returns a dictionary keyed by username.
        """
        connection, api = self.get_api()
        if not api:
            return {}
        try:
            users = api.get_resource('/ip/hotspot/user')
            user_list = users.get()
            
            usage_dict = {}
            for u in user_list:
                username = u.get('name')
                usage_dict[username] = {
                    'bytes_in': int(u.get('bytes-in', 0)),
                    'bytes_out': int(u.get('bytes-out', 0)),
                    'uptime': u.get('uptime', '0s'),
                    'limit_uptime': u.get('limit-uptime', ''),
                    'disabled': u.get('disabled', 'no'),
                    'comment': u.get('comment', '')
                }
            return usage_dict
        except Exception as e:
            logger.error(f"Error fetching all users usage: {e}")
            return {}
        finally:
            if connection:
                connection.disconnect()

    def get_bulk_user_usage(self, usernames: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetch usage only for specific usernames efficiently.
        Returns a dictionary keyed by username.
        """
        connection, api = self.get_api()
        if not api:
            return {}
        try:
            users = api.get_resource('/ip/hotspot/user')
            user_list = users.get()
            
            usage_dict = {}
            for u in user_list:
                name = u.get('name')
                if name in usernames:
                    usage_dict[name] = {
                        'bytes_in': int(u.get('bytes-in', 0)),
                        'bytes_out': int(u.get('bytes-out', 0)),
                        'uptime': u.get('uptime', '0s'),
                        'limit_uptime': u.get('limit-uptime', ''),
                        'disabled': u.get('disabled', 'no'),
                        'comment': u.get('comment', '')
                    }
            return usage_dict
        except Exception as e:
            logger.error(f"Error fetching bulk user usage: {e}")
            return {}
        finally:
            if connection:
                connection.disconnect()

    # -------------------- System Info --------------------
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

    # -------------------- User Removal/Update --------------------
    def remove_expired_user(self, username: str) -> bool:
        """Remove expired user from MikroTik"""
        connection, api = self.get_api()
        if not api:
            return False
        try:
            users = api.get_resource('/ip/hotspot/user')
            user_list = users.get(name=username)
            if user_list:
                users.remove(id=user_list[0]['id'])
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
                users.set(id=user_list[0]['id'], comment=comment)
                logger.info(f"Updated comment for user: {username}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error updating user comment {username}: {e}")
            return False
        finally:
            if connection:
                connection.disconnect()
