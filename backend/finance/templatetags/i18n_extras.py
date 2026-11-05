from django import template
from django.utils.translation import get_language

register = template.Library()


@register.filter
def lower_fr(value):
    """Minuscules en français (« novembre 2026 » en milieu de phrase) ;
    les mois gardent leur majuscule en anglais (« November 2026 »)."""

    text = str(value)
    return text.lower() if (get_language() or "fr").startswith("fr") else text
