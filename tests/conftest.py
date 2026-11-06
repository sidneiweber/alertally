import os
import sys

import mongomock
import pymongo
import pytest

# Configuração de teste: definida antes de importar o app, que lê o ambiente no import
os.environ.update({
    "SECRET_KEY": "test-secret",
    "DISABLE_GOOGLE_LOGIN": "false",
    "CLIENT_ID": "test-client-id",
    "CLIENT_SECRET": "test-client-secret",
    "ALLOWED_EMAIL_DOMAINS": "example.com",
    "ALLOWED_EMAILS": "guest@example.net",
    "ADMIN_EMAILS": "admin@example.org",
    "TIMEZONE": "UTC",
})
for var in ("GCP_WEBHOOK_USER", "GCP_WEBHOOK_PASSWORD", "ALERTMANAGER_TOKEN",
            "AWS_SNS_USER", "AWS_SNS_PASSWORD", "AWS_SNS_TOPIC_ARNS"):
    os.environ.pop(var, None)

# Mongo em memória no lugar do servidor real
pymongo.MongoClient = mongomock.MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app  # noqa: E402
from utils.helpers import alerts as alerts_collection  # noqa: E402

USER = {"name": "Ana", "email": "ana@example.com", "picture": None}
OTHER_USER = {"name": "Bruno", "email": "bruno@example.com", "picture": None}
ADMIN = {"name": "Admin", "email": "admin@example.org", "picture": None}


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield flask_app
    flask_app.config.update(WTF_CSRF_ENABLED=True)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def alerts():
    alerts_collection.delete_many({})
    yield alerts_collection
    alerts_collection.delete_many({})


def login(client, user=USER):
    with client.session_transaction() as sess:
        sess["user"] = dict(user)
