from flask import Blueprint, request, jsonify

from models.schemas import Profile
from utils.validators import validate_profile_name

profiles_bp = Blueprint('profiles', __name__)

def init_profiles_routes(app, database_service, mikrotik_manager):
    """Initialize profile routes"""
    
    @profiles_bp.route("/profiles")
    def get_profiles():
        try:
            profiles = mikrotik_manager.get_profiles()
            
            # If no profiles from MikroTik, use default profiles from database
            if not profiles:
                db_profiles = database_service.get_all_profiles()
                enhanced_profiles = []
                for profile in db_profiles:
                    enhanced_profiles.append({
                        'name': profile['name'],
                        'rate-limit': profile['rate_limit'],
                        'price': profile['price'],
                        'time_limit': profile['time_limit'],
                        'data_limit': profile['data_limit'],
                        'uptime_limit': profile['uptime_limit']
                    })
                return jsonify({"profiles": enhanced_profiles})
            
            # Enhance profiles with pricing information
            enhanced_profiles = []
            for profile in profiles:
                profile_name = profile.get('name', '')
                
                # Get pricing from database
                db_profile = database_service.get_profile(profile_name)
                
                if db_profile:
                    profile['price'] = db_profile['price']
                    profile['time_limit'] = db_profile['time_limit']
                    profile['data_limit'] = db_profile['data_limit']
                    profile['validity_period'] = db_profile['validity_period']
                    profile['uptime_limit'] = db_profile['uptime_limit']
                else:
                    # Default values if not in database
                    from services.voucher_service import VoucherService
                    temp_service = VoucherService(None, database_service, None)
                    profile['price'] = temp_service._calculate_price(profile_name)
                    profile['time_limit'] = "24h"
                    profile['data_limit'] = "Unlimited"
                    profile['validity_period'] = 24
                    profile['uptime_limit'] = "1d"
                
                enhanced_profiles.append(profile)
            
            return jsonify({"profiles": enhanced_profiles})
        except Exception as e:
            return jsonify({"profiles": []})

    @profiles_bp.route("/profiles/add", methods=["POST"])
    def add_profile():
        data = request.json
        profile_name = data.get("profile_name")
        
        is_valid, error = validate_profile_name(profile_name)
        if not is_valid:
            return jsonify({"error": error}), 400

        profiles = mikrotik_manager.get_profiles()
        profile = next((p for p in profiles if p.get("name") == profile_name), None)
        if not profile:
            return jsonify({"error": "Profile not found on MikroTik"}), 404

        # Create profile object
        profile_obj = Profile(
            name=profile.get('name'),
            rate_limit=profile.get('rate-limit', 'unlimited'),
            description=f"Profile {profile.get('name')}",
            price=1000,  # Default price
            time_limit="24h",
            data_limit="Unlimited",
            validity_period=24,
            uptime_limit="1d"
        )
        
        success = database_service.add_profile(profile_obj)
        if not success:
            return jsonify({"error": "Failed to add profile to database"}), 500

        return jsonify({"message": f"Profile '{profile_name}' added to database successfully"})

    @profiles_bp.route("/profiles/enhanced")
    def get_enhanced_profiles():
        """Get profiles with enhanced information for the frontend"""
        profiles = mikrotik_manager.get_profiles()
        enhanced_profiles = []
        
        for profile in profiles:
            profile_name = profile.get('name', '')
            
            # Get additional info from database
            db_profile = database_service.get_profile(profile_name)
            
            enhanced_profile = {
                'name': profile_name,
                'rate_limit': profile.get('rate-limit', 'unlimited'),
                'price': db_profile['price'] if db_profile else 1000,
                'time_limit': db_profile['time_limit'] if db_profile else "24h",
                'data_limit': db_profile['data_limit'] if db_profile else "Unlimited",
                'validity_period': db_profile['validity_period'] if db_profile else 24,
                'uptime_limit': db_profile['uptime_limit'] if db_profile else "1d"
            }
            enhanced_profiles.append(enhanced_profile)
        
        return jsonify({"profiles": enhanced_profiles})

    # Register blueprint
    app.register_blueprint(profiles_bp)