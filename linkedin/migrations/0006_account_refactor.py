"""Account refactor: drop Django auth User coupling.

LinkedInProfile becomes LinkedInAccount with its own username field. The
backfill derives usernames from ``linkedin_username`` (the LinkedIn login
email) via email-prefix extraction — matching the convention in
``onboarding.py:_email_to_handle``. No auth_user queries.

Campaign.users (M2M to User) becomes Campaign.account (FK to LinkedInAccount).
The still-present ``account.user_id`` column is used only as an opaque
integer bridge to match Campaign.users rows to accounts — no User lookups.
Orphan campaigns raise rather than being silently deleted.

ActionLog.linkedin_profile is renamed to ActionLog.account.
A partial unique constraint enforces one active campaign per account.
"""

import re

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Count


def _email_to_handle(email: str) -> str:
    """Derive a Django-username-safe handle from an email local part.

    Matches the convention used at runtime in linkedin/onboarding.py. Inlined
    here because migrations must not import from app code (the runtime module
    can change shape across migration states).
    """
    local = (email or "").split("@", 1)[0].lower()
    handle = re.sub(r"[^a-z0-9_]", "_", local).strip("_")
    return handle or "account"


def populate_account_username(apps, schema_editor):
    LinkedInAccount = apps.get_model("linkedin", "LinkedInAccount")
    seen: set[str] = set()
    for acc in LinkedInAccount.objects.order_by("pk"):
        base = _email_to_handle(acc.linkedin_username)[:146]
        handle = base
        n = 2
        while handle in seen:
            handle = f"{base}_{n}"
            n += 1
        seen.add(handle)
        acc.username = handle
        acc.save(update_fields=["username"])


def verify_usernames_unique(apps, schema_editor):
    LinkedInAccount = apps.get_model("linkedin", "LinkedInAccount")
    empty = list(
        LinkedInAccount.objects.filter(username="").values_list("pk", flat=True)
    )
    if empty:
        raise RuntimeError(
            f"LinkedInAccount pks with empty username after backfill: {empty}"
        )
    dupes = list(
        LinkedInAccount.objects.values("username")
        .annotate(n=Count("username"))
        .filter(n__gt=1)
    )
    if dupes:
        raise RuntimeError(f"Duplicate LinkedInAccount usernames: {dupes}")


def populate_campaign_account(apps, schema_editor):
    Campaign = apps.get_model("linkedin", "Campaign")
    LinkedInAccount = apps.get_model("linkedin", "LinkedInAccount")

    for campaign in Campaign.objects.all():
        first_user_id = campaign.users.values_list("id", flat=True).first()
        if first_user_id is None:
            raise RuntimeError(
                f"Campaign pk={campaign.pk} name={campaign.name!r} has no users — "
                f"assign or delete before running migration 0006."
            )
        account = LinkedInAccount.objects.filter(user_id=first_user_id).first()
        if account is None:
            raise RuntimeError(
                f"Campaign pk={campaign.pk} name={campaign.name!r} has user_id="
                f"{first_user_id} with no matching LinkedInAccount — re-onboard "
                f"the account first."
            )
        campaign.account = account
        campaign.save(update_fields=["account"])


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0005_add_send_message_check_inbox_task_types"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="LinkedInProfile",
            new_name="LinkedInAccount",
        ),
        migrations.AddField(
            model_name="linkedinaccount",
            name="username",
            field=models.CharField(default="", max_length=150),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="linkedinaccount",
            name="is_archived",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(populate_account_username, migrations.RunPython.noop),
        migrations.RunPython(verify_usernames_unique, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="linkedinaccount",
            name="username",
            field=models.CharField(max_length=150, unique=True),
        ),
        migrations.AddField(
            model_name="campaign",
            name="account",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="campaigns",
                to="linkedin.linkedinaccount",
            ),
        ),
        migrations.RunPython(populate_campaign_account, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="campaign",
            name="account",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="campaigns",
                to="linkedin.linkedinaccount",
            ),
        ),
        migrations.RemoveField(
            model_name="campaign",
            name="users",
        ),
        migrations.RemoveField(
            model_name="linkedinaccount",
            name="user",
        ),
        migrations.AlterField(
            model_name="campaign",
            name="active",
            field=models.BooleanField(default=False),
        ),
        migrations.RemoveIndex(
            model_name="actionlog",
            name="linkedin_ac_linkedi_37318d_idx",
        ),
        migrations.RenameField(
            model_name="actionlog",
            old_name="linkedin_profile",
            new_name="account",
        ),
        migrations.AddIndex(
            model_name="actionlog",
            index=models.Index(
                fields=["account", "action_type", "created_at"],
                name="linkedin_ac_account_d8d09c_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.UniqueConstraint(
                condition=models.Q(("active", True)),
                fields=("account",),
                name="one_active_campaign_per_account",
            ),
        ),
        migrations.AlterField(
            model_name="task",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("running", "Running"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
