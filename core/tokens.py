"""The design tokens, in one place, for both the public pages and the admin.

They used to be declared in four: `templates/site_base.html`, `core/templates/core/
landing.html`, `UNFOLD["COLORS"]` in settings, and `core/static/core/unfold-overrides.css`.
Nothing tied them together, so changing a colour meant editing two or three files and
noticing later that a fourth had not moved. Every "the admin doesn't match the site" bug
came from that.

Now: this module is the source. `settings.UNFOLD["COLORS"]` imports `ADMIN_COLORS`; the
public templates render `site_css()` through the `{% site_tokens %}` tag; the admin's
override sheet consumes only the `--color-*` variables Unfold emits from `ADMIN_COLORS`.
One edit here moves all of it.

**Kept pure on purpose.** No Django imports, because `config/settings.py` imports this
before the app registry exists. Data and string formatting only.

**The admin is a variant, not a copy.** Its neutral ramp is deliberately wider than the
site's: the site's own planes sit within a couple of values of each other, which is right
for small bordered cards and merges into one flat band across the admin's large surfaces.
So the accent, the ink and the type are shared — those are what make it read as one product
— and the greys are derived separately, below, with that reasoning attached.
"""

# --- The palette ------------------------------------------------------------------------
# Two ladder inputs and a stop, per theme. Every plane is a mix of them, so a new surface is
# a new percentage rather than a colour picked once for light and again, by eye, for dark.
#
# The stops differ per theme on purpose. Light has to fit its whole ladder into the narrow
# band below white, so its fraction is large; dark spans a far wider range and needs a small
# one. Equal percentages would give light three tones nobody can tell apart.
#
# Light was #f0ebe0 until it read as dust rather than warmth across a full page — the same
# thing that made the admin's warm base scale look dirty. Both ends are cool neutrals now.
# The lightness did not change, so the ladder kept the room it was given.
LIGHT = {
    "plane": "#ececf1",  # the page — the least elevated thing on screen
    "lift": "#ffffff",  # what "up" means in this theme
    "step": "55%",  # where the middle plane sits between the two
    "text": "#16181d",
    "muted": "#5b616d",
    "line": "#e3e4e9",
    "accent": "#1f6f78",  # the one petrol accent
    "accent-ink": "#0e4a51",
    "danger": "#ce2c31",
    "on-accent": "#ffffff",
    "raised-line-mix": "24%",  # how far --line moves toward the ink on raised elements
    "shadow-sm": "0 1px 2px rgba(22,24,29,.04), 0 1px 3px rgba(22,24,29,.06)",
    "shadow-md": "0 2px 4px rgba(22,24,29,.05), 0 6px 16px rgba(22,24,29,.08)",
    "shadow-lg": "0 8px 24px rgba(22,24,29,.10), 0 2px 6px rgba(22,24,29,.06)",
}

# Dark restates only what actually differs. It was already right when light was broken, so
# these values are unchanged from before the ladder was rebuilt.
DARK = {
    "plane": "#0d0f12",
    "lift": "#2d3035",
    "step": "25%",
    "text": "#e8e9ec",
    "muted": "#99a0ac",
    "line": "#222730",
    "accent": "#5cb6be",
    "accent-ink": "#aee3e7",
    "danger": "#ff9592",
    "on-accent": "#08272a",
    "raised-line-mix": "34%",
    "shadow-sm": "0 1px 2px rgba(0,0,0,.4)",
    "shadow-md": "0 4px 14px rgba(0,0,0,.5)",
    "shadow-lg": "0 10px 30px rgba(0,0,0,.6)",
}

# Both faces come from the visitor's own system, so nothing is downloaded: no font file to
# host, no @font-face, and no dependency on collectstatic. That last part is load-bearing —
# the public pages must render on a cold instance with nothing collected, which is why they
# cannot reference a static file at all. The admin gets the same pair by overriding Unfold's
# --font-sans, which drops its Inter download for the same reason.
FONT_SYSTEM = (
    "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', Roboto, system-ui, sans-serif"
)
FONT_SERIF = "ui-serif, 'New York', 'Iowan Old Style', Palatino, Georgia, serif"

# Headings take the serif, body the sans. --font-display briefly pointed at the sans, on the
# reasoning that marketing gets a voice and product interface shouldn't; that drew the line
# in the wrong place. Everything these tokens dress — the landing, /signin, every allauth
# page — is pre-login marketing, so the wordmark was serif on one page and sans one click
# later. The interface that stays plain is the signed-in dashboard, in another repository.
EASE = "cubic-bezier(0.22, 1, 0.36, 1)"

_THEME_KEYS = (
    "plane",
    "lift",
    "step",
    "text",
    "muted",
    "line",
    "accent",
    "accent-ink",
    "danger",
    "on-accent",
    "shadow-sm",
    "shadow-md",
    "shadow-lg",
)


def _theme_block(theme: dict, indent: str = "  ") -> str:
    lines = [f"{indent}--{key}: {theme[key]};" for key in _THEME_KEYS]
    lines.append(
        f"{indent}--raised-line: color-mix(in srgb, var(--line), "
        f"var(--text) {theme['raised-line-mix']});"
    )
    return "\n".join(lines)


def site_css() -> str:
    """The public pages' `:root` blocks, rendered from the palette above.

    Dark is emitted twice — once behind `prefers-color-scheme` and once behind
    `[data-theme="dark"]` — because CSS cannot share one declaration block between a media
    query and a selector, and both are needed: the media query follows the operating system,
    the attribute lets the toggle override it. `:not([data-theme="light"])` is what lets
    someone on a dark OS force light.
    """
    derived = f"""  --e0: var(--plane);
  --e1: color-mix(in srgb, var(--plane), var(--lift) var(--step));
  --e2: var(--lift);
  /* Semantic names for the planes. Everything else uses these, never the rungs, so the
     ladder can be retuned without touching a single component. */
  --bg: var(--e0);       /* page, and inset wells: form fields sit back down here */
  --surface: var(--e1);  /* cards, header, panels */
  --raised: var(--e2);   /* raised on a card: the provider buttons */
  --font-system: {FONT_SYSTEM};
  --font-serif: {FONT_SERIF};
  --font-display: var(--font-serif);
  --font-sans: var(--font-system);
  --ease: {EASE};"""

    dark = _theme_block(DARK, indent="    ")
    # No Django tag syntax in this comment: it lands verbatim in the page, and the suite
    # asserts that no "{%" ever reaches rendered output — a leaked tag once printed as body
    # text on a live page, so the rule is enforced rather than trusted.
    return (
        """/* Design tokens — generated from core/tokens.py, the one source the admin
   shares too. Edit them there, not here; the site_tokens template tag renders this. */
:root {
"""
        + _theme_block(LIGHT)
        + "\n"
        + derived
        + """
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
"""
        + dark
        + """
  }
}
:root[data-theme="dark"] {
"""
        + _theme_block(DARK)
        + """
}"""
    )


# --- The admin's variant ------------------------------------------------------------------
# Unfold wants scales, not semantic names, so the shared values are placed on the shades it
# actually renders. The accent, the ink and the type come straight from the palette above;
# only the neutral ramp is the admin's own.
#
# base-50 / base-950 are load-bearing INSIDE Unfold — it reuses them for zebra striping and
# hover tints — so they have to stay near the surface tone. The page and field planes get
# their own values in core/static/core/unfold-overrides.css for that reason.
ADMIN_COLORS = {
    "base": {
        "50": "#f8f8fa",
        "100": "#f1f1f4",
        "200": "#e3e4e8",  # borders
        "300": "#c7c9d0",
        "400": DARK["muted"],
        "500": "#7c828e",
        "600": LIGHT["muted"],
        "700": "#3a4049",
        "800": DARK["line"],  # dark-mode borders
        "900": "#1e242e",  # dark surfaces
        "950": "#13171e",  # the dark page
    },
    "primary": {
        "50": "#eef7f8",
        "100": "#d8edef",
        "200": "#b9dfe3",
        "300": "#93cfd5",
        "400": "#78c3ca",
        "500": DARK["accent"],
        "600": LIGHT["accent"],
        "700": "#1a5f68",
        "800": "#14525a",
        "900": LIGHT["accent-ink"],
        "950": DARK["on-accent"],
    },
    # The site has ONE ink and separates headings by weight, not colour, so `important` and
    # `default` are the same value. These were base-700/base-300 before, and base-300 was a
    # warm tan — dark-mode body text came out sepia beside the site's cool grey.
    "font": {
        "subtle-light": LIGHT["muted"],
        "subtle-dark": DARK["muted"],
        "default-light": LIGHT["text"],
        "default-dark": DARK["text"],
        "important-light": LIGHT["text"],
        "important-dark": DARK["text"],
    },
}
