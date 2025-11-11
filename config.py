import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Database configuration
    DB_CONFIG = {
        'host': os.getenv('DB_HOST', 'localhost'),
        'port': os.getenv('DB_PORT', '5432'),
        'database': os.getenv('DB_NAME', 'voucher_system'),
        'user': os.getenv('DB_USER', 'postgres'),
        'password': os.getenv('DB_PASSWORD', '')
    }
    
    # MikroTik configuration
    MIKROTIK_CONFIG = {
        'host': os.getenv('MIKROTIK_HOST', '192.168.88.1'),
        'username': os.getenv('MIKROTIK_USERNAME', 'admin'),
        'password': os.getenv('MIKROTIK_PASSWORD', 'kaumelinen8')
    }
    
    # Flask configuration
    FLASK_HOST = os.getenv('FLASK_HOST', '0.0.0.0')
    FLASK_PORT = int(os.getenv('FLASK_PORT', 5000))
    FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'True').lower() == 'true'
    
    # CORS configuration
    CORS_ORIGINS = os.getenv('CORS_ORIGINS', 'http://localhost:5173,http://localhost:3000').split(',')
    
    # Voucher configuration
    VOUCHER_CONFIG = {
        '1d': {'length': 5, 'chars': 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'},
        '7d': {'length': 6, 'chars': 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'},
        '30d': {'length': 7, 'chars': 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'}
    }
    
    PDF_OUTPUT_DIR = "generated_vouchers"
    PDF_TEMPLATE_DIR = "templates"