from .models import Category, EntryType, Subcategory

DEFAULT_EXPENSE_CATEGORIES = [
    (
        "Achats & shopping",
        [
            "Articles de sport",
            "Cadeaux",
            "Dons",
            "High Tech, Jeux vidéos",
            "Livres, musique",
            "Mobilier décoration",
            "Prêt consommation",
            "Tabac, presse",
            "Vêtements, chaussures, accessoires",
            "Achats & shopping - Autres",
        ],
    ),
    (
        "Alimentation & restaurants",
        [
            "Marché",
            "Restaurants, snacks",
            "Supermarché, épicerie",
            "Vins et spiritueux",
            "Alimentation & restaurants - Autres",
        ],
    ),
    (
        "Animaux",
        [
            "Jouets",
            "Mutuelle",
            "Nourriture",
            "Santé, soins",
            "Animaux - Autres",
        ],
    ),
    (
        "Épargne & placements",
        [
            "Livret",
            "PEA",
            "CTO",
            "Assurance vie",
            "Fond retraite",
        ],
    ),
    (
        "Retraits",
        [],
    ),
    (
        "Travail & études",
        [
            "Dépenses professionnelles",
            "Notes de frais",
            "Prêt étudiant",
            "Repas au travail",
            "Travail & études - Autres",
        ],
    ),
    (
        "Transport",
        [
            "Assurance véhicule",
            "Avion, train, bateau",
            "Carburant",
            "Entretien, équipement véhicule",
            "Location véhicule",
            "Péage",
            "Prêt véhicule",
            "Stationnement",
            "Taxi, VTC",
            "Transport en commun",
            "Transport - Autres",
        ],
    ),
    (
        "Santé",
        [
            "Médecin",
            "Mutuelle",
            "Optique",
            "Pharmacie",
            "Santé - Autres",
        ],
    ),
    (
        "Loisirs & vacances",
        [
            "Abonnement multimédia",
            "Bars et clubs",
            "Coiffeur, esthétique",
            "Sorties culturelles",
            "Sport",
            "Vacances, voyages",
            "Loisirs & vacances - Autres",
        ],
    ),
    (
        "Logement & charges",
        [
            "Assurance logement",
            "Charges logement, accessoires",
            "Eau, électricité, gaz",
            "Internet, téléphonie",
            "Loyer",
            "Prêt immobilier",
            "Résidence secondaire",
            "Travaux, entretien",
            "Logement & charges - Autres",
        ],
    ),
    (
        "Impôts, taxes & frais",
        [
            "Amendes",
            "Contributions sociales",
            "Frais bancaires",
            "Impôts sur la fortune",
            "Impôts sur le revenu",
            "Taxes foncières, d'habitation",
            "Impôts, taxes & frais - Autres",
        ],
    ),
    (
        "Enfants",
        [
            "Activités enfants",
            "Argent de poche",
            "Assurances enfants",
            "Fourniture scolaire",
            "Frais de scolarité",
            "Garde d'enfants",
            "Jouets, cadeaux",
            "Pensions alimentaires",
            "Enfants - Autres",
        ],
    ),
]

DEFAULT_INCOME_CATEGORIES = [
    ("Salaire", []),
    ("Freelance, indépendant", []),
    ("Aides (CAF et autres)", []),
    ("Pensions, retraite", []),
    ("Remboursements", []),
    ("Revenus - Autres", []),
]


def _create_categories(user, categories, entry_type, start_order=0):
    for order, (category_name, subcategory_names) in enumerate(
        categories, start=start_order
    ):
        category, _ = Category.objects.get_or_create(
            user=user,
            name=category_name,
            entry_type=entry_type,
            defaults={"order": order},
        )
        for sub_order, subcategory_name in enumerate(subcategory_names):
            Subcategory.objects.get_or_create(
                category=category,
                name=subcategory_name,
                defaults={"order": sub_order},
            )


def create_default_categories(user):
    """Populate a new user's category list with the standard set of
    income/expense categories/subcategories. Safe to call more than once."""
    _create_categories(user, DEFAULT_INCOME_CATEGORIES, EntryType.INCOME)
    _create_categories(user, DEFAULT_EXPENSE_CATEGORIES, EntryType.EXPENSE)
