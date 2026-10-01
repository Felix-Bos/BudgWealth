"""
Prépare les fichiers statiques pour l'empaquetage de BudgWealth.

Lance collectstatic en mode "application empaquetée", pour que staticfiles/
contienne le manifeste (staticfiles.json) exigé par WhiteNoise dans l'app.

Appelé par build_app.sh ; pas besoin de le lancer à la main.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# On force le mode "empaqueté" le temps du collectstatic, pour générer
# le manifeste comme le fera l'application distribuée.
sys.path.insert(0, str(ROOT / "backend"))
sys.frozen = True
sys._MEIPASS = str(ROOT)
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

import django
from django.core.management import call_command

django.setup()
call_command("collectstatic", interactive=False, clear=True, verbosity=1)

manifest = ROOT / "staticfiles" / "staticfiles.json"
if not manifest.exists():
    sys.exit("Échec : staticfiles.json n'a pas été généré.")

print("Fichiers statiques prêts (avec manifeste).")
