# FOR DATABASE CONNECTIONS

import os
import psycopg2
import psycopg2.extras
from app.utils.parameters import get_param

def get_db_connection():
    try:
        host = get_param("/squaremethods/DB_HOST")
    except Exception:
        host = os.getenv("DB_HOST")

    try:
        port = get_param("/squaremethods/DB_PORT")
    except Exception:
        port = os.getenv("DB_PORT", "5432")

    try:
        name = get_param("/squaremethods/DB_NAME")
    except Exception:
        name = os.getenv("DB_NAME")

    try:
        user = get_param("/squaremethods/DB_USER")
    except Exception:
        user = os.getenv("DB_USER")

    try:
        password = get_param("/squaremethods/DB_PASSWORD")
    except Exception:
        password = os.getenv("DB_PASSWORD")

    conn = psycopg2.connect(
        host=host,
        port=int(port),
        dbname=name,
        user=user,
        password=password,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=5
    )
    return conn