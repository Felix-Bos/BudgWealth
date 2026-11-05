import os
import sys
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

# Mode "application empaquetée" (PyInstaller) : le code est en lecture seule,
# donc la base et les fichiers générés vont dans le dossier personnel de l'utilisateur.
IS_PACKAGED = getattr(sys, "frozen", False)

def _default_data_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BudgWealth"
    if sys.platform.startswith("win"):
        return Path(os.getenv("APPDATA", Path.home())) / "BudgWealth"
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "BudgWealth"


# Une seule base par utilisateur de l'ordinateur : l'app est 100 % locale.
# Dev et application empaquetée partagent le même dossier de données
# (BUDGWEALTH_DATA_DIR permet d'en choisir un autre, par exemple pour tester).
DATA_DIR = Path(os.getenv("BUDGWEALTH_DATA_DIR") or _default_data_dir()).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR = DATA_DIR / "backups"

if IS_PACKAGED:
    # Ressources en lecture seule dépaquetées par PyInstaller
    BUNDLE_DIR = Path(sys._MEIPASS)
    FRONTEND_DIR = BUNDLE_DIR / "frontend"
else:
    load_dotenv(BASE_DIR / ".env")
load_dotenv(DATA_DIR / ".env")

# Sauvegarde automatique quotidienne (désactivée pendant les tests)
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
AUTO_BACKUP = not TESTING and os.getenv("BUDGWEALTH_AUTO_BACKUP", "1") != "0"
AUTO_BACKUP_KEEP = 14


def _get_or_create_secret_key():
    """Clé propre à chaque installation, générée au premier lancement et
    conservée dans le dossier de données (SECRET_KEY la remplace)."""
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    key_file = DATA_DIR / "secret_key.txt"
    if key_file.exists():
        return key_file.read_text().strip()
    from django.core.management.utils import get_random_secret_key
    key = get_random_secret_key()
    key_file.write_text(key)
    return key


SECRET_KEY = _get_or_create_secret_key()

# App empaquetée : tout est local, DEBUG sert à voir les erreurs éventuelles
DEBUG = os.getenv("DEBUG", "False").lower() in {"1", "true", "yes", "on"}

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

# App empaquetée : le serveur local écoute sur un port variable de 127.0.0.1
if IS_PACKAGED:
    ALLOWED_HOSTS = ["127.0.0.1", "localhost"]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "finance",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Sert les fichiers statiques sans serveur de dev (utile pour l'app empaquetée)
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Langue choisie dans les paramètres (français par défaut)
    "finance.middleware.LanguageCookieMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Une sauvegarde de la base par jour, au premier passage
    "finance.middleware.DailyBackupMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [FRONTEND_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "finance.context_processors.quick_add_transaction_form",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600,
            ssl_require=DATABASE_URL.startswith("postgres"),
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": DATA_DIR / "db.sqlite3",
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "fr"

# Langues de l'interface (sélecteur dans Paramètres › Général). Les textes
# sont écrits en français dans le code ; l'anglais est dans locale/en.
LANGUAGES = [
    ("fr", "Français"),
    ("en", "English"),
]
LOCALE_PATHS = [(BUNDLE_DIR if IS_PACKAGED else BASE_DIR) / "locale"]
# Le choix de langue est gardé un an
LANGUAGE_COOKIE_AGE = 365 * 24 * 60 * 60

TIME_ZONE = "Europe/Paris"

USE_I18N = True

USE_TZ = True


STATIC_URL = "static/"
STATICFILES_DIRS = [FRONTEND_DIR / "static"]
STATIC_ROOT = (BUNDLE_DIR / "staticfiles") if IS_PACKAGED else (BASE_DIR / "staticfiles")
# En app empaquetée seulement : statiques compressés et avec empreinte
# (nécessite collectstatic, fait au build). En dev, WhiteNoise sert
# directement depuis STATICFILES_DIRS, sans collectstatic.
if IS_PACKAGED:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }
else:
    WHITENOISE_USE_FINDERS = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "finance:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
