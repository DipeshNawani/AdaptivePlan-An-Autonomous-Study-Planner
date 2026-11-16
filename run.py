"""Entry point to launch the AdaptivePlan application on Windows and other operating systems."""
import sys
from pathlib import Path

# Ensure UTF-8 standard output on Windows
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.config import HOST, PORT, MOCK_LLM, GEMINI_MODEL

def main():
    import uvicorn
    mode_str = "MOCK MODE (Offline Fallback)" if MOCK_LLM else f"LIVE GEMINI ({GEMINI_MODEL})"
    print("=" * 60)
    print("  AdaptivePlan: An Autonomous Study Planner")
    print(f"  Mode:   {mode_str}")
    print(f"  URL:    http://{HOST}:{PORT}")
    print("=" * 60)

    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        reload=False
    )

if __name__ == "__main__":
    main()
