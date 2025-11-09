from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
import logging

financial_bp = Blueprint('financial', __name__)
logger = logging.getLogger(__name__)

def init_financial_routes(app, database_service, mikrotik_manager):
    """Initialize financial routes"""

    # ---------------------------------------------------------
    # /financial/stats — overall stats summary
    # ---------------------------------------------------------
    @financial_bp.route("/financial/stats")
    def get_financial_stats():
        try:
            stats = database_service.get_financial_stats(mikrotik_manager=mikrotik_manager)
            return jsonify(stats)
        except Exception as e:
            logger.error(f"Error getting financial stats: {e}")
            return jsonify({"error": str(e)}), 500

    # ---------------------------------------------------------
    # /financial/active-revenue — trigger DB sync + return active revenue
    # ---------------------------------------------------------
    @financial_bp.route("/financial/active-revenue")
    def get_active_revenue():
        """
        Fetch active MikroTik users, record them in DB, and return daily revenue + count.
        """
        try:
            # Step 1: Get live users from MikroTik
            active_users = mikrotik_manager.get_active_users() or []  # [{username, profile_name, uptime}, ...]

            # Step 2: Record & update users in DB (this automatically handles transactions)
            database_service.record_active_users(active_users)

            # Step 3: Get today's total revenue (no args needed)
            daily_revenue = database_service.calculate_daily_revenue()

            # Step 4: Respond
            return jsonify({
                "active_users_count": len(active_users),
                "daily_revenue": daily_revenue
            })
        except Exception as e:
            logger.error(f"Error in /financial/active-revenue: {e}")
            return jsonify({"error": str(e)}), 500

    # ---------------------------------------------------------
    # /financial/revenue-data — N-day historical revenue
    # ---------------------------------------------------------
    @financial_bp.route("/financial/revenue-data")
    def get_revenue_data():
        """
        Return daily revenue + voucher count for the past N days.
        """
        try:
            days = int(request.args.get("days", 30))
            # If database_service implements get_revenue_data, use it; otherwise compute manually
            if hasattr(database_service, "get_revenue_data"):
                rows = database_service.get_revenue_data(days) or []
            else:
                # Safe fallback query
                rows = database_service.execute_query(
                    '''
                    SELECT DATE(transaction_date) AS date,
                           SUM(amount) AS revenue,
                           COUNT(DISTINCT voucher_code) AS voucher_count
                    FROM financial_transactions
                    WHERE transaction_type='SALE'
                      AND transaction_date >= CURRENT_DATE - INTERVAL '%s days'
                    GROUP BY DATE(transaction_date)
                    ORDER BY date ASC
                    ''',
                    (days,),
                    fetch=True
                ) or []

            # Normalize to full-day window, filling empty days with 0
            data = []
            row_map = {r["date"].strftime("%Y-%m-%d"): r for r in rows if r.get("date")}
            for i in range(days):
                date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                r = row_map.get(date, {"revenue": 0, "voucher_count": 0})
                data.append({
                    "date": date,
                    "revenue": r["revenue"] or 0,
                    "voucher_count": r["voucher_count"] or 0
                })

            data.reverse()
            return jsonify({"revenue_data": data})
        except Exception as e:
            logger.error(f"Error in /financial/revenue-data: {e}")
            return jsonify({"error": str(e)}), 500

    # ---------------------------------------------------------
    # /financial/profile-stats — revenue by bandwidth profile
    # ---------------------------------------------------------
    @financial_bp.route("/financial/profile-stats")
    def get_profile_stats():
        """
        Return per-profile sales & revenue summary.
        """
        try:
            rows = database_service.execute_query(
                '''
                SELECT v.profile_name,
                       COUNT(v.voucher_code) AS total_sold,
                       SUM(CASE WHEN ft.amount IS NOT NULL THEN ft.amount ELSE 0 END) AS total_revenue,
                       SUM(CASE WHEN v.is_used = TRUE THEN 1 ELSE 0 END) AS used_count
                FROM vouchers v
                LEFT JOIN financial_transactions ft
                    ON v.voucher_code = ft.voucher_code AND ft.transaction_type='SALE'
                GROUP BY v.profile_name
                ORDER BY total_sold DESC
                ''',
                fetch=True
            ) or []

            profile_stats = [
                {
                    "profile_name": r["profile_name"],
                    "total_sold": r["total_sold"],
                    "total_revenue": r["total_revenue"] or 0,
                    "used_count": r["used_count"]
                }
                for r in rows
            ]

            return jsonify({"profile_stats": profile_stats})
        except Exception as e:
            logger.error(f"Error in /financial/profile-stats: {e}")
            return jsonify({"error": str(e)}), 500

    # ---------------------------------------------------------
    # REGISTER BLUEPRINT ON APP
    # ---------------------------------------------------------
    app.register_blueprint(financial_bp)
