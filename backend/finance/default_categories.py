from django.utils.translation import gettext_noop

from .models import BudgetSection, Category, EntryType, Subcategory

# Les noms restent en français en base (l'import bancaire et le budget s'y
# réfèrent) ; ils sont traduits à l'affichage (Category.__str__).

DEFAULT_EXPENSE_CATEGORIES = [
    (
        gettext_noop("Achats & shopping"),
        [
            gettext_noop("Articles de sport"),
            gettext_noop("Cadeaux"),
            gettext_noop("Dons"),
            gettext_noop("High Tech, Jeux vidéos"),
            gettext_noop("Livres, musique"),
            gettext_noop("Mobilier décoration"),
            gettext_noop("Prêt consommation"),
            gettext_noop("Tabac, presse"),
            gettext_noop("Vêtements, chaussures, accessoires"),
            gettext_noop("Achats & shopping - Autres"),
        ],
    ),
    (
        gettext_noop("Alimentation & restaurants"),
        [
            gettext_noop("Marché"),
            gettext_noop("Restaurants, snacks"),
            gettext_noop("Supermarché, épicerie"),
            gettext_noop("Vins et spiritueux"),
            gettext_noop("Alimentation & restaurants - Autres"),
        ],
    ),
    (
        gettext_noop("Animaux"),
        [
            gettext_noop("Jouets"),
            gettext_noop("Mutuelle"),
            gettext_noop("Nourriture"),
            gettext_noop("Santé, soins"),
            gettext_noop("Animaux - Autres"),
        ],
    ),
    (
        gettext_noop("Épargne & placements"),
        [
            gettext_noop("Livret"),
            gettext_noop("PEA"),
            gettext_noop("CTO"),
            gettext_noop("Assurance vie"),
            gettext_noop("Fond retraite"),
        ],
    ),
    (
        gettext_noop("Retraits"),
        [],
    ),
    (
        gettext_noop("Travail & études"),
        [
            gettext_noop("Dépenses professionnelles"),
            gettext_noop("Notes de frais"),
            gettext_noop("Prêt étudiant"),
            gettext_noop("Repas au travail"),
            gettext_noop("Travail & études - Autres"),
        ],
    ),
    (
        gettext_noop("Transport"),
        [
            gettext_noop("Assurance véhicule"),
            gettext_noop("Avion, train, bateau"),
            gettext_noop("Carburant"),
            gettext_noop("Entretien, équipement véhicule"),
            gettext_noop("Location véhicule"),
            gettext_noop("Péage"),
            gettext_noop("Prêt véhicule"),
            gettext_noop("Stationnement"),
            gettext_noop("Taxi, VTC"),
            gettext_noop("Transport en commun"),
            gettext_noop("Transport - Autres"),
        ],
    ),
    (
        gettext_noop("Santé"),
        [
            gettext_noop("Médecin"),
            gettext_noop("Mutuelle"),
            gettext_noop("Optique"),
            gettext_noop("Pharmacie"),
            gettext_noop("Santé - Autres"),
        ],
    ),
    (
        gettext_noop("Loisirs & vacances"),
        [
            gettext_noop("Abonnement multimédia"),
            gettext_noop("Bars et clubs"),
            gettext_noop("Coiffeur, esthétique"),
            gettext_noop("Sorties culturelles"),
            gettext_noop("Sport"),
            gettext_noop("Vacances, voyages"),
            gettext_noop("Loisirs & vacances - Autres"),
        ],
    ),
    (
        gettext_noop("Abonnements & services"),
        [
            gettext_noop("Streaming vidéo"),
            gettext_noop("Musique"),
            gettext_noop("Outils IA & logiciels"),
            gettext_noop("Cloud & stockage"),
            gettext_noop("Jeux vidéo"),
            gettext_noop("Presse & magazines"),
            gettext_noop("Abonnements & services - Autres"),
        ],
    ),
    (
        gettext_noop("Logement & charges"),
        [
            gettext_noop("Assurance logement"),
            gettext_noop("Charges logement, accessoires"),
            gettext_noop("Eau, électricité, gaz"),
            gettext_noop("Internet, téléphonie"),
            gettext_noop("Loyer"),
            gettext_noop("Prêt immobilier"),
            gettext_noop("Résidence secondaire"),
            gettext_noop("Travaux, entretien"),
            gettext_noop("Logement & charges - Autres"),
        ],
    ),
    (
        gettext_noop("Impôts, taxes & frais"),
        [
            gettext_noop("Amendes"),
            gettext_noop("Contributions sociales"),
            gettext_noop("Frais bancaires"),
            gettext_noop("Impôts sur la fortune"),
            gettext_noop("Impôts sur le revenu"),
            gettext_noop("Taxes foncières, d'habitation"),
            gettext_noop("Impôts, taxes & frais - Autres"),
        ],
    ),
    (
        gettext_noop("Enfants"),
        [
            gettext_noop("Activités enfants"),
            gettext_noop("Argent de poche"),
            gettext_noop("Assurances enfants"),
            gettext_noop("Fourniture scolaire"),
            gettext_noop("Frais de scolarité"),
            gettext_noop("Garde d'enfants"),
            gettext_noop("Jouets, cadeaux"),
            gettext_noop("Pensions alimentaires"),
            gettext_noop("Enfants - Autres"),
        ],
    ),
]

DEFAULT_INCOME_CATEGORIES = [
    (gettext_noop("Salaire"), []),
    (gettext_noop("Freelance, indépendant"), []),
    (gettext_noop("Aides (CAF et autres)"), []),
    (gettext_noop("Pensions, retraite"), []),
    (gettext_noop("Remboursements"), []),
    (gettext_noop("Revenus - Autres"), []),
]


# Type de chaque sous-catégorie par défaut dans le budget ; les autres sont
# des charges variables. Clé : (catégorie, sous-catégorie).
DEFAULT_BUDGET_SECTIONS = {
    ("Achats & shopping", "Prêt consommation"): BudgetSection.FIXED,
    ("Animaux", "Mutuelle"): BudgetSection.FIXED,
    ("Travail & études", "Prêt étudiant"): BudgetSection.FIXED,
    ("Transport", "Assurance véhicule"): BudgetSection.FIXED,
    ("Transport", "Location véhicule"): BudgetSection.FIXED,
    ("Transport", "Prêt véhicule"): BudgetSection.FIXED,
    ("Transport", "Transport en commun"): BudgetSection.SUBSCRIPTION,
    ("Santé", "Mutuelle"): BudgetSection.FIXED,
    ("Loisirs & vacances", "Abonnement multimédia"): BudgetSection.SUBSCRIPTION,
    ("Loisirs & vacances", "Sport"): BudgetSection.SUBSCRIPTION,
    ("Loisirs & vacances", "Vacances, voyages"): BudgetSection.PROVISION,
    ("Abonnements & services", "Streaming vidéo"): BudgetSection.SUBSCRIPTION,
    ("Abonnements & services", "Musique"): BudgetSection.SUBSCRIPTION,
    ("Abonnements & services", "Outils IA & logiciels"): BudgetSection.SUBSCRIPTION,
    ("Abonnements & services", "Cloud & stockage"): BudgetSection.SUBSCRIPTION,
    ("Abonnements & services", "Jeux vidéo"): BudgetSection.SUBSCRIPTION,
    ("Abonnements & services", "Presse & magazines"): BudgetSection.SUBSCRIPTION,
    ("Abonnements & services", "Abonnements & services - Autres"): BudgetSection.SUBSCRIPTION,
    ("Logement & charges", "Assurance logement"): BudgetSection.FIXED,
    ("Logement & charges", "Charges logement, accessoires"): BudgetSection.FIXED,
    ("Logement & charges", "Eau, électricité, gaz"): BudgetSection.FIXED,
    ("Logement & charges", "Internet, téléphonie"): BudgetSection.FIXED,
    ("Logement & charges", "Loyer"): BudgetSection.FIXED,
    ("Logement & charges", "Prêt immobilier"): BudgetSection.FIXED,
    ("Logement & charges", "Résidence secondaire"): BudgetSection.FIXED,
    ("Impôts, taxes & frais", "Contributions sociales"): BudgetSection.FIXED,
    ("Impôts, taxes & frais", "Frais bancaires"): BudgetSection.FIXED,
    ("Impôts, taxes & frais", "Impôts sur la fortune"): BudgetSection.PROVISION,
    ("Impôts, taxes & frais", "Impôts sur le revenu"): BudgetSection.FIXED,
    ("Impôts, taxes & frais", "Taxes foncières, d'habitation"): BudgetSection.PROVISION,
    ("Enfants", "Assurances enfants"): BudgetSection.FIXED,
    ("Enfants", "Fourniture scolaire"): BudgetSection.PROVISION,
    ("Enfants", "Frais de scolarité"): BudgetSection.PROVISION,
    ("Enfants", "Garde d'enfants"): BudgetSection.FIXED,
    ("Enfants", "Pensions alimentaires"): BudgetSection.FIXED,
}


def _create_categories(user, categories, entry_type, start_order=0):
    for order, (category_name, subcategory_names) in enumerate(
        categories, start=start_order
    ):
        category, _ignored = Category.objects.get_or_create(
            user=user,
            name=category_name,
            entry_type=entry_type,
            defaults={"order": order},
        )
        for sub_order, subcategory_name in enumerate(subcategory_names):
            Subcategory.objects.get_or_create(
                category=category,
                name=subcategory_name,
                defaults={
                    "order": sub_order,
                    "budget_section": DEFAULT_BUDGET_SECTIONS.get(
                        (category_name, subcategory_name), BudgetSection.VARIABLE
                    ),
                },
            )


def create_default_categories(user):
    """Populate a new user's category list with the standard set of
    income/expense categories/subcategories. Safe to call more than once."""
    _create_categories(user, DEFAULT_INCOME_CATEGORIES, EntryType.INCOME)
    _create_categories(user, DEFAULT_EXPENSE_CATEGORIES, EntryType.EXPENSE)
