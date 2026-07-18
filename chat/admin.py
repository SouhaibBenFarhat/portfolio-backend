"""Django admin registrations for the chat app.

ChatModel is the failover chain, reordered by dragging. Fact and Document are the
editable knowledge base the assistant reads from.
Documents can be uploaded as files (PDF/Word/text): the text is extracted into
`content` for the agent, and the original bytes are kept for an in-admin preview.
Conversations are shown read-only so you can review chats without editing them.
"""

import os
import re
from functools import lru_cache

import litellm
from django import forms
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, F, Q, Window
from django.db.models.functions import RowNumber
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.http import content_disposition_header
from unfold.admin import ModelAdmin, TabularInline
from unfold.widgets import (
    UnfoldAdminFileFieldWidget,
    UnfoldAdminSelect2Widget,
    UnfoldBooleanWidget,
)

from .agent import context_limit
from .extraction import MAX_UPLOAD_BYTES, content_type_for, extract_text
from .models import ChatModel, Conversation, Document, Fact, LLMCredential, Message, TokenUsage

# Control characters must never reach a stored filename: Django's multipart parser only
# strips path separators, and a CR/LF smuggled into file_name would make every later
# Content-Disposition header raise BadHeaderError — a permanent 500 on the preview.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class SearchableSelect(UnfoldAdminSelect2Widget):
    """A Select2 dropdown — opens on click, with a search box — that still accepts
    values outside its suggestions.

    A strict `<select>` would only offer what LiteLLM has catalogued — and its table
    lags new releases (`gemini/gemma-4-31b-it` routes fine but isn't listed), so the
    newest model is exactly the one a hard dropdown would reject. Two things keep free
    entry working: `data-tags` lets the search box submit a value that isn't in the
    list (Unfold's select2 init passes `data-*` options through), and the bound value
    is grafted into the options so a saved uncatalogued id renders selected on the
    change form instead of silently blanking.

    `options` is a callable so the (cached) suggestion lists are built on first
    render, not at import time.
    """

    def __init__(self, options, attrs=None):
        super().__init__(attrs={"data-tags": "true", **(attrs or {})})
        self._options = options

    def optgroups(self, name, value, attrs=None):
        suggestions = self._options()
        current = [v for v in value if v and v not in suggestions]
        # The leading empty option keeps the add form blank rather than silently
        # preselecting the alphabetically-first suggestion.
        self.choices = [("", "")] + [(v, v) for v in (*current, *suggestions)]
        return super().optgroups(name, value, attrs)


@lru_cache(maxsize=1)
def _provider_suggestions() -> tuple[str, ...]:
    """Every provider LiteLLM can route, plus the integration names this credential
    store also holds (github). Cached — the list is static for the process."""
    providers = {str(getattr(provider, "value", provider)) for provider in litellm.provider_list}
    return tuple(sorted(providers | {"github"}))


@lru_cache(maxsize=1)
def _model_id_suggestions() -> tuple[str, ...]:
    """Provider-prefixed chat model ids from LiteLLM's catalogue. Prefixed only — the
    prefix is what picks the API key — and chat-mode only (the table also carries
    embedding/image entries the chain can't use). Cached — it's a static table."""
    return tuple(
        sorted(
            model_id
            for model_id, info in litellm.model_cost.items()
            if "/" in model_id and isinstance(info, dict) and info.get("mode") == "chat"
        )
    )


class LLMCredentialAdminForm(forms.ModelForm):
    class Meta:
        model = LLMCredential
        fields = "__all__"
        widgets = {"provider": SearchableSelect(_provider_suggestions)}


@admin.register(LLMCredential)
class LLMCredentialAdmin(ModelAdmin):
    form = LLMCredentialAdminForm
    list_display = ("provider", "label", "masked_key", "is_active", "updated_at")
    list_filter = ("provider", "is_active")
    list_editable = ("is_active",)
    search_fields = ("provider", "label")

    @admin.display(description="key")
    def masked_key(self, obj):
        key = obj.api_key or ""
        return f"…{key[-4:]}" if len(key) >= 4 else "····"


class ChatModelAdminForm(forms.ModelForm):
    """Rejects a model id LiteLLM can't route, before it reaches the chain."""

    class Meta:
        model = ChatModel
        fields = ["model_id", "order", "is_active"]
        widgets = {"model_id": SearchableSelect(_model_id_suggestions)}

    def clean_model_id(self):
        model_id = (self.cleaned_data.get("model_id") or "").strip()
        # Validate the *provider prefix*, not the model name. LiteLLM's model table lags
        # new releases — zai/glm-5.2 routes correctly but get_model_info() raises "isn't
        # mapped yet" — so validating the name would reject models that work. Resolving
        # the prefix proves the provider is real while tolerating a name LiteLLM hasn't
        # catalogued yet; a genuinely wrong name then fails at the provider, loudly.
        try:
            litellm.get_llm_provider(model_id)
        except Exception as exc:  # noqa: BLE001 — any resolution failure means a bad id
            raise forms.ValidationError(
                f"LiteLLM cannot route '{model_id}'. Use a provider-prefixed id, such as "
                '"mistral/mistral-small-latest" or "zai/glm-4.7-flash".'
            ) from exc
        return model_id


@admin.register(ChatModel)
class ChatModelAdmin(ModelAdmin):
    """The chat's failover chain. Drag the rows into order and press Save: the top
    active model answers every turn, and the ones under it are tried in order when it
    fails. An empty list falls back to the CHAT_MODEL/CHAT_FALLBACK_MODEL env vars.
    """

    form = ChatModelAdminForm
    # Unfold renders a drag handle per row and rewrites this field to the new positions
    # on drop. The drag alone doesn't persist — it fills the form inputs, so the Save
    # button below the list is what commits the new order.
    ordering_field = "order"
    list_display = ("model_id", "role", "key_source", "context_window", "is_active")
    list_editable = ("is_active",)
    list_filter = ("is_active",)
    search_fields = ("model_id",)

    def get_queryset(self, request):
        # Rank each row among the *active* ones, so `role` can name its place in the
        # chain without a query per row. Inactive rows rank within their own partition,
        # which `role` ignores — they're off the chain entirely.
        return (
            super()
            .get_queryset(request)
            .annotate(
                chain_rank=Window(
                    expression=RowNumber(),
                    partition_by=[F("is_active")],
                    order_by=[F("order").asc(), F("id").asc()],
                )
            )
        )

    @admin.display(description="role")
    def role(self, obj):
        if not obj.is_active:
            return "—"
        return "primary" if obj.chain_rank == 1 else f"fallback {obj.chain_rank - 1}"

    @admin.display(description="key")
    def key_source(self, obj):
        # Adding a model whose provider has no key configured is the expected first
        # mistake (GLM with no ZAI_API_KEY). The chain would just fail past it, which
        # looks like the model being bad rather than unconfigured — so name it here.
        if LLMCredential.objects.filter(provider=obj.provider, is_active=True).exists():
            return "admin"
        if os.getenv(f"{obj.provider.upper()}_API_KEY"):
            return "env"
        return "missing"

    @admin.display(description="context window")
    def context_window(self, obj):
        # 0 means LiteLLM hasn't catalogued this model yet (a very new one). It still
        # runs — the context gauge just falls back to CHAT_MAX_CONTEXT_TOKENS unclamped.
        window = context_limit(obj.model_id)
        return f"{window:,}" if window else "unknown to LiteLLM"


@admin.register(Fact)
class FactAdmin(ModelAdmin):
    list_display = ("question", "category", "is_active", "updated_at")
    list_editable = ("is_active",)
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
    readonly_fields = ("role", "content", "rating", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(ModelAdmin):
    list_display = ("id", "created_at", "updated_at", "rating_summary")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [MessageInline]

    def get_queryset(self, request):
        # Sum each thread's thumbs in one query, so the changelist doesn't run a count
        # per row. Ups and downs stay separate columns: a lively thread with praise and
        # complaints in equal measure should read differently from an unrated one, which
        # a single net figure would hide.
        return (
            super()
            .get_queryset(request)
            .annotate(
                ups=Count("messages", filter=Q(messages__rating=Message.Rating.UP)),
                downs=Count("messages", filter=Q(messages__rating=Message.Rating.DOWN)),
            )
        )

    @admin.display(description="ratings")
    def rating_summary(self, obj):
        if not (obj.ups or obj.downs):
            return "—"
        return f"↑ {obj.ups}  ↓ {obj.downs}"

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
