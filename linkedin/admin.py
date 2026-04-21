import urllib.parse

from django.contrib import admin

from chat.models import ChatMessage

from linkedin.models import ActionLog, Campaign, LinkedInAccount, SearchKeyword, Task


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "account", "active", "booking_link", "is_freemium", "action_fraction")
    list_filter = ("active", "account")
    list_editable = ("active",)
    raw_id_fields = ("account",)


@admin.register(LinkedInAccount)
class LinkedInAccountAdmin(admin.ModelAdmin):
    list_display = (
        "username", "linkedin_username", "active", "is_archived",
        "claimed_by", "last_heartbeat", "proxy_host",
    )
    list_filter = ("active", "is_archived", "claimed_by")
    search_fields = ("username", "linkedin_username", "claimed_by")
    raw_id_fields = ("self_lead",)
    readonly_fields = ("claimed_at", "last_heartbeat")
    exclude = ("linkedin_password",)

    @admin.display(description="Proxy host")
    def proxy_host(self, obj):
        if not obj.proxy_url:
            return ""
        parsed = urllib.parse.urlparse(obj.proxy_url)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}" if host else ""


@admin.register(SearchKeyword)
class SearchKeywordAdmin(admin.ModelAdmin):
    list_display = ("keyword", "campaign", "used", "used_at")
    list_filter = ("used", "campaign")
    raw_id_fields = ("campaign",)


@admin.register(ActionLog)
class ActionLogAdmin(admin.ModelAdmin):
    list_display = ("action_type", "account", "campaign", "created_at")
    list_filter = ("action_type", "campaign")
    raw_id_fields = ("account", "campaign")
    date_hierarchy = "created_at"
    readonly_fields = ("account", "campaign", "action_type", "created_at")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("task_type", "status", "scheduled_at", "payload", "created_at")
    list_filter = ("task_type", "status")
    readonly_fields = (
        "task_type", "status", "scheduled_at", "payload", "error",
        "created_at", "started_at", "completed_at",
    )
    date_hierarchy = "scheduled_at"


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("content_type", "object_id", "owner", "creation_date")
    list_filter = ("content_type", "owner")
    raw_id_fields = ("owner", "answer_to", "topic")
    date_hierarchy = "creation_date"
    readonly_fields = ("content_type", "object_id", "content", "owner", "creation_date")
