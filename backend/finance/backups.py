"""
Sauvegardes de la base locale (SQLite).

  - automatique : une par jour, au premier passage dans l'app ; on garde
    les AUTO_BACKUP_KEEP plus récentes ;
  - avant une mise à jour du schéma (lanceur, avant les migrations) ;
  - à la demande, depuis Paramètres → Sauvegarde ;
  - restauration : la base actuelle est d'abord sauvegardée, puis remplacée
    par la copie choisie (ou un fichier .sqlite3 importé).

Les copies sont faites avec l'API de sauvegarde de SQLite : elles restent
cohérentes même si l'app écrit au même moment.
"""

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connection

AUTO = "auto"
MANUAL = "manuelle"
BEFORE_UPDATE = "avant-mise-a-jour"
BEFORE_RESTORE = "avant-restauration"
LABELS = (AUTO, MANUAL, BEFORE_UPDATE, BEFORE_RESTORE)
# Combien de copies garder par type (None = toutes)
KEEP = {AUTO: None, BEFORE_UPDATE: 5, BEFORE_RESTORE: 5, MANUAL: None}

NAME_RE = re.compile(r"^budgwealth-(?P<stamp>\d{8}-\d{6})-(?P<label>[a-z-]+)\.sqlite3$")


class BackupError(Exception):
    pass


def backup_dir():
    path = Path(settings.BACKUP_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_sqlite():
    return settings.DATABASES["default"]["ENGINE"].endswith("sqlite3")


def create_backup(label=MANUAL, now=None):
    """Copie la base dans le dossier des sauvegardes. Renvoie le chemin."""

    if not _is_sqlite():
        raise BackupError("Sauvegarde possible uniquement avec la base locale SQLite.")
    if label not in LABELS:
        raise BackupError(f"Type de sauvegarde inconnu : {label}")
    now = now or datetime.now()
    target = backup_dir() / f"budgwealth-{now:%Y%m%d-%H%M%S}-{label}.sqlite3"
    connection.ensure_connection()
    destination = sqlite3.connect(target)
    try:
        connection.connection.backup(destination)
    finally:
        destination.close()
    prune()
    return target


def list_backups():
    """Sauvegardes du dossier, de la plus récente à la plus ancienne."""

    rows = []
    for path in backup_dir().glob("budgwealth-*.sqlite3"):
        match = NAME_RE.match(path.name)
        if not match:
            continue
        rows.append({
            "name": path.name,
            "path": path,
            "label": match["label"],
            "created": datetime.strptime(match["stamp"], "%Y%m%d-%H%M%S"),
            "size": path.stat().st_size,
        })
    rows.sort(key=lambda row: row["created"], reverse=True)
    return rows


def prune():
    keep = dict(KEEP, **{AUTO: settings.AUTO_BACKUP_KEEP})
    seen = {}
    for row in list_backups():
        seen[row["label"]] = seen.get(row["label"], 0) + 1
        limit = keep.get(row["label"])
        if limit is not None and seen[row["label"]] > limit:
            row["path"].unlink(missing_ok=True)


def backup_path(name):
    """Chemin d'une sauvegarde à partir de son nom (pas de chemin arbitraire)."""

    if not NAME_RE.match(name or ""):
        raise BackupError("Sauvegarde introuvable.")
    path = backup_dir() / name
    if not path.exists():
        raise BackupError("Sauvegarde introuvable.")
    return path


def last_backup_date(label=AUTO):
    for row in list_backups():
        if row["label"] == label:
            return row["created"].date()
    return None


def backup_if_due(today=None):
    """Sauvegarde automatique si aucune n'a été faite aujourd'hui."""

    today = today or datetime.now().date()
    if not _is_sqlite():
        return None
    if last_backup_date(AUTO) == today:
        return None
    return create_backup(AUTO)


def check_backup_file(path):
    """Vérifie qu'un fichier est bien une base BudgWealth."""

    try:
        with open(path, "rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise BackupError("Ce fichier n'est pas une sauvegarde BudgWealth.")
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            source.close()
    except sqlite3.DatabaseError as error:
        raise BackupError("Ce fichier n'est pas une sauvegarde BudgWealth.") from error
    if not {"django_migrations", "finance_transaction", "auth_user"} <= tables:
        raise BackupError("Ce fichier n'est pas une sauvegarde BudgWealth.")


def restore_backup(path):
    """Remplace la base par la sauvegarde `path` (la base actuelle est
    sauvegardée d'abord). Les migrations éventuelles sont appliquées ensuite."""

    path = Path(path)
    check_backup_file(path)
    safety = create_backup(BEFORE_RESTORE)
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        connection.ensure_connection()
        source.backup(connection.connection)
    finally:
        source.close()

    # Une sauvegarde plus ancienne peut avoir un schéma en retard
    from django.core.management import call_command
    call_command("migrate", interactive=False, verbosity=0)
    return safety
