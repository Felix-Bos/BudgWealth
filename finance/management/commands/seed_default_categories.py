from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from finance.default_categories import create_default_categories


class Command(BaseCommand):
    help = "Create the default expense categories/subcategories for all existing users."

    def handle(self, *args, **options):
        User = get_user_model()
        users = User.objects.all()
        for user in users:
            create_default_categories(user)
        self.stdout.write(self.style.SUCCESS(f"Seeded categories for {users.count()} user(s)."))
