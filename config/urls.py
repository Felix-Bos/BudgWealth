from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import include, path


def index(request):
    if request.user.is_authenticated:
        return redirect("home")
    return redirect("accounts:login")


@login_required
def home(request):
    return render(request, "home.html")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", index, name="index"),
    path("", include("accounts.urls")),
    path("home/", home, name="home"),
    path("finance/", include("finance.urls")),
]
