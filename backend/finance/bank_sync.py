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
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, connection as db_connection
from django.utils import timezone

from .bank_import import SAVINGS_ACCOUNT_RE, import_operations
from .ca_csv import mask_digits, outline_ca_csv, parse_account_card, parse_ca_csv
from .ca_scraper import BankSyncError, download_ca_operations
from .models import BankConnection, BankSync, BankSyncKind, BankSyncStatus
from .wealth import SECURITIES_ACCOUNT_RE, import_bank_balances


logger = logging.getLogger(__name__)

# Un seul Chrome à la fois par utilisateur (le profil ne peut pas être ouvert deux fois)
_running_users = set()
_running_lock = threading.Lock()

# Le site ne propose que 12 mois d'historique en plus du mois en cours
MAX_MONTHS_BACK = 12

# Plan anonymisé de la dernière synchronisation (voir write_diagnostic)
DIAGNOSTIC_FILE = "diagnostic_banque.txt"


def is_savings_label(label):
    return bool(SAVINGS_ACCOUNT_RE.search(label or "")) and not SECURITIES_ACCOUNT_RE.search(label or "")


def missing_savings_accounts(csv_path, card_texts):
    """
    Numéros des livrets cochés dans le formulaire mais absents du relevé :
    la banque n'y met pas un compte sans opération sur la période (un Livret
    Jeune ne bouge souvent qu'une fois par an, pour les intérêts). Le
    scraper les télécharge alors à nouveau, sur 12 mois, pour avoir leur solde.

    Appelé depuis le thread de Playwright : pas d'accès à la base ici.
    """

    _, balances = parse_ca_csv(csv_path)
    known = {balance.account_number for balance in balances}
    missing = []
    for text in card_texts:
        card = parse_account_card(text)
        if card and is_savings_label(card.account) and card.account_number not in known:
            missing.append(card.account_number)
    return missing


def download_balances(download):
    """Soldes du relevé, puis ceux des livrets pris dans les relevés complémentaires."""

    operations, balances = parse_ca_csv(download.path)
    known = {balance.account_number for balance in balances}
    for path in download.balance_paths:
        _, extra = parse_ca_csv(path)
        for balance in extra:
            if balance.account_number not in known:
                known.add(balance.account_number)
                balances.append(balance)
    return operations, balances


def _number_end(number):
    return f"n° …{number[-3:]}" if number else "sans numéro"


def write_diagnostic(download, balances, imported_numbers):
    """
    Plan anonymisé de ce que la banque a renvoyé (relevés CSV et cartes de
    comptes), réécrit à chaque synchronisation dans DIAGNOSTIC_FILE : sert à
    comprendre pourquoi un livret n'est pas relevé. Chiffres masqués, seuls
    les 3 derniers chiffres des numéros de compte restent.
    """

    lines = [f"Synchronisation du {timezone.localtime():%d/%m/%Y %H:%M}", ""]

    lines.append(f"Soldes lus : {len(balances)}")
    for balance in balances:
        state = "importé" if balance.account_number in imported_numbers else "ignoré (pas un livret)"
        lines.append(f"  - {balance.account} · {_number_end(balance.account_number)} → {state}")

    for title, path in [("Relevé de la période", download.path)] + [
        ("Relevé complémentaire (solde des livrets)", path) for path in download.balance_paths
    ]:
        lines += ["", f"{title}, hors opérations :"]
        lines += [f"  {line}" for line in outline_ca_csv(path)]

    lines += ["", f"Cartes de comptes du formulaire : {len(download.account_cards)}"]
    for index, text in enumerate(download.account_cards, 1):
        card = parse_account_card(text)
        state = "illisible" if card is None else f"{card.account}, {_number_end(card.account_number)}"
        flat = " | ".join(line.strip() for line in text.splitlines() if line.strip())
        lines.append(f"  [{index}] {state}")
        lines.append(f"      texte : {mask_digits(flat)[:300]}")

    Path(settings.DATA_DIR, DIAGNOSTIC_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def balances_summary(accounts):
    if not accounts:
        return ""
    return ("Soldes mis à jour : " + ", ".join(account.name for account in accounts) + ".")[:255]


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
            download = download_ca_operations(
                login=connection.login_id,
                postal_code=connection.postal_code,
                start=start,
                end=end,
                target_dir=export_dir,
                on_status=set_status,
                balance_accounts=missing_savings_accounts,
                balance_start=initial_period()[0],
            )

            set_status(BankSyncStatus.IMPORTING, "Import des opérations…")

            operations, balances = download_balances(download)
            result = import_operations(user, operations)
            # Les soldes des livrets alimentent le module Épargne
            updated_accounts = import_bank_balances(user, balances)

            try:
                write_diagnostic(download, balances, {account.bank_account_number for account in updated_accounts})
            except Exception:
                logger.exception("Diagnostic de synchronisation impossible à écrire")

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
        # Affiché avec le message de fin : on voit quels livrets ont suivi
        sync.message = balances_summary(updated_accounts)

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
