"""The admin sign-in form.

Only one thing is added: placeholders. Unfold's `AuthenticationForm` is subclassed rather
than Django's, because Unfold's version is what carries the widget classes the admin theme
styles against — swap in a plain Django form and the fields lose their borders and the
stylesheet's field rules stop matching.

`setdefault` rather than assignment, so a placeholder set anywhere else (a later Unfold
version, a form kwarg) wins instead of being silently overwritten.
"""

from unfold.forms import AuthenticationForm

PLACEHOLDERS = {
    "username": "Your username",
    "password": "Your password",
}


class AdminLoginForm(AuthenticationForm):
    """Unfold's sign-in form with placeholder text in the two fields."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, text in PLACEHOLDERS.items():
            if name in self.fields:
                self.fields[name].widget.attrs.setdefault("placeholder", text)
