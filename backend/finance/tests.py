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
from .bank_sync import initial_period, is_month_syncable, run_bank_sync, sync_period
from .ca_csv import parse_ca_csv
from .default_categories import create_default_categories
from .models import BankConnection, BankSync, BankSyncKind, BankSyncStatus, EntryType, Transaction, TransactionSource


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
            return write_sample_export(kwargs["target_dir"])

        with mock.patch("finance.bank_sync.download_ca_operations", side_effect=fake_download):
            run_bank_sync(sync.pk)

        sync.refresh_from_db()
        self.assertEqual(sync.status, BankSyncStatus.DONE, sync.message)
        self.assertTrue(BankConnection.objects.get(user=self.user).initial_import_done)


class OnboardingViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("jean", password="x")
        self.client.force_login(self.user)

    def test_signup_redirects_to_bank_connect(self):
        self.client.logout()
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
    SavingsProject,
    Subcategory,
)


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
        self.income("2800", date(2026, 7, 28))
        self.income("3000", date(2026, 9, 28))
        self.income("9999", date(2026, 10, 1))  # mois en cours : exclu
        average, months = budget_calc.average_salary(self.user, 2026, 10, 6)
        self.assertEqual(months, 2)
        self.assertEqual(average, Decimal("2900.00"))

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
        self.income("1741.70", date(2026, 7, 28))
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
        self.income("1800", date(2026, 7, 28))
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
