from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    gemini_api_key: str
    database_url: str = 'postgresql+psycopg://demo:demo@localhost:5434/multimodal_memory'
    app_host: str = '0.0.0.0'
    app_port: int = 8000
    debug: bool = True
    timezone: str = 'Asia/Bangkok'
    upload_dir: str = './data/uploads'

    generation_model: str = 'gemini-3.1-flash-lite-preview'
    image_generation_model: str = 'gemini-3.1-flash-image-preview'
    embedding_model: str = 'gemini-embedding-2-preview'
    embedding_dim: int = 768
    max_recent_turns: int = 10
    max_retrieved_items: int = 8
    max_ocr_chars: int = 1200
    max_generated_images_per_turn: int = 1
    max_reference_images_per_generation: int = 6


@lru_cache
def get_settings() -> Settings:
    return Settings()
