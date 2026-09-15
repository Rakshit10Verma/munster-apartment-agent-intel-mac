import os, requests, shutil, platform
from rich.console import Console
from rich.table import Table
from .config_loader import load_all

def doctor():
    load_all()
    c=Console(); t=Table(title="Apartment Agent Doctor")
    t.add_column("Check"); t.add_column("Status"); t.add_column("Details")
    t.add_row("CPU", "INFO", platform.processor() or platform.machine())
    t.add_row("llama command", "OK" if shutil.which("llama") else "MISSING", shutil.which("llama") or "not found")
    try:
        base=os.getenv("LLAMA_CPP_BASE_URL","http://127.0.0.1:8080/v1").rsplit("/v1",1)[0]
        r=requests.get(base+"/health",timeout=3)
        t.add_row("local AI server","OK" if r.ok else "ERROR",str(r.status_code))
    except Exception:
        t.add_row("local AI server","MISSING","run ./start_local_ai.sh in another Terminal")
    for label,key in [("Claude","ANTHROPIC_API_KEY"),("OpenAI","OPENAI_API_KEY"),("Gemini","GEMINI_API_KEY")]:
        t.add_row(label+" key","OK" if os.getenv(key) else "OPTIONAL","configured" if os.getenv(key) else "blank")
    c.print(t)
