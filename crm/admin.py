from django.contrib import admin

from crm.models.deal import Deal
from crm.models.lead import Lead


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("first_name", "last_name", "company_name", "linkedin_url", "disqualified", "creation_date")
    list_filter = ("disqualified",)
    search_fields = ("first_name", "last_name", "company_name", "public_identifier")
    readonly_fields = ("creation_date", "update_date")


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = ("lead", "campaign", "state", "closing_reason", "creation_date")
    list_filter = ("state", "closing_reason", "campaign")
    search_fields = ("lead__first_name", "lead__last_name", "lead__company_name")
    raw_id_fields = ("lead", "campaign")
    readonly_fields = ("creation_date", "update_date")
