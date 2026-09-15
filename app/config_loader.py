from pathlib import Path
import os, yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

def load_all():
    load_dotenv(ROOT / ".env")
    with open(ROOT/"config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    with open(ROOT/"answer_bank.yaml", encoding="utf-8") as f:
        ans = yaml.safe_load(f)
    return cfg, ans

def env_bool(name, default=True):
    v = os.getenv(name)
    return default if v is None else v.lower() in ("1","true","yes","on")

def cloud_order():
    return [x.strip() for x in os.getenv("CLOUD_FALLBACK_ORDER","anthropic,openai,gemini").split(",") if x.strip()]
