"""
Synchronisation d'un mois avec la banque, exécutée en arrière-plan.

La vue crée un BankSync puis lance `run_bank_sync` dans un thread ; la page
interroge ensuite le statut du BankSync pour afficher l'avancement.

Chrome s'ouvre sur la machine qui fait tourner Django : cette fonctionnalité
suppose que l'application tourne en local sur l'ordinateur de l'utilisateur.
"""

import calendar
import logging
import shutil
import tempfile
import threading
from datetime import date

from django.db import close_old_connections, connection as db_connection
from django.utils import timezone

from .bank_import import import_operations
from .ca_csv import parse_ca_csv
from .ca_scraper import BankSyncError, download_ca_operations
from .models import BankConnection, BankSync, BankSyncKind, BankSyncStatus


logger = logging.getLogger(__name__)

# Un seul Chrome à la fois par utilisateur (le profil ne peut pas être ouvert deux fois)
_running_users = set()
_running_lock = threading.Lock()

# Le site ne propose que 12 mois d'historique en plus du mois en cours
MAX_MONTHS_BACK = 12


def sync_period(year, month, today=None):
    """
    Période à télécharger pour un mois : du 1er au dernier jour,
    ou jusqu'à aujourd'hui pour le mois en cours.
    """

    today = today or date.today()
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])

    return start, min(end, today)


def initial_period(today=None):
    """
    Import initial : du 1er du mois d'il y a 11 mois jusqu'à aujourd'hui,
    soit les 12 mois d'historique que la banque autorise (mois en cours inclus).
    """

    today = today or date.today()
    start_year = today.year
    start_month = today.month - (MAX_MONTHS_BACK - 1)
    while start_month <= 0:
        start_month += 12
        start_year -= 1

    return date(start_year, start_month, 1), today


def is_month_syncable(year, month, today=None):

    today = today or date.today()
    months_back = (today.year - year) * 12 + (today.month - month)

    return 0 <= months_back <= MAX_MONTHS_BACK


def start_bank_sync(user, year=None, month=None, kind=BankSyncKind.MONTH):
    """
    Crée le BankSync et lance la synchronisation en arrière-plan.
    Renvoie le BankSync (existant s'il y en a déjà un en cours).
    Pour un import initial (kind=INITIAL), year/month sont ignorés (mois en cours).
    """

    with _running_lock:

        if user.pk in _running_users:
            running = BankSync.objects.filter(user=user).first()
            if running and not running.is_finished:
                return running

        _running_users.add(user.pk)

    if kind == BankSyncKind.INITIAL:
        today = date.today()
        year, month = today.year, today.month

    sync = BankSync.objects.create(user=user, kind=kind, year=year, month=month)

    thread = threading.Thread(
        target=run_bank_sync,
        args=(sync.pk,),
        name=f"bank-sync-{sync.pk}",
        daemon=True,
    )
    thread.start()

    return sync


def run_bank_sync(sync_id):

    sync = BankSync.objects.select_related("user").get(pk=sync_id)
    user = sync.user

    def set_status(status, message=""):
        # Appelé depuis Playwright, dont la boucle asyncio interdit l'accès à la
        # base dans ce thread (SynchronousOnlyOperation) : on écrit depuis un
        # thread dédié, sans boucle asyncio.
        def save():
            try:
                BankSync.objects.filter(pk=sync.pk).update(status=status, message=message[:255])
            except Exception:
                logger.exception("Impossible d'enregistrer le statut de la synchronisation %s", sync_id)
            finally:
                db_connection.close()

        writer = threading.Thread(target=save)
        writer.start()
        writer.join()

    try:

        connection = BankConnection.objects.filter(user=user).first()
        if not connection:
            raise BankSyncError("Renseignez votre identifiant bancaire dans les paramètres.")

        if sync.kind == BankSyncKind.INITIAL:
            start, end = initial_period()
        else:
            start, end = sync_period(sync.year, sync.month)

        # Le relevé contient des données bancaires : il ne vit que le temps de l'import
        export_dir = tempfile.mkdtemp(prefix="ca_export_")

        try:
            csv_path = download_ca_operations(
                login=connection.login_id,
                postal_code=connection.postal_code,
                start=start,
                end=end,
                target_dir=export_dir,
                on_status=set_status,
            )

            set_status(BankSyncStatus.IMPORTING, "Import des opérations…")

            operations, _balances = parse_ca_csv(csv_path)
            result = import_operations(user, operations)

        finally:
            shutil.rmtree(export_dir, ignore_errors=True)

        connection.last_sync_at = timezone.now()
        update_fields = ["last_sync_at"]
        if sync.kind == BankSyncKind.INITIAL:
            connection.initial_import_done = True
            update_fields.append("initial_import_done")
        connection.save(update_fields=update_fields)

        sync.created_count = result.created
        sync.skipped_count = result.skipped
        sync.status = BankSyncStatus.DONE
        sync.message = ""

    except BankSyncError as error:

        sync.status = BankSyncStatus.ERROR
        sync.message = str(error)[:255]

    except Exception:

        logger.exception("Échec de la synchronisation bancaire %s", sync_id)
        sync.status = BankSyncStatus.ERROR
        sync.message = "Erreur inattendue pendant la synchronisation."

    finally:

        sync.finished_at = timezone.now()
        sync.save()

        with _running_lock:
            _running_users.discard(user.pk)

        close_old_connections()
