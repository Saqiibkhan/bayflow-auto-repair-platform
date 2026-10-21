import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bayflow.db")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
TOKEN_HOURS = int(os.getenv("TOKEN_HOURS", "12"))
AUTO_CREATE_TABLES = os.getenv("AUTO_CREATE_TABLES", "1") == "1"
