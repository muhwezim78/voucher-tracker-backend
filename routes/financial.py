from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from typing import List, Dict, Any

financial_bp = Blueprint('financial', __name__)

def init_financial_routes(app, database_service):
    """Initialize financial routes"""
    
    @financial_bp.route("/financial/stats")
    def get_financial_stats():
        stats = database_service.get_financial_stats()
        return jsonify(stats)

    @financial_bp.route("/financial/revenue-data")
    def get_revenue_data():
        days = int(request.args.get("days", 30))
        
        rows = database_service.get_revenue_data(days)
        
        # Generate data for all days, even if no transactions
        data = []
        for i in range(days):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            revenue = 0
            voucher_count = 0
            
            for row in rows:
                if row['date'].strftime('%Y-%m-%d') == date:
                    revenue = row['revenue']
                    voucher_count = row['voucher_count']
                    break
                    
            data.append({'date': date, 'revenue': revenue, 'voucher_count': voucher_count})
        
        data.reverse()  # Sort chronologically
        return jsonify({"revenue_data": data})

    @financial_bp.route("/financial/profile-stats")
    def get_profile_stats():
        rows = database_service.execute_query(
            '''
            SELECT v.profile_name,
                   COUNT(*) as total_sold,
                   SUM(CASE WHEN ft.amount IS NOT NULL THEN ft.amount ELSE 0 END) as total_revenue,
                   SUM(CASE WHEN v.is_used=TRUE THEN 1 ELSE 0 END) as used_count
            FROM vouchers v
            LEFT JOIN financial_transactions ft ON v.voucher_code = ft.voucher_code
            GROUP BY v.profile_name
            ''',
            fetch=True
        ) or []

        profile_stats = []
        for row in rows:
            profile_stats.append({
                'profile_name': row['profile_name'],
                'total_sold': row['total_sold'],
                'total_revenue': row['total_revenue'] or 0,
                'used_count': row['used_count']
            })

        return jsonify({"profile_stats": profile_stats})

    # Register blueprint
    app.register_blueprint(financial_bp)