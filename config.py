import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    """
    Application Configuration Class
    Loads settings from environment variables with fallback defaults
    """
    
    # ============================================================
    # FLASK APP CONFIGURATION
    # ============================================================
    
    # Security — በምስሉ ላይ በሰጠኸው መረጃ መሰረት ተስተካክሏል
    SECRET_KEY = os.environ.get('SECRET_KEY', '@99Nameallah')
    
    # Debug mode - Enable for development, disable for production
    DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'
    
    # Host and Port configuration
    HOST = os.environ.get('HOST', '0.0.0.0')
    PORT = int(os.environ.get('PORT', 5000))
    
    # ============================================================
    # FILE UPLOAD CONFIGURATION
    # ============================================================
    
    # Upload folders
    UPLOAD_FOLDER = 'static/uploads'
    PRODUCT_UPLOAD_FOLDER = 'static/uploads/products'
    AD_UPLOAD_FOLDER = 'static/uploads/ads'
    
    # Maximum file size (16MB)
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
    
    # Allowed file extensions for uploads
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'webm', 'mov'}
    
    # Image-specific settings
    MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB for images
    ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    
    # Video-specific settings
    MAX_VIDEO_SIZE = 16 * 1024 * 1024  # 16MB for videos
    ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'webm', 'mov'}
    
    # ============================================================
    # BUSINESS CONFIGURATION
    # ============================================================
    
    # WhatsApp Business Integration
    WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '251906020606')
    
    # Admin Authentication
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '1234')
    
    # Shipping Configuration
    FREE_SHIPPING_THRESHOLD = int(os.environ.get('FREE_SHIPPING_THRESHOLD', 5000))
    SHIPPING_COST = int(os.environ.get('SHIPPING_COST', 200))
    
    # Currency Settings
    CURRENCY = os.environ.get('CURRENCY', 'ETB')
    CURRENCY_SYMBOL = os.environ.get('CURRENCY_SYMBOL', 'ETB')
    CURRENCY_POSITION = os.environ.get('CURRENCY_POSITION', 'after')  # 'before' or 'after'
    
    # Tax Configuration (if applicable)
    TAX_RATE = float(os.environ.get('TAX_RATE', 0))  # 0% by default
    TAX_ENABLED = os.environ.get('TAX_ENABLED', 'False').lower() == 'true'
    
    # ============================================================
    # DATABASE CONFIGURATION
    # ============================================================
    
    # PostgreSQL connection string — በሰጠኸው ፓስዎርድ እና ፖርት ተስተካክሏል
    DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres:admin123@localhost:5432/ethiosadat_db')

    # Kept for backwards-compatibility (no longer used for SQLite)
    DATABASE_PATH = os.environ.get('DATABASE_PATH', 'database/ethiosadat.db')

    @staticmethod
    def ensure_db_directory():
        db_dir = os.path.dirname(Config.DATABASE_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
    
    # ============================================================
    # LANGUAGE & LOCALIZATION
    # ============================================================
    
    # Default language (Amharic)
    DEFAULT_LANGUAGE = os.environ.get('DEFAULT_LANGUAGE', 'am')
    
    # Supported languages
    SUPPORTED_LANGUAGES = ['am', 'en', 'ar']
    
    # Language names for display
    LANGUAGE_NAMES = {
        'am': 'አማርኛ',
        'en': 'English',
        'ar': 'العربية'
    }
    
    # ============================================================
    # PAGINATION CONFIGURATION
    # ============================================================
    
    # Customer-facing pagination
    PRODUCTS_PER_PAGE = int(os.environ.get('PRODUCTS_PER_PAGE', 12))
    
    # Admin panel pagination
    ADMIN_PRODUCTS_PER_PAGE = int(os.environ.get('ADMIN_PRODUCTS_PER_PAGE', 20))
    ADMIN_ORDERS_PER_PAGE = int(os.environ.get('ADMIN_ORDERS_PER_PAGE', 20))
    ADMIN_ADS_PER_PAGE = int(os.environ.get('ADMIN_ADS_PER_PAGE', 20))
    
    # ============================================================
    # CACHE CONFIGURATION
    # ============================================================
    
    # Cache timeout in seconds
    CACHE_TIMEOUT = int(os.environ.get('CACHE_TIMEOUT', 3600))  # 1 hour
    
    # Static files cache
    STATIC_CACHE_TIMEOUT = int(os.environ.get('STATIC_CACHE_TIMEOUT', 86400))  # 24 hours
    
    # ============================================================
    # SESSION CONFIGURATION
    # ============================================================
    
    # Session lifetime (in seconds)
    SESSION_LIFETIME = int(os.environ.get('SESSION_LIFETIME', 86400))  # 24 hours
    
    # Session cookie settings
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'False').lower() == 'true'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # ============================================================
    # LOGGING CONFIGURATION
    # ============================================================
    
    # Log level
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    
    # Log file path
    LOG_FILE = os.environ.get('LOG_FILE', 'logs/app.log')
    
    # Maximum log file size (in bytes, 10MB default)
    LOG_MAX_BYTES = int(os.environ.get('LOG_MAX_BYTES', 10 * 1024 * 1024))
    
    # Number of backup log files to keep
    LOG_BACKUP_COUNT = int(os.environ.get('LOG_BACKUP_COUNT', 5))
    
    # ============================================================
    # MAINTENANCE MODE
    # ============================================================
    
    MAINTENANCE_MODE = os.environ.get('MAINTENANCE_MODE', 'False').lower() == 'true'
    MAINTENANCE_MESSAGE = os.environ.get('MAINTENANCE_MESSAGE', 'Site is under maintenance. Please check back soon.')
    
    # ============================================================
    # EMAIL CONFIGURATION (Optional - for future use)
    # ============================================================
    
    MAIL_ENABLED = os.environ.get('MAIL_ENABLED', 'False').lower() == 'true'
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'True').lower() == 'true'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME', '')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@ethiosadat.com')
    
    # ============================================================
    # PAYMENT CONFIGURATION (Optional - for future use)
    # ============================================================
    
    # Chapa Payment Gateway (Ethiopian)
    CHAPA_ENABLED = os.environ.get('CHAPA_ENABLED', 'False').lower() == 'true'
    CHAPA_SECRET_KEY = os.environ.get('CHAPA_SECRET_KEY', '')
    CHAPA_PUBLIC_KEY = os.environ.get('CHAPA_PUBLIC_KEY', '')
    
    # Telebirr (Ethiopian mobile money)
    TELEBIRR_ENABLED = os.environ.get('TELEBIRR_ENABLED', 'False').lower() == 'true'
    TELEBIRR_APP_ID = os.environ.get('TELEBIRR_APP_ID', '')
    TELEBIRR_APP_KEY = os.environ.get('TELEBIRR_APP_KEY', '')
    
    # ============================================================
    # CORS CONFIGURATION (for API)
    # ============================================================
    
    CORS_ENABLED = os.environ.get('CORS_ENABLED', 'False').lower() == 'true'
    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', '*').split(',')
    
    # ============================================================
    # UTILITY METHODS
    # ============================================================
    
    @classmethod
    def init_app(cls, app):
        """Initialize application with configuration"""
        # Ensure upload directories exist
        os.makedirs(cls.UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(cls.PRODUCT_UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(cls.AD_UPLOAD_FOLDER, exist_ok=True)
        
        # Ensure database directory exists
        cls.ensure_db_directory()
        
        # Ensure logs directory exists
        log_dir = os.path.dirname(cls.LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        
        # Apply security settings
        if app is not None and not cls.DEBUG:
            app.config.update(
                SESSION_COOKIE_SECURE=cls.SESSION_COOKIE_SECURE,
                SESSION_COOKIE_HTTPONLY=cls.SESSION_COOKIE_HTTPONLY,
                SESSION_COOKIE_SAMESITE=cls.SESSION_COOKIE_SAMESITE
            )
    
    @classmethod
    def is_allowed_file(cls, filename):
        """Check if file extension is allowed"""
        if not filename or '.' not in filename:
            return False
        ext = filename.rsplit('.', 1)[1].lower()
        return ext in cls.ALLOWED_EXTENSIONS
    
    @classmethod
    def is_allowed_image(cls, filename):
        """Check if file is an allowed image type"""
        if not filename or '.' not in filename:
            return False
        ext = filename.rsplit('.', 1)[1].lower()
        return ext in cls.ALLOWED_IMAGE_EXTENSIONS
    
    @classmethod
    def is_allowed_video(cls, filename):
        """Check if file is an allowed video type"""
        if not filename or '.' not in filename:
            return False
        ext = filename.rsplit('.', 1)[1].lower()
        return ext in cls.ALLOWED_VIDEO_EXTENSIONS
    
    @classmethod
    def get_shipping_cost(cls, subtotal):
        """Calculate shipping cost based on subtotal"""
        if subtotal >= cls.FREE_SHIPPING_THRESHOLD:
            return 0
        return cls.SHIPPING_COST
    
    @classmethod
    def get_total_with_shipping(cls, subtotal):
        """Calculate total including shipping"""
        return subtotal + cls.get_shipping_cost(subtotal)
    
    @classmethod
    def get_total_with_tax(cls, subtotal):
        """Calculate total including tax (if enabled)"""
        total = subtotal
        if cls.TAX_ENABLED:
            total += subtotal * cls.TAX_RATE
        return total
    
    @classmethod
    def display_price(cls, price):
        """Format price with currency symbol"""
        if cls.CURRENCY_POSITION == 'before':
            return f"{cls.CURRENCY_SYMBOL} {price:,.0f}"
        else:
            return f"{price:,.0f} {cls.CURRENCY_SYMBOL}"
    
    @classmethod
    def get_config_dict(cls):
        """Return configuration as dictionary for debugging"""
        # Don't expose sensitive information
        sensitive_keys = ['SECRET_KEY', 'ADMIN_PASSWORD', 'MAIL_PASSWORD', 
                         'CHAPA_SECRET_KEY', 'TELEBIRR_APP_KEY']
        
        config_dict = {}
        for key in dir(cls):
            if not key.startswith('_') and not callable(getattr(cls, key)):
                if key not in sensitive_keys:
                    config_dict[key] = getattr(cls, key)
                else:
                    config_dict[key] = '********'
        
        return config_dict
    
    @classmethod
    def print_config(cls):
        """Print current configuration (for debugging)"""
        print("\n" + "=" * 60)
        print("📋 ETHIOSADAT CONFIGURATION")
        print("=" * 60)
        for key, value in cls.get_config_dict().items():
            print(f"   {key}: {value}")
        print("=" * 60 + "\n")


# ============================================================
# DEVELOPMENT vs PRODUCTION CONFIGURATIONS
# ============================================================

class DevelopmentConfig(Config):
    """Development environment configuration"""
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    MAINTENANCE_MODE = False


class ProductionConfig(Config):
    """Production environment configuration"""
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    MAINTENANCE_MODE = os.environ.get('MAINTENANCE_MODE', 'False').lower() == 'true'
    
    # Production security
    @classmethod
    def init_app(cls, app):
        super().init_app(app)
        
        # Production-specific settings
        if not cls.SECRET_KEY or cls.SECRET_KEY == 'ethiosadat_default_secret_key_2026':
            import secrets
            cls.SECRET_KEY = secrets.token_hex(32)
            if app:
                app.logger.warning("Using auto-generated SECRET_KEY. Set a permanent key in production!")


class TestingConfig(Config):
    """Testing environment configuration"""
    TESTING = True
    DEBUG = True
    DATABASE_PATH = 'database/test.db'
    MAINTENANCE_MODE = False


# ============================================================
# CONFIGURATION SELECTOR
# ============================================================

# Get environment from FLASK_ENV variable
FLASK_ENV = os.environ.get('FLASK_ENV', 'development')

config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig
}

# Select appropriate configuration
AppConfig = config_map.get(FLASK_ENV, DevelopmentConfig)

# Ensure configuration is initialized when imported
if __name__ != '__main__':
    # Initialize basic directories on import
    AppConfig.init_app(None)