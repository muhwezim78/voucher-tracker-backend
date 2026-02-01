from flask import Blueprint, jsonify, current_app, g
from functools import lru_cache

system_bp = Blueprint('system', __name__)

@lru_cache(maxsize=1)
@system_bp.route("/system/info")
def get_system_info_route():
    """Get MikroTik system information"""
    mikrotik_manager = current_app.config['mikrotik_manager']
    system_info = mikrotik_manager.get_system_info()
    return jsonify({"system_info": system_info})

@system_bp.route("/system/sync-profiles", methods=["POST"])
def sync_profiles():
    """Sync profiles from MikroTik"""
    database_service = current_app.config['database_service']
    mikrotik_manager = current_app.config['mikrotik_manager']
    
    success, message = database_service.sync_profiles_from_mikrotik(mikrotik_manager)
    if success:
         return jsonify({"message": message, "success": True})
    else:
         return jsonify({"error": message, "success": False}), 500

def init_system_routes(app, mikrotik_manager=None):
    """Register system blueprint"""
    app.register_blueprint(system_bp)