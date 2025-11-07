from flask import Blueprint, request, jsonify

pricing_bp = Blueprint('pricing', __name__)

def init_pricing_routes(app, database_service):
    """Initialize pricing routes"""
    
    @pricing_bp.route("/pricing/rates", methods=["GET", "PUT"])
    def handle_pricing_rates():
        """Get or update pricing rates"""
        if request.method == "GET":
            rates = database_service.get_pricing_rates()
            return jsonify({"base_rates": rates})
        elif request.method == "PUT":
            data = request.json
            if 'base_rates' not in data:
                return jsonify({"error": "base_rates is required"}), 400
            
            database_service.update_pricing_rates(data['base_rates'])
            return jsonify({"message": "Pricing rates updated successfully"})

    # Register blueprint
    app.register_blueprint(pricing_bp)