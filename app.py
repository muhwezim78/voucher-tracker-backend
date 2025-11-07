from flask import Flask, jsonify
from flask_cors import CORS
import logging
import os

from config import Config
from services import DatabaseService, MikroTikManager, VoucherService, MonitoringService
from routes import (
    init_vouchers_routes,
    init_profiles_routes,
    init_users_routes,
    init_financial_routes,
    init_system_routes,
    init_pricing_routes
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_app():
    """Application factory pattern"""
    app = Flask(__name__)
    
    # Load configuration
    config = Config()
    
    # CORS configuration
    CORS(app, origins=config.CORS_ORIGINS)
    
    # Initialize services
    database_service = DatabaseService(config)
    mikrotik_manager = MikroTikManager(config)
    voucher_service = VoucherService(config, database_service, mikrotik_manager)
    monitoring_service = MonitoringService(database_service, mikrotik_manager, voucher_service)
    
    # Initialize database
    database_service.init_db()
    
    # Initialize routes
    _initialize_routes(app, database_service, mikrotik_manager, voucher_service)
    
    # Start monitoring service
    monitoring_service.start_monitoring()
    
    # Store services in app context for access in routes if needed
    app.config['database_service'] = database_service
    app.config['mikrotik_manager'] = mikrotik_manager
    app.config['voucher_service'] = voucher_service
    app.config['monitoring_service'] = monitoring_service
    
    return app

def _initialize_routes(app, database_service, mikrotik_manager, voucher_service):
    """Initialize all application routes"""
    
    # Root route
    @app.route("/")
    def root():
        return jsonify({"message": "MikroTik Voucher Tracker API"})
    
    # Initialize all route blueprints
    init_vouchers_routes(app, voucher_service)
    init_profiles_routes(app, database_service, mikrotik_manager)
    init_users_routes(app, database_service, mikrotik_manager)
    init_financial_routes(app, database_service)
    init_system_routes(app, mikrotik_manager)
    init_pricing_routes(app, database_service)

# Create application instance
app = create_app()

if __name__ == "__main__":
    from config import Config
    config = Config()
    
    host = config.FLASK_HOST
    port = config.FLASK_PORT
    debug = config.FLASK_DEBUG
    
    app.run(host=host, port=port, debug=debug)