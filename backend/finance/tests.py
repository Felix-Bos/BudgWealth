import shutil
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .bank_import import import_operations
from .bank_sync import is_month_syncable, run_bank_sync, sync_period
from .ca_csv import parse_ca_csv
from .default_categories import create_default_categories
from .models import BankConnection, BankSync, BankSyncStatus, EntryType, Transaction, TransactionSource


# Relevé fictif au format de l'export Crédit Agricole (Windows-1252, libellés multi-lignes)
SAMPLE_EXPORT = """Téléchargement du 01/10/2026;


M.       DUPONT JEAN
Compte de Dépôt carte n° 11111111111;
Solde au 01/10/2026 1 234,56

Liste des opérations du compte entre le 01/09/2026 et le 30/09/2026;

Date;Libellé;Débit euros;Crédit euros;
30/09/2026;"Paiement par carte
X1234 Spotify P4776921FD S 30/09 - Spotify



";7,07;;
21/09/2026;"Paiement par carte
X1234 CARREFOUR MARKET BEZ 20/09 - Carrefour Market



";3,49;;
21/09/2026;"Paiement par carte
X1234 CARREFOUR MARKET BEZ 20/09 - Carrefour Market



";3,49;;
16/09/2026;"Virement émis
WEB M. DUPONT JEAN



";100,00;;
10/09/2026;"Virement en votre faveur
DE M.       DUPONT JEAN



";;40,00;
04/09/2026;"Virement en votre faveur
CAF DES HAUTS DE SEINE 46046018V - 8624116XDUP         082026ME



46046018V16";;149,43;
01/09/2026;"Paiement par carte
X1234 BOUTIQUE INCONNUE    31/08



";12,00;;







M.       DUPONT JEAN


Livret A carte n° 22222222222

Solde au 01/10/2026 5 000,00


Liste des opérations du compte entre le 01/09/2026 et le 30/09/2026;

Date;Libellé;Débit euros;Crédit euros;
16/09/2026;"Virement en votre faveur
DE M.       DUPONT JEAN



";;100,00;
"""


def write_sample_export(directory):
    path = Path(directory) / "export.csv"
    path.write_text(SAMPLE_EXPORT, encoding="cp1252")
    return path


class CreditAgricoleCsvTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_parses_operations_and_balances(self):
        operations, balances = parse_ca_csv(write_sample_export(self.tmp))

        self.assertEqual(len(operations), 8)
        self.assertEqual(
            [(b.account, b.balance) for b in balances],
            [("Compte de Dépôt", Decimal("1234.56")), ("Livret A", Decimal("5000.00"))],
        )

        spotify = operations[0]
        self.assertEqual(spotify.holder, "M. DUPONT JEAN")
        self.assertEqual(spotify.merchant, "Spotify")
        self.assertEqual(spotify.amount, Decimal("-7.07"))

        carrefour = operations[1]
        self.assertEqual(carrefour.booking_date, date(2026, 9, 21))
        self.assertEqual(carrefour.operation_date, date(2026, 9, 20))

        caf = operations[5]
        self.assertEqual(caf.merchant, "CAF DES HAUTS DE SEINE")
        self.assertEqual(caf.amount, Decimal("149.43"))

    def test_card_purchase_from_previous_month_keeps_its_date(self):
        operations, _ = parse_ca_csv(write_sample_export(self.tmp))

        self.assertEqual(operations[6].operation_date, date(2026, 8, 31))


class BankImportTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.user = get_user_model().objects.create_user("jean", password="x")
        create_default_categories(self.user)
        self.operations, _ = parse_ca_csv(write_sample_export(self.tmp))

    def test_imports_current_account_with_categories(self):
        result = import_operations(self.user, self.operations)

        # 8 opérations - 1 sur le livret - 1 retour depuis le livret
        self.assertEqual(result.created, 6)
        transactions = Transaction.objects.filter(user=self.user)
        self.assertTrue(all(t.source == TransactionSource.BANK for t in transactions))

        spotify = transactions.get(description="Spotify")
        self.assertEqual(spotify.transaction_type, EntryType.EXPENSE)
        self.assertEqual(spotify.amount, Decimal("7.07"))
        self.assertEqual(spotify.subcategory.name, "Abonnement multimédia")

        caf = transactions.get(description="CAF DES HAUTS DE SEINE")
        self.assertEqual(caf.transaction_type, EntryType.INCOME)
        self.assertEqual(caf.category.name, "Aides (CAF et autres)")

        unknown = transactions.get(description="BOUTIQUE INCONNUE")
        self.assertIsNone(unknown.category)

    def test_transfer_to_savings_is_a_savings_expense(self):
        import_operations(self.user, self.operations)

        saving = Transaction.objects.get(user=self.user, description="Virement vers l'épargne")
        self.assertEqual(saving.amount, Decimal("100.00"))
        self.assertEqual(saving.category.name, "Épargne & placements")
        self.assertEqual(saving.subcategory.name, "Livret")
        self.assertFalse(
            Transaction.objects.filter(user=self.user, transaction_type=EntryType.INCOME, amount=40).exists()
        )

    def test_identical_operations_are_both_kept(self):
        import_operations(self.user, self.operations)

        self.assertEqual(
            Transaction.objects.filter(user=self.user, description="Carrefour Market").count(), 2
        )

    def test_reimport_does_not_duplicate(self):
        import_operations(self.user, self.operations)
        result = import_operations(self.user, self.operations)

        self.assertEqual(result.created, 0)
        self.assertEqual(result.skipped, 6)
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 6)


class BankSyncTests(TransactionTestCase):
    # Sans transaction englobante : les statuts sont écrits depuis un autre thread
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.user = get_user_model().objects.create_user("jean", password="x")
        create_default_categories(self.user)

    def test_sync_period(self):
        self.assertEqual(
            sync_period(2026, 9, today=date(2026, 10, 1)),
            (date(2026, 9, 1), date(2026, 9, 30)),
        )
        self.assertEqual(
            sync_period(2026, 10, today=date(2026, 10, 15)),
            (date(2026, 10, 1), date(2026, 10, 15)),
        )

    def test_is_month_syncable(self):
        today = date(2026, 10, 1)
        self.assertTrue(is_month_syncable(2026, 10, today))
        self.assertTrue(is_month_syncable(2025, 10, today))
        self.assertFalse(is_month_syncable(2025, 9, today))
        self.assertFalse(is_month_syncable(2026, 11, today))

    def test_run_bank_sync_imports_downloaded_file(self):
        BankConnection.objects.create(user=self.user, postal_code="75001", login_id="12345678901")
        sync = BankSync.objects.create(user=self.user, year=2026, month=9)
        export_dirs = []

        def fake_download(**kwargs):
            kwargs["on_status"]("WAITING_PASSWORD", "code")
            export_dirs.append(Path(kwargs["target_dir"]))
            return write_sample_export(kwargs["target_dir"])

        with mock.patch("finance.bank_sync.download_ca_operations", side_effect=fake_download):
            run_bank_sync(sync.pk)

        sync.refresh_from_db()
        self.assertEqual(sync.status, BankSyncStatus.DONE)
        self.assertEqual(sync.created_count, 6)
        # Le relevé bancaire ne reste pas sur le disque
        self.assertFalse(export_dirs[0].exists())
        self.assertIsNotNone(BankConnection.objects.get(user=self.user).last_sync_at)

    def test_status_updates_work_inside_playwright_event_loop(self):
        # Playwright (API sync) fait tourner une boucle asyncio dans le thread :
        # Django y interdit l'accès direct à la base.
        import asyncio

        BankConnection.objects.create(user=self.user, postal_code="75001", login_id="12345678901")
        sync = BankSync.objects.create(user=self.user, year=2026, month=9)
        export = write_sample_export(self.tmp)

        seen = []

        def fake_download(**kwargs):
            async def inside_loop():
                kwargs["on_status"]("WAITING_PASSWORD", "Code…")
            asyncio.run(inside_loop())
            seen.append(BankSync.objects.get(pk=sync.pk).status)
            return export

        with mock.patch("finance.bank_sync.download_ca_operations", side_effect=fake_download):
            run_bank_sync(sync.pk)

        sync.refresh_from_db()
        self.assertEqual(seen, [BankSyncStatus.WAITING_PASSWORD])
        self.assertEqual(sync.status, BankSyncStatus.DONE, sync.message)

    def test_run_bank_sync_without_connection_fails_cleanly(self):
        sync = BankSync.objects.create(user=self.user, year=2026, month=9)

        run_bank_sync(sync.pk)

        sync.refresh_from_db()
        self.assertEqual(sync.status, BankSyncStatus.ERROR)
        self.assertIn("paramètres", sync.message)


class BankViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("jean", password="x")
        self.client.force_login(self.user)

    def test_save_bank_connection(self):
        response = self.client.post(
            reverse("finance:update_bank_connection"),
            {"postal_code": "75001", "login_id": "123 456 789 01"},
        )

        self.assertRedirects(response, f"{reverse('finance:settings')}?tab=banque")
        connection = BankConnection.objects.get(user=self.user)
        self.assertEqual(connection.login_id, "12345678901")

    def test_invalid_bank_connection_is_rejected(self):
        self.client.post(
            reverse("finance:update_bank_connection"),
            {"postal_code": "750", "login_id": "123"},
        )

        self.assertFalse(BankConnection.objects.filter(user=self.user).exists())

    def test_settings_bank_tab_renders(self):
        response = self.client.get(reverse("finance:settings") + "?tab=banque")

        self.assertContains(response, "Identifiant Crédit Agricole")

    def test_start_sync_requires_connection(self):
        today = date.today()
        response = self.client.post(reverse("finance:bank_sync_start", args=[today.year, today.month]))

        self.assertEqual(response.status_code, 400)

    def test_start_sync_and_poll_status(self):
        BankConnection.objects.create(user=self.user, postal_code="75001", login_id="12345678901")
        today = date.today()

        with mock.patch("finance.views.start_bank_sync") as start:
            start.return_value = BankSync.objects.create(user=self.user, year=today.year, month=today.month)
            response = self.client.post(
                reverse("finance:bank_sync_start", args=[today.year, today.month])
            )

        self.assertEqual(response.status_code, 200)
        sync_id = response.json()["id"]

        status = self.client.get(reverse("finance:bank_sync_status", args=[sync_id])).json()
        self.assertEqual(status["status"], BankSyncStatus.PENDING)
        self.assertFalse(status["finished"])

    def test_month_page_shows_sync_button(self):
        BankConnection.objects.create(user=self.user, postal_code="75001", login_id="12345678901")
        today = date.today()

        response = self.client.get(reverse("finance:month", args=[today.year, today.month]))

        self.assertContains(response, "Synchroniser avec la banque")
        self.assertContains(response, "bankSyncModal")
