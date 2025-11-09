import eventlet
eventlet.monkey_patch()   # MUST BE FIRST LINE

import logging
import os
from flask import Flask, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO

from config import Config
from services import (
    DatabaseService,
    MikroTikManager,
    VoucherService,
    MonitoringService
)
from routes import (
    init_vouchers_routes,
    init_profiles_routes,
    init_users_routes,
    init_financial_routes,
    init_system_routes,
    init_pricing_routes
)


# ===========================================================
# GLOBALS
# ===========================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

socketio = None   # will be initialized in create_app()


# ===========================================================
# INITIALIZE ROUTES
# ===========================================================
def _initialize_routes(app, database_service, mikrotik_manager, voucher_service):
    """Initialize all application routes"""

    @app.route("/")
    def root():
        return jsonify({"message": "MikroTik Voucher Tracker API"})

    init_vouchers_routes(app, voucher_service)
    init_profiles_routes(app, database_service, mikrotik_manager)
    init_users_routes(app, database_service, mikrotik_manager)
    init_financial_routes(app, database_service, mikrotik_manager)
    init_system_routes(app, mikrotik_manager)
    init_pricing_routes(app, database_service)


# ===========================================================
# APPLICATION FACTORY
# ===========================================================
def create_app():
    global socketio

    app = Flask(__name__)

    # Load configuration
    config = Config()

    # CORS
    CORS(app, origins=config.CORS_ORIGINS)

    # SocketIO
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

    # Services
    database_service = DatabaseService(config)
    mikrotik_manager = MikroTikManager(config)
    voucher_service = VoucherService(config, database_service, mikrotik_manager)
    monitoring_service = MonitoringService(
        database_service, mikrotik_manager, voucher_service
    )

    # Initialize database
    database_service.init_db()

    # Init routes
    _initialize_routes(app, database_service, mikrotik_manager, voucher_service)

    # Start background monitoring
    monitoring_service.start_monitoring()

    # Store services in app config
    app.config["database_service"] = database_service
    app.config["mikrotik_manager"] = mikrotik_manager
    app.config["voucher_service"] = voucher_service
    app.config["monitoring_service"] = monitoring_service

    return app


# ===========================================================
# START SERVER
# ===========================================================
if __name__ == "__main__":
    config = Config()

    app = create_app()

    host = config.FLASK_HOST
    port = config.FLASK_PORT
    debug = config.FLASK_DEBUG

    # USE SOCKETIO TO RUN IT
    socketio.run(app, host=host, port=port, debug=debug)
