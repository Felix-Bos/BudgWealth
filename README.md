# BudgWealth

**Application de finances personnelles, 100 % locale.** BudgWealth importe automatiquement tes opérations Crédit Agricole, les catégorise et sépare revenus, charges et épargne. Tu construis ton budget, tu suis tes objectifs d'épargne et tes projets, et tes abonnements sont détectés tout seuls, avec une alerte quand l'un d'eux s'arrête. Côté épargne, tu suis tes livrets, ton PEA et ton compte-titres : valorisation avec les cours Yahoo Finance, frais et allocation.

Django · SQLite (ou PostgreSQL) · Playwright · yfinance · Chart.js · application macOS autonome

---

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Confidentialité et sécurité](#confidentialité-et-sécurité)
- [Démarrage rapide](#démarrage-rapide)
- [Configuration](#configuration)
- [Utilisation](#utilisation)
- [Comment ça marche](#comment-ça-marche)
- [Architecture](#architecture)
- [Développement](#développement)
- [Construire l'application macOS](#construire-lapplication-macos)
- [Dépannage](#dépannage)
- [Limites connues et pistes](#limites-connues-et-pistes)

---

## Fonctionnalités

### Import bancaire automatique (Crédit Agricole)
- **Import initial** des 12 derniers mois en une fois, puis synchronisation mois par mois.
- **Tu tapes toi-même ton code** sur le clavier virtuel du site de la banque. L'application ne le voit jamais.
- **Catégorisation automatique** par règles (commerçants, prélèvements, salaire, CAF…).
- **Virements vers ton épargne** reconnus et comptés comme de l'épargne, jamais comme des charges.
- **Aucun doublon** : chaque opération a une empreinte, donc resynchroniser un mois n'ajoute rien en double.

### Accueil (`/finance/dashboard/`)
- **Patrimoine financier** : livrets + placements, répartition, versements des 6 derniers mois (ou évolution du patrimoine dès qu'il y a un historique de soldes).
- **Le mois en cours** : jauge du budget dépensé avec le rythme du mois, reste à dépenser et **montant disponible par jour** pour les dépenses variables. Sans budget : revenus, charges et épargne du mois comparés à ta moyenne.
- **À retenir** : des conseils calculés sur tes chiffres et triés par importance (budget en danger, catégorie qui s'emballe, taux d'épargne, matelas de sécurité en mois de dépenses, livret presque plein, projet en retard, abonnements arrêtés ou à valider, opérations à classer…).
- **Tes repères**, les 6 derniers mois, où part ton argent, dernières opérations, projets et prochains prélèvements.

### Vision d'un mois (`/finance/<année>/<mois>/`)
- **Revenus, charges, épargne et solde**, bien distingués par la couleur et un badge.
- **Liste des opérations** avec filtres (Tout · Revenus · Charges · Épargne) et totaux par type.
- **Édition sur place, sans recharger la page** : intitulé, catégorie, sous-catégorie, suppression, et « Suivre comme abonnement ».
- **Anneau des charges par catégorie** : un clic sur une part affiche ses sous-catégories.
- **Cumul jour par jour** des revenus, charges et épargne, face au rythme du budget.
- **Abonnements du mois** : payés, à venir, en attente, non payés.
- **Suivi du budget du mois** poste par poste.

### Budget (`/finance/dashboard/budget/`), en 4 onglets

| Onglet | Contenu |
|---|---|
| **Analyse** | Vision globale sur 12 mois : moyennes, taux d'épargne, mois à l'équilibre, budget tenu X / Y mois, évolution mensuelle (un clic ouvre le mois), réussite du budget, charges par catégorie dans le temps, postes dépassés régulièrement, abonnements, projets |
| **Mon budget** | Budget type en étapes numérotées : revenu de référence (moyenne de tes salaires ou montant fixe), charges fixes, variables, abonnements, provisions annuelles, épargne visée (suggestions en un clic : 10 / 15 / 20 %, projets, tout le reste). Reste à vivre, répartition des ressources et repère 50 / 30 / 20 calculés en direct, tickets restaurant |
| **Objectifs** | Objectif d'épargne mensuel et projets (montant, date, déjà de côté) avec le montant à mettre de côté chaque mois et un statut « dans les temps / en retard » |
| **Abonnements** | Abonnements détectés automatiquement, calendrier des paiements, statuts (actif, attendu, arrêté) |

### Épargne (`/finance/epargne/`), en 4 onglets

| Onglet | Contenu |
|---|---|
| **Vue d'ensemble** | Épargne totale, évolution dans le temps (livrets + titres), répartition par support et par classe d'actifs, rendement de chaque support, versements des 12 derniers mois d'après le budget |
| **Livrets** | Livret A, LDDS, LEP, PEL, assurance vie… suivis par leur solde : historique, intérêts estimés, remplissage du plafond. Les livrets Crédit Agricole (Livret A, LDDS, LEP, Livret Jeune…) se mettent à jour à chaque synchronisation bancaire, même sans opération sur la période |
| **Portefeuille** | PEA et compte-titres ligne par ligne : ajout par **ISIN, nom ou ticker** (recherche Yahoo Finance), date, prix et frais ; positions (PRU frais inclus, plus-value), évolution valeur / montant investi, allocation par ligne, classe d'actifs et zone, plafond et ancienneté du PEA, historique modifiable |
| **Frais** | Courtage, droits de garde et frais courants des ETF (estimés jour par jour), coût annuel en % du portefeuille, frais par ligne, projection sur 10 ans |

- **Cours automatiques** : les cours de clôture sont téléchargés depuis Yahoo Finance et gardés en base ; ils s'actualisent tout seuls à l'ouverture d'une page s'ils ont plus de 6 heures.
- **Prix d'achat pré-rempli** : en choisissant la date, le cours de clôture du jour est proposé (« Utiliser ce prix »).
- **Achats, ventes, dividendes et frais de compte** ; une vente au-delà des titres détenus est refusée.
- **Titres hors euro** (actions américaines…) convertis en euros au taux du jour.

### Tickets restaurant
- **Réglages** : valeur faciale, part salariale, nombre de tickets (jours ouvrés par défaut).
- **Case « Payé en tickets restaurant »**, réservée aux dépenses d'alimentation.
- **Suivi mensuel** : tickets crédités, retenue sur salaire, dépensés, restants.

### Interface
- **Navigation latérale** sur ordinateur ; sur mobile, **barre d'onglets** en bas (Accueil, Mois, Budget, Épargne) avec le bouton « + » au centre.
- **Mode jour / nuit**, avec des graphiques qui se recolorent instantanément.
- **Saisie rapide** d'une opération depuis n'importe quelle page (« + Nouvelle opération »), en feuille glissante sur mobile.
- **Responsive**, pensé pour le mobile.

---

## Confidentialité et sécurité

- **Tout reste sur ta machine.** La base de données est un fichier SQLite local. Le seul service extérieur contacté est **Yahoo Finance**, pour les cours de bourse : il reçoit le nom, l'ISIN ou le ticker que tu cherches et les tickers de tes lignes, jamais tes quantités ni tes montants.
- **Le code personnel bancaire n'est jamais saisi ni stocké par l'application.** Tu le tapes directement sur le site du Crédit Agricole, dans la fenêtre ouverte par l'import.
- **Seuls ton identifiant à 11 chiffres et ton code postal sont enregistrés**, pour pré-remplir la connexion.
- **Chaque synchronisation part d'un navigateur neuf** : aucun cookie ni session bancaire ne reste sur le disque.
- **Le relevé CSV téléchargé est supprimé** dès la fin de l'import.
- **Les sauvegardes locales et les données personnelles sont exclues de Git** : `db.sqlite3`, `*.sqlite3.*.bak` et `backend/data/` sont dans le `.gitignore`.

> L'automatisation d'un site bancaire reste une zone grise vis-à-vis des conditions d'utilisation de la banque. BudgWealth n'agit que sur ton propre compte, à ta demande, avec ta saisie du code.

---

## Démarrage rapide

### Option A : l'application macOS

```bash
./build_app.sh          # construit dist/BudgWealth.app
open dist/BudgWealth.app
```

Au premier lancement, l'application :
1. télécharge Chromium pour l'import bancaire (une seule fois) ;
2. crée la base de données ;
3. ouvre ton navigateur sur l'application.

Les données sont stockées dans `~/Library/Application Support/BudgWealth/`.

### Option B : en développement

Prérequis : **Python 3.12** (conda ou venv).

```bash
# 1. Environnement
conda create -n BudgWealth_env python=3.12 && conda activate BudgWealth_env
#   ou : python -m venv venv && source venv/bin/activate

# 2. Dépendances
pip install -r requirements.txt
playwright install chromium

# 3. Base de données
python manage.py migrate
python manage.py seed_default_categories   # catégories par défaut pour les comptes existants

# 4. Lancement
DEBUG=1 python manage.py runserver
```

L'application est sur **http://127.0.0.1:8000/**. Crée un compte : le parcours d'accueil te propose ensuite de connecter ta banque et d'importer ton historique.

---

## Configuration

Les variables sont lues dans un fichier `.env` : à la racine du projet en développement, dans `~/Library/Application Support/BudgWealth/.env` dans l'application empaquetée. Aucune n'est obligatoire.

| Variable | Défaut | Rôle |
|---|---|---|
| `DEBUG` | `False` | `1` pour afficher les erreurs détaillées et recharger les templates à chaud |
| `SECRET_KEY` | clé de dev / clé générée | En application empaquetée, une clé unique est générée au premier lancement |
| `DATABASE_URL` | SQLite local | Ex. `postgresql://user:password@host/db` pour utiliser PostgreSQL |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Hôtes autorisés, séparés par des virgules |
| `CSRF_TRUSTED_ORIGINS` | *(vide)* | Origines de confiance si l'app est servie derrière un proxy |

Exemple de `.env` :

```bash
DEBUG=1
# DATABASE_URL=postgresql://user:password@localhost:5432/budgwealth
```

---

## Utilisation

### 1. Importer son historique
1. **Paramètres › Banque** : renseigne ton identifiant Crédit Agricole (11 chiffres) et le code postal de ton agence.
2. Va sur **`/finance/banque/historique/`** et clique sur **Démarrer l'import**.
3. Une fenêtre Crédit Agricole s'ouvre : **tape ton code personnel** (et valide Secur'Pass si demandé). Elle se referme toute seule.
4. Les 12 derniers mois sont importés et catégorisés.

Ensuite, chaque page de mois propose **Synchroniser avec la banque** pour mettre à jour ce mois-là.

### 2. Vérifier et catégoriser
Sur la page d'un mois, clique sur une opération pour modifier son intitulé, sa catégorie et sa sous-catégorie. Les totaux et les graphiques se mettent à jour en direct. Les opérations non reconnues sont marquées **À classer**.

### 3. Créer son budget
**Budget › Mon budget** est pré-rempli avec tes postes fixes, tes abonnements et la moyenne de tes dépenses des 3 derniers mois. Ajuste, choisis le mois d'application, enregistre. Une modification crée une nouvelle version : les mois passés gardent l'ancienne pour l'analyse.

### 4. Fixer ses objectifs
**Budget › Objectifs** : un objectif d'épargne mensuel et des projets, par exemple un vélo à 1 800 € pour mars. Quand tu enregistres un versement « Épargne & placements », choisis le projet : sa progression se met à jour.

### 5. Suivre ses abonnements
**Budget › Abonnements** liste les abonnements détectés. Clique sur **Suivre** pour être alerté s'ils s'arrêtent, ou déclare-en un toi-même depuis l'éditeur d'une opération (« ↻ Suivre comme abonnement »).

### 6. Suivre son épargne
1. **Dashboard › Épargne › Livrets** : ajoute tes livrets avec leur solde et leur taux. Ceux du Crédit Agricole apparaissent seuls après une synchronisation bancaire ; un livret réglementé déjà saisi à la main (Livret A, Livret Jeune…) est rattaché au compte de la banque au lieu d'être créé en double. Le message de fin de synchronisation liste les livrets mis à jour.
2. **Portefeuille** : crée ton PEA (avec sa date d'ouverture) ou ton compte-titres, puis ajoute chaque achat : tape l'ISIN (ex. `LU1681043599`), le nom ou le ticker (`CW8.PA`), choisis le titre, la date, la quantité, le prix et les frais.
3. Dans **Positions › Modifier**, indique les **frais courants** (TER) de tes ETF et corrige au besoin la classe d'actifs ou la zone : l'onglet **Frais** et l'allocation en tiennent compte.

---

## Comment ça marche

### Import bancaire
```
ca_scraper.py  ──►  CSV Crédit Agricole  ──►  ca_csv.py  ──►  bank_import.py  ──►  Transaction
 (Playwright)        (Windows-1252,           (parsing)       (catégorisation,
                      multi-comptes)                           dédoublonnage)
```
- **`ca_scraper.py`** pilote Chromium :
  - choix de la caisse par le code postal (la géolocalisation est refusée) ;
  - saisie de l'identifiant ;
  - attente de ton code ;
  - remplissage du formulaire « Télécharger mes opérations », qui est dans une iframe : tous les comptes, format CSV, période.
- **`ca_csv.py`** lit l'export. Le fichier contient un bloc par compte, des libellés sur plusieurs lignes, des montants « 1 234,56 », et des colonnes Débit / Crédit séparées. Pour un paiement par carte, c'est la **date d'achat** qui est retenue, pas la date de débit.
- **`bank_import.py`** :
  - ignore les opérations des comptes d'épargne, mais relève le solde des livrets pour le module Épargne. La banque n'inclut pas dans le relevé un compte sans opération sur la période (un Livret Jeune ne bouge souvent qu'une fois par an) : un second relevé, sur 12 mois et pour ces seuls livrets, donne alors leur solde ;
  - transforme un virement vers ton propre livret en dépense « Épargne & placements › Livret » ;
  - ignore les retours du livret vers le compte courant ;
  - catégorise par règles (`BANK_CATEGORY_RULES`) ;
  - crée une empreinte unique par opération (`external_id`), qui garde aussi deux paiements identiques le même jour.

### Revenus, charges, épargne
Les totaux (`budget.totals_annotations` / `split_totals`) séparent toujours trois flux :
- **Charges** : dépenses hors épargne.
- **Épargne** : dépenses de la catégorie « Épargne & placements ».
- **Solde** : revenus − charges. Un mois où tu épargnes beaucoup n'apparaît donc pas en négatif.
- **Taux d'épargne** : épargne ÷ revenus.

### Budget
- **Versions** : un `BudgetPlan` s'applique à partir de `valid_from` jusqu'à la version suivante. Avant le premier budget, l'historique est comparé à ce premier budget.
- **Revenu de référence** : la moyenne des salaires (catégorie « Salaire ») des N derniers mois complets, en ignorant les mois sans salaire. Tu peux aussi choisir un montant fixe.
- **Rattachement** : une dépense va à la ligne la plus précise, d'abord la sous-catégorie, puis la catégorie. Les dépenses sans ligne apparaissent en « Hors budget ».
- **Projection de fin de mois** :
  - charges fixes, abonnements et provisions : le maximum entre le réalisé et le budget ;
  - charges variables : le réalisé plus le reste du budget au rythme prévu ;
  - une ligne passe en « à surveiller » au-delà du rythme attendu, avec 10 % de tolérance.
- **Ressources** : revenu de référence + crédit de tickets restaurant.

### Détection des abonnements (`subscriptions.py`)
1. **Clé commerçant** : le premier mot significatif du libellé, avec des alias (`claude → anthropic`, `chatgpt → openai`…). « ANTHROPIC* CLAUDE SU » et « Anthropic » sont donc le même commerçant.
2. **Séparation par montant proche (±20 %)** : une même enseigne peut avoir plusieurs abonnements, comme iCloud à 0,99 € et Apple Music à 10,99 €.
3. **Cadence régulière** : au moins 75 % des écarts entre paiements doivent correspondre à la cadence.

| Cadence | Paiements minimum | Écart accepté | Tolérance de retard |
|---|---|---|---|
| Mensuelle | 3 | 24 à 38 jours | 10 jours |
| Annuelle | 2 | 340 à 390 jours | 30 jours |

4. **Statut** :
   - **Actif** : le dernier paiement est récent ;
   - **Paiement attendu** : l'échéance est proche ;
   - **Arrêté** : aucun paiement après l'échéance plus la tolérance.

   Un abonnement suivi passe tout seul en « arrêté », et redevient actif si un paiement réapparaît.
5. **Pas de doublon** : les abonnements suivis (`match_key`) ne génèrent pas de ligne automatique, puisque la banque fournit le vrai paiement. Seuls les postes saisis à la main sont générés chaque mois.

L'alimentation et l'épargne ne sont jamais proposées comme abonnements.

### Épargne (`wealth.py`, `market_data.py`)
- **Montants en euros** : prix d'achat, frais et dividendes se saisissent en euros. Les cours Yahoo d'un titre coté en dollars ou en livres sont convertis avec le taux de change du jour (paire `USDEUR=X`…).
- **PRU frais inclus**, au coût moyen pondéré, comme chez les courtiers. Une vente réalise la plus-value `prix de vente net − PRU × quantité`.
- **Gain total** = plus-value latente + plus-values réalisées + dividendes − frais de compte.
- **Rendement annualisé** (taux de rendement interne) affiché après un an de détention ; avant, la performance simple depuis le premier achat.
- **Frais courants des ETF** : jamais facturés à part, ils sont prélevés dans le cours. Ils sont estimés jour par jour : valeur détenue × TER ÷ 365.
- **Plafond du PEA** : les versements sont estimés à partir des achats, des ventes et des liquidités déclarées (l'application ne voit pas les virements vers le PEA).
- **Classement automatique** : classe d'actifs et zone sont devinées d'après le nom (« MSCI World » → Monde, « S&P 500 » → États-Unis, « Bond » → Obligations…) ou la place de cotation pour une action. Elles restent modifiables.
- **Cours en cache** : `PricePoint` garde les clôtures quotidiennes depuis le premier achat ; une actualisation ne télécharge que les derniers jours. Si Yahoo ne répond pas, l'application garde les derniers cours connus, et une ligne sans aucun cours est valorisée à son prix de revient.

---

## Architecture

```
BudgWealth/
├── manage.py
├── launcher.py              # Point d'entrée de l'app empaquetée (waitress + ouverture du navigateur)
├── build_app.sh / .py       # Construction de dist/BudgWealth.app
├── budgwealth.spec          # Configuration PyInstaller
├── requirements.txt
├── backend/
│   ├── config/              # settings, urls, wsgi
│   ├── accounts/            # inscription, connexion, compte
│   └── finance/
│       ├── models.py        # données (voir ci-dessous)
│       ├── views.py         # pages et actions (dont édition sur place en JSON)
│       ├── forms.py
│       ├── budget.py        # calculs : totaux, budget, projection, tendances, projets, tickets
│       ├── subscriptions.py # détection et suivi des abonnements
│       ├── wealth.py        # épargne : livrets, positions, performance, frais, allocation
│       ├── market_data.py   # Yahoo Finance : recherche de titres, cours, taux de change
│       ├── savings_views.py # pages et actions du module Épargne
│       ├── templatetags/    # filtres d'affichage (montants signés, quantités)
│       ├── ca_scraper.py    # automatisation du site Crédit Agricole (Playwright)
│       ├── ca_csv.py        # lecture de l'export CSV
│       ├── bank_import.py   # création des opérations, règles de catégorisation
│       ├── bank_sync.py     # synchronisations en arrière-plan (thread) et leur statut
│       ├── default_categories.py
│       └── tests.py
└── frontend/
    ├── templates/           # base.html, accounts/, finance/
    └── static/
        ├── css/styles.css   # thème clair/sombre (variables CSS --bw-*, --chart-*, --kind-*)
        └── js/
            ├── bw-charts.js     # socle des graphiques (palette du thème, redessin jour/nuit)
            ├── bw-month.js      # page mois : anneau, cumul, filtres, édition sur place
            ├── bw-savings.js    # épargne : graphiques, recherche de titres, ajout d'une ligne
            └── bw-dashboard.js  # dashboard : tendances sur 12 mois
```

### Modèle de données

| Modèle | Rôle |
|---|---|
| `Category` / `Subcategory` | Catégories de revenus et de dépenses, propres à chaque utilisateur |
| `Transaction` | Opération : type, montant, date, catégorie, source (manuelle / banque), `external_id`, `paid_with_meal_voucher`, `savings_project`, `recurring_item` |
| `RecurringItem` | Poste fixe ou abonnement. Avec `match_key`, il est rattaché aux paiements bancaires ; sans, il est généré chaque mois |
| `MonthlyBudget` | Mois « démarré » (génération des postes fixes) |
| `BudgetPlan` / `BudgetLine` | Budget type versionné et ses objectifs par poste |
| `SavingsProject` | Projet d'épargne (montant, date, déjà de côté) |
| `MealVoucherSettings` | Réglages des tickets restaurant |
| `BankConnection` / `BankSync` | Identifiants de connexion (sans code) et historique des synchronisations |
| `DismissedSubscription` | Suggestions d'abonnement ignorées |
| `SavingsAccount` | Support d'épargne : livret, assurance vie… (suivi par son solde) ou PEA / compte-titres (suivi ligne par ligne) |
| `BalanceSnapshot` | Solde d'un livret à une date, saisi à la main ou relevé par l'import bancaire |
| `Instrument` | Titre identifié par son ticker Yahoo : ISIN, devise, classe d'actifs, zone, frais courants, dernier cours |
| `PortfolioOperation` | Achat, vente, dividende ou frais de compte sur un PEA / compte-titres |
| `PricePoint` / `ExchangeRate` | Cours de clôture quotidiens et taux de change en euros, mis en cache |

### Principales routes

| URL | Page |
|---|---|
| `/finance/<année>/<mois>/` | Vision du mois |
| `/finance/dashboard/budget/` | Analyse (tendances globales) |
| `/finance/dashboard/budget/mon-budget/` | Budget type |
| `/finance/dashboard/budget/objectifs/` | Épargne et projets |
| `/finance/dashboard/budget/abonnements/` | Abonnements et calendrier |
| `/finance/banque/connexion/` · `/finance/banque/historique/` | Connexion bancaire et import initial |
| `/finance/epargne/` | Épargne : vue d'ensemble |
| `/finance/epargne/livrets/` · `/finance/epargne/portefeuille/` · `/finance/epargne/frais/` | Livrets, PEA / compte-titres (`?compte=<id>`), frais |
| `/finance/parametres/` | Compte, banque, postes fixes, catégories |

---

## Développement

### Tests
```bash
python manage.py test finance
```
Ils couvrent la lecture du CSV, l'import et le dédoublonnage, la synchronisation, les calculs de budget, la détection d'abonnements, l'édition sur place et le module Épargne (PRU, frais, rendement, devises, soldes des livrets). Yahoo Finance y est simulé : aucun appel réseau. `build_app.sh` les lance avant chaque construction.

### Migrations
`runserver` **n'applique pas** les migrations tout seul : après une modification des modèles, lance
```bash
python manage.py makemigrations finance
python manage.py migrate
```
L'application empaquetée, elle, migre automatiquement au lancement.

### Recettes courantes
- **Ajouter une règle de catégorisation** : ajoute une ligne `(motif, catégorie, sous-catégorie)` dans `BANK_CATEGORY_RULES` (`bank_import.py`). La première règle qui correspond l'emporte.
- **Regrouper un commerçant libellé de plusieurs façons** : ajoute un alias dans `MERCHANT_ALIASES` (`subscriptions.py`).
- **Ajouter une catégorie par défaut** : modifie `default_categories.py`, puis lance `python manage.py seed_default_categories`. La commande peut être relancée sans risque.
- **Tester l'automatisation bancaire seule** (depuis `backend/`) :
  ```bash
  python -m finance.ca_scraper --login 12345678901 --cp 75001 --du 01/09/2026 --au 30/09/2026
  ```

### Front-end
- **Couleurs** : elles passent par des variables CSS. Ne mets jamais de couleur en dur dans un graphique : utilise `BW.charts.palette()`.
- **Nouveau graphique** : crée-le avec `BW.charts.create(canvas, function (p) { return config; })`. Il sera redessiné automatiquement au changement de thème.
- **Cache** : les fichiers statiques sont versionnés dans les templates (`styles.css?v=…`, `bw-month.js?v=…`). Incrémente le numéro après une modification pour contourner le cache du navigateur.

---

## Construire l'application macOS

```bash
conda activate BudgWealth_env
./build_app.sh
```
Le script :
1. vérifie les outils (PyInstaller, WhiteNoise, Waitress) ;
2. lance les tests ;
3. prépare les fichiers statiques avec leur manifeste (`build_app.py`) ;
4. construit **`dist/BudgWealth.app`**.

Relance-le après chaque modification du code, des templates ou du CSS.

| Emplacement | Contenu |
|---|---|
| `~/Library/Application Support/BudgWealth/db.sqlite3` | Base de données |
| `~/Library/Application Support/BudgWealth/secret_key.txt` | Clé secrète générée au premier lancement |
| `~/Library/Application Support/BudgWealth/browsers/` | Chromium, téléchargé au premier lancement (non embarqué, ~360 Mo) |
| `~/Library/Application Support/BudgWealth/.env` | Configuration facultative |

---

## Dépannage

| Symptôme | Solution |
|---|---|
| `OperationalError: no such table …` | La base n'est pas à jour : `python manage.py migrate` |
| Les styles ou les scripts ne changent pas | Rechargement forcé (Cmd + Shift + R), ou incrémente `?v=` dans le template |
| Les modifications de template ne s'affichent pas | Sans `DEBUG=1`, Django met les templates en cache : redémarre le serveur |
| L'import reste bloqué sur « Saisie de votre code » | Tape ton code dans la fenêtre Crédit Agricole, et valide Secur'Pass sur ton téléphone si demandé |
| La caisse régionale n'est pas trouvée | Vérifie le code postal dans Paramètres › Banque (5 chiffres) |
| Un mois apparaît sans salaire et un autre avec deux | Le salaire a été versé à cheval sur deux mois : c'est la date réelle de l'opération qui compte |
| « Cours indisponibles » sur une ligne | Yahoo Finance n'a pas répondu, ou le ticker n'existe plus. Clique sur **Actualiser** ; la ligne reste valorisée à son prix de revient en attendant |
| Un titre est introuvable dans la recherche | Essaie son ISIN, ou son ticker Yahoo avec le suffixe de la place (`.PA` pour Paris, `.DE` pour Xetra, `.AS` pour Amsterdam) |
| Un abonnement n'est pas détecté | Il faut au moins 3 paiements mensuels réguliers. Sinon, déclare-le depuis l'éditeur de l'opération (« ↻ Suivre comme abonnement ») |

---

## Limites connues et pistes

- **Une seule banque** prise en charge : Crédit Agricole, particuliers.
- **Historique bancaire limité à 12 mois** par la banque : les abonnements annuels ne sont détectés qu'après 2 ans d'utilisation.
- **Repérage par le nom des catégories** : alimentation, épargne, salaire et logement sont reconnus par leur nom. Renommer ces catégories désactive les comportements associés.
- **Pas d'écran pour modifier le montant ou la date** d'une opération importée : seuls l'intitulé et la catégorie sont modifiables.
- **Épargne** : les lignes du PEA et du compte-titres se saisissent à la main (pas d'import depuis le courtier) ; les divisions d'actions ne sont pas gérées ; les intérêts des livrets sont une estimation (solde × taux), sans la règle des quinzaines.
- **Pistes** :
  - rattacher un salaire reçu en fin de mois au mois suivant ;
  - affecter automatiquement les virements bancaires aux projets ;
  - export des données.
