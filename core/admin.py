"""Admin for the core app: the themed auth User/Group, and the encrypted social-login apps.

django.contrib.auth registers User/Group with plain ModelAdmins, which would render as the
one unstyled corner of the themed admin, so they're re-registered with Unfold. The
OAuthCredential admin manages social-login client credentials with the secret encrypted at
rest; allauth's own plaintext SocialApp admin is hidden so credentials only ever go through
the encrypted path.
"""

from allauth.socialaccount.models import SocialApp
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from .models import OAuthCredential, Profile

admin.site.unregister(User)
admin.site.unregister(Group)

# Social-login apps are managed as encrypted OAuthCredential rows, so hide allauth's own
# SocialApp admin: a row added there would store the secret in plaintext and collide with
# ours (allauth allows only one app per provider). Guarded in case it isn't registered.
try:
    admin.site.unregister(SocialApp)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin):
    pass


@admin.register(OAuthCredential)
class OAuthCredentialAdmin(ModelAdmin):
    """Social-login client credentials, secret encrypted at rest (core.models). One row per
    provider; add the client id and secret from the provider's developer console."""

    list_display = (
        "__str__",
        "provider",
        "provider_id",
        "masked_secret",
        "link_by_verified_email",
        "is_active",
        "updated_at",
    )
    list_filter = ("provider", "link_by_verified_email", "is_active")
    list_editable = ("is_active",)
    search_fields = ("name", "provider", "provider_id", "client_id")
    fields = (
        "provider",
        "provider_id",
        "name",
        "client_id",
        "secret",
        "server_url",
        "link_by_verified_email",
        "is_active",
    )

    @admin.display(description="secret")
    def masked_secret(self, obj):
        secret = obj.secret or ""
        return f"…{secret[-4:]}" if len(secret) >= 4 else "····"


@admin.register(Profile)
class ProfileAdmin(ModelAdmin):
    """The tenant list — one row per signed-in user, keyed by their public handle."""

    list_display = ("handle", "user", "github_username", "is_published", "updated_at")
    list_filter = ("is_published",)
    list_editable = ("is_published",)
    search_fields = ("handle", "user__email", "github_username")
    list_select_related = ("user",)
    readonly_fields = ("created_at", "updated_at")
