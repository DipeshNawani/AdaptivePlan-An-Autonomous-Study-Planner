"""Application configuration and environment settings."""
import os
from pathlib import Path
from dotenv import load_dotenv

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"

# Ensure data directory exists
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Load environment variables
load_dotenv(BASE_DIR / ".env")

# LLM Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
_mock_env = os.getenv("MOCK_LLM", "").strip().lower()

# Mock mode is active if explicitly set to true OR if API key is missing
MOCK_LLM = (_mock_env in ("true", "1", "yes")) or (not GEMINI_API_KEY)

# Server Configuration
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

# Database path
DB_PATH = DATA_DIR / "adaptiveplan.db"
