import eventlet

eventlet.monkey_patch()  # MUST BE FIRST LINE

import logging
import os
import time
from datetime import datetime, timedelta
from flask import Flask, jsonify, g
from flask_cors import CORS
from flask_socketio import SocketIO

from config import Config
from services import (
    DatabaseService,
    MikroTikManager,
    VoucherService,
    MonitoringService,
    AuthService,
)
from services.auth_service import SubscriptionService  # Import directly
from routes import (
    init_vouchers_routes,
    init_profiles_routes,
    init_users_routes,
    init_financial_routes,
    init_system_routes,
    init_pricing_routes,
    init_auth_routes,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

socketio = None


def _initialize_routes(
    app,
    database_service,
    mikrotik_manager,
    voucher_service,
    auth_service,
    subscription_service,
):
    """Initialize all application routes"""

    @app.route("/")
    def root():
        return jsonify(
            {
                "message": "MikroTik Voucher Tracker API",
                "version": "1.0.0",
                "status": "running",
            }
        )

    @app.route("/health")
    def health_check():
        """Comprehensive health check endpoint"""
        try:
            # Test database connection
            db_status = (
                "healthy"
                if database_service.execute_query("SELECT 1", fetch_one=True)
                else "unhealthy"
            )

            # Test MikroTik connection
            mikrotik_status = "unknown"
            try:
                system_info = mikrotik_manager.get_system_info()
                mikrotik_status = "healthy" if system_info else "unhealthy"
            except Exception as mt_err:
                logger.warning(f"MikroTik health check failed: {mt_err}")
                mikrotik_status = "unhealthy"

            # Check monitoring service threads
            monitoring_status = "unknown"
            try:
                monitoring_svc = app.config.get("monitoring_service")
                if monitoring_svc:
                    threads_alive = all(
                        t.is_alive() for t in monitoring_svc._threads.values()
                    ) if monitoring_svc._threads else False
                    monitoring_status = "healthy" if threads_alive else "degraded"
            except Exception as mon_err:
                logger.warning(f"Monitoring health check failed: {mon_err}")
                monitoring_status = "unhealthy"

            overall_healthy = (
                db_status == "healthy" and 
                mikrotik_status == "healthy" and 
                monitoring_status in ("healthy", "unknown")
            )

            return jsonify(
                {
                    "success": overall_healthy,
                    "status": "service is running" if overall_healthy else "degraded",
                    "services": {
                        "database": db_status,
                        "mikrotik": mikrotik_status,
                        "monitoring": monitoring_status,
                        "authentication": "healthy",
                        "voucher_service": "healthy",
                    },
                    "timestamp": time.time(),
                }
            )
        except Exception as e:
            logger.error(f"Health check failed: {str(e)}")
            return (
                jsonify(
                    {
                        "success": False,
                        "status": "service error",
                        "error": "Health check failed",
                    }
                ),
                500,
            )

    # Initialize all route modules
    init_vouchers_routes(app, voucher_service)
    init_profiles_routes(app, database_service, mikrotik_manager)
    init_users_routes(app, database_service, mikrotik_manager, auth_service)
    init_financial_routes(app, database_service, mikrotik_manager)
    init_system_routes(app, mikrotik_manager)
    init_pricing_routes(app, database_service)
    init_auth_routes(
        app, database_service, mikrotik_manager, auth_service, subscription_service
    )


def create_app():
    global socketio

    app = Flask(__name__)

    # Load configuration
    config = Config()

    # Apply configuration to app
    app.config.from_object(config)

    # Security configurations
    app.config["SECRET_KEY"] = os.getenv(
        "APP_SECRET_KEY", "fallback-secret-key-change-in-production"
    )
    app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", app.config["SECRET_KEY"])

    # Initialize CORS with full configuration for preflight requests
    CORS(app, 
         origins=config.CORS_ORIGINS,
         supports_credentials=True,
         allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
         methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])

    # Initialize SocketIO
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

    # Initialize services
    logger.info("Initializing database service...")
    database_service = DatabaseService(config)

    logger.info("Initializing MikroTik manager...")
    mikrotik_manager = MikroTikManager(config)

    logger.info("Initializing voucher service...")
    voucher_service = VoucherService(config, database_service, mikrotik_manager)

    logger.info("Initializing authentication services...")
    auth_service = AuthService()
    subscription_service = SubscriptionService()

    logger.info("Initializing monitoring service...")
    monitoring_service = MonitoringService(
        database_service, mikrotik_manager, voucher_service, socketio
    )

    # Initialize database
    logger.info("Initializing database...")
    try:
        database_service.init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {str(e)}")
        raise

    # Initialize routes
    logger.info("Initializing routes...")
    _initialize_routes(
        app,
        database_service,
        mikrotik_manager,
        voucher_service,
        auth_service,
        subscription_service,
    )

    # Initialize middleware
    @app.before_request
    def before_request():
        g.database_service = database_service

    # Start monitoring service
    logger.info("Starting monitoring service...")
    try:
        monitoring_service.start_monitoring()
        logger.info("Monitoring service started successfully")
    except Exception as e:
        logger.error(f"Failed to start monitoring service: {str(e)}")

    # Store services in app config
    app.config["database_service"] = database_service
    app.config["mikrotik_manager"] = mikrotik_manager
    app.config["voucher_service"] = voucher_service
    app.config["monitoring_service"] = monitoring_service
    app.config["auth_service"] = auth_service
    app.config["subscription_service"] = subscription_service
    app.config["socketio"] = socketio

    logger.info("Application initialization completed successfully")
    return app


if __name__ == "__main__":
    config = Config()
    app = create_app()

    host = config.FLASK_HOST
    port = config.FLASK_PORT
    debug = config.FLASK_DEBUG

    logger.info(f"Starting server on {host}:{port} (debug: {debug})")
    socketio.run(app, host=host, port=port, debug=debug)
