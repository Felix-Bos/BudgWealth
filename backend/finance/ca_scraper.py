"""
Téléchargement automatique des opérations Crédit Agricole.

Déroulé :
  1. Chrome est lancé puis réduit : l'utilisateur ne voit pas les étapes automatiques ;
  2. choix de la caisse régionale via le code postal, saisie de l'identifiant ;
  3. la fenêtre s'affiche UNIQUEMENT pour que l'utilisateur tape son code personnel
     sur le clavier virtuel du site (et valide Secur'Pass si demandé) ;
  4. dès la connexion détectée, la fenêtre est à nouveau réduite ;
  5. téléchargement du CSV de la période demandée ;
  6. si des livrets sont absents de ce relevé (aucune opération sur la période),
     second relevé sur 12 mois pour ces seuls comptes, afin d'avoir leur solde ;
  7. fermeture de Chrome.

Chaque synchronisation part d'un navigateur neuf (aucun profil ni cookie conservé) :
l'utilisateur retape son code à chaque fois et aucune session bancaire ne reste
sur le disque. Le code personnel ne transite jamais par l'application.

Test en ligne de commande (depuis backend/) :
    python -m finance.ca_scraper --login 12345678901 --cp 75001 --du 01/09/2026 --au 30/09/2026
"""

import argparse
import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    sync_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)


logger = logging.getLogger(__name__)

REGION_URL = (
    "https://www.credit-agricole.fr/particulier/acces-cr.html"
    "?origin=/content/ca/national/npc/fr/particulier/acceder-a-mes-comptes.html"
)

# Iframe qui contient le formulaire de téléchargement
FORM_IFRAME = "#TELECHARGER-OPERATIONS"

# Liste les éléments du formulaire (hors cartes de comptes) : "balise[name][role] libellé"
FORM_OUTLINE_JS = """
body => [...body.querySelectorAll('*')]
    .filter(el => !el.closest('app-account-card'))
    .filter(el => el.tagName.startsWith('MDS-') || el.hasAttribute('role')
        || ['INPUT', 'BUTTON', 'LABEL'].includes(el.tagName))
    .slice(0, 150)
    .map(el => {
        const name = el.getAttribute('name') ? `[name=${el.getAttribute('name')}]` : '';
        const role = el.getAttribute('role') ? `[role=${el.getAttribute('role')}]` : '';
        const text = (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
        return `${el.tagName.toLowerCase()}${name}${role} ${text}`;
    })
"""

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


def check_box(checkbox, checked=True):
    """
    Coche (ou décoche) un <mds-checkbox>, case personnalisée du site,
    s'il n'est pas déjà dans l'état voulu.
    """

    if (checkbox.get_attribute("aria-checked") == "true") != checked:
        checkbox.click()


@dataclass
class Download:
    path: Path                                          # relevé de la période demandée
    balance_paths: list = field(default_factory=list)   # relevés pris pour le solde des livrets absents
    account_cards: list = field(default_factory=list)   # texte des cartes de comptes du formulaire


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
        self.account_cards = []

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
        """
        Choisit le format CSV. Le libellé de l'option a déjà changé côté site
        ("CSV" seul auparavant) : on accepte tout libellé contenant le mot CSV.
        """

        select = form.locator('mds-select[name="exportFormat"]')
        csv = re.compile(r"\bcsv\b", re.I)

        select.click()

        if click_first([
            form.locator("mds-option", has_text=csv),
            form.get_by_role("option", name=csv),
        ], timeout=5000):
            return

        raise BankSyncError("Le site ne propose plus le format CSV : synchronisation impossible.")

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

        # Le libellé était "Période personnalisée" (bouton radio) jusqu'en octobre 2026 :
        # on accepte une autre balise ou tout libellé contenant "personnalisé"
        custom = re.compile(r"personnalis", re.I)

        clicked = click_first([
            form.locator("mds-radio", has_text=custom),
            form.get_by_role("radio", name=custom),
            form.get_by_text(custom),
        ])

        # Sans choix à faire, les dates peuvent être affichées d'office
        if not clicked and not form.locator('mds-date-picker[name="startDate"]').is_visible():
            raise BankSyncError("Choix de la période introuvable sur le site.")

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

    def download_csv(self, start, end, account_numbers=None):
        """
        Télécharge le relevé de `start` à `end` : tous les comptes, ou
        seulement ceux de `account_numbers`.
        """

        page = self.page

        self.go_to_download_page()

        form = page.frame_locator(FORM_IFRAME)

        # Attend que le formulaire soit chargé dans l'iframe
        form.locator("app-account-card").first.wait_for(state="visible", timeout=20000)

        try:

            if account_numbers is None:
                self.select_all_accounts(form)
                self.account_cards = self.read_account_cards(form)
            else:
                self.select_accounts(form, account_numbers)
            self.select_csv_format(form)
            self.fill_period(form, start, end)

            # Le site affiche une fenêtre "Téléchargement en cours" puis envoie le fichier
            form.locator(
                "mds-button", has_text=re.compile(r"t[ée]l[ée]charger mes op[ée]rations", re.I)
            ).click()

        except (PlaywrightTimeoutError, BankSyncError):
            self.log_form_outline(form)
            raise

        return self.wait_for_downloaded_file()

    def select_accounts(self, form, numbers):
        """
        Ne coche que les comptes dont le numéro est dans `numbers`.
        """

        cards = form.locator("app-account-card")

        for i in range(cards.count()):
            card = cards.nth(i)
            digits = re.sub(r"\D", "", card.inner_text())
            check_box(card.locator("mds-checkbox"), any(number in digits for number in numbers))

    def read_account_cards(self, form):
        """
        Texte de chaque carte de compte du formulaire (libellé, numéro, solde).
        Le relevé CSV ne contient pas toujours tous les comptes : ces cartes
        permettent de relever quand même le solde de chaque livret.
        """

        try:
            return form.locator("app-account-card").all_inner_texts()
        except PlaywrightError:
            return []

    def log_form_outline(self, form):
        """
        Écrit dans les logs le plan du formulaire (balises, noms, libellés),
        pour adapter les sélecteurs quand le site change. Le contenu des
        cartes de comptes (numéros, soldes) est exclu.
        """

        try:
            lines = form.locator("body").evaluate(FORM_OUTLINE_JS)
        except PlaywrightError:
            return

        logger.warning("Plan du formulaire de téléchargement :\n%s", "\n".join(lines))

    # --------------------------------------------------------
    # Point d'entrée
    # --------------------------------------------------------

    def run(self, start, end, target_dir, balance_accounts=None, balance_start=None):
        """
        Télécharge les opérations de `start` à `end` dans `target_dir`.

        `balance_accounts(chemin_du_relevé, cartes)` renvoie les numéros des
        comptes dont il manque le solde : ils sont alors téléchargés une
        seconde fois, de `balance_start` à `end`.
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

                    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / f"ca_operations_{stamp}.csv"
                    shutil.move(downloaded, target)
                    download = Download(path=target, account_cards=self.account_cards)

                    missing = balance_accounts(target, self.account_cards) if balance_accounts and balance_start else []
                    if missing:
                        self.on_status("DOWNLOADING", "Relevé du solde des livrets…")
                        try:
                            downloaded = self.download_csv(balance_start, end, account_numbers=missing)
                        except (PlaywrightError, BankSyncError):
                            # Les opérations sont déjà là : seul le solde de ces livrets manquera
                            logger.warning("Second relevé (solde des livrets) impossible", exc_info=True)
                        else:
                            balance_target = target_dir / f"ca_soldes_{stamp}.csv"
                            shutil.move(downloaded, balance_target)
                            download.balance_paths.append(balance_target)

                    return download

                except PlaywrightError as error:

                    if "closed" in str(error).lower():
                        raise BankSyncError(
                            "La fenêtre Crédit Agricole s'est fermée avant la fin. Relancez la synchronisation."
                        )

                    # Le message affiché reste générique : le détail (élément introuvable,
                    # page en cours) ne sert qu'à diagnostiquer un changement du site
                    logger.exception(
                        "Erreur Playwright pendant la synchronisation (page : %s)",
                        urlparse(self.page.url).path if hasattr(self, "page") else "-",
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


def download_ca_operations(
    login, postal_code, start, end, target_dir, on_status=None, balance_accounts=None, balance_start=None
):
    """
    Renvoie un Download : relevé de la période, relevés complémentaires pour
    le solde des livrets absents (voir CreditAgricoleScraper.run), cartes de comptes.
    """

    scraper = CreditAgricoleScraper(login, postal_code, on_status)

    return scraper.run(start, end, target_dir, balance_accounts, balance_start)


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

    download = download_ca_operations(
        args.login,
        args.cp,
        args.du,
        args.au,
        target_dir=Path.cwd(),
        on_status=lambda status, message="": print(f"[{status}] {message}"),
    )

    print("Fichier téléchargé :", download.path)


if __name__ == "__main__":
    main()
