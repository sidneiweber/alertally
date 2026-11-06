import logging
import sys
from datetime import datetime
from dotenv import load_dotenv

import humanize
from flask import Flask

# Carrega variáveis do arquivo .env
load_dotenv()
from flask_dance.contrib.google import make_google_blueprint
from flask_wtf.csrf import CSRFProtect
from pytz import UTC, timezone

from config import Config
from routes.alerts import alerts_bp
from routes.auth import auth_bp
from routes.main import main_bp
from utils.i18n import activate_humanize, init_i18n

for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger('alertally.alertmanager').setLevel(
    logging.DEBUG if Config.ALERTMANAGER_DEBUG else logging.INFO
)


def check_config():
    """Falha cedo quando falta configuração obrigatória."""
    if not Config.SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY is not set. Generate one with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if Config.DISABLE_GOOGLE_LOGIN:
        logging.warning("DISABLE_GOOGLE_LOGIN is on: anyone can access Alertally. Use it only locally.")
        return
    if not (Config.CLIENT_ID and Config.CLIENT_SECRET):
        raise RuntimeError("CLIENT_ID and CLIENT_SECRET (Google OAuth) are required.")
    if not (Config.ALLOWED_EMAIL_DOMAINS or Config.ALLOWED_EMAILS or Config.ADMIN_USERS):
        logging.warning(
            "No ALLOWED_EMAIL_DOMAINS, ALLOWED_EMAILS or ADMIN_EMAILS set: nobody will be able to log in."
        )


check_config()

app = Flask(__name__)
app.config.from_object(Config())
app.logger.handlers = []
app.logger.propagate = True

# Protege os formulários e POSTs da interface. Os webhooks são isentos:
# quem chama é a fonte de alertas, que se autentica de outro jeito.
csrf = CSRFProtect(app)

google_bp = make_google_blueprint(
    client_id=Config.CLIENT_ID,
    client_secret=Config.CLIENT_SECRET,
    scope=[
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid",
    ],
    # Mostra o seletor de contas: permite trocar de conta depois de um acesso negado
    reprompt_select_account=True,
    redirect_to="auth.google_authorized",
)
app.register_blueprint(google_bp, url_prefix="/login")
app.register_blueprint(main_bp)
app.register_blueprint(alerts_bp)
app.register_blueprint(auth_bp)

for webhook in ("parse_request", "parse_alertmanager", "parse_aws_sns"):
    csrf.exempt(f"routes.alerts.{webhook}")

init_i18n(app)


@app.template_filter("humanize_time")
def humanize_time(value):
    if value is None:
        return ""
    if value.tzinfo is None:
        value = UTC.localize(value)

    # Converte para o fuso configurado
    tz = timezone(Config.TIMEZONE)
    local_dt = value.astimezone(tz)

    now = datetime.now(tz)
    activate_humanize()
    return humanize.naturaltime(now - local_dt)


if __name__ == "__main__":
    app.run(debug=True)
