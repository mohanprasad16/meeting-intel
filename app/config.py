from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str = ""
    answer_model: str = "gemini-3.5-flash"
    fast_model: str = "gemini-3.5-flash-lite"  # routing + format stencil
    fallback_model: str = "gemini-3.5-flash-lite"  # when the main model is busy (503)
    embedding_model: str = "gemini-embedding-001"
    embedding_dims: int = 768

    db_path: str = "data/meetings.db"

    chunk_max_turns: int = 5
    chunk_max_words: int = 300
    search_candidates: int = 20  # per search method, before fusion
    top_k: int = 8  # chunks sent to the answer model


settings = Settings()
