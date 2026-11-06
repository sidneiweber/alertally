import logging

from flask import session
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from config import Config

client = MongoClient(
    Config.MONGO_URL,
    Config.MONGO_PORT,
    maxPoolSize=50,  # Pool de conexões
    minPoolSize=10,
    maxIdleTimeMS=30000,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=10000,
    socketTimeoutMS=20000,
)
db = client.flask_db
alerts = db.alerts


def create_indexes():
    try:
        # Índice composto para queries mais comuns
        alerts.create_index([("state", 1), ("severity_number", -1), ("update_at", -1)])

        # Índice para incident_id (usado em updates e deletes)
        alerts.create_index("incident_id", unique=True)

        # Índice para queries de relatório
        alerts.create_index([("update_at", -1), ("name", 1), ("summary", 1)])

        logging.info("MongoDB indexes created successfully")
    except PyMongoError as e:
        logging.error(f"Error creating indexes: {e}")


create_indexes()

alerts_dict = {
    "Critical": 5,
    "Error": 4,
    "Warning": 3,
    "Info": 2,
    "Debug": 1,
    "Unknown": 0,
    "No severity": 0,
}


def get_session_user():
    if Config.DISABLE_GOOGLE_LOGIN:
        return {
            "name": "Local user",
            "email": "local@example.com",
            "picture": "/static/images/user.png",
        }
    return session.get("user", {})
