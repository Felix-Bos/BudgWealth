from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("", views.index, name="index"),
    path("<int:year>/<int:month>/", views.month_view, name="month"),
    path("<int:year>/<int:month>/start/", views.start_month, name="start_month"),
    path(
        "<int:year>/<int:month>/transactions/add/",
        views.add_transaction,
        name="add_transaction",
    ),
    path(
        "<int:year>/<int:month>/transactions/<int:pk>/edit/",
        views.edit_transaction,
        name="edit_transaction",
    ),
    path(
        "<int:year>/<int:month>/transactions/<int:pk>/delete/",
        views.delete_transaction,
        name="delete_transaction",
    ),
    path("dashboard/", views.dashboard_hub_view, name="dashboard"),
    path("dashboard/budget/", views.dashboard_budget_view, name="dashboard_budget"),
    path("parametres/", views.settings_view, name="settings"),
    path("parametres/compte/", views.update_account, name="update_account"),
    path("parametres/mot-de-passe/", views.update_password, name="update_password"),
    path("parametres/postes-fixes/add/", views.add_recurring_item, name="add_recurring_item"),
    path(
        "parametres/postes-fixes/<int:pk>/edit/",
        views.edit_recurring_item,
        name="edit_recurring_item",
    ),
    path(
        "parametres/postes-fixes/<int:pk>/delete/",
        views.delete_recurring_item,
        name="delete_recurring_item",
    ),
    path("parametres/categories/add/", views.add_category, name="add_category"),
    path(
        "parametres/categories/<int:pk>/delete/",
        views.delete_category,
        name="delete_category",
    ),
    path(
        "parametres/categories/sous-categories/add/",
        views.add_subcategory,
        name="add_subcategory",
    ),
    path(
        "parametres/categories/sous-categories/<int:pk>/delete/",
        views.delete_subcategory,
        name="delete_subcategory",
    ),
]
