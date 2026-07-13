"""Django admin registrations for the chat app.

Fact and Document are the editable knowledge base the assistant reads from.
Conversations are shown read-only so you can review chats without editing them.
"""

from django.contrib import admin

from .models import Conversation, Document, Fact, Message


@admin.register(Fact)
class FactAdmin(admin.ModelAdmin):
    list_display = ("question", "category", "is_active", "order", "updated_at")
    list_editable = ("is_active", "order")
    list_filter = ("category", "is_active")
    search_fields = ("question", "answer", "category")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("title", "content", "slug")
    prepopulated_fields = {"slug": ("title",)}


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    readonly_fields = ("role", "content", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "updated_at")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [MessageInline]

    def has_add_permission(self, request):
        return False
