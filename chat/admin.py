"""Django admin registrations for the chat app.

Fact and Document are the editable knowledge base the assistant reads from.
Conversations are shown read-only so you can review chats without editing them.
"""

from django.conf import settings
from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from .models import Conversation, Document, Fact, LLMCredential, Message, TokenUsage


@admin.register(LLMCredential)
class LLMCredentialAdmin(ModelAdmin):
    list_display = ("provider", "label", "masked_key", "is_active", "updated_at")
    list_filter = ("provider", "is_active")
    list_editable = ("is_active",)
    search_fields = ("provider", "label")

    @admin.display(description="key")
    def masked_key(self, obj):
        key = obj.api_key or ""
        return f"…{key[-4:]}" if len(key) >= 4 else "····"


@admin.register(Fact)
class FactAdmin(ModelAdmin):
    list_display = ("question", "category", "is_active", "order", "updated_at")
    list_editable = ("is_active", "order")
    list_filter = ("category", "is_active")
    search_fields = ("question", "answer", "category")


@admin.register(Document)
class DocumentAdmin(ModelAdmin):
    list_display = ("title", "slug", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("title", "content", "slug")
    prepopulated_fields = {"slug": ("title",)}


class MessageInline(TabularInline):
    model = Message
    extra = 0
    readonly_fields = ("role", "content", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(ModelAdmin):
    list_display = ("id", "created_at", "updated_at")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [MessageInline]

    def has_add_permission(self, request):
        return False


@admin.register(TokenUsage)
class TokenUsageAdmin(ModelAdmin):
    """Read-only view of token consumption per model per month, with the share of the
    Mistral free-tier monthly ceiling used. Rows are written by the chat stream."""

    list_display = (
        "model",
        "period",
        "input_tokens",
        "output_tokens",
        "total_display",
        "quota_used",
    )
    list_filter = ("model", "period")
    readonly_fields = ("model", "period", "input_tokens", "output_tokens", "updated_at")

    def has_add_permission(self, request):
        return False

    @admin.display(description="total")
    def total_display(self, obj):
        return f"{obj.total_tokens:,}"

    @admin.display(description="free quota used")
    def quota_used(self, obj):
        cap = settings.MISTRAL_FREE_TOKENS_PER_MONTH
        if not cap:
            return "—"
        return f"{obj.total_tokens / cap * 100:.2f}%"
