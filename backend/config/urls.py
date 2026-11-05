from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path
from django.views.i18n import JavaScriptCatalog


def index(request):
    if request.user.is_authenticated:
        return redirect("finance:dashboard")
    return redirect("accounts:login")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", index, name="index"),
    path("", include("accounts.urls")),
    path("finance/", include("finance.urls")),
    # Choix de la langue (vue set_language) et traductions des scripts
    path("i18n/", include("django.conf.urls.i18n")),
    path("jsi18n/", JavaScriptCatalog.as_view(), name="javascript-catalog"),
]
