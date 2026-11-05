"""
Langue de l'interface : celle choisie dans les paramètres (cookie posé par
la vue `set_language` de Django), sinon le français. La langue du navigateur
est volontairement ignorée : l'application reste en français tant qu'on ne
choisit pas l'anglais.
"""

from django.conf import settings
from django.utils import translation


class LanguageCookieMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response
        self.supported = {code for code, _ in settings.LANGUAGES}

    def __call__(self, request):
        language = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if language not in self.supported:
            language = settings.LANGUAGE_CODE
        translation.activate(language)
        request.LANGUAGE_CODE = language

        response = self.get_response(request)
        response.headers.setdefault("Content-Language", language)
        return response


class DailyBackupMiddleware:
    """Une sauvegarde automatique de la base par jour, au premier passage.
    La vérification ne coûte rien les autres fois (date gardée en mémoire)."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.checked_on = None

    def __call__(self, request):
        if settings.AUTO_BACKUP:
            from datetime import date

            today = date.today()
            if self.checked_on != today:
                self.checked_on = today
                try:
                    from .backups import backup_if_due
                    backup_if_due(today)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("Sauvegarde automatique impossible")
        return self.get_response(request)
