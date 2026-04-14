# linkedin/urls.py
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

from linkedin.api_views import (
    account_detail_view,
    accounts_collection_view,
    campaign_activate_view,
    campaign_deactivate_view,
    campaign_deals_view,
    campaign_detail_view,
    campaign_stats_view,
    campaigns_collection_view,
    conversation_view,
    deal_detail_view,
    lead_deals_view,
    lead_detail_view,
    send_message_view,
    task_status_view,
)

urlpatterns = [
    path("admin/", admin.site.urls),

    # Messages
    path("api/messages/send/", send_message_view),
    path("api/messages/<str:public_id>/", conversation_view),

    # Tasks
    path("api/tasks/<int:task_id>/", task_status_view),

    # Accounts
    path("api/accounts/", accounts_collection_view),
    path("api/accounts/<int:account_id>/", account_detail_view),

    # Campaigns
    path("api/campaigns/", campaigns_collection_view),
    path("api/campaigns/<int:campaign_id>/", campaign_detail_view),
    path("api/campaigns/<int:campaign_id>/activate/", campaign_activate_view),
    path("api/campaigns/<int:campaign_id>/deactivate/", campaign_deactivate_view),
    path("api/campaigns/<int:campaign_id>/deals/", campaign_deals_view),
    path("api/campaigns/<int:campaign_id>/stats/", campaign_stats_view),

    # Leads & deals (read-only)
    path("api/leads/<str:public_id>/", lead_detail_view),
    path("api/leads/<str:public_id>/deals/", lead_deals_view),
    path("api/deals/<int:deal_id>/", deal_detail_view),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
