import shutil
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .bank_import import import_operations
from .bank_sync import initial_period, is_month_syncable, run_bank_sync, sync_period
from .ca_scraper import Download
from .ca_csv import parse_account_card, parse_ca_csv
from .default_categories import create_default_categories
from .models import BankConnection, BankSync, BankSyncKind, BankSyncStatus, EntryType, Transaction, TransactionSource
from .models import SavingsAccount, SavingsKind


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


# Relevé sur 12 mois du seul Livret Jeune (intérêts annuels)
LIVRET_JEUNE_EXPORT = """Téléchargement du 30/10/2026;


M.       DUPONT JEAN


Livret Jeune Mozaic carte n° 33333333333

Solde au 30/10/2026 1 600,00 €


Liste des opérations du compte entre le 01/11/2025 et le 30/10/2026;

Date;Libellé;Débit euros;Crédit euros;
31/12/2025;"Intérêts
INTERETS 2025



";;24,00;
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

    def test_account_header_without_card_word(self):
        export = SAMPLE_EXPORT.replace("Livret A carte n° 22222222222", "Livret Jeune Mozaïc n° 33333333333")
        path = Path(self.tmp) / "export.csv"
        path.write_text(export, encoding="cp1252")
        _, balances = parse_ca_csv(path)
        self.assertEqual(
            [(b.account, b.account_number) for b in balances],
            [("Compte de Dépôt", "11111111111"), ("Livret Jeune Mozaïc", "33333333333")],
        )

    def test_parse_account_card(self):
        # Format réel : titulaire, libellé, numéro, date du dernier téléchargement (pas de solde)
        card = parse_account_card(
            "M. BOS FELIX\nLivret Jeune Mozaic\nN°33333333333\nDernier téléchargement le 30/10/2026"
        )
        self.assertEqual((card.account, card.account_number), ("Livret Jeune Mozaic", "33333333333"))
        self.assertIsNone(parse_account_card("M. BOS FELIX\nLivret A\nDernier téléchargement le 30/10/2026"))

    def test_card_purchase_from_previous_month_keeps_its_date(self):
        operations, _ = parse_ca_csv(write_sample_export(self.tmp))

        self.assertEqual(operations[6].operation_date, date(2026, 8, 31))


class BankImportTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        # Le diagnostic de synchronisation s'écrit dans le dossier temporaire
        self.enterContext(override_settings(DATA_DIR=self.tmp))
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
        self.assertEqual(spotify.category.name, "Abonnements & services")
        self.assertEqual(spotify.subcategory.name, "Musique")

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

    def test_card_purchase_keeps_its_purchase_date(self):
        import_operations(self.user, self.operations)

        # Achat du 31/08 débité le 01/09 : compté en août
        boutique = Transaction.objects.get(user=self.user, description="BOUTIQUE INCONNUE")
        self.assertEqual(boutique.transaction_date, date(2026, 8, 31))

    def test_category_learned_from_previous_operation(self):
        salary = Category.objects.get(user=self.user, name="Salaire")
        Transaction.objects.create(
            user=self.user, transaction_type=EntryType.INCOME, amount=Decimal("149.43"),
            description="CAF DES HAUTS DE SEINE", category=salary, transaction_date=date(2026, 8, 4),
        )

        import_operations(self.user, self.operations)

        caf = Transaction.objects.get(user=self.user, description="CAF DES HAUTS DE SEINE", transaction_date=date(2026, 9, 4))
        self.assertEqual(caf.category, salary)

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
        # Le diagnostic de synchronisation s'écrit dans le dossier temporaire
        self.enterContext(override_settings(DATA_DIR=self.tmp))
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
            return Download(path=write_sample_export(kwargs["target_dir"]))

        with mock.patch("finance.bank_sync.download_ca_operations", side_effect=fake_download):
            run_bank_sync(sync.pk)

        sync.refresh_from_db()
        self.assertEqual(sync.status, BankSyncStatus.DONE)
        self.assertEqual(sync.created_count, 6)
        self.assertEqual(sync.message, "Soldes mis à jour : Livret A.")
        # Le relevé bancaire ne reste pas sur le disque
        self.assertFalse(export_dirs[0].exists())
        self.assertIsNotNone(BankConnection.objects.get(user=self.user).last_sync_at)
        # Le solde du livret alimente le module Épargne
        livret = SavingsAccount.objects.get(user=self.user, bank_account_number="22222222222")
        self.assertEqual(livret.kind, SavingsKind.LIVRET_A)
        self.assertEqual(livret.snapshots.get().balance, Decimal("5000.00"))

    def test_livret_missing_from_export_gets_a_balance_download(self):
        BankConnection.objects.create(user=self.user, postal_code="75001", login_id="12345678901")
        sync = BankSync.objects.create(user=self.user, year=2026, month=9)
        cards = [
            "M. DUPONT JEAN\nLivret Jeune Mozaic\nN°33333333333\nDernier téléchargement le 30/10/2026",
            "M. DUPONT JEAN\nLivret A\nN°22222222222\nDernier téléchargement le 30/10/2026",
            "M. DUPONT JEAN\nCompte de Dépôt\nN°11111111111\nDernier téléchargement le 30/10/2026",
        ]
        asked = []

        def fake_download(**kwargs):
            path = write_sample_export(kwargs["target_dir"])
            # Le scraper demande quels comptes télécharger à nouveau, et depuis quand
            missing = kwargs["balance_accounts"](path, cards)
            asked.append((missing, kwargs["balance_start"]))
            balance_path = Path(kwargs["target_dir"]) / "soldes.csv"
            balance_path.write_text(LIVRET_JEUNE_EXPORT, encoding="cp1252")
            return Download(path=path, balance_paths=[balance_path], account_cards=cards)

        with mock.patch("finance.bank_sync.download_ca_operations", side_effect=fake_download):
            run_bank_sync(sync.pk)

        sync.refresh_from_db()
        self.assertEqual(sync.status, BankSyncStatus.DONE, sync.message)
        # Seul le Livret Jeune manque au relevé (le compte courant n'est pas un livret)
        self.assertEqual(asked, [(["33333333333"], initial_period()[0])])
        jeune = SavingsAccount.objects.get(user=self.user, bank_account_number="33333333333")
        self.assertEqual(jeune.kind, SavingsKind.LIVRET_JEUNE)
        self.assertEqual(jeune.ceiling, Decimal("1600"))
        self.assertEqual(jeune.snapshots.get().balance, Decimal("1600.00"))
        # Les opérations du relevé complémentaire ne sont pas importées
        self.assertEqual(sync.created_count, 6)
        self.assertEqual(sync.message, "Soldes mis à jour : Livret A, Livret Jeune Mozaic.")

        # Diagnostic anonymisé : structure lisible, ni numéro complet ni montant
        diagnostic = (Path(self.tmp) / "diagnostic_banque.txt").read_text(encoding="utf-8")
        self.assertIn("Livret Jeune Mozaic · n° …333 → importé", diagnostic)
        self.assertIn("Livret Jeune Mozaic carte n° 99999999999", diagnostic)
        self.assertNotIn("33333333333", diagnostic)
        self.assertNotIn("1 600,00", diagnostic)
        self.assertNotIn("Spotify", diagnostic)

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
            return Download(path=export)

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

        # Première connexion : on enchaîne sur l'import de l'historique
        self.assertRedirects(response, reverse("finance:bank_initial_sync"))
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
        BankConnection.objects.create(
            user=self.user, postal_code="75001", login_id="12345678901", initial_import_done=True
        )
        today = date.today()

        response = self.client.get(reverse("finance:month", args=[today.year, today.month]))

        self.assertContains(response, "Synchroniser avec la banque")
        self.assertContains(response, "bankSyncModal")


class InitialImportTests(TransactionTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        # Le diagnostic de synchronisation s'écrit dans le dossier temporaire
        self.enterContext(override_settings(DATA_DIR=self.tmp))
        self.user = get_user_model().objects.create_user("jean", password="x")
        create_default_categories(self.user)

    def test_initial_period_covers_12_months(self):
        start, end = initial_period(today=date(2026, 10, 1))
        self.assertEqual(start, date(2025, 11, 1))
        self.assertEqual(end, date(2026, 10, 1))

    def test_initial_sync_imports_and_marks_done(self):
        BankConnection.objects.create(user=self.user, postal_code="75001", login_id="12345678901")
        sync = BankSync.objects.create(user=self.user, kind=BankSyncKind.INITIAL, year=2026, month=10)

        def fake_download(**kwargs):
            # L'import initial demande bien une large période
            self.assertLess((kwargs["end"] - kwargs["start"]).days, 400)
            self.assertGreater((kwargs["end"] - kwargs["start"]).days, 300)
            return Download(path=write_sample_export(kwargs["target_dir"]))

        with mock.patch("finance.bank_sync.download_ca_operations", side_effect=fake_download):
            run_bank_sync(sync.pk)

        sync.refresh_from_db()
        self.assertEqual(sync.status, BankSyncStatus.DONE, sync.message)
        self.assertTrue(BankConnection.objects.get(user=self.user).initial_import_done)


class OnboardingViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("jean", password="x")
        self.client.force_login(self.user)

    def test_signup_refused_once_an_account_exists(self):
        self.client.logout()
        self.assertRedirects(self.client.get(reverse("accounts:signup")), reverse("accounts:login"))
        self.client.post(reverse("accounts:signup"), {
            "username": "neo", "email": "neo@ex.fr",
            "password1": "Tr3sL0ngMotDePasse2026", "password2": "Tr3sL0ngMotDePasse2026",
        })
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_first_launch_goes_to_signup(self):
        self.client.logout()
        get_user_model().objects.all().delete()
        self.assertRedirects(self.client.get(reverse("accounts:login")), reverse("accounts:signup"))

    def test_signup_redirects_to_bank_connect(self):
        self.client.logout()
        get_user_model().objects.all().delete()
        response = self.client.post(
            reverse("accounts:signup"),
            {"username": "neo", "email": "neo@ex.fr",
             "password1": "Tr3sL0ngMotDePasse2026", "password2": "Tr3sL0ngMotDePasse2026"},
        )
        self.assertRedirects(response, reverse("finance:bank_connect"))

    def test_bank_connect_saves_and_goes_to_initial_sync(self):
        response = self.client.post(
            reverse("finance:bank_connect"),
            {"postal_code": "92130", "login_id": "65053051450"},
        )
        self.assertRedirects(response, reverse("finance:bank_initial_sync"))
        self.assertTrue(BankConnection.objects.filter(user=self.user).exists())

    def test_initial_sync_page_renders_before_import(self):
        BankConnection.objects.create(user=self.user, postal_code="75001", login_id="12345678901")
        response = self.client.get(reverse("finance:bank_initial_sync"))
        self.assertContains(response, "12 derniers mois")

    def test_initial_sync_page_redirects_when_already_done(self):
        BankConnection.objects.create(
            user=self.user, postal_code="75001", login_id="12345678901", initial_import_done=True
        )
        response = self.client.get(reverse("finance:bank_initial_sync"))
        self.assertRedirects(response, reverse("finance:dashboard"))

    def test_updating_bank_connection_first_time_goes_to_initial_sync(self):
        response = self.client.post(
            reverse("finance:update_bank_connection"),
            {"postal_code": "75001", "login_id": "12345678901"},
        )
        self.assertRedirects(response, reverse("finance:bank_initial_sync"))

    def test_month_sync_button_hidden_until_initial_done(self):
        BankConnection.objects.create(user=self.user, postal_code="75001", login_id="12345678901")
        today = date.today()
        response = self.client.get(reverse("finance:month", args=[today.year, today.month]))
        self.assertContains(response, "Importer l'historique")
        self.assertNotContains(response, "bankSyncModal")

    def test_month_sync_button_shown_after_initial_done(self):
        BankConnection.objects.create(
            user=self.user, postal_code="75001", login_id="12345678901", initial_import_done=True
        )
        today = date.today()
        response = self.client.get(reverse("finance:month", args=[today.year, today.month]))
        self.assertContains(response, "Synchroniser avec la banque")

    def test_initial_sync_start_blocked_when_already_done(self):
        BankConnection.objects.create(
            user=self.user, postal_code="75001", login_id="12345678901", initial_import_done=True
        )
        response = self.client.post(reverse("finance:bank_initial_sync_start"))
        self.assertEqual(response.status_code, 400)


# ============================================================
# MODULE BUDGET
# ============================================================

from . import budget as budget_calc  # noqa: E402
from .models import (  # noqa: E402
    BudgetLine,
    BudgetPlan,
    BudgetSection,
    Category,
    IncomeMode,
    MealVoucherSettings,
    RecurrenceKind,
    RecurrenceUnit,
    RecurringItem,
    SavingsProject,
    Subcategory,
)
from . import views  # noqa: E402


class BudgetTestMixin:
    def setUp(self):
        self.user = get_user_model().objects.create_user("budget", password="pass-12345")
        create_default_categories(self.user)
        self.food = Category.objects.get(user=self.user, name=budget_calc.FOOD_CATEGORY_NAME)
        self.restaurants = Subcategory.objects.get(category=self.food, name="Restaurants, snacks")
        self.housing = Category.objects.get(user=self.user, name=budget_calc.HOUSING_CATEGORY_NAME)
        self.savings = Category.objects.get(user=self.user, name=budget_calc.SAVINGS_CATEGORY_NAME)
        self.salary = Category.objects.get(user=self.user, name=budget_calc.SALARY_CATEGORY_NAME)

    def expense(self, amount, category, day=date(2026, 10, 5), **kwargs):
        return Transaction.objects.create(
            user=self.user,
            transaction_type=EntryType.EXPENSE,
            amount=Decimal(amount),
            category=category,
            transaction_date=day,
            **kwargs,
        )

    def income(self, amount, day, category=None):
        return Transaction.objects.create(
            user=self.user,
            transaction_type=EntryType.INCOME,
            amount=Decimal(amount),
            category=category or self.salary,
            transaction_date=day,
        )

    def plan(self, valid_from=date(2026, 9, 1), **kwargs):
        defaults = {"income_mode": IncomeMode.MANUAL, "manual_income": Decimal("2800"), "savings_target_monthly": Decimal("300")}
        defaults.update(kwargs)
        return BudgetPlan.objects.create(user=self.user, valid_from=valid_from, **defaults)


class BudgetCalculationTests(BudgetTestMixin, TestCase):
    def test_average_salary_ignores_months_without_salary(self):
        self.income("2800", date(2026, 6, 28))  # compté en juillet
        self.income("3000", date(2026, 8, 28))  # compté en septembre
        self.income("9999", date(2026, 10, 1))  # mois en cours : exclu
        average, months = budget_calc.average_salary(self.user, 2026, 10, 6)
        self.assertEqual(months, 2)
        self.assertEqual(average, Decimal("2900.00"))

    def test_end_of_month_salary_counts_for_next_month(self):
        late = self.income("2800", date(2026, 10, 30))
        early = self.income("2800", date(2026, 12, 1))  # versement décalé : reste en décembre
        december = self.income("2800", date(2026, 12, 24))
        other_income = self.income("50", date(2026, 10, 30), category=Category.objects.get(user=self.user, name="Remboursements"))

        self.assertEqual(late.transaction_date, date(2026, 11, 1))
        self.assertEqual(early.transaction_date, date(2026, 12, 1))
        self.assertEqual(december.transaction_date, date(2027, 1, 1))
        self.assertEqual(other_income.transaction_date, date(2026, 10, 30))

    def test_savings_after_salary_follow_it_to_next_month(self):
        before = self.expense("200", self.savings, day=date(2026, 10, 10))
        end_of_month = self.expense("150", self.savings, day=date(2026, 10, 29))
        salary = self.income("1961", date(2026, 10, 30))
        same_day = self.expense("1000", self.savings, day=date(2026, 10, 30))
        spending = self.expense("21.60", self.food, day=date(2026, 10, 30))

        for transaction in (before, end_of_month, salary, same_day, spending):
            transaction.refresh_from_db()
        self.assertEqual((salary.transaction_date, salary.actual_date), (date(2026, 11, 1), date(2026, 10, 30)))
        self.assertEqual((same_day.transaction_date, same_day.actual_date), (date(2026, 11, 1), date(2026, 10, 30)))
        # Épargne versée avant le salaire et dépenses : restent en octobre
        self.assertEqual(before.transaction_date, date(2026, 10, 10))
        self.assertEqual(end_of_month.transaction_date, date(2026, 10, 29))
        self.assertIsNone(end_of_month.actual_date)
        self.assertEqual(spending.transaction_date, date(2026, 10, 30))

        # Resauvegarder ne décale pas une deuxième fois
        same_day.save()
        self.assertEqual(same_day.transaction_date, date(2026, 11, 1))

    def test_savings_imported_before_salary_follow_it(self):
        saving = self.expense("500", self.savings, day=date(2026, 10, 31))
        self.income("1961", date(2026, 10, 30))

        saving.refresh_from_db()
        self.assertEqual((saving.transaction_date, saving.actual_date), (date(2026, 11, 1), date(2026, 10, 31)))

    def test_savings_date_edited_by_hand_is_kept(self):
        self.income("1961", date(2026, 10, 30))
        saving = self.expense("500", self.savings, day=date(2026, 10, 30))
        saving.transaction_date = date(2026, 10, 20)
        saving.save()

        self.assertEqual((saving.transaction_date, saving.actual_date), (date(2026, 10, 20), None))

    def test_recurring_salary_is_not_duplicated(self):
        item = RecurringItem.objects.create(
            user=self.user, name="Salaire", kind=RecurrenceKind.FIXED, entry_type=EntryType.INCOME,
            default_amount=Decimal("2800"), category=self.salary, frequency_interval=1,
            frequency_unit=RecurrenceUnit.MONTH, start_date=date(2026, 9, 28), is_active=True,
        )
        views._materialize_recurring_items(self.user, 2026, 10)
        views._materialize_recurring_items(self.user, 2026, 10)

        self.assertEqual(
            list(item.transactions.values_list("transaction_date", flat=True)), [date(2026, 11, 1)]
        )

    def test_plan_for_month_uses_version_in_force(self):
        first = self.plan(valid_from=date(2026, 6, 1))
        second = self.plan(valid_from=date(2026, 9, 1))
        self.assertEqual(budget_calc.plan_for_month(self.user, 2026, 8), first)
        self.assertEqual(budget_calc.plan_for_month(self.user, 2026, 10), second)
        # Avant le premier budget, on compare au premier budget
        self.assertEqual(budget_calc.plan_for_month(self.user, 2026, 1), first)

    def test_meal_vouchers_month(self):
        MealVoucherSettings.objects.create(
            user=self.user, face_value=Decimal("9.50"), employee_share_percent=Decimal("50"), days_per_month=20
        )
        self.expense("12.40", self.food, paid_with_meal_voucher=True)
        vouchers = budget_calc.meal_voucher_month(self.user, 2026, 10)
        self.assertEqual(vouchers["credit"], Decimal("190.00"))
        self.assertEqual(vouchers["employee_cost"], Decimal("95.00"))
        self.assertEqual(vouchers["remaining"], Decimal("177.60"))

    def test_working_days_when_no_fixed_count(self):
        self.assertEqual(budget_calc.working_days(2026, 10), 22)

    def test_tracking_matches_most_specific_line_and_flags_overrun(self):
        plan = self.plan()
        BudgetLine.objects.create(plan=plan, section=BudgetSection.VARIABLE, category=self.food, amount=Decimal("300"))
        BudgetLine.objects.create(
            plan=plan, section=BudgetSection.VARIABLE, category=self.food, subcategory=self.restaurants, amount=Decimal("50")
        )
        BudgetLine.objects.create(plan=plan, section=BudgetSection.FIXED, category=self.housing, amount=Decimal("900"))

        self.expense("80", self.food, subcategory=self.restaurants)
        self.expense("40", self.food)
        self.expense("900", self.housing)
        self.expense("200", self.savings)
        self.expense("25", Category.objects.get(user=self.user, name="Transport"))

        tracking = budget_calc.month_tracking(self.user, 2026, 10, today=date(2026, 10, 15))
        rows = {row["label"]: row for row in tracking["rows"]}
        self.assertEqual(rows["Restaurants, snacks"]["spent"], Decimal("80.00"))
        self.assertEqual(rows["Restaurants, snacks"]["status"], "over")
        self.assertEqual(rows["Alimentation & restaurants"]["spent"], Decimal("40.00"))
        self.assertEqual(rows["Logement & charges"]["status"], "ok")
        self.assertEqual(tracking["savings_actual"], Decimal("200.00"))
        self.assertEqual(tracking["unbudgeted_total"], Decimal("25.00"))
        self.assertEqual(tracking["spent_total"], Decimal("1045.00"))

    def test_variable_line_projection_warns_before_overrun(self):
        plan = self.plan()
        BudgetLine.objects.create(plan=plan, section=BudgetSection.VARIABLE, category=self.food, amount=Decimal("300"))
        self.expense("200", self.food, day=date(2026, 10, 2))
        tracking = budget_calc.month_tracking(self.user, 2026, 10, today=date(2026, 10, 10))
        row = tracking["rows"][0]
        self.assertEqual(row["status"], "warning")
        self.assertGreater(row["projected"], Decimal("300"))

    def test_project_progress_and_monthly_need(self):
        project = SavingsProject.objects.create(
            user=self.user,
            name="Vélo",
            target_amount=Decimal("1200"),
            target_date=date(2027, 3, 15),
            initial_amount=Decimal("200"),
        )
        self.expense("100", self.savings, savings_project=project)
        progress = budget_calc.project_progress(project, today=date(2026, 10, 10))
        self.assertEqual(progress["saved"], Decimal("300.00"))
        self.assertEqual(progress["months_left"], 6)
        self.assertEqual(progress["monthly_needed"], Decimal("150.00"))

    def test_trend_detects_recurring_overrun(self):
        plan = self.plan(valid_from=date(2026, 6, 1))
        BudgetLine.objects.create(
            plan=plan, section=BudgetSection.VARIABLE, category=self.food, subcategory=self.restaurants, amount=Decimal("50")
        )
        for month in (7, 8, 9):
            self.expense("90", self.food, day=date(2026, month, 10), subcategory=self.restaurants)
        trend = budget_calc.budget_trend(self.user, today=date(2026, 10, 10), months=4)
        self.assertEqual(len(trend["history"]), 4)
        self.assertEqual(trend["recurring_overruns"][0]["label"], "Restaurants, snacks")
        self.assertEqual(trend["recurring_overruns"][0]["average"], Decimal("90.00"))


class BudgetViewsTests(BudgetTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="budget", password="pass-12345")

    def test_budget_pages_render(self):
        for name in ("dashboard_budget", "budget_plan", "budget_goals"):
            response = self.client.get(reverse(f"finance:{name}"))
            self.assertEqual(response.status_code, 200, name)

    def test_month_page_invites_to_create_budget(self):
        today = date.today()
        response = self.client.get(reverse("finance:month", args=[today.year, today.month]))
        self.assertContains(response, "Créer mon budget")

    def test_save_plan_with_lines(self):
        today = date.today()
        valid_from = date(today.year, today.month, 1)
        response = self.client.post(reverse("finance:budget_plan"), {
            "valid_from": valid_from.isoformat(),
            "income_mode": IncomeMode.MANUAL,
            "manual_income": "2861",
            "income_average_months": "6",
            "savings_target_monthly": "1000",
            "lines-TOTAL_FORMS": "3",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-section": BudgetSection.FIXED,
            "lines-0-category": self.housing.pk,
            "lines-0-label": "Loyer",
            "lines-0-amount": "960",
            "lines-1-section": BudgetSection.VARIABLE,
            "lines-1-category": self.food.pk,
            "lines-1-amount": "300",
            "lines-2-section": BudgetSection.VARIABLE,
            "lines-2-category": self.food.pk,
            "lines-2-amount": "10",
            "lines-2-DELETE": "on",
        })
        self.assertRedirects(response, reverse("finance:budget_plan"))
        plan = BudgetPlan.objects.get(user=self.user, valid_from=valid_from)
        self.assertEqual(plan.lines.count(), 2)
        self.assertEqual(plan.savings_target_monthly, Decimal("1000"))

        summary = budget_calc.plan_summary(plan, self.user, today.year, today.month)
        self.assertEqual(summary["left_to_live"], Decimal("601.00"))

    def test_create_project_and_link_savings(self):
        response = self.client.post(reverse("finance:add_savings_project"), {
            "name": "Voyage au Japon",
            "target_amount": "3000",
            "target_date": "2027-06-01",
            "initial_amount": "0",
        })
        self.assertRedirects(response, reverse("finance:budget_goals"))
        project = SavingsProject.objects.get(user=self.user)

        today = date.today()
        self.client.post(reverse("finance:add_transaction", args=[today.year, today.month]), {
            "transaction_type": EntryType.EXPENSE,
            "amount": "250",
            "category": self.savings.pk,
            "transaction_date": today.isoformat(),
            "savings_project": project.pk,
        })
        self.assertEqual(project.contributions.count(), 1)
        self.assertContains(self.client.get(reverse("finance:budget_goals")), "Voyage au Japon")

    def test_meal_voucher_only_for_food(self):
        today = date.today()
        transport = Category.objects.get(user=self.user, name="Transport")
        self.client.post(reverse("finance:add_transaction", args=[today.year, today.month]), {
            "transaction_type": EntryType.EXPENSE,
            "amount": "12",
            "category": transport.pk,
            "transaction_date": today.isoformat(),
            "paid_with_meal_voucher": "on",
        })
        self.assertFalse(Transaction.objects.filter(user=self.user).exists())

        self.client.post(reverse("finance:add_transaction", args=[today.year, today.month]), {
            "transaction_type": EntryType.EXPENSE,
            "amount": "12",
            "category": self.food.pk,
            "transaction_date": today.isoformat(),
            "paid_with_meal_voucher": "on",
        })
        self.assertTrue(Transaction.objects.get(user=self.user).paid_with_meal_voucher)


# ============================================================
# ABONNEMENTS DÉTECTÉS
# ============================================================

from . import subscriptions as subscription_detection  # noqa: E402
from .models import DismissedSubscription, RecurrenceUnit, RecurringItem  # noqa: E402


class SubscriptionDetectionTests(BudgetTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.leisure = Category.objects.get(user=self.user, name="Loisirs & vacances")

    def bank_payment(self, amount, day, description, category=None):
        return Transaction.objects.create(
            user=self.user,
            transaction_type=EntryType.EXPENSE,
            amount=Decimal(amount),
            category=category or self.leisure,
            transaction_date=day,
            description=description,
            source=TransactionSource.BANK,
        )

    def test_merchant_key_ignores_references(self):
        self.assertEqual(subscription_detection.merchant_key("SPOTIFY P4776921FD S"), "spotify")
        self.assertEqual(subscription_detection.merchant_key("Spotify"), "spotify")
        self.assertEqual(subscription_detection.merchant_key("PRLV SEPA Free Mobile"), "free mobile")

    def test_detects_monthly_subscription_and_separates_amounts(self):
        for month in (5, 6, 7, 8, 9):
            self.bank_payment("10.99", date(2026, month, 5), "Apple")
            self.bank_payment("0.99", date(2026, month, 18), "Apple")
        # Achat ponctuel chez le même commerçant : ignoré
        self.bank_payment("49.00", date(2026, 7, 2), "Apple")

        candidates = subscription_detection.detect_candidates(self.user, today=date(2026, 9, 25))
        amounts = sorted(c.amount for c in candidates)
        self.assertEqual(amounts, [Decimal("0.99"), Decimal("10.99")])
        self.assertTrue(all(c.unit == RecurrenceUnit.MONTH for c in candidates))

    def test_irregular_spending_is_not_a_subscription(self):
        for day in (date(2026, 7, 2), date(2026, 7, 9), date(2026, 7, 30), date(2026, 8, 4), date(2026, 9, 21)):
            self.bank_payment("15.00", day, "Uber Eats")
        self.assertEqual(subscription_detection.detect_candidates(self.user, today=date(2026, 9, 25)), [])

    def test_missing_payment_means_stopped(self):
        for month in (3, 4, 5, 6):
            self.bank_payment("29.90", date(2026, month, 3), "Basic Fit")
        suggestions = subscription_detection.subscription_suggestions(self.user, today=date(2026, 9, 25))
        self.assertEqual(suggestions["active"], [])
        self.assertEqual(len(suggestions["stopped"]), 1)
        self.assertEqual(suggestions["stopped"][0]["status"]["last_paid"], date(2026, 6, 3))

    def test_follow_links_payments_and_auto_stops_then_resumes(self):
        for month in (5, 6, 7):
            self.bank_payment("12.00", date(2026, month, 10), "Netflix")
        candidate = subscription_detection.detect_candidates(self.user, today=date(2026, 7, 20))[0]
        item = subscription_detection.follow_candidate(self.user, candidate, today=date(2026, 7, 20))
        self.assertEqual(item.transactions.count(), 3)
        self.assertEqual(item.match_key, "netflix")

        # Nouveau paiement importé : rattaché automatiquement
        self.bank_payment("12.00", date(2026, 8, 10), "NETFLIX.COM 866-579")
        subscription_detection.sync_subscriptions(self.user, today=date(2026, 8, 20))
        self.assertEqual(item.transactions.count(), 4)

        # Septembre et octobre non payés : arrêté
        changes = subscription_detection.sync_subscriptions(self.user, today=date(2026, 10, 1))
        item.refresh_from_db()
        self.assertEqual(changes, [("stopped", item)])
        self.assertFalse(item.is_active)
        self.assertEqual(item.end_date, date(2026, 8, 10))

        # Il réapparaît : réactivé
        self.bank_payment("13.49", date(2026, 10, 12), "Netflix")
        subscription_detection.sync_subscriptions(self.user, today=date(2026, 10, 15))
        item.refresh_from_db()
        self.assertTrue(item.is_active)
        self.assertEqual(item.default_amount, Decimal("13.49"))

    def test_detected_subscription_is_not_materialized(self):
        from .views import _materialize_recurring_items

        RecurringItem.objects.create(
            user=self.user, name="Spotify", kind="SUBSCRIPTION", entry_type=EntryType.EXPENSE,
            default_amount=Decimal("10.99"), start_date=date(2026, 9, 5), match_key="spotify",
        )
        self.assertEqual(_materialize_recurring_items(self.user, 2026, 10), 0)

    def test_subscriptions_page_follow_and_dismiss(self):
        self.client.login(username="budget", password="pass-12345")
        today = date.today()
        for offset in (3, 2, 1):
            y, m = budget_calc.add_months(today.year, today.month, -offset)
            self.bank_payment("9.99", date(y, m, 2), "Deezer")
            self.bank_payment("5.00", date(y, m, 8), "Cloud Box")

        response = self.client.get(reverse("finance:budget_subscriptions"))
        self.assertContains(response, "Deezer")
        candidates = {c.name: c for c in subscription_detection.detect_candidates(self.user)}

        self.client.post(reverse("finance:follow_subscription"), {"key": candidates["Deezer"].key})
        self.assertTrue(RecurringItem.objects.filter(user=self.user, match_key="deezer").exists())

        self.client.post(reverse("finance:dismiss_subscription"), {"key": candidates["Cloud Box"].key})
        self.assertTrue(DismissedSubscription.objects.filter(user=self.user).exists())
        suggestions = subscription_detection.subscription_suggestions(self.user)
        self.assertEqual(suggestions["active"], [])


class SavingsTotalsTests(BudgetTestMixin, TestCase):
    def test_savings_transfer_is_not_a_charge(self):
        self.income("1741.70", date(2026, 6, 30))  # salaire de fin juin : compté en juillet
        self.expense("1146.43", self.housing, day=date(2026, 7, 3))
        self.expense("2700", self.savings, day=date(2026, 7, 9))

        totals = budget_calc.split_totals(
            Transaction.objects.filter(user=self.user).aggregate(**budget_calc.totals_annotations())
        )
        self.assertEqual(totals["expense"], Decimal("1146.43"))
        self.assertEqual(totals["savings"], Decimal("2700.00"))
        self.assertEqual(totals["balance"], Decimal("595.27"))

        self.client.login(username="budget", password="pass-12345")
        response = self.client.get(reverse("finance:month", args=[2026, 7]))
        self.assertEqual(response.context["total_expense"], Decimal("1146.43"))
        self.assertEqual(response.context["total_savings"], Decimal("2700.00"))
        self.assertEqual(response.context["balance"], Decimal("595.27"))


class MonthLiveEditTests(BudgetTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="budget", password="pass-12345")
        self.ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

    def test_delete_without_reload_returns_new_totals(self):
        self.expense("40", self.food, day=date(2026, 7, 5))
        doomed = self.expense("60", self.housing, day=date(2026, 7, 6))
        response = self.client.post(reverse("finance:delete_transaction", args=[2026, 7, doomed.pk]), **self.ajax)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["month"]["totals"]["expense"], "40.00")
        self.assertFalse(Transaction.objects.filter(pk=doomed.pk).exists())

    def test_inline_recategorize_and_rename(self):
        transaction = self.expense("250", self.food, day=date(2026, 7, 9), description="WEB M. DUPONT")
        livret = Subcategory.objects.get(category=self.savings, name="Livret")
        response = self.client.post(
            reverse("finance:update_transaction_details", args=[2026, 7, transaction.pk]),
            {"description": "Virement livret", "category": self.savings.pk, "subcategory": livret.pk},
            **self.ajax,
        )
        payload = response.json()
        self.assertEqual(payload["row"]["kind"], "savings")
        self.assertEqual(payload["row"]["description"], "Virement livret")
        self.assertEqual(payload["month"]["totals"]["savings"], "250.00")
        self.assertEqual(payload["month"]["totals"]["expense"], "0.00")
        transaction.refresh_from_db()
        self.assertEqual(transaction.subcategory, livret)

    def test_inline_edit_rejects_income_category_on_expense(self):
        transaction = self.expense("12", self.food, day=date(2026, 7, 9))
        response = self.client.post(
            reverse("finance:update_transaction_details", args=[2026, 7, transaction.pk]),
            {"description": "", "category": self.salary.pk},
            **self.ajax,
        )
        self.assertEqual(response.status_code, 400)

    def test_month_page_shows_kinds_and_subscriptions(self):
        leisure = Category.objects.get(user=self.user, name="Loisirs & vacances")
        item = RecurringItem.objects.create(
            user=self.user, name="Spotify", kind="SUBSCRIPTION", entry_type=EntryType.EXPENSE,
            default_amount=Decimal("10.99"), start_date=date(2026, 5, 5), match_key="spotify",
        )
        Transaction.objects.create(
            user=self.user, transaction_type=EntryType.EXPENSE, amount=Decimal("10.99"),
            category=leisure, transaction_date=date(2026, 7, 5), description="Spotify", recurring_item=item,
        )
        self.income("1800", date(2026, 6, 30))  # salaire de fin juin : compté en juillet
        self.expense("300", self.savings, day=date(2026, 7, 10))
        response = self.client.get(reverse("finance:month", args=[2026, 7]))
        self.assertContains(response, "tx-kind-income")
        self.assertContains(response, "tx-kind-savings")
        self.assertEqual(len(response.context["month_subscriptions"]["paid"]), 1)


class SubscriptionLabelVariantsTests(BudgetTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.ai = Category.objects.get(user=self.user, name="Abonnements & services")

    def pay(self, day, description):
        return Transaction.objects.create(
            user=self.user, transaction_type=EntryType.EXPENSE, amount=Decimal("21.60"),
            category=None, transaction_date=day, description=description, source=TransactionSource.BANK,
        )

    def test_same_merchant_with_different_bank_labels_is_detected(self):
        self.pay(date(2026, 6, 24), "ANTHROPIC* CLAUDE SU")
        self.pay(date(2026, 7, 30), "ANTHROPIC* CLAUDE SU")
        self.pay(date(2026, 8, 30), "Anthropic")
        self.pay(date(2026, 9, 30), "Anthropic")
        candidates = subscription_detection.detect_candidates(self.user, today=date(2026, 10, 1))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].count, 4)

    def test_track_single_operation_from_month_page(self):
        self.client.login(username="budget", password="pass-12345")
        first = self.pay(date(2026, 8, 30), "Anthropic")
        latest = self.pay(date(2026, 9, 30), "ANTHROPIC* CLAUDE SU")
        response = self.client.post(
            reverse("finance:track_transaction_subscription", args=[2026, 9, latest.pk]),
            {"unit": "MONTH"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(response.json()["ok"])
        item = RecurringItem.objects.get(user=self.user)
        self.assertEqual(item.match_key, "anthropic")
        self.assertEqual(set(item.transactions.values_list("pk", flat=True)), {first.pk, latest.pk})

        # Le paiement suivant se rattache tout seul
        nxt = self.pay(date(2026, 10, 30), "Anthropic")
        subscription_detection.sync_subscriptions(self.user, today=date(2026, 10, 31))
        nxt.refresh_from_db()
        self.assertEqual(nxt.recurring_item, item)

    def test_ai_tools_are_categorized(self):
        from .bank_import import guess_category
        category, subcategory = guess_category(self.user, EntryType.EXPENSE, "Paiement par carte ANTHROPIC* CLAUDE SU", {})
        self.assertEqual(category, self.ai)
        self.assertEqual(subcategory.name, "Outils IA & logiciels")


# ============================================================
# ÉPARGNE
# ============================================================

from datetime import timedelta  # noqa: E402

from . import market_data, wealth  # noqa: E402
from .ca_csv import AccountBalance  # noqa: E402
from .models import (  # noqa: E402
    AssetClass,
    BalanceSnapshot,
    ExchangeRate,
    Instrument,
    OperationKind,
    PortfolioOperation,
    PricePoint,
    Zone,
)


def fake_history(prices, currency="EUR", name=""):
    """Remplace market_data.fetch_history : cours fixes, sans réseau."""

    def fetch(symbol, start, end=None):
        closes = [(day, Decimal(str(value))) for day, value in prices if day >= start and (end is None or day <= end)]
        if not closes:
            raise market_data.MarketDataError(f"Aucun cours trouvé pour {symbol}.")
        return {"currency": currency, "name": name, "closes": closes}

    return fetch


class SavingsTestMixin:
    def setUp(self):
        self.user = get_user_model().objects.create_user("epargnant", password="x")
        self.pea = SavingsAccount.objects.create(
            user=self.user, kind=SavingsKind.PEA, name="PEA", ceiling=Decimal("150000"),
            opened_on=date(2020, 1, 15),
        )
        self.etf = Instrument.objects.create(
            user=self.user, symbol="CW8.PA", name="Amundi MSCI World", quote_type="ETF",
        )

    def buy(self, day, quantity, price, fees="0", instrument=None, account=None):
        return PortfolioOperation.objects.create(
            account=account or self.pea, instrument=instrument or self.etf, kind=OperationKind.BUY,
            date=day, quantity=Decimal(quantity), unit_price=Decimal(price), fees=Decimal(fees),
        )

    def sell(self, day, quantity, price, fees="0"):
        return PortfolioOperation.objects.create(
            account=self.pea, instrument=self.etf, kind=OperationKind.SELL,
            date=day, quantity=Decimal(quantity), unit_price=Decimal(price), fees=Decimal(fees),
        )

    def price(self, day, close, instrument=None):
        PricePoint.objects.create(instrument=instrument or self.etf, date=day, close=Decimal(close))


class WealthCalculationTests(SavingsTestMixin, TestCase):
    def test_pru_includes_fees_and_sale_realizes_gain(self):
        self.buy(date(2026, 1, 5), "10", "100", fees="2")
        self.buy(date(2026, 2, 5), "10", "110", fees="2")
        self.sell(date(2026, 3, 5), "5", "120", fees="1")
        self.price(date(2026, 1, 5), "100")
        self.price(date(2026, 3, 10), "130")

        analysis = wealth.analyse_portfolio([self.pea], today=date(2026, 3, 10))
        row = analysis["positions"][0]

        self.assertEqual(row["quantity"], Decimal("15"))
        # (1002 + 1102) / 20 = 105,20 € de PRU ; 5 parts vendues à 599 € nets
        self.assertEqual(row["pru"], Decimal("105.2"))
        self.assertEqual(row["cost"], Decimal("1578.00"))
        self.assertEqual(row["value"], Decimal("1950.00"))
        self.assertEqual(analysis["totals"]["realized"], Decimal("73.00"))
        self.assertEqual(analysis["totals"]["latent"], Decimal("372.00"))
        self.assertEqual(analysis["totals"]["gain"], Decimal("445.00"))
        self.assertEqual(analysis["fees"]["brokerage"], Decimal("5.00"))
        self.assertEqual(analysis["fees"]["orders"], 3)

    def test_existing_holding_starts_at_pru_then_follows_price(self):
        PortfolioOperation.objects.create(
            account=self.pea, instrument=self.etf, kind=OperationKind.POSITION,
            date=date(2026, 1, 5), quantity=Decimal("20"), unit_price=Decimal("80"),
        )
        self.buy(date(2026, 2, 5), "10", "110", fees="2")
        self.sell(date(2026, 3, 5), "6", "120")
        self.price(date(2026, 1, 5), "100")
        self.price(date(2026, 1, 6), "101")
        self.price(date(2026, 3, 10), "130")

        analysis = wealth.analyse_portfolio([self.pea], today=date(2026, 3, 10))
        row = analysis["positions"][0]
        history = analysis["history"]

        # t = 0 : valeur = 20 × 80 = 1 600 € (le PRU), puis le cours du lendemain
        self.assertEqual(history["dates"][0], "2026-01-05")
        self.assertEqual(history["value"][0], "1600.00")
        self.assertEqual(history["invested"][0], "1600.00")
        self.assertEqual(history["value"][1], "2020.00")
        # (1600 + 1102) / 30 = 90,07 € de PRU ; 6 parts vendues
        self.assertEqual(row["quantity"], Decimal("24"))
        self.assertEqual(row["cost"], Decimal("2161.60"))
        self.assertEqual(row["value"], Decimal("3120.00"))
        self.assertEqual(analysis["totals"]["realized"], Decimal("179.60"))
        # La reprise n'est ni un ordre ni des frais de courtage
        self.assertEqual(analysis["fees"]["orders"], 2)
        self.assertEqual(analysis["fees"]["brokerage"], Decimal("2.00"))
        # Elle compte dans les versements du PEA
        self.assertEqual(analysis["accounts"][0]["pea"]["deposits"], Decimal("1982.00"))

    def test_existing_holding_entered_today_is_valued_at_market(self):
        PortfolioOperation.objects.create(
            account=self.pea, instrument=self.etf, kind=OperationKind.POSITION,
            date=date(2026, 3, 10), quantity=Decimal("10"), unit_price=Decimal("80"),
        )
        self.price(date(2026, 3, 10), "100")

        analysis = wealth.analyse_portfolio([self.pea], today=date(2026, 3, 10))

        self.assertEqual(analysis["positions"][0]["value"], Decimal("1000.00"))
        self.assertEqual(analysis["totals"]["latent"], Decimal("200.00"))
        self.assertEqual(analysis["history"]["value"], ["1000.00"])

    def test_foreign_listing_is_converted_to_euros(self):
        apple = Instrument.objects.create(user=self.user, symbol="AAPL", name="Apple", currency="USD", quote_type="EQUITY")
        self.buy(date(2026, 1, 5), "2", "180", instrument=apple)
        self.price(date(2026, 1, 5), "200", instrument=apple)
        ExchangeRate.objects.create(currency="USD", date=date(2026, 1, 5), rate=Decimal("0.9"))

        analysis = wealth.analyse_portfolio([self.pea], today=date(2026, 1, 6))

        self.assertEqual(analysis["positions"][0]["value"], Decimal("360.00"))

    def test_history_and_ter_accrual(self):
        self.etf.ter = Decimal("0.5")
        self.etf.save()
        self.buy(date(2025, 1, 1), "10", "100")
        self.price(date(2025, 1, 1), "100")
        self.price(date(2026, 1, 1), "100")

        analysis = wealth.analyse_portfolio([self.pea], today=date(2026, 1, 1))
        history = analysis["history"]

        self.assertEqual(history["dates"][0], "2025-01-01")
        self.assertEqual(history["value"][-1], "1000.00")
        self.assertEqual(history["invested"][-1], "1000.00")
        # 0,5 % par an sur 1 000 € pendant un an
        self.assertEqual(analysis["fees"]["ter_to_date"], Decimal("5.00"))
        self.assertEqual(analysis["fees"]["ter_year"], Decimal("5.00"))

    def test_annualized_return_after_one_year(self):
        self.assertEqual(
            wealth.xirr([(date(2025, 1, 1), Decimal("-1000")), (date(2026, 1, 1), Decimal("1100"))]),
            Decimal("10.0"),
        )
        self.buy(date(2025, 1, 1), "10", "100")
        self.price(date(2025, 1, 1), "100")
        self.price(date(2026, 1, 1), "110")
        analysis = wealth.analyse_portfolio([self.pea], today=date(2026, 1, 1))
        self.assertEqual(analysis["totals"]["annualized"], Decimal("10.0"))

    def test_account_fees_dividends_and_pea_status(self):
        self.pea.cash_balance = Decimal("50")
        self.pea.save()
        self.buy(date(2026, 1, 5), "10", "100", fees="5")
        PortfolioOperation.objects.create(account=self.pea, kind=OperationKind.FEE, date=date(2026, 2, 1), amount=Decimal("12"))
        PortfolioOperation.objects.create(
            account=self.pea, instrument=self.etf, kind=OperationKind.DIVIDEND, date=date(2026, 3, 1), amount=Decimal("8"),
        )
        self.price(date(2026, 1, 5), "100")

        analysis = wealth.analyse_portfolio([self.pea], today=date(2026, 3, 2))

        self.assertEqual(analysis["fees"]["paid"], Decimal("17.00"))
        self.assertEqual(analysis["totals"]["dividends"], Decimal("8.00"))
        self.assertEqual(analysis["totals"]["value"], Decimal("1050.00"))
        pea = analysis["accounts"][0]["pea"]
        # 50 € de liquidités + 1 005 € d'achat - 8 € de dividendes + 12 € de frais
        self.assertEqual(pea["deposits"], Decimal("1059.00"))
        self.assertTrue(pea["is_mature"])

    def test_held_quantity_for_sales(self):
        self.buy(date(2026, 1, 5), "10", "100")
        self.sell(date(2026, 2, 5), "4", "100")
        self.assertEqual(wealth.held_quantity(self.pea, self.etf, date(2026, 1, 31)), Decimal("10"))
        self.assertEqual(wealth.held_quantity(self.pea, self.etf, date(2026, 2, 5)), Decimal("6"))

    def test_guess_classification(self):
        self.assertEqual(
            wealth.guess_classification("Amundi MSCI World Swap UCITS ETF", "ETF"),
            (AssetClass.EQUITY, Zone.WORLD),
        )
        self.assertEqual(
            wealth.guess_classification("iShares Core Euro Government Bond", "ETF"),
            (AssetClass.BOND, Zone.EUROPE),
        )
        self.assertEqual(
            wealth.guess_classification("Amundi PEA S&P 500 ESG", "ETF"),
            (AssetClass.EQUITY, Zone.US),
        )
        self.assertEqual(wealth.guess_classification("TotalEnergies SE", "EQUITY", "Paris"), (AssetClass.EQUITY, Zone.FRANCE))

    def test_balance_accounts_interest_and_ceiling(self):
        livret = SavingsAccount.objects.create(
            user=self.user, kind=SavingsKind.LIVRET_A, name="Livret A",
            interest_rate=Decimal("2"), ceiling=Decimal("22950"),
        )
        BalanceSnapshot.objects.create(account=livret, date=date(2025, 6, 1), balance=Decimal("8000"))
        BalanceSnapshot.objects.create(account=livret, date=date(2026, 6, 1), balance=Decimal("10000"))

        row = wealth.balance_account_rows([livret], today=date(2026, 6, 2))[0]

        self.assertEqual(row["balance"], Decimal("10000"))
        self.assertEqual(row["interest_year"], Decimal("200.00"))
        self.assertEqual(row["room"], Decimal("12950"))
        self.assertEqual(row["change_year"], Decimal("2000"))

    def test_overview_combines_livrets_and_portfolio(self):
        livret = SavingsAccount.objects.create(user=self.user, kind=SavingsKind.LDDS, name="LDDS")
        BalanceSnapshot.objects.create(account=livret, date=date(2026, 1, 1), balance=Decimal("3000"))
        self.buy(date(2026, 1, 5), "10", "100")
        self.price(date(2026, 1, 5), "120")

        overview = wealth.savings_overview(self.user, today=date(2026, 1, 6))

        self.assertEqual(overview["total"], Decimal("4200.00"))
        self.assertEqual(overview["history"]["balances"][-1], "3000.00")
        self.assertEqual(overview["history"]["securities"][-1], "1200.00")
        classes = {item["name"]: item["value"] for item in overview["allocation"]["classes"]}
        self.assertEqual(classes[wealth.LIVRETS_LABEL], Decimal("3000.00"))
        self.assertEqual(classes["Actions"], Decimal("1200.00"))


class BankBalanceImportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("jean", password="x")

    def balance(self, account, number, amount, day=date(2026, 10, 1)):
        return AccountBalance(account=account, account_number=number, balance_date=day, balance=Decimal(amount))

    def test_livrets_are_created_then_updated(self):
        balances = [
            self.balance("Compte de Dépôt", "111", "1234.56"),
            self.balance("Livret A", "222", "5000"),
            self.balance("LDD SOLIDAIRE", "333", "1200"),
            self.balance("PEA", "444", "50"),
        ]
        self.assertEqual(len(wealth.import_bank_balances(self.user, balances)), 2)

        livret = SavingsAccount.objects.get(bank_account_number="222")
        self.assertEqual(livret.kind, SavingsKind.LIVRET_A)
        self.assertEqual(livret.ceiling, Decimal("22950"))
        self.assertEqual(SavingsAccount.objects.get(bank_account_number="333").kind, SavingsKind.LDDS)
        self.assertFalse(SavingsAccount.objects.filter(bank_account_number__in=["111", "444"]).exists())

        # Nouvelle synchronisation : même compte, nouveau relevé
        livret.name = "Mon livret A"
        livret.save()
        wealth.import_bank_balances(self.user, [self.balance("Livret A", "222", "5100", day=date(2026, 11, 1))])
        livret.refresh_from_db()
        self.assertEqual(livret.name, "Mon livret A")
        self.assertEqual(livret.snapshots.count(), 2)
        self.assertEqual(livret.snapshots.first().balance, Decimal("5100"))

    def test_livret_entered_by_hand_is_linked_to_the_bank_account(self):
        manual = SavingsAccount.objects.create(user=self.user, kind=SavingsKind.LIVRET_JEUNE, name="Mon livret jeune")
        updated = wealth.import_bank_balances(self.user, [self.balance("LIVRET JEUNE MOZAIC", "333", "1600")])

        self.assertEqual(updated, [manual])
        manual.refresh_from_db()
        self.assertEqual(manual.bank_account_number, "333")
        self.assertEqual(manual.name, "Mon livret jeune")
        self.assertEqual(SavingsAccount.objects.filter(user=self.user).count(), 1)

        # Un « autre livret » saisi à la main n'est jamais rattaché d'office
        other = SavingsAccount.objects.create(user=self.user, kind=SavingsKind.LIVRET, name="Livret perso")
        wealth.import_bank_balances(self.user, [self.balance("Livret Sociétaire", "555", "300")])
        other.refresh_from_db()
        self.assertEqual(other.bank_account_number, "")

    def test_guess_savings_kind(self):
        self.assertEqual(wealth.guess_savings_kind("Livret d'Epargne Populaire"), SavingsKind.LEP)
        self.assertEqual(wealth.guess_savings_kind("Plan d'Epargne Logement"), SavingsKind.PEL)
        self.assertEqual(wealth.guess_savings_kind("Livret Jeune"), SavingsKind.LIVRET_JEUNE)
        self.assertEqual(wealth.guess_savings_kind("Livret Sociétaire"), SavingsKind.LIVRET)


class MarketDataTests(SavingsTestMixin, TestCase):
    def test_search_keeps_tradable_titles_and_isin(self):
        class FakeSearch:
            def __init__(self, query, **kwargs):
                self.quotes = [
                    {"symbol": "CW8.PA", "shortname": "Amundi MSCI World", "quoteType": "ETF", "exchDisp": "Paris", "typeDisp": "ETF"},
                    {"symbol": "CW8=F", "shortname": "Future", "quoteType": "FUTURE"},
                ]

        fake_yf = mock.Mock(Search=FakeSearch)
        with mock.patch("finance.market_data._yfinance", return_value=fake_yf):
            results = market_data.search_instruments("lu1681043599")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["symbol"], "CW8.PA")
        self.assertEqual(results[0]["isin"], "LU1681043599")

    def test_refresh_stores_prices_and_exchange_rates(self):
        apple = Instrument.objects.create(user=self.user, symbol="AAPL", name="Apple")
        self.buy(date(2026, 1, 5), "1", "180", instrument=apple)
        prices = [(date(2026, 1, 2), 200), (date(2026, 1, 5), 210)]

        def fetch(symbol, start, end=None):
            if symbol == "USDEUR=X":
                return fake_history([(date(2026, 1, 2), 0.9)], currency="EUR")(symbol, start, end)
            return fake_history(prices, currency="USD")(symbol, start, end)

        with mock.patch("finance.market_data.fetch_history", side_effect=fetch):
            updated, errors = market_data.refresh_prices(self.user)

        apple.refresh_from_db()
        self.assertEqual((updated, errors), (1, []))
        self.assertEqual(apple.currency, "USD")
        self.assertEqual(apple.last_price, Decimal("210"))
        self.assertEqual(apple.prices.count(), 2)
        self.assertTrue(ExchangeRate.objects.filter(currency="USD").exists())
        self.assertFalse(market_data.prices_are_stale(self.user))

    def test_failed_refresh_keeps_app_usable(self):
        self.buy(date(2026, 1, 5), "1", "100")
        with mock.patch("finance.market_data.fetch_history", side_effect=market_data.MarketDataError("Hors ligne")):
            updated, errors = market_data.refresh_prices(self.user)
        self.assertEqual((updated, errors), (0, ["Hors ligne"]))
        # Pas de nouvelle tentative immédiate (pas de boucle de rechargement)
        self.assertFalse(market_data.prices_are_stale(self.user))
        analysis = wealth.analyse_portfolio([self.pea])
        self.assertEqual(analysis["positions"][0]["value"], Decimal("100.00"))


class SavingsViewsTests(SavingsTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_pages_render_empty_and_with_data(self):
        other = get_user_model().objects.create_user("vide", password="x")
        self.client.force_login(other)
        for name in ("savings", "savings_balances", "savings_portfolio", "savings_fees"):
            self.assertEqual(self.client.get(reverse(f"finance:{name}")).status_code, 200, name)

        self.client.force_login(self.user)
        livret = SavingsAccount.objects.create(user=self.user, kind=SavingsKind.LIVRET_A, name="Livret A", interest_rate=Decimal("1.7"))
        BalanceSnapshot.objects.create(account=livret, date=date.today() - timedelta(days=40), balance=Decimal("900"))
        BalanceSnapshot.objects.create(account=livret, date=date.today(), balance=Decimal("1000"))
        self.buy(date.today() - timedelta(days=30), "10", "100", fees="1")
        self.price(date.today() - timedelta(days=30), "100")
        self.price(date.today(), "105")
        for name in ("savings", "savings_balances", "savings_portfolio", "savings_fees"):
            response = self.client.get(reverse(f"finance:{name}"))
            self.assertEqual(response.status_code, 200, name)
        self.assertContains(self.client.get(reverse("finance:savings_portfolio")), "Amundi MSCI World")

    def test_add_livret_with_balance_and_default_ceiling(self):
        response = self.client.post(reverse("finance:add_balance_account"), {
            "kind": SavingsKind.LDDS, "name": "", "institution": "CA", "interest_rate": "1.7", "balance": "2500",
        })
        self.assertRedirects(response, reverse("finance:savings_balances"))
        account = SavingsAccount.objects.get(user=self.user, kind=SavingsKind.LDDS)
        self.assertEqual(account.name, "LDDS")
        self.assertEqual(account.ceiling, Decimal("12000"))
        self.assertEqual(account.snapshots.get().balance, Decimal("2500"))

    def test_add_buy_creates_instrument_and_downloads_prices(self):
        day = date.today() - timedelta(days=3)
        with mock.patch(
            "finance.market_data.fetch_history",
            side_effect=fake_history([(day - timedelta(days=2), 98), (day, 100)]),
        ):
            response = self.client.post(reverse("finance:add_portfolio_operation"), {
                "account": self.pea.pk, "kind": OperationKind.BUY, "date": day.isoformat(),
                "quantity": "3", "unit_price": "100.5", "fees": "0.99",
                "symbol": "WPEA.PA", "instrument_name": "iShares MSCI World Swap PEA UCITS ETF",
                "isin": "IE0002XZSHO1", "exchange": "Paris", "quote_type": "ETF",
            })
        self.assertRedirects(response, f"{reverse('finance:savings_portfolio')}?compte={self.pea.pk}")
        instrument = Instrument.objects.get(user=self.user, symbol="WPEA.PA")
        self.assertEqual((instrument.asset_class, instrument.zone), (AssetClass.EQUITY, Zone.WORLD))
        self.assertEqual(instrument.isin, "IE0002XZSHO1")
        self.assertEqual(instrument.prices.count(), 2)
        operation = instrument.operations.get()
        self.assertEqual(operation.total, Decimal("302.49"))

    def test_add_existing_holding_with_pru(self):
        day = date.today() - timedelta(days=3)
        with mock.patch(
            "finance.market_data.fetch_history",
            side_effect=fake_history([(day - timedelta(days=2), 98), (day, 100)]),
        ):
            response = self.client.post(reverse("finance:add_portfolio_operation"), {
                "account": self.pea.pk, "kind": OperationKind.POSITION, "date": day.isoformat(),
                "quantity": "12", "unit_price": "85.40", "fees": "5",
                "symbol": "WPEA.PA", "instrument_name": "iShares MSCI World Swap PEA UCITS ETF",
                "exchange": "Paris", "quote_type": "ETF",
            })
        self.assertRedirects(response, f"{reverse('finance:savings_portfolio')}?compte={self.pea.pk}")
        operation = PortfolioOperation.objects.get(kind=OperationKind.POSITION)
        # Pas de frais : le PRU les contient déjà
        self.assertEqual((operation.fees, operation.total), (Decimal("0"), Decimal("1024.80")))
        page = self.client.get(reverse("finance:savings_portfolio"))
        self.assertContains(page, "Déjà détenu")
        self.assertContains(page, "PRU (€)")

    def test_sale_cannot_exceed_holdings(self):
        self.buy(date(2026, 1, 5), "2", "100")
        response = self.client.post(reverse("finance:add_portfolio_operation"), {
            "account": self.pea.pk, "kind": OperationKind.SELL, "date": "2026-02-01",
            "quantity": "5", "unit_price": "110", "fees": "0", "symbol": "CW8.PA",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PortfolioOperation.objects.filter(kind=OperationKind.SELL).exists())

    def test_account_fee_without_title(self):
        self.client.post(reverse("finance:add_portfolio_operation"), {
            "account": self.pea.pk, "kind": OperationKind.FEE, "date": "2026-01-31",
            "amount": "15", "note": "Droits de garde", "next": reverse("finance:savings_fees"),
        })
        fee = PortfolioOperation.objects.get(kind=OperationKind.FEE)
        self.assertIsNone(fee.instrument)
        self.assertEqual(fee.total, Decimal("15"))

    def test_delete_position_removes_its_history(self):
        self.buy(date(2026, 1, 5), "2", "100")
        self.buy(date(2026, 2, 5), "1", "100")
        self.client.post(reverse("finance:delete_portfolio_position", args=[self.pea.pk, self.etf.pk]))
        self.assertFalse(PortfolioOperation.objects.filter(instrument=self.etf).exists())

    def test_edit_instrument_sets_ter_and_class(self):
        prefix = f"i{self.etf.pk}"
        self.client.post(reverse("finance:edit_instrument", args=[self.etf.pk]), {
            f"{prefix}-name": "Amundi MSCI World", f"{prefix}-isin": "lu1681043599",
            f"{prefix}-asset_class": AssetClass.EQUITY, f"{prefix}-zone": Zone.WORLD, f"{prefix}-ter": "0.38",
        })
        self.etf.refresh_from_db()
        self.assertEqual(self.etf.ter, Decimal("0.380"))
        self.assertEqual(self.etf.isin, "LU1681043599")

    def test_other_users_data_is_protected(self):
        other = get_user_model().objects.create_user("autre", password="x")
        self.client.force_login(other)
        response = self.client.post(reverse("finance:delete_savings_account", args=[self.pea.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(SavingsAccount.objects.filter(pk=self.pea.pk).exists())

    def test_search_and_price_endpoints(self):
        with mock.patch("finance.market_data.search_instruments", return_value=[{"symbol": "CW8.PA"}]):
            response = self.client.get(reverse("finance:search_instruments"), {"q": "CW8"})
        self.assertEqual(response.json()["results"][0]["symbol"], "CW8.PA")

        with mock.patch(
            "finance.market_data.fetch_history",
            side_effect=fake_history([(date(2026, 3, 13), "52.3")]),
        ):
            response = self.client.get(reverse("finance:instrument_price"), {"symbol": "CW8.PA", "date": "2026-03-15"})
        self.assertEqual(response.json()["eur"], "52.30")
        self.assertEqual(response.json()["date"], "2026-03-13")

    def test_refresh_endpoint(self):
        self.buy(date(2026, 1, 5), "1", "100")
        with mock.patch("finance.market_data.fetch_history", side_effect=fake_history([(date(2026, 1, 5), 101)])):
            response = self.client.post(reverse("finance:refresh_prices"), {"force": "1"})
        self.assertEqual(response.json(), {"ok": True, "updated": 1, "errors": []})


# ============================================================
# ACCUEIL : PATRIMOINE, MOIS EN COURS, CONSEILS
# ============================================================

from . import home  # noqa: E402
from .views import _monthly_history  # noqa: E402


class HomeTests(BudgetTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="budget", password="pass-12345")
        self.today = date.today()

    def month_day(self, offset, day=10):
        year, month = budget_calc.add_months(self.today.year, self.today.month, offset)
        return date(year, month, min(day, self.today.day) if offset == 0 else day)

    def summary(self):
        return home.home_summary(self.user, self.today, _monthly_history(self.user, self.today))

    def test_home_renders_without_data(self):
        response = self.client.get(reverse("finance:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bonjour budget")
        self.assertContains(response, "Crée ton budget")
        self.assertContains(response, "Trésorerie")
        self.assertContains(response, "Saisir mon solde")
        self.assertContains(response, "Fixe un objectif d'épargne")
        self.assertNotContains(response, "À retenir")

    def test_safety_cushion_in_months_of_expenses(self):
        for offset in (-2, -1):
            self.income("2000", self.month_day(offset))
            self.expense("1000", self.housing, day=self.month_day(offset))
        livret = SavingsAccount.objects.create(user=self.user, kind=SavingsKind.LIVRET_A, name="Livret A")
        BalanceSnapshot.objects.create(account=livret, date=self.today, balance=Decimal("2000"))
        summary = self.summary()
        self.assertEqual(summary["wealth"]["safety_months"], Decimal("2.0"))

    def test_daily_allowance_from_variable_budget(self):
        plan = self.plan(valid_from=date(self.today.year, self.today.month, 1))
        BudgetLine.objects.create(plan=plan, section=BudgetSection.VARIABLE, category=self.food, amount=Decimal("300"))
        self.expense("100", self.food, day=self.month_day(0))
        current = self.summary()["current"]
        self.assertEqual(current["variable_left"], Decimal("200.00"))
        self.assertEqual(current["daily_allowance"], budget_calc.money(Decimal("200") / current["days_left"]))
        response = self.client.get(reverse("finance:dashboard"))
        self.assertContains(response, "Par jour, d'ici la fin du mois")


class LanguageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("langue", password="x")
        create_default_categories(self.user)
        self.client.force_login(self.user)

    def test_french_by_default_even_with_english_browser(self):
        response = self.client.get(reverse("finance:settings"), HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(response, "Paramètres")
        self.assertContains(response, '<html lang="fr">')

    def test_switch_to_english_from_settings(self):
        response = self.client.post(reverse("set_language"), {"language": "en", "next": reverse("finance:settings")})
        self.assertRedirects(response, reverse("finance:settings"), fetch_redirect_response=False)

        page = self.client.get(reverse("finance:settings"))
        self.assertContains(page, '<html lang="en">')
        self.assertContains(page, "Settings")
        self.assertNotContains(page, "Paramètres")
        # Catégories par défaut traduites à l'affichage, inchangées en base
        self.assertContains(self.client.get(reverse("finance:settings") + "?tab=categories"), "Food &amp; dining")
        self.assertTrue(Category.objects.filter(user=self.user, name="Alimentation & restaurants").exists())


import math  # noqa: E402

import numpy as np  # noqa: E402

from . import simulation  # noqa: E402


def synthetic_returns(volatilities, correlation, months=120, seed=7):
    """Rendements log mensuels corrélés, indexés (année, mois)."""
    rng = np.random.default_rng(seed)
    chol = np.linalg.cholesky(np.array(correlation))
    shocks = rng.standard_normal((months, len(volatilities))) @ chol.T
    keys = [(2016 + i // 12, i % 12 + 1) for i in range(months)]
    return [
        {key: float(shocks[t, k] * vol / math.sqrt(12)) for t, key in enumerate(keys)}
        for k, vol in enumerate(volatilities)
    ]


def synthetic_vasicek(theta=0.02, kappa=1.0, vol=0.01, months=360, start=0.05, seed=11):
    rng = np.random.default_rng(seed)
    rates, pi, dt = {}, start, 1 / 12
    for i in range(months):
        rates[(1990 + i // 12, i % 12 + 1)] = pi
        pi += kappa * (theta - pi) * dt + vol * math.sqrt(dt) * rng.standard_normal()
    return rates


class SimulationEngineTests(TestCase):
    def lines(self):
        return [
            {"key": "A", "name": "ETF Monde", "value": 6000, "asset_class": AssetClass.EQUITY},
            {"key": "B", "name": "Action", "value": 4000, "asset_class": AssetClass.EQUITY},
        ]

    def calibration(self, assets, correlation, inflation=None):
        inflation = inflation or simulation.InflationCalibration(target=0.02, start=0.02, speed=0.5, volatility=0.01)
        return simulation.Calibration(assets=assets, inflation=inflation, correlation=np.array(correlation, dtype=float))

    def test_calibration_is_historical(self):
        a, b, market = synthetic_returns([0.15, 0.30, 0.15], [[1, 0.7, 0.95], [0.7, 1, 0.6], [0.95, 0.6, 1]], months=600)
        flat = simulation.calibrate(self.lines(), {"A": a, "B": b}, market, {}).assets[0]
        a = {k: v + 0.005 for k, v in a.items()}  # +0,5 % par mois
        calibration = simulation.calibrate(self.lines(), {"A": a, "B": b}, market, {})

        etf, stock = calibration.assets
        self.assertAlmostEqual(etf.volatility, 0.15, delta=0.015)
        self.assertAlmostEqual(stock.volatility, 0.30, delta=0.03)
        _mu, sigma = simulation.return_statistics(a)
        self.assertAlmostEqual(etf.drift, np.mean(list(a.values())) * 12 + sigma ** 2 / 2, places=9)
        # +0,5 % par mois = exactement +6 % par an sur la dérive, volatilité inchangée
        self.assertAlmostEqual(etf.drift - flat.drift, 0.06, places=9)
        self.assertAlmostEqual(etf.volatility, flat.volatility, places=9)
        self.assertAlmostEqual(calibration.correlation[0, 1], 0.7, delta=0.06)
        self.assertAlmostEqual(etf.beta, 0.95, delta=0.08)
        self.assertEqual(etf.source, "history")
        self.assertIsNotNone(calibration.market)
        # Sans Eurostat : inflation par défaut, non corrélée
        self.assertIn("inflation", calibration.notes)
        self.assertEqual(calibration.correlation.shape, (3, 3))
        self.assertAlmostEqual(calibration.correlation[0, 2], 0, places=6)

    def test_short_history_uses_market_history(self):
        _other, market = synthetic_returns([0.2, 0.15], [[1, 0], [0, 1]], months=120)
        calibration = simulation.calibrate(self.lines(), {"A": {}, "B": {}}, market, {})
        etf, _stock = calibration.assets
        self.assertEqual(etf.source, "market")
        self.assertEqual((etf.drift, etf.volatility), (calibration.market.drift, calibration.market.volatility))
        # Deux lignes calquées sur le marché : parfaitement corrélées
        self.assertAlmostEqual(calibration.correlation[0, 1], 1, places=3)

    def test_no_data_at_all_uses_defaults(self):
        calibration = simulation.calibrate(self.lines(), {}, {}, {})
        self.assertEqual(calibration.assets[0].source, "default")
        self.assertEqual(calibration.assets[0].drift, simulation.FALLBACK_DRIFT)
        self.assertEqual(set(calibration.notes), {"market", "inflation"})

    def test_vasicek_fit_recovers_parameters(self):
        rates = synthetic_vasicek(theta=0.02, kappa=1.0, vol=0.01, months=600)
        inflation, residuals = simulation.fit_vasicek(rates)
        self.assertAlmostEqual(inflation.target, 0.02, delta=0.005)
        self.assertAlmostEqual(inflation.speed, 1.0, delta=0.5)
        self.assertAlmostEqual(inflation.volatility, 0.01, delta=0.0015)
        self.assertEqual(len(residuals), 599)
        self.assertTrue(inflation.estimated)

    def test_inflation_shocks_correlate_with_assets(self):
        rates = synthetic_vasicek(months=120, seed=3)
        _infl, residuals = simulation.fit_vasicek(rates)
        # Une ligne qui baisse quand l'inflation surprend à la hausse
        asset = {k: -4 * e + 0.001 for k, e in residuals.items()}
        calibration = simulation.calibrate(self.lines()[:1], {"A": asset}, {}, rates)
        self.assertLess(calibration.correlation[0, 1], -0.95)

    def test_parse_hicp(self):
        payload = {"dimension": {"time": {"category": {"index": {"2025-11": 0, "2025-12": 1}}}}, "value": {"0": 0.8, "1": 0.7}}
        rates = simulation.parse_hicp(payload)
        self.assertEqual(sorted(rates), [(2025, 11), (2025, 12)])
        self.assertAlmostEqual(rates[(2025, 12)], 0.007)

    def test_nearest_correlation_is_valid(self):
        broken = np.array([[1, 0.9, -0.9], [0.9, 1, 0.9], [-0.9, 0.9, 1]])
        fixed = simulation.nearest_correlation(broken)
        self.assertTrue(np.all(np.linalg.eigvalsh(fixed) > 0))
        np.testing.assert_allclose(np.diag(fixed), 1)
        np.linalg.cholesky(fixed)

    def test_zero_volatility_grows_at_drift_and_inflation_deflates(self):
        assets = [simulation.AssetCalibration(key="A", name="A", value=1000, volatility=1e-9, drift=0.05)]
        inflation = simulation.InflationCalibration(target=0.02, start=0.02, speed=0.5, volatility=0)
        assumptions = simulation.Assumptions(years=10)

        result = simulation.simulate(self.calibration(assets, np.eye(2), inflation), assumptions, cash=100, invested=900)

        self.assertEqual(result["nominal"].shape, (simulation.SCENARIOS, 121))
        np.testing.assert_allclose(result["nominal"][:, -1], 1000 * math.exp(0.5) + 100, rtol=1e-6)
        np.testing.assert_allclose(result["real"][:, -1], (1000 * math.exp(0.5) + 100) * math.exp(-0.2), rtol=1e-6)
        summary = simulation.summarize(result, assumptions)
        self.assertEqual(summary["nominal"]["loss_probability"], 0)
        self.assertAlmostEqual(summary["inflation_median"], 2.0, places=6)

    def test_scenarios_are_correlated_and_reproducible(self):
        assets = [simulation.AssetCalibration(key=k, name=k, value=1000, volatility=0.2, drift=0.06) for k in "AB"]
        calibration = self.calibration(assets, [[1, 0.8, 0], [0.8, 1, 0], [0, 0, 1]])
        first = simulation.simulate(calibration, simulation.Assumptions(years=5, seed=42))
        again = simulation.simulate(calibration, simulation.Assumptions(years=5, seed=42))
        other = simulation.simulate(calibration, simulation.Assumptions(years=5, seed=43))

        np.testing.assert_array_equal(first["nominal"], again["nominal"])
        self.assertFalse(np.array_equal(first["nominal"], other["nominal"]))
        self.assertGreater(np.std(first["nominal"][:, -1]), 100)

    def test_contributions_stop_at_pea_ceiling(self):
        assets = [simulation.AssetCalibration(key="A", name="A", value=1000, volatility=0.01, drift=0.02)]
        assumptions = simulation.Assumptions(years=2, monthly_contribution=1000)

        result = simulation.simulate(self.calibration(assets, np.eye(2)), assumptions, invested=1000, deposits=146_500, ceiling=150_000)

        self.assertEqual(result["invested"][-1], 1000 + 3500)

    def test_month_end_series_and_returns(self):
        closes = [(date(2026, 1, 30), Decimal("100")), (date(2026, 1, 31), Decimal("101")), (date(2026, 2, 27), Decimal("110"))]
        series = simulation.month_end_series(closes)
        self.assertEqual(series, [(2026, 1, 101.0), (2026, 2, 110.0)])
        self.assertAlmostEqual(simulation.log_returns(series)[(2026, 2)], math.log(110 / 101))


class SimulationViewTests(SavingsTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def fake_prices(self, symbol, today=None):
        returns = synthetic_returns([0.15], [[1]], months=60, seed=len(symbol))[0]
        price, series = 100.0, []
        for (year, month), r in returns.items():
            price *= math.exp(r)
            series.append((year, month, price))
        return series

    def test_page_without_pea(self):
        SavingsAccount.objects.all().delete()
        response = self.client.get(reverse("finance:savings_simulation"))
        self.assertContains(response, "Pas encore de PEA")

    def test_simulation_page_renders_scenarios(self):
        self.buy(date(2026, 1, 5), "10", "100")
        self.price(date(2026, 1, 5), "100")
        with mock.patch("finance.simulation.monthly_eur_prices", side_effect=self.fake_prices), \
                mock.patch("finance.simulation.inflation_history", return_value=synthetic_vasicek(months=120)):
            response = self.client.get(reverse("finance:savings_simulation"), {"years": "5", "monthly_contribution": "100", "seed": "3"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "100 scénarios")
        self.assertContains(response, "STOXX Europe 600")
        data = response.context["simulation_data"]
        self.assertEqual(len(data["nominal"]["paths"]), 100)
        self.assertEqual(len(data["labels"]), 61)
        self.assertEqual(len(data["inflation"]["p50"]), 61)
        self.assertEqual(data["assets"][0]["weight"], 100)
        self.assertContains(response, "simHistogramChart")
        self.assertContains(response, "sim-heat-cell")
        self.assertEqual(response.context["summary"]["invested"], 1000 + 60 * 100)
        calibration = response.context["calibration"]
        self.assertEqual(calibration.assets[0].source, "history")
        self.assertTrue(calibration.inflation.estimated)
        self.assertEqual(response.context["correlation_labels"][-1], "Inflation")

    def test_simulation_follows_portfolio_changes(self):
        """Nouvel actif, achat et vente : la simulation repart du portefeuille du jour."""

        def simulate():
            with mock.patch("finance.simulation.monthly_eur_prices", side_effect=self.fake_prices), \
                    mock.patch("finance.simulation.inflation_history", return_value=synthetic_vasicek(months=120)):
                response = self.client.get(reverse("finance:savings_simulation"), {"years": "5", "seed": "3"})
            self.assertEqual(response.status_code, 200)
            assets = {a["key"]: a for a in response.context["simulation_data"]["assets"]}
            return response, assets

        self.buy(date(2026, 1, 5), "10", "100")
        self.price(date(2026, 1, 5), "100")
        response, assets = simulate()
        self.assertEqual(list(assets), ["CW8.PA"])
        self.assertEqual(len(response.context["correlation_rows"]), 2)

        # Nouvel actif : il entre dans la calibration et la matrice de corrélation
        other = Instrument.objects.create(user=self.user, symbol="AI.PA", name="Air Liquide", quote_type="EQUITY")
        self.buy(date(2026, 2, 5), "5", "200", instrument=other)
        self.price(date(2026, 2, 5), "200", instrument=other)
        response, assets = simulate()
        self.assertEqual(set(assets), {"CW8.PA", "AI.PA"})
        self.assertEqual(assets["CW8.PA"]["weight"], 50)
        self.assertEqual(len(response.context["correlation_rows"]), 3)
        self.assertEqual(response.context["start_value"], 2000)

        # Vente partielle : poids et valeur de départ suivent
        self.sell(date(2026, 3, 5), "5", "100")
        response, assets = simulate()
        self.assertAlmostEqual(assets["CW8.PA"]["weight"], 33.3, places=1)
        self.assertEqual(response.context["start_value"], 1500)

        # Vente totale : la ligne disparaît de la simulation
        self.sell(date(2026, 4, 5), "5", "100")
        response, assets = simulate()
        self.assertEqual(list(assets), ["AI.PA"])
        self.assertEqual([row["name"] for row in response.context["correlation_rows"]], ["Air Liquide", "Inflation (France)"])

    def test_new_draw_without_market_data(self):
        self.buy(date(2026, 1, 5), "10", "100")
        with mock.patch("finance.simulation.monthly_eur_prices", return_value=None), \
                mock.patch("finance.simulation.inflation_history", return_value={}):
            response = self.client.get(reverse("finance:savings_simulation"), {"nouveau_tirage": "1", "years": "10", "seed": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["calibration"].assets[0].source, "default")
        self.assertContains(response, "Eurostat indisponible")


# ============================================================
# TYPES DE BUDGET DES CATÉGORIES (fixes / variables)
# ============================================================

from .forms import BudgetLineForm  # noqa: E402
from .models import BudgetSection, Category, Subcategory  # noqa: E402


class BudgetSectionTypeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("typage", password="x")
        create_default_categories(self.user)
        self.client.force_login(self.user)
        self.housing = Category.objects.get(user=self.user, name="Logement & charges")
        self.rent = self.housing.subcategories.get(name="Loyer")
        self.works = self.housing.subcategories.get(name="Travaux, entretien")

    def form(self, section, category, subcategory=None):
        return BudgetLineForm(
            {"section": section, "category": category.pk, "subcategory": subcategory.pk if subcategory else "", "amount": "100"},
            user=self.user,
        )

    def test_default_types(self):
        self.assertEqual(self.rent.budget_section, BudgetSection.FIXED)
        self.assertEqual(self.works.budget_section, BudgetSection.VARIABLE)
        streaming = Subcategory.objects.get(category__user=self.user, name="Streaming vidéo")
        self.assertEqual(streaming.budget_section, BudgetSection.SUBSCRIPTION)

    def test_line_must_match_subcategory_type(self):
        self.assertTrue(self.form(BudgetSection.FIXED, self.housing, self.rent).is_valid())
        self.assertFalse(self.form(BudgetSection.VARIABLE, self.housing, self.rent).is_valid())
        self.assertFalse(self.form(BudgetSection.FIXED, self.housing, self.works).is_valid())

    def test_category_without_matching_subcategory_is_refused(self):
        food = Category.objects.get(user=self.user, name="Alimentation & restaurants")
        self.assertFalse(self.form(BudgetSection.FIXED, food).is_valid())
        self.assertTrue(self.form(BudgetSection.VARIABLE, food).is_valid())

    def test_options_carry_sections(self):
        html = str(self.form(BudgetSection.FIXED, self.housing)["subcategory"])
        self.assertIn(f'value="{self.rent.pk}" data-category="{self.housing.pk}" data-section="FIXED"', html)

    def test_change_type_in_settings(self):
        response = self.client.post(
            reverse("finance:update_budget_section", args=["sous-categorie", self.works.pk]),
            {"budget_section": BudgetSection.FIXED},
        )
        self.assertEqual(response.status_code, 302)
        self.works.refresh_from_db()
        self.assertEqual(self.works.budget_section, BudgetSection.FIXED)
        self.assertTrue(self.form(BudgetSection.FIXED, self.housing, self.works).is_valid())

    def test_settings_split_in_two_groups(self):
        response = self.client.get(reverse("finance:settings"), {"tab": "categories"})
        fixed, variable = response.context["category_groups"]
        fixed_subs = {sub.name for entry in fixed["categories"] for sub in entry["subcategories"]}
        variable_subs = {sub.name for entry in variable["categories"] for sub in entry["subcategories"]}
        self.assertIn("Loyer", fixed_subs)
        self.assertIn("Streaming vidéo", fixed_subs)
        self.assertIn("Supermarché, épicerie", variable_subs)
        self.assertNotIn("Loyer", variable_subs)


# ============================================================
# OBJECTIF D'ÉPARGNE GLOBAL
# ============================================================

from . import savings_goal as goal_calc  # noqa: E402
from .models import SavingsGoal  # noqa: E402


class GlobalSavingsGoalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("objectif", password="x")
        create_default_categories(self.user)
        self.client.force_login(self.user)
        self.today = date(2026, 11, 5)
        self.savings = Category.objects.get(user=self.user, name="Épargne & placements")
        self.salary = Category.objects.get(user=self.user, name="Salaire")
        self.food = Category.objects.get(user=self.user, name="Alimentation & restaurants")
        # 6 mois : 2 500 € de revenus, 1 500 € de charges, 300 € d'épargne
        for offset in range(1, 7):
            year, month = budget_calc.add_months(2026, 11, -offset)
            for category, kind, amount in (
                (self.salary, EntryType.INCOME, "2500"),
                (self.food, EntryType.EXPENSE, "1500"),
                (self.savings, EntryType.EXPENSE, "300"),
            ):
                Transaction.objects.create(
                    user=self.user, category=category, transaction_type=kind,
                    amount=Decimal(amount), transaction_date=date(year, month, 10), description="x",
                )

    def overview(self, target, target_date, rate="0", extra="0", current=Decimal("1000")):
        goal = SavingsGoal(user=self.user, target_amount=Decimal(target), target_date=target_date,
                           expected_return=Decimal(rate), extra_amount=Decimal(extra))
        return goal_calc.goal_overview(goal, self.user, current, self.today)

    def test_formulas(self):
        self.assertAlmostEqual(goal_calc.required_monthly(1000, 13000, 0, 12), 1000)
        rate = goal_calc.monthly_rate(5)
        monthly = goal_calc.required_monthly(0, 10000, rate, 60)
        self.assertAlmostEqual(goal_calc.future_value(0, monthly, rate, 60), 10000, places=6)
        self.assertLess(monthly, 10000 / 60)

    def test_habits(self):
        habits = goal_calc.saving_habits(self.user, self.today)
        self.assertEqual(habits["months"], 6)
        self.assertEqual(habits["savings"], Decimal("300"))
        self.assertEqual(habits["capacity"], Decimal("1000"))

    def test_feasibility_levels(self):
        # 12 versements : 1 000 + 12 × 250 = 4 000 → au rythme actuel (300 €)
        on = self.overview("4000", date(2027, 11, 15))
        self.assertEqual(on["months"], 12)
        self.assertEqual(on["needed"], Decimal("250.00"))
        self.assertEqual(on["status"], "on")
        # 1 000 + 12 × 800 → plus que l'épargne actuelle, moins que le reste après charges
        self.assertEqual(self.overview("10600", date(2027, 11, 15))["status"], "watch")
        off = self.overview("50000", date(2027, 11, 15))
        self.assertEqual(off["status"], "off")
        self.assertEqual(off["reachable_by_date"], Decimal("13000.00"))
        self.assertIsNotNone(off["capacity_reach"])
        self.assertEqual(self.overview("900", date(2027, 11, 15))["status"], "done")

    def test_extra_amount_counts(self):
        self.assertEqual(self.overview("4000", date(2027, 11, 15), extra="3000")["status"], "done")

    def test_page_and_apply_monthly(self):
        response = self.client.post(reverse("finance:update_global_savings_goal"), {
            "target_amount": "20000", "target_date": "2030-01-01", "expected_return": "3", "extra_amount": "0",
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("finance:budget_goals"))
        self.assertContains(response, "goalChart")
        needed = response.context["global_goal"]["needed"]
        self.assertGreater(needed, 0)
        self.client.post(reverse("finance:apply_global_goal_monthly"))
        plan = budget_calc.plan_for_month(self.user, date.today().year, date.today().month)
        self.assertGreaterEqual(plan.savings_target_monthly, needed.quantize(Decimal("1")) - 1)

    def test_invalid_goal_shows_errors_inline(self):
        response = self.client.post(reverse("finance:update_global_savings_goal"), {
            "target_amount": "20000", "target_date": "2020-01-01", "expected_return": "3", "extra_amount": "0",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choisis une date dans le futur.")
        self.assertFalse(SavingsGoal.objects.exists())


# ============================================================
# SAUVEGARDES, EXPORT, TRÉSORERIE
# ============================================================

from datetime import datetime  # noqa: E402

from . import backups as backup_calc  # noqa: E402
from . import forecast as forecast_calc  # noqa: E402
from .ca_csv import AccountBalance as CaBalance  # noqa: E402
from .models import CurrentAccount, CurrentAccountBalance, RecurrenceKind, RecurringItem  # noqa: E402


class BackupTests(TransactionTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.enterContext(override_settings(BACKUP_DIR=self.tmp / "backups", AUTO_BACKUP_KEEP=2))
        self.user = get_user_model().objects.create_user("sauve", password="x")
        self.client.force_login(self.user)

    def test_create_list_and_prune(self):
        for day in (1, 2, 3):
            backup_calc.create_backup(backup_calc.AUTO, now=datetime(2026, 11, day, 8, 0))
        backup_calc.create_backup(backup_calc.MANUAL, now=datetime(2026, 11, 1, 9, 0))
        rows = backup_calc.list_backups()
        self.assertEqual([r["label"] for r in rows].count("auto"), 2)
        self.assertEqual([r["label"] for r in rows].count("manuelle"), 1)
        backup_calc.check_backup_file(rows[0]["path"])

    def test_daily_backup_only_once(self):
        today = date(2026, 11, 5)
        with mock.patch.object(backup_calc, "datetime") as fake:
            fake.now.return_value = datetime(2026, 11, 5, 10, 0)
            fake.strptime = datetime.strptime
            self.assertIsNotNone(backup_calc.backup_if_due(today))
            self.assertIsNone(backup_calc.backup_if_due(today))

    def test_restore_brings_back_old_data(self):
        path = backup_calc.create_backup(backup_calc.MANUAL)
        get_user_model().objects.create_user("apres", password="x")
        backup_calc.restore_backup(path)
        self.assertFalse(get_user_model().objects.filter(username="apres").exists())
        labels = [r["label"] for r in backup_calc.list_backups()]
        self.assertIn("avant-restauration", labels)

    def test_rejects_other_files(self):
        bad = self.tmp / "pas-une-base.sqlite3"
        bad.write_bytes(b"bonjour")
        with self.assertRaises(backup_calc.BackupError):
            backup_calc.check_backup_file(bad)
        with self.assertRaises(backup_calc.BackupError):
            backup_calc.backup_path("../db.sqlite3")

    def test_settings_tab_and_download(self):
        self.client.post(reverse("finance:backup_now"))
        name = backup_calc.list_backups()[0]["name"]
        page = self.client.get(reverse("finance:settings"), {"tab": "sauvegarde", "etat": "sauvegarde"})
        self.assertContains(page, "Sauvegarde enregistrée.")
        self.assertContains(page, name.split("-")[1][:4])
        response = self.client.get(reverse("finance:download_backup", args=[name]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Disposition"], f'attachment; filename="{name}"')


class ExportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("export", password="x")
        create_default_categories(self.user)
        self.client.force_login(self.user)
        food = Category.objects.get(user=self.user, name="Alimentation & restaurants")
        Transaction.objects.create(user=self.user, category=food, transaction_type=EntryType.EXPENSE,
                                   amount=Decimal("12.50"), transaction_date=date(2026, 10, 3), description="Boulangerie")

    def test_csv(self):
        response = self.client.get(reverse("finance:export_transactions_csv"))
        content = response.content.decode("utf-8-sig")
        self.assertIn("Boulangerie", content)
        self.assertIn("-12,50", content)

    def test_workbook(self):
        from openpyxl import load_workbook
        import io as _io

        response = self.client.get(reverse("finance:export_workbook"))
        book = load_workbook(_io.BytesIO(response.content))
        self.assertIn("Opérations", book.sheetnames)
        self.assertIn("Catégories", book.sheetnames)
        self.assertEqual(book["Opérations"]["F2"].value, "Boulangerie")
        self.assertEqual(book["Opérations"]["H2"].value, -12.5)


class ForecastTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("treso", password="x")
        create_default_categories(self.user)
        self.client.force_login(self.user)
        self.today = date(2026, 11, 5)
        self.salary = Category.objects.get(user=self.user, name="Salaire")
        self.food = Category.objects.get(user=self.user, name="Alimentation & restaurants")
        self.housing = Category.objects.get(user=self.user, name="Logement & charges")

    def tx(self, category, kind, amount, day, **extra):
        return Transaction.objects.create(user=self.user, category=category, transaction_type=kind,
                                          amount=Decimal(amount), transaction_date=day, description="x", **extra)

    def test_current_balance_imported_from_bank(self):
        snapshot = forecast_calc.import_current_balance(self.user, [
            CaBalance("Livret A", "111", date(2026, 11, 4), Decimal("5000")),
            CaBalance("Compte de Dépôt", "222", date(2026, 11, 4), Decimal("1234.56")),
        ])
        self.assertEqual(snapshot.balance, Decimal("1234.56"))
        self.assertEqual(CurrentAccount.objects.get(user=self.user).bank_account_number, "222")

    def test_forecast_events_and_low_point(self):
        forecast_calc.set_manual_balance(self.user, date(2026, 11, 3), Decimal("800"))
        # Dépense saisie après le relevé : elle compte déjà
        self.tx(self.food, EntryType.EXPENSE, "50", date(2026, 11, 4))
        # Loyer le 10 de chaque mois
        RecurringItem.objects.create(user=self.user, name="Loyer", entry_type=EntryType.EXPENSE, category=self.housing,
                                     default_amount=Decimal("700"), start_date=date(2026, 1, 10))
        # Salaire reçu le 28 des mois précédents
        for month in (8, 9, 10):
            self.tx(self.salary, EntryType.INCOME, "2000", date(2026, month, 28), actual_date=date(2026, month, 28))
        result = forecast_calc.forecast(self.user, self.today, "30")
        self.assertEqual(result["current"], Decimal("750.00"))
        labels = [(e["date"], e["label"]) for e in result["events"]]
        self.assertIn((date(2026, 11, 10), "Loyer"), labels)
        self.assertTrue(any(label.startswith("Salaire") and day == date(2026, 11, 28) for day, label in labels))
        # Point bas juste après le loyer, avant le salaire
        self.assertTrue(date(2026, 11, 10) <= result["low"]["date"] < date(2026, 11, 28))
        self.assertLess(result["low"]["balance"], Decimal("100"))
        self.assertIn(result["status"], {"watch", "off"})

    def test_subscription_expected_after_last_payment(self):
        item = RecurringItem.objects.create(user=self.user, name="Netflix", kind=RecurrenceKind.SUBSCRIPTION,
                                            entry_type=EntryType.EXPENSE, default_amount=Decimal("13.49"),
                                            match_key="netflix", start_date=date(2026, 1, 12))
        self.tx(None, EntryType.EXPENSE, "13.49", date(2026, 10, 12), recurring_item=item)
        forecast_calc.set_manual_balance(self.user, self.today, Decimal("500"))
        result = forecast_calc.forecast(self.user, self.today, "30")
        self.assertIn((date(2026, 11, 12), "Netflix"), [(e["date"], e["label"]) for e in result["events"]])

    def test_page(self):
        response = self.client.get(reverse("finance:budget_cashflow"))
        self.assertContains(response, "Quel est le solde de ton compte courant")
        response = self.client.post(reverse("finance:update_current_balance"),
                                    {"date": date.today().isoformat(), "balance": "1500", "safety_buffer": "300"})
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("finance:budget_cashflow"))
        self.assertContains(response, "cashChart")
        self.assertEqual(CurrentAccount.objects.get(user=self.user).safety_buffer, Decimal("300"))
        self.assertEqual(CurrentAccountBalance.objects.count(), 1)
