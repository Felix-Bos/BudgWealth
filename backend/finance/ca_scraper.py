"""
Téléchargement automatique des opérations Crédit Agricole.

Déroulé :
  1. Chrome est lancé puis réduit : l'utilisateur ne voit pas les étapes automatiques ;
  2. choix de la caisse régionale via le code postal, saisie de l'identifiant ;
  3. la fenêtre s'affiche UNIQUEMENT pour que l'utilisateur tape son code personnel
     sur le clavier virtuel du site (et valide Secur'Pass si demandé) ;
  4. dès la connexion détectée, la fenêtre est à nouveau réduite ;
  5. téléchargement du CSV de la période demandée, puis fermeture de Chrome.

Chaque synchronisation part d'un navigateur neuf (aucun profil ni cookie conservé) :
l'utilisateur retape son code à chaque fois et aucune session bancaire ne reste
sur le disque. Le code personnel ne transite jamais par l'application.

Test en ligne de commande (depuis backend/) :
    python -m finance.ca_scraper --login 12345678901 --cp 75001 --du 01/09/2026 --au 30/09/2026
"""

import argparse
import re
import shutil
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    sync_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)


REGION_URL = (
    "https://www.credit-agricole.fr/particulier/acces-cr.html"
    "?origin=/content/ca/national/npc/fr/particulier/acceder-a-mes-comptes.html"
)

# Iframe qui contient le formulaire de téléchargement
FORM_IFRAME = "#TELECHARGER-OPERATIONS"

# Temps laissé à l'utilisateur pour taper son code (+ Secur'Pass éventuel)
LOGIN_TIMEOUT_MS = 3 * 60 * 1000

DOWNLOAD_TIMEOUT_S = 60

# Taille de la fenêtre affichée pour la saisie du code
LOGIN_WINDOW = {"left": 120, "top": 80, "width": 520, "height": 820}


class BankSyncError(Exception):
    """Erreur à afficher telle quelle à l'utilisateur."""


# ============================================================
# OUTILS
# ============================================================

def click_first(candidates, timeout=3000):
    """
    Clique sur le premier locator visible parmi `candidates`.
    Retourne True si un clic a eu lieu.
    """

    for locator in candidates:

        try:
            locator.first.wait_for(state="visible", timeout=timeout)
            locator.first.click()
            return True
        except PlaywrightTimeoutError:
            continue

    return False


def is_logged_in(url):
    """
    Connecté = sur une page de l'espace client, ex.
    https://espace-client.credit-agricole.fr/ca-paris/particulier/synthese

    On ignore la racine ".../ca-paris/particulier" : le site y passe aussi
    sans être connecté, avant de rediriger vers la page de connexion.
    On teste le domaine (et pas l'URL complète) car la page de connexion
    contient elle aussi cette adresse dans ses paramètres.
    """

    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]

    return parsed.hostname == "espace-client.credit-agricole.fr" and len(segments) >= 3


def check_box(checkbox):
    """
    Coche un <mds-checkbox> (case personnalisée du site) s'il ne l'est pas déjà.
    """

    if checkbox.get_attribute("aria-checked") != "true":
        checkbox.click()


# ============================================================
# SESSION DE NAVIGATION
# ============================================================

class CreditAgricoleScraper:
    """
    Pilote une session Chrome pour un utilisateur.

    `on_status(status, message)` est appelé à chaque étape, avec status parmi
    CONNECTING, WAITING_PASSWORD, DOWNLOADING (cf. finance.models.BankSyncStatus).
    """

    def __init__(self, login, postal_code, on_status=None):

        self.login = login
        self.postal_code = postal_code
        self.on_status = on_status or (lambda status, message="": None)

    # --------------------------------------------------------
    # Fenêtre
    # --------------------------------------------------------

    def _window(self, bounds):

        self.cdp.send(
            "Browser.setWindowBounds",
            {"windowId": self.window_id, "bounds": bounds},
        )

    def hide_window(self):

        self._window({"windowState": "minimized"})

    def show_window(self):

        self._window({"windowState": "normal"})
        self._window(LOGIN_WINDOW)
        self.page.bring_to_front()

    # --------------------------------------------------------
    # Connexion
    # --------------------------------------------------------

    def accept_cookies(self):

        page = self.page

        click_first([
            page.locator("#popin_tc_privacy_button_3"),  # Continuer sans accepter
            page.get_by_role("button", name="Continuer sans accepter"),
        ], timeout=5000)

    def open_login_page(self):

        page = self.page

        page.goto(REGION_URL, wait_until="domcontentloaded")

        self.accept_cookies()

        search = page.locator("#GeolocSearchInput")

        # Si le site nous a quand même géolocalisés, il propose une liste de caisses
        # à la place du champ : "Autre caisse régionale" ramène au code postal
        if not search.is_visible():
            click_first([
                page.get_by_role("button", name=re.compile(r"autre caisse r[ée]gionale", re.I)),
                page.get_by_role("link", name=re.compile(r"autre caisse r[ée]gionale", re.I)),
            ], timeout=5000)

        search.fill(self.postal_code)
        page.get_by_role("button", name="Rechercher une caisse régionale").click()

        # La recherche redirige vers la page de connexion de la caisse,
        # ou directement vers l'espace client si la session précédente est encore valide
        try:
            page.wait_for_url(
                lambda url: urlparse(url).hostname == "client.ca-connect.credit-agricole.fr"
                or is_logged_in(url),
                timeout=20000,
            )
        except PlaywrightTimeoutError:
            raise BankSyncError(
                "Caisse régionale introuvable : vérifiez le code postal dans les paramètres."
            )

    def log_in(self):
        """
        Remplit l'identifiant, affiche la fenêtre pour la saisie du code,
        puis la recache dès que l'utilisateur est connecté.
        """

        page = self.page

        field = page.locator('input[name="identifiant"]')
        field.wait_for(state="visible", timeout=15000)
        field.fill(self.login)

        page.get_by_role("button", name=re.compile("valider", re.I)).first.click()

        self.on_status("WAITING_PASSWORD", "Saisissez votre code personnel dans la fenêtre Crédit Agricole.")
        self.show_window()

        try:
            page.wait_for_url(lambda url: is_logged_in(url), timeout=LOGIN_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            raise BankSyncError("Code personnel non saisi à temps : relancez la synchronisation.")

        self.hide_window()
        self.on_status("CONNECTING", "Connecté.")

        page.wait_for_load_state("networkidle")

    # --------------------------------------------------------
    # Formulaire de téléchargement (dans une iframe)
    # --------------------------------------------------------

    def go_to_download_page(self):
        """
        Va directement sur la page de téléchargement des opérations.
        Si la session a expiré, le site redirige vers la page de connexion :
        on reconnecte l'utilisateur, puis le site revient tout seul ici.
        """

        page = self.page

        region = urlparse(page.url).path.strip("/").split("/")[0]

        page.goto(
            f"https://espace-client.credit-agricole.fr/{region}"
            "/particulier/operations-courantes/telechargement-operations",
            wait_until="domcontentloaded",
        )

        login_field = page.locator('input[name="identifiant"]')

        # On attend soit le formulaire de téléchargement, soit la page de connexion
        page.locator(FORM_IFRAME).or_(login_field).first.wait_for(
            state="visible",
            timeout=30000,
        )

        if login_field.is_visible():
            self.log_in()
            page.locator(FORM_IFRAME).wait_for(state="visible", timeout=30000)

    def select_all_accounts(self, form):

        check_box(
            form.locator("mds-checkbox", has_text=re.compile(r"s[ée]lectionner tous mes comptes", re.I))
        )

        # Vérification : chaque carte de compte doit être cochée
        accounts = form.locator("app-account-card mds-checkbox")

        for i in range(accounts.count()):
            check_box(accounts.nth(i))

    def select_csv_format(self, form):

        form.locator('mds-select[name="exportFormat"]').click()
        form.locator("mds-option", has_text=re.compile(r"^\s*csv\s*$", re.I)).click()

    def fill_date(self, form, name, value):
        """
        Remplit un <mds-date-picker>. Le champ est découpé en trois cases
        (role="spinbutton") : Jour / Mois / Année, remplies une par une.
        """

        page = self.page
        picker = form.locator(f'mds-date-picker[name="{name}"]')

        for segment, number, digits in [
            ("Jour", value.day, f"{value.day:02d}"),
            ("Mois", value.month, f"{value.month:02d}"),
            ("Année", value.year, f"{value.year:04d}"),
        ]:

            field = picker.get_by_role("spinbutton", name=segment, exact=True)

            field.focus()

            # On sélectionne le contenu de la case pour le remplacer
            page.keyboard.press("ControlOrMeta+A")

            page.keyboard.type(digits, delay=80)

            if field.get_attribute("aria-valuenow") != str(number):
                raise BankSyncError(f"Impossible de remplir la date ({segment}).")

        page.keyboard.press("Tab")

    def fill_period(self, form, start, end):

        form.locator("mds-radio", has_text=re.compile(r"p[ée]riode personnalis[ée]e", re.I)).click()

        self.fill_date(form, "startDate", start)
        self.fill_date(form, "endDate", end)

    def wait_for_downloaded_file(self):
        """
        Attend qu'un fichier complet apparaisse dans le dossier de téléchargement
        (Chrome utilise l'extension .crdownload tant que ce n'est pas fini).
        """

        deadline = time.time() + DOWNLOAD_TIMEOUT_S

        while time.time() < deadline:

            files = [
                f for f in self.download_dir.iterdir()
                if not f.name.endswith(".crdownload")
            ]

            if files:
                return files[0]

            time.sleep(0.5)

        raise BankSyncError("La banque n'a pas envoyé le fichier des opérations.")

    def download_csv(self, start, end):

        page = self.page

        self.go_to_download_page()

        form = page.frame_locator(FORM_IFRAME)

        # Attend que le formulaire soit chargé dans l'iframe
        form.locator("app-account-card").first.wait_for(state="visible", timeout=20000)

        self.select_all_accounts(form)
        self.select_csv_format(form)
        self.fill_period(form, start, end)

        # Le site affiche une fenêtre "Téléchargement en cours" puis envoie le fichier
        form.locator(
            "mds-button", has_text=re.compile(r"t[ée]l[ée]charger mes op[ée]rations", re.I)
        ).click()

        return self.wait_for_downloaded_file()

    # --------------------------------------------------------
    # Point d'entrée
    # --------------------------------------------------------

    def run(self, start, end, target_dir):
        """
        Télécharge les opérations de `start` à `end` dans `target_dir`.
        Renvoie le chemin du CSV brut.
        """

        target_dir = Path(target_dir)
        self.download_dir = Path(tempfile.mkdtemp(prefix="ca_download_"))

        try:

            with sync_playwright() as p:

                # Navigateur neuf à chaque fois : pas de profil conservé qui
                # pourrait se corrompre après une session interrompue.
                # Aucune autorisation accordée : la géolocalisation est refusée,
                # la caisse est choisie uniquement via le code postal.
                browser = p.chromium.launch(
                    headless=False,
                    args=[f"--window-size={LOGIN_WINDOW['width']},{LOGIN_WINDOW['height']}"],
                )
                context = browser.new_context(
                    no_viewport=True,
                    accept_downloads=True,
                    locale="fr-FR",
                )

                try:

                    self.page = context.new_page()
                    self.cdp = context.new_cdp_session(self.page)
                    self.window_id = self.cdp.send("Browser.getWindowForTarget")["windowId"]

                    self.hide_window()

                    # Chrome enregistre lui-même le fichier : plus fiable que l'objet
                    # Download de Playwright, perdu si la page se ferme avant la fin
                    browser_context_id = self.cdp.send("Target.getTargetInfo")["targetInfo"]["browserContextId"]
                    self.cdp.send(
                        "Browser.setDownloadBehavior",
                        {
                            "behavior": "allow",
                            "downloadPath": str(self.download_dir),
                            "browserContextId": browser_context_id,
                        },
                    )

                    self.on_status("CONNECTING", "Connexion au Crédit Agricole…")
                    self.open_login_page()

                    if not is_logged_in(self.page.url):
                        self.log_in()

                    self.on_status("DOWNLOADING", "Téléchargement des opérations…")
                    downloaded = self.download_csv(start, end)

                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / f"ca_operations_{datetime.now():%Y%m%d_%H%M%S}.csv"
                    shutil.move(downloaded, target)

                    return target

                except PlaywrightError as error:

                    if "closed" in str(error).lower():
                        raise BankSyncError(
                            "La fenêtre Crédit Agricole s'est fermée avant la fin. Relancez la synchronisation."
                        )

                    raise BankSyncError(
                        "Le site du Crédit Agricole ne répond pas comme prévu. Réessayez dans un instant."
                    ) from error

                finally:

                    # Si Chrome a planté, la fermeture échoue : on l'ignore
                    try:
                        browser.close()
                    except PlaywrightError:
                        pass

        finally:

            shutil.rmtree(self.download_dir, ignore_errors=True)


def download_ca_operations(login, postal_code, start, end, target_dir, on_status=None):

    scraper = CreditAgricoleScraper(login, postal_code, on_status)

    return scraper.run(start, end, target_dir)


# ============================================================
# TEST EN LIGNE DE COMMANDE
# ============================================================

def main():

    parse = lambda text: datetime.strptime(text, "%d/%m/%Y").date()

    parser = argparse.ArgumentParser(description="Télécharge les opérations Crédit Agricole.")
    parser.add_argument("--login", required=True, help="identifiant 11 chiffres")
    parser.add_argument("--cp", required=True, help="code postal de l'agence")
    parser.add_argument("--du", type=parse, default=date.today().replace(day=1))
    parser.add_argument("--au", type=parse, default=date.today())
    args = parser.parse_args()

    path = download_ca_operations(
        args.login,
        args.cp,
        args.du,
        args.au,
        target_dir=Path.cwd(),
        on_status=lambda status, message="": print(f"[{status}] {message}"),
    )

    print("Fichier téléchargé :", path)


if __name__ == "__main__":
    main()
