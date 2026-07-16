"""Django admin registrations for the chat app.

Fact and Document are the editable knowledge base the assistant reads from.
Documents can be uploaded as files (PDF/Word/text): the text is extracted into
`content` for the agent, and the original bytes are kept for an in-admin preview.
Conversations are shown read-only so you can review chats without editing them.
"""

import re

from django import forms
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.http import content_disposition_header
from unfold.admin import ModelAdmin, TabularInline
from unfold.widgets import UnfoldAdminFileFieldWidget, UnfoldBooleanWidget

from .extraction import MAX_UPLOAD_BYTES, content_type_for, extract_text
from .models import Conversation, Document, Fact, LLMCredential, Message, TokenUsage

# Control characters must never reach a stored filename: Django's multipart parser only
# strips path separators, and a CR/LF smuggled into file_name would make every later
# Content-Disposition header raise BadHeaderError — a permanent 500 on the preview.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


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


class DocumentAdminForm(forms.ModelForm):
    """Adds an upload field: the file's text is extracted into `content` on save, and
    the original bytes are kept for the preview (see the Document model docstring)."""

    upload = forms.FileField(
        required=False,
        # Declared form fields skip Unfold's widget swapping — without the explicit
        # widget this renders as a bare, unthemed native file input.
        widget=UnfoldAdminFileFieldWidget,
        help_text="PDF, Word (.docx), text, or markdown — max 10 MB. Replaces the "
        "content below with the file's extracted text (editable afterwards).",
    )
    remove_file = forms.BooleanField(
        required=False,
        widget=UnfoldBooleanWidget,
        help_text="Remove the stored file and its preview (ignored when a new file is "
        "chosen above). The content below is kept.",
    )

    class Meta:
        model = Document
        fields = ["slug", "title", "upload", "remove_file", "content", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Content is filled from the uploaded file when one is given, so the field
        # itself can't be required — clean() enforces "content or file" instead.
        self.fields["content"].required = False

    def clean_upload(self):
        upload = self.cleaned_data.get("upload")
        if not upload:
            return None
        if upload.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("File is too large — 10 MB max.")
        data = upload.read()
        try:
            self.extracted_text = extract_text(data, upload.name)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        self.uploaded_data = data
        return upload

    def clean(self):
        cleaned = super().clean()
        if getattr(self, "extracted_text", ""):
            cleaned["content"] = self.extracted_text
        elif not (cleaned.get("content") or "").strip():
            self.add_error("content", "Provide content, or upload a file to extract it from.")
        return cleaned


@admin.register(Document)
class DocumentAdmin(ModelAdmin):
    form = DocumentAdminForm
    list_display = ("title", "slug", "has_file", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("title", "content", "slug")
    prepopulated_fields = {"slug": ("title",)}
    fields = ("slug", "title", "upload", "remove_file", "preview", "content", "is_active")
    readonly_fields = ("preview",)

    def get_queryset(self, request):
        # The blob is only ever read by serve_file. Keeping it out of the changelist
        # and change form matters on the 512MB worker: a page of 10MB uploads would
        # otherwise be pulled from Postgres on every admin visit — and written back
        # on every content edit (a deferred field is left out of the UPDATE too).
        return super().get_queryset(request).defer("file_data")

    def save_model(self, request, obj, form, change):
        data = getattr(form, "uploaded_data", None)
        if data:
            upload = form.cleaned_data["upload"]
            obj.file_data = data
            obj.file_name = _CONTROL_CHARS.sub("", upload.name)
            obj.file_content_type = content_type_for(upload.name)
            obj.file_uploaded_at = timezone.now()
        elif form.cleaned_data.get("remove_file"):
            obj.file_data = None
            obj.file_name = ""
            obj.file_content_type = ""
            obj.file_uploaded_at = None
        super().save_model(request, obj, form, change)

    def get_urls(self):
        # The uploaded file lives in the database, not on disk (Render's disk is
        # ephemeral), so it's served by a small admin-only view. Custom URLs must
        # precede the default ones, whose catch-all would swallow this path.
        custom = [
            path(
                "<int:pk>/file/",
                self.admin_site.admin_view(self.serve_file),
                name="chat_document_file",
            )
        ]
        return custom + super().get_urls()

    def serve_file(self, request, pk):
        # admin_view() only checks is_staff — enforce the same Document permission the
        # rest of this ModelAdmin does, or any staff account could fetch every blob.
        if not self.has_view_permission(request):
            raise PermissionDenied
        document = get_object_or_404(Document, pk=pk, file_data__isnull=False)
        response = HttpResponse(
            bytes(document.file_data),  # Postgres hands back a memoryview
            content_type=document.file_content_type or "application/octet-stream",
        )
        # inline → browsers render PDFs natively; this is what the preview iframe
        # loads. content_disposition_header() RFC-5987-encodes non-ASCII filenames and
        # escapes quotes — a bare f-string would mangle both.
        disposition = content_disposition_header(as_attachment=False, filename=document.file_name)
        if disposition:
            response["Content-Disposition"] = disposition
        # Explicit, so the iframe keeps working even if the clickjacking middleware is
        # ever enabled (it leaves an existing header alone).
        response["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @admin.display(description="file", boolean=True)
    def has_file(self, obj):
        # file_name, not file_data: the blob is deferred in the changelist queryset,
        # and touching a deferred field refetches it — one blob query per row.
        return bool(obj.file_name)

    @admin.display(description="preview")
    def preview(self, obj):
        if not (obj.pk and obj.file_name):
            return "No file uploaded."
        url = reverse("admin:chat_document_file", args=[obj.pk])
        if obj.file_content_type == "application/pdf":
            return format_html(
                '<iframe src="{}" title="{}" style="width:100%;height:480px;'
                'border:1px solid var(--color-base-200);border-radius:6px;"></iframe>',
                url,
                obj.file_name,
            )
        return format_html('<a href="{}">Download {}</a>', url, obj.file_name)


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
