from flask import Blueprint, jsonify
from functools import lru_cache

system_bp = Blueprint('system', __name__)

def init_system_routes(app, mikrotik_manager):
    """Initialize system routes"""
    
    @lru_cache(maxsize=1)
    @system_bp.route("/system/info")
    def get_system_info_route():
        """Get MikroTik system information"""
        system_info = mikrotik_manager.get_system_info()
        return jsonify({"system_info": system_info})

    # Register blueprint
    app.register_blueprint(system_bp)