# BudgWealth

Base Django pour une application de finances personnelles et de gestion de patrimoine.

Cette première version contient uniquement le socle: création de compte, connexion, page privée, déconnexion et premier modèle `Transaction`.

## Installation locale

Créer et activer un environnement virtuel:

```bash
python -m venv venv
source venv/bin/activate
```

Installer les dépendances:

```bash
pip install -r requirements.txt
```

Créer le fichier d'environnement:

```bash
cp .env.example .env
```

Par défaut, si `DATABASE_URL` n'est pas défini, le projet utilise SQLite pour simplifier le développement local.
Pour PostgreSQL ou Neon, définir une variable de ce type dans `.env`:

```bash
DATABASE_URL=postgresql://user:password@host/database
```

Appliquer les migrations:

```bash
python manage.py migrate
```

Lancer le serveur:

```bash
python manage.py runserver
```

L'application est ensuite accessible sur:

```text
http://127.0.0.1:8000/
```

## Parcours disponible

- `/` redirige vers `/home/` si l'utilisateur est connecté, sinon vers `/login/`.
- `/signup/` permet de créer un compte.
- `/login/` permet de se connecter.
- `/home/` affiche l'espace privé de l'utilisateur connecté.
- `/logout/` déconnecte l'utilisateur via un formulaire POST puis redirige vers `/login/`.

## Notes

Le projet utilise le système d'authentification Django et ne stocke jamais les mots de passe en clair.
Les fonctionnalités de budget, dashboard, investissements et simulations ne sont pas encore développées.
