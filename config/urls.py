from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path


def index(request):
    if request.user.is_authenticated:
        return redirect("finance:dashboard")
    return redirect("accounts:login")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", index, name="index"),
    path("", include("accounts.urls")),
    path("finance/", include("finance.urls")),
]
