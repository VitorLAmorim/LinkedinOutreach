#!/usr/bin/env python
"""Django management entrypoint.

Usage:
    python manage.py rundaemon                       # run the daemon (interactive onboarding)
    python manage.py rundaemon --profile <username>  # run the daemon for a specific account
    python manage.py setup_account <username>        # interactive VNC login for account setup
    python manage.py browse_account <username>       # browser + VNC, no automation
    python manage.py onboard --config-file f.json    # non-interactive onboard
    python manage.py runserver                       # Django Admin at http://localhost:8000/admin/
    python manage.py migrate                         # run Django migrations
"""
import os
import sys
import warnings

# langchain-openai stores a Pydantic model in a dict-typed field, triggering
# a harmless serialization warning on every structured-output call.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")


if __name__ == "__main__":
    from django.core.management import execute_from_command_line
    from linkedin.premigrations import run_premigrations

    run_premigrations()

    # Bare `python manage.py` with no args → run the daemon (backward compat).
    if len(sys.argv) == 1:
        sys.argv = [sys.argv[0], "rundaemon"]

    execute_from_command_line(sys.argv)
