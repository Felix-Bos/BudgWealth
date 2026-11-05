"""
Lanceur de l'application BudgWealth.

Démarre l'application en local puis ouvre le navigateur, pour que
l'utilisateur double-clique sur une icône sans rien installer. C'est ce
fichier qui est empaqueté par PyInstaller.

Lancement manuel (pour tester avant l'empaquetage) :
    python launcher.py
"""

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


HOST = "127.0.0.1"


def setup_python_path():
    """Rend le projet importable, en dev comme dans l'app empaquetée."""
    if getattr(sys, "frozen", False):
        # Dans l'app empaquetée, config/accounts/finance sont à la racine dépaquetée
        base = Path(sys._MEIPASS)
        sys.path.insert(0, str(base))
    else:
        base = Path(__file__).resolve().parent
        sys.path.insert(0, str(base / "backend"))


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def ensure_chromium():
    """
    Au premier lancement de l'app empaquetée, télécharge Chromium pour Playwright
    dans le dossier de données de l'utilisateur. En dev, ne fait rien : Chromium
    est déjà là (playwright install). Renvoie True si prêt, False en cas d'échec.
    """
    if not getattr(sys, "frozen", False):
        return True

    browsers_dir = Path.home() / "Library" / "Application Support" / "BudgWealth" / "browsers"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)

    # Un navigateur est déjà installé ?
    if browsers_dir.exists() and any(browsers_dir.glob("chromium-*")):
        return True

    print("Première installation : téléchargement du navigateur (une seule fois)…")
    browsers_dir.mkdir(parents=True, exist_ok=True)

    from playwright.__main__ import main as playwright_main

    argv = sys.argv
    try:
        sys.argv = ["playwright", "install", "chromium"]
        playwright_main()
    except SystemExit as exit_signal:
        if exit_signal.code not in (0, None):
            return False
    finally:
        sys.argv = argv

    return True


def prepare_django():
    """Configure Django et applique les migrations (crée la base au 1er lancement)."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    import django
    from django.core.management import call_command

    django.setup()

    # Mise à jour de l'app : sauvegarde de la base avant de changer son schéma
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if pending and executor.loader.applied_migrations:
        from finance.backups import BEFORE_UPDATE, create_backup
        try:
            create_backup(BEFORE_UPDATE)
        except Exception as error:  # la mise à jour ne doit pas bloquer le lancement
            print(f"Sauvegarde avant mise à jour impossible : {error}")

    call_command("migrate", interactive=False, verbosity=0)


def open_browser_when_ready(url):
    """Attend que le serveur réponde puis ouvre le navigateur."""
    for _ in range(100):
        try:
            with socket.create_connection((HOST, int(url.rsplit(":", 1)[1].rstrip("/"))), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    webbrowser.open(url)


def main():
    setup_python_path()
    ensure_chromium()
    prepare_django()

    port = find_free_port()
    url = f"http://{HOST}:{port}/"

    threading.Thread(target=open_browser_when_ready, args=(url,), daemon=True).start()

    print(f"BudgWealth est lancé : {url}")
    print("Gardez cette fenêtre ouverte tant que vous utilisez l'application.")

    from config.wsgi import application
    from waitress import serve

    # threads=6 : assez pour l'app + le suivi de synchronisation bancaire
    serve(application, host=HOST, port=port, threads=6)


if __name__ == "__main__":
    main()
