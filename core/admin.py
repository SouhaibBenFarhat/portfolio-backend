"""Admin for the core app: the themed auth User/Group, and the encrypted social-login apps.

django.contrib.auth registers User/Group with plain ModelAdmins, which would render as the
one unstyled corner of the themed admin, so they're re-registered with Unfold. The
OAuthCredential admin manages social-login client credentials with the secret encrypted at
rest; allauth's own plaintext SocialApp admin is hidden so credentials only ever go through
the encrypted path.
"""

from allauth.account.admin import EmailAddressAdmin as BaseEmailAddressAdmin
from allauth.account.models import EmailAddress
from allauth.socialaccount.admin import SocialAccountAdmin as BaseSocialAccountAdmin
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from django.contrib.sites.models import Site
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from core.paging import PageSizeAdminMixin

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
class UserAdmin(PageSizeAdminMixin, BaseUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm


@admin.register(Group)
class GroupAdmin(PageSizeAdminMixin, BaseGroupAdmin, ModelAdmin):
    pass


@admin.register(OAuthCredential)
class OAuthCredentialAdmin(PageSizeAdminMixin, ModelAdmin):
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
class ProfileAdmin(PageSizeAdminMixin, ModelAdmin):
    """The tenant list — one row per signed-in user, keyed by their public handle."""

    list_display = ("handle", "user", "github_username", "is_published", "updated_at")
    list_filter = ("is_published",)
    list_editable = ("is_published",)
    search_fields = ("handle", "user__email", "github_username")
    list_select_related = ("user",)
    readonly_fields = ("created_at", "updated_at")


# --- allauth's own admins, re-registered ------------------------------------
# allauth registers EmailAddress and SocialAccount with plain django.contrib ModelAdmins,
# which render as the one unstyled corner of a themed admin — the same reason User and
# Group are re-registered above. They only became visible enough to matter when the
# sidebar started pointing at them by name.
#
# Subclassing allauth's classes rather than replacing them keeps whatever behaviour they
# define; Unfold's ModelAdmin goes first in the bases so its templates win. Each also gains
# the search box and filters every other changelist here has.
admin.site.unregister(EmailAddress)
admin.site.unregister(SocialAccount)


@admin.register(EmailAddress)
class EmailAddressAdmin(PageSizeAdminMixin, ModelAdmin, BaseEmailAddressAdmin):
    """Which addresses an account holds, and which of them are confirmed."""

    list_display = ("email", "user", "primary", "verified")
    list_filter = ("verified", "primary")
    search_fields = ("email", "user__email", "user__username")
    list_select_related = ("user",)


@admin.register(SocialAccount)
class SocialAccountAdmin(PageSizeAdminMixin, ModelAdmin, BaseSocialAccountAdmin):
    """Which provider a given account signs in through."""

    list_display = ("user", "provider", "uid", "last_login", "date_joined")
    list_filter = ("provider",)
    search_fields = ("user__email", "user__username", "uid")
    list_select_related = ("user",)


# --- Third-party registrations, brought up to the same standard ---------------
# Sites and social tokens arrive as plain django.contrib ModelAdmins. They used to be left
# out of the sidebar as noise, which made that acceptable; now the sidebar is an honest map
# of every app, so anything it points at has to be themed, paged and searchable like the
# rest. A link to an unstyled page is the sidebar telling the truth about a lie.
admin.site.unregister(Site)
admin.site.unregister(SocialToken)


@admin.register(Site)
class SiteAdmin(PageSizeAdminMixin, ModelAdmin):
    """django.contrib.sites' one row. allauth requires SITE_ID; nothing here edits it."""

    list_display = ("domain", "name")
    search_fields = ("domain", "name")


@admin.register(SocialToken)
class SocialTokenAdmin(PageSizeAdminMixin, ModelAdmin):
    """Raw OAuth tokens. Read-only: there is no reason to hand-edit one, and every reason
    not to paste a bearer token into a form that logs its errors."""

    list_display = ("app", "account", "expires_at")
    list_filter = ("app__provider",)
    search_fields = ("account__uid", "account__user__email")
    list_select_related = ("app", "account")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
