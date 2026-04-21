from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0002_linkedinprofile_self_lead"),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("llm_api_key", models.CharField(blank=True, default="", max_length=500)),
                ("ai_model", models.CharField(blank=True, default="", max_length=200)),
                ("llm_api_base", models.CharField(blank=True, default="", max_length=500)),
            ],
            options={
                "verbose_name": "Site Configuration",
                "verbose_name_plural": "Site Configuration",
            },
        ),
    ]
