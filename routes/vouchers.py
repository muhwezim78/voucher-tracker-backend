from flask import Blueprint, request, jsonify, abort
from typing import Dict, Any

from services.voucher_service import VoucherService

vouchers_bp = Blueprint('vouchers', __name__)

def init_vouchers_routes(app, voucher_service: VoucherService):
    """Initialize voucher routes with the service"""
    
    @vouchers_bp.route("/vouchers/generate", methods=["POST"])
    def generate_vouchers():
        data = request.json
        profile_name = data.get("profile_name")
        quantity = data.get("quantity", 1)
        customer_name = data.get("customer_name", "")
        customer_contact = data.get("customer_contact", "")
        password_type = data.get("password_type", "blank")

        success, vouchers, message = voucher_service.create_vouchers(
            profile_name, quantity, customer_name, customer_contact, password_type
        )
        
        if not success:
            return jsonify({"error": message}), 400
            
        total_price = sum(voucher_service.db.get_profile(voucher['profile']).get('price', 1000) 
                         for voucher in vouchers if voucher_service.db.get_profile(voucher['profile']))
            
        return jsonify({
            "vouchers": vouchers, 
            "message": message,
            "total_price": total_price
        })

    @vouchers_bp.route("/vouchers/<voucher_code>")
    def get_voucher_info(voucher_code):
        success, voucher_info, message = voucher_service.get_voucher_info(voucher_code)
        
        if not success:
            abort(404, description=message)
            
        return jsonify(voucher_info)

    @vouchers_bp.route("/vouchers/expired")
    def get_expired_vouchers_endpoint():
        try:
            expired_vouchers = voucher_service.get_expired_vouchers()
            return jsonify({"expired_vouchers": expired_vouchers})
        except Exception as e:
            return jsonify({"expired_vouchers": [], "error": str(e)}), 500

    # Register blueprint
    app.register_blueprint(vouchers_bp)