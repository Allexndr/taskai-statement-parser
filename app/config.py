import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = os.getenv("DATABASE_URL", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    bot_token: str = os.getenv("BOT_TOKEN", "")
    webhook_url: str = os.getenv("WEBHOOK_URL", "")
    
    class Config:
        env_file = ".env"

settings = Settings()
