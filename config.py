import os
import re


def _env_list(name):
    """Lista separada por vírgula vinda do ambiente (vazia se não definida)."""
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _env_labels(name, default):
    """Label names from a comma-separated variable, skipping names Prometheus would reject."""
    names = [item for item in _env_list(name) if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,127}", item)]
    return names or list(default)


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # Modo local: sem login do Google, usuário fixo "local@example.com" (admin)
    DISABLE_GOOGLE_LOGIN = _env_bool("DISABLE_GOOGLE_LOGIN")

    # Obrigatória fora do modo local (assina o cookie de sessão).
    # Gere com: python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY = os.environ.get("SECRET_KEY") or (
        "dev-insecure-secret-key" if DISABLE_GOOGLE_LOGIN else None
    )
    SESSION_COOKIE_SAMESITE = "Lax"
    # Trata toda requisição como HTTPS, mesmo que chegue por HTTP de um proxy
    # reverso que termina o TLS. Assim o retorno do login do Google e os links
    # gerados pelo app usam https://.
    FORCE_HTTPS = _env_bool("FORCE_HTTPS")
    PREFERRED_URL_SCHEME = "https" if FORCE_HTTPS else "http"
    # Ative quando o app for servido só por HTTPS (ligado por padrão com FORCE_HTTPS)
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", FORCE_HTTPS)

    MONGO_URL = os.environ.get("MONGO_URL", "127.0.0.1")
    MONGO_PORT = int(os.environ.get("MONGO_PORT", "27017"))

    # OAuth do Google
    CLIENT_ID = os.environ.get("CLIENT_ID")
    CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

    TIMEZONE = os.environ.get("TIMEZONE", "UTC")

    # Catálogo: labels que nomeiam o serviço técnico, em ordem de prioridade
    # (CATALOG_SERVICE_LABELS=service,resource). Vale a primeira presente no alerta.
    CATALOG_SERVICE_LABELS = _env_labels("CATALOG_SERVICE_LABELS", ["service"])
    CATALOG_AUTO_CREATE = _env_bool("CATALOG_AUTO_CREATE", True)
    # Dias sem alertas para marcar um serviço como "sem atividade"
    CATALOG_STALE_DAYS = int(os.environ.get("CATALOG_STALE_DAYS", "30"))

    # Janelas de manutenção (só admins criam): duração máxima em horas
    MAINTENANCE_MAX_HOURS = int(os.environ.get("MAINTENANCE_MAX_HOURS", "24"))
    ALLOWED_DAYS = [1, 7, 15, 30, 60]

    # Quem pode entrar: e-mails de um domínio (ALLOWED_EMAIL_DOMAINS=example.com)
    # e/ou e-mails específicos (ALLOWED_EMAILS). Admins sempre podem entrar.
    # Sem nenhum dos três definido, ninguém entra.
    ALLOWED_EMAIL_DOMAINS = [d.lower().lstrip("@") for d in _env_list("ALLOWED_EMAIL_DOMAINS")]
    ALLOWED_EMAILS = [e.lower() for e in _env_list("ALLOWED_EMAILS")]

    # Admins podem fechar alertas e remover o ACK de outras pessoas
    ADMIN_USERS = (
        ["local@example.com"]
        if DISABLE_GOOGLE_LOGIN
        else [e.lower() for e in _env_list("ADMIN_EMAILS")]
    )

    # GCP Monitoring (opcional): basic auth do canal de webhook
    # (tipo "Webhook with basic auth" no Cloud Monitoring)
    GCP_WEBHOOK_USER = os.environ.get("GCP_WEBHOOK_USER")
    GCP_WEBHOOK_PASSWORD = os.environ.get("GCP_WEBHOOK_PASSWORD")

    # Alertmanager webhook (opcional): se definido, exige "Authorization: Bearer <token>"
    ALERTMANAGER_TOKEN = os.environ.get("ALERTMANAGER_TOKEN")
    ALERTMANAGER_DEBUG = _env_bool("ALERTMANAGER_DEBUG")

    # AWS CloudWatch via SNS (opcional): basic auth que vai na URL da assinatura
    # (https://user:senha@host/aws) e lista de tópicos aceitos, separados por vírgula
    AWS_SNS_USER = os.environ.get("AWS_SNS_USER")
    AWS_SNS_PASSWORD = os.environ.get("AWS_SNS_PASSWORD")
    AWS_SNS_TOPIC_ARNS = _env_list("AWS_SNS_TOPIC_ARNS")
    AWS_SNS_VERIFY_SIGNATURE = _env_bool("AWS_SNS_VERIFY_SIGNATURE", True)

    EVENTS_TOKEN = os.environ.get("EVENTS_TOKEN")

    # A webhook without credentials refuses deliveries unless this is on.
    # Local development (no Google login) allows them by default.
    ALLOW_UNAUTHENTICATED_WEBHOOKS = _env_bool("ALLOW_UNAUTHENTICATED_WEBHOOKS", DISABLE_GOOGLE_LOGIN)
