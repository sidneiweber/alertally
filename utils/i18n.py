"""Suporte a idiomas (inglês, português e espanhol) via Flask-Babel.

Ordem de escolha do idioma: cookie "lang" (seletor no header) e, sem ele,
o idioma preferido do navegador. Inglês é o padrão e o idioma dos textos-fonte.
"""
import humanize
from babel.numbers import format_decimal, format_percent
from flask import request
from flask_babel import Babel, get_locale, lazy_gettext as _l

# código do catálogo -> nome exibido no seletor (sempre no próprio idioma)
LANGUAGES = {
    "en": "English",
    "pt_BR": "Português",
    "es": "Español",
}
DEFAULT_LANGUAGE = "en"
LANG_COOKIE = "lang"

# idioma primário do navegador -> código do catálogo
_PRIMARY = {"en": "en", "pt": "pt_BR", "es": "es"}

# catálogos do humanize ("há 5 minutos"); inglês é o embutido
_HUMANIZE = {"pt_BR": "pt_BR", "es": "es_ES"}

# Severidade e estado vêm gravados em inglês no banco; aqui só muda a exibição
SEVERITY_LABELS = {
    "Critical": _l("Critical"),
    "Error": _l("Error"),
    "Warning": _l("Warning"),
    "Info": _l("Info"),
    "Debug": _l("Debug"),
    "Unknown": _l("Unknown"),
    "No severity": _l("No severity"),
}
STATE_LABELS = {
    "open": _l("open"),
    "closed": _l("closed"),
}


def select_locale():
    cookie = request.cookies.get(LANG_COOKIE)
    if cookie in LANGUAGES:
        return cookie
    for lang, _quality in request.accept_languages:
        code = _PRIMARY.get(lang.replace("_", "-").split("-")[0].lower())
        if code:
            return code
    return DEFAULT_LANGUAGE


def current_language():
    return str(get_locale() or DEFAULT_LANGUAGE)


def activate_humanize():
    """Ativa o idioma do humanize para a requisição atual."""
    lang = _HUMANIZE.get(current_language())
    if lang:
        humanize.i18n.activate(lang)
    else:
        humanize.i18n.deactivate()


def init_i18n(app):
    Babel(app, locale_selector=select_locale)

    @app.context_processor
    def i18n_context():
        lang = current_language()
        return {
            "current_lang": lang,
            "html_lang": lang.replace("_", "-"),
            "languages": LANGUAGES,
        }

    @app.template_global()
    def fmt_number(value, decimals=0):
        pattern = "#,##0" + ("." + "#" * decimals if decimals else "")
        return format_decimal(value, format=pattern, locale=current_language())

    @app.template_global()
    def fmt_percent(fraction, decimals=0):
        pattern = "#,##0" + ("." + "#" * decimals if decimals else "") + "%"
        return format_percent(fraction, format=pattern, locale=current_language())

    @app.template_filter("severity_label")
    def severity_label(value):
        return SEVERITY_LABELS.get(value, value or SEVERITY_LABELS["Unknown"])

    @app.template_filter("state_label")
    def state_label(value):
        return STATE_LABELS.get(value, value)
