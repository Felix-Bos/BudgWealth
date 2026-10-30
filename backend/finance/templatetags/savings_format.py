from django import template
from django.template.defaultfilters import floatformat

register = template.Library()


@register.filter
def signed(value, decimals=2):
    """Montant ou pourcentage avec son signe : +1 234,56 / −12,30."""
    if value is None or value == "":
        return "—"
    formatted = floatformat(abs(value), f"{decimals}g")
    if value > 0:
        return f"+{formatted}"
    if value < 0:
        return f"−{formatted}"
    return formatted


@register.filter
def trend(value):
    """Classe CSS selon le signe : is-up (gain) ou is-down (perte)."""
    if value is None or value == "":
        return ""
    if value > 0:
        return "is-up"
    if value < 0:
        return "is-down"
    return ""


@register.filter
def quantity(value):
    """Quantité de titres sans zéros inutiles : 10, 2,5, 0,1234."""
    if value is None or value == "":
        return "—"
    if value == value.to_integral_value():
        return floatformat(value, "0g")
    return floatformat(value.normalize(), f"{max(0, -value.normalize().as_tuple().exponent)}g")
