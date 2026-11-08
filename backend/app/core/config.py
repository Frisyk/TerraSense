import os
import json
from pathlib import Path
from urllib.parse import urlparse, unquote

UNIT_SETTINGS = {
    "valid_units": ["concession", "grid"],  # List of valid units
    "default_unit": "concession"  # Optional: Default unit
}

KOBO_API_TOKEN = os.getenv("KOBO_API_TOKEN", "d0794054916d55463e7027969684a61e052b4073")
TARGET_USERNAME = os.getenv("TARGET_USERNAME", "tesdev")
TARGET_PASSWORD = os.getenv("TARGET_PASSWORD", "Okokokok1!")

# ---------------------------------------------------------------------------
# Database Configuration (Supabase & PostGIS compatible)
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")

# Resolve default values, parsing DATABASE_URL if provided
if DATABASE_URL:
    try:
        _parsed = urlparse(DATABASE_URL)
        _def_user = unquote(_parsed.username) if _parsed.username else "postgres"
        _def_password = unquote(_parsed.password) if _parsed.password else ""
        _def_host = _parsed.hostname or "localhost"
        _def_port = str(_parsed.port or 5432)
        _def_db = _parsed.path.lstrip("/") or "postgres"
    except Exception:
        _def_user = os.getenv("POSTGRES_USER", "postgres")
        _def_password = os.getenv("POSTGRES_PASSWORD", "postgres")
        _def_host = os.getenv("DB_HOST", "db")
        _def_port = str(os.getenv("POSTGRES_PORT", "5432"))
        _def_db = os.getenv("POSTGRES_DB", "geonode_data")
else:
    _def_user = os.getenv("POSTGRES_USER", "postgres")
    _def_password = os.getenv("POSTGRES_PASSWORD", "postgres")
    _def_host = os.getenv("DB_HOST", "db")
    _def_port = str(os.getenv("POSTGRES_PORT", "5432"))
    _def_db = os.getenv("POSTGRES_DB", "geonode_data")

DATABASE_CONFIG = {
    "user": os.getenv("POSTGRES_USER", _def_user),
    "password": os.getenv("POSTGRES_PASSWORD", _def_password),
    "host": os.getenv("DB_HOST", _def_host),
    "port": str(os.getenv("POSTGRES_PORT", _def_port)),
    "database": os.getenv("DB_NAME", _def_db),
}

# Geonode / Spatial database configuration (PostGIS)
SPATIAL_DATABASE_CONFIG = {
    "user": os.getenv("GEONODE_DB_USER", os.getenv("POSTGRES_USER", _def_user)),
    "password": os.getenv("GEONODE_DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", _def_password)),
    "host": os.getenv("GEONODE_DB_HOST", os.getenv("DB_HOST", _def_host)),
    "port": str(os.getenv("GEONODE_DB_PORT", os.getenv("POSTGRES_PORT", _def_port))),
    "database": os.getenv("GEONODE_DB_NAME", os.getenv("POSTGRES_DB", _def_db)),
}

# OAuth2 configuration for legacy/external services
OAUTH2_CONFIG = {
    "client_id": os.getenv("OAUTH2_CLIENT_ID", "iO7oYJPgmmbxJ08SzLJ3KftRZrYMlX4Ur2iqPgNj"),
    "client_secret": os.getenv("OAUTH2_CLIENT_SECRET", "go3xUfAvWYSzNHg3LWY658rEfuhvdLX324joLqGGl1BBO3qpwfFDm4eAwwwzInDR3D7cFSXZOjvHJoJoiCcnm3GDgPZOtk04K406mEsdLuTjnvBfGlwbHRGPiDinZXz8"),
    "url": os.getenv("OAUTH2_URL", "https://data.fist.infini.asia/o/token/"),
    "app_key": os.getenv("APP_ENCRYPT_KEY", "L_kMdHJe8KHo9GZTWfFRw8BajAG8yKZH4zFs_7eTBZI="),
}

# OAuth2 configuration for INTEGRA-FIST Homepage
OAUTH_HOMEPAGE_CONFIG = {
    "client_id": os.getenv("HOMEPAGE_OAUTH_CLIENT_ID"),
    "client_secret": os.getenv("HOMEPAGE_OAUTH_CLIENT_SECRET"),
    "authorization_url": os.getenv("HOMEPAGE_OAUTH_AUTHORIZATION_URL", "http://localhost:3001/api/v1/oauth/authorize"),
    "token_url": os.getenv("HOMEPAGE_OAUTH_TOKEN_URL", "http://localhost:3001/api/v1/oauth/token"),
    "userinfo_url": os.getenv("HOMEPAGE_OAUTH_USERINFO_URL", "http://localhost:3001/api/v1/oauth/userinfo"),
    "validate_url": os.getenv("HOMEPAGE_OAUTH_VALIDATE_URL", "http://localhost:3001/api/v1/oauth/validate"),
}

async def get_email_config():
    """Fetches email configuration from the database."""
    try:
        from app.db import db_connect
    except ImportError:
        try:
            import backend.db_connect as db_connect
        except ImportError:
            import db_connect

    config = {
        "SMTP_HOST": await db_connect.get_setting_value('email.smtp_server') or "mail.infini.co.id",
        "SMTP_PORT": await db_connect.get_setting_value('email.smtp_port') and int(await db_connect.get_setting_value('email.smtp_port')) or 587,
        "SMTP_USER": await db_connect.get_setting_value('email.smtp_sender') or "mail@infini.co.id",
        "SMTP_PASSWORD": await db_connect.get_setting_value('email.smtp_password') or "Infini_2025#",
        "EMAIL_FROM": await db_connect.get_setting_value('email.from') or "mail@infini.co.id",
    }
    return config

# Google Earth Engine Configuration
def get_gee_config():
    """Load Google Earth Engine configuration from file"""
    try:
        project_root = Path(__file__).resolve().parents[3]
        gee_creds_path = project_root / "key" / "gee_credentials.json"
        
        if gee_creds_path.exists():
            with open(gee_creds_path, 'r') as f:
                config = json.load(f)
                
            # Resolve the service account key path
            key_path = project_root / "key" / "project-fist-ee-sa.json"
            config["service_account_key"] = str(key_path)
            
            return config
        else:
            # Fallback to environment variables if file doesn't exist
            return {
                "service_account": os.getenv("GEE_SERVICE_ACCOUNT", "fist-gee-sa@project-363319.iam.gserviceaccount.com"),
                "service_account_key": os.getenv("GEE_SERVICE_ACCOUNT_KEY", str(project_root / "key" / "project-fist-ee-sa.json"))
            }
    except Exception as e:
        print(f"Error loading GEE config: {e}")
        project_root = Path(__file__).resolve().parents[3]
        return {
            "service_account": "fist-gee-sa@project-363319.iam.gserviceaccount.com",
            "service_account_key": str(project_root / "key" / "project-fist-ee-sa.json")
        }

GEE_CONFIG = get_gee_config()

# Firebase Cloud Messaging (FCM) configuration
def get_fcm_config():
    """Load Firebase Admin SDK service account key path."""
    project_root = Path(__file__).resolve().parents[3]
    key_path = project_root / "key" / "Integrafist_firebase_cred.json"
    return {
        "credentials_path": os.getenv("FIREBASE_CREDENTIALS_PATH", str(key_path))
    }

FCM_CONFIG = get_fcm_config()

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "mail@infini.co.id")

GEONODE_LAYER_URL = os.getenv(
    "GEONODE_LAYER_URL",
    "https://data.fist.nusantaraclimate.co.id/api/v2/datasets"
)

# NIMBO configuration
NIMBO_CONFIG = {
    "token": os.getenv("NIMBO_API_KEY", "")
}

JWT_CONFIG = {
    "SECRET_KEY": os.getenv("SECRET_KEY", "8ff028fa7caf74bc50ac601f94500070e45265d2733f90b8d5442c07faf1ed2d"),
    "ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": 30 # 30 minutes
}

WEBHOOK_CONFIG = {
    "SECRET_KEY": os.getenv("WEBHOOK_SECRET_KEY", "iGFBj6tE7AiklhA9bZk127gouczpU7lKFY78hGxbneg=")
}

HOMEPAGE_CONFIG = {
    "BASE_URL": os.getenv("HOMEPAGE_URL", "http://host.docker.internal:3004"),
    "VERIFY_SSO_URL": os.getenv("HOMEPAGE_VERIFY_SSO_URL", "http://host.docker.internal:3004/api/v1/auth/sso"),
    "USERINFO_URL": os.getenv("HOMEPAGE_USERINFO_URL", "http://host.docker.internal:3004/api/v1/oauth/userinfo"),
    "ACCESS_LOG_STORE_URL": os.getenv("HOMEPAGE_ACCESS_LOG_STORE_URL", "http://host.docker.internal:3004/api/v1/dashboard/access-logs/store"),
    "ACCESS_LOG_API_KEY": os.getenv("HOMEPAGE_ACCESS_LOG_API_KEY", os.getenv("HOMEPAGE_API_KEY", "TH!51SD3VTOK3N")),
    "API_KEY": os.getenv("HOMEPAGE_ACCESS_LOG_API_KEY", os.getenv("HOMEPAGE_API_KEY", "TH!51SD3VTOK3N")),
    "SSO_TOKEN": os.getenv("HOMEPAGE_SSO_TOKEN", ""),
    "ENABLE_ACCESS_LOGS": os.getenv("ENABLE_HOMEPAGE_ACCESS_LOGS", "true").lower() == "true",
}

DASHBOARD_CONFIG = {
    "DASHBOARD_URL": os.getenv("DASHBOARD_URL", "https://dashboard.fist.infini.asia"),
    "DASHBOARD_API_URL": os.getenv("DASHBOARD_API_URL", "https://dashboard.fist.infini.asia/api"),
}

# Redis / RQ configuration
REDIS_CONFIG = {
    "host": os.getenv("REDIS_HOST", "redis"),
    "port": int(os.getenv("REDIS_PORT", "6379")),
    "db": int(os.getenv("REDIS_DB", "0")),
    "password": os.getenv("REDIS_PASSWORD", None),
    "url": os.getenv("REDIS_URL", "redis://redis:6379/0"),
    "connect_timeout": int(os.getenv("REDIS_CONNECT_TIMEOUT", "60")),
    "socket_timeout": int(os.getenv("REDIS_SOCKET_TIMEOUT", "60")),
    "socket_keepalive": os.getenv("REDIS_SOCKET_KEEPALIVE", "True").lower() == "true",
    "health_check_interval": int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL", "30")),
}

# Web Push Notification VAPID Keys
VAPID_CONFIG = {
    "VAPID_PUBLIC_KEY": os.getenv("VAPID_PUBLIC_KEY", "BJ_k-b1hT2p-QzI-gC24A4z4-z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z"),
    "VAPID_PRIVATE_KEY": os.getenv("VAPID_PRIVATE_KEY", "4-z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z8z8Y8q8Z"),
    "VAPID_ADMIN_EMAIL": os.getenv("VAPID_ADMIN_EMAIL", "mailto:admin@example.com")
}

COLOR_CONFIG = {
    "male": "#003366",
    "female": "#FFB6C1",
    "not_attending": "#C2A377",
    "elementary": "#B22222",
    "middle_school": "#1E90FF",
    "high_school": "#D2E0E4",
    "undergraduate": "#002366",
    "in_house_sanitary": "#4CAF50",
    "out_house_sanitary": "#FFC107",
    "agriculture_farmers": "#A9C470",
    "fisherman": "#64B5F6",
    "enterpreneur": "#82B341",
    "employee": "#8E44AD",
    "dayak": "#B7410E",     
    "melayu": "#D4A276",    
    "banjar": "#3A6B35",    
    "bugis": "#2C5D63",     
    "jawa": "#6F4E37",      
    "sunda": "#627C85",     
    "kutai": "#BC945C",     
    "madura": "#E4D5B7",    
    "others": "#A9714B",     
    "tionghoa": "#8B0000",
    "expected_people": "#48B3AF",
    "current_people": "#476EAE",
    "expected_woman": "#FFD5D5",
    "current_woman": "#FDAAAA"
}
