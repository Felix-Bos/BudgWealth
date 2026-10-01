#!/usr/bin/env bash
#
# Construit l'application BudgWealth (macOS) après une modification du code.
#
#   ./build_app.sh
#
# Résultat : dist/BudgWealth.app — à double-cliquer ou à distribuer.
#
# Relance-le à chaque fois que tu changes le code, les templates ou le CSS.

set -euo pipefail

cd "$(dirname "$0")"

# --- Python à utiliser -----------------------------------------------------
# On prend le Python de l'environnement actif (conda ou venv) : active ton
# environnement avant de lancer ce script. Surchargeable :  PYTHON=/chemin ./build_app.sh
if [ -z "${PYTHON:-}" ]; then
    if command -v python >/dev/null 2>&1; then
        PYTHON="$(command -v python)"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON="$(command -v python3)"
    else
        echo "Python introuvable. Active ton environnement, ou :  PYTHON=/chemin/vers/python ./build_app.sh"
        exit 1
    fi
fi

echo "==> Python : $PYTHON ($("$PYTHON" --version 2>&1))"

# --- Dépendances de build (installées si absentes) -------------------------
echo "==> Vérification des outils d'empaquetage…"
"$PYTHON" - <<'PYEOF' || "$PYTHON" -m pip install --quiet pyinstaller whitenoise waitress
import importlib.util as u
import sys
missing = [m for m in ("PyInstaller", "whitenoise", "waitress") if u.find_spec(m) is None]
sys.exit(1 if missing else 0)
PYEOF

# --- Tests (sécurité avant de figer une version) ---------------------------
echo "==> Tests…"
"$PYTHON" manage.py test finance

# --- Fichiers statiques (avec manifeste) -----------------------------------
echo "==> Préparation des fichiers statiques…"
"$PYTHON" build_app.py

# --- Empaquetage -----------------------------------------------------------
echo "==> Construction de l'application (peut prendre 1 à 2 minutes)…"
rm -rf build dist
"$PYTHON" -m PyInstaller budgwealth.spec --noconfirm --clean

echo ""
echo "✅ Terminé : dist/BudgWealth.app"
echo "   Double-clique dessus, ou glisse-le dans /Applications."
