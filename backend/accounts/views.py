from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from finance.default_categories import create_default_categories

from .forms import LoginForm, SignUpForm


def account_exists():
    """L'app est locale : une installation = une base = un seul compte."""
    return get_user_model().objects.exists()


def signup(request):
    if request.user.is_authenticated:
        return redirect("finance:dashboard")
    if account_exists():
        return redirect("accounts:login")

    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            create_default_categories(user)
            login(request, user)
            # Juste après l'inscription : proposer de connecter sa banque
            return redirect("finance:bank_connect")
    else:
        form = SignUpForm()

    return render(request, "accounts/signup.html", {"form": form})


class AccountLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        # Premier lancement : pas encore de compte, on le crée
        if not request.user.is_authenticated and not account_exists():
            return redirect("accounts:signup")
        return super().dispatch(request, *args, **kwargs)


login_view = AccountLoginView.as_view()


@login_required
@require_POST
def logout_view(request):
    logout(request)
    return redirect("accounts:login")
