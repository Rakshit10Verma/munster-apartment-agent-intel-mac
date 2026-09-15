import argparse, sys
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from .analyzer import analyze
from .doctor import doctor

c=Console()

def main():
    p=argparse.ArgumentParser()
    s=p.add_subparsers(dest="cmd",required=True)
    s.add_parser("doctor")
    a=s.add_parser("analyze"); a.add_argument("--file"); a.add_argument("--text")
    args=p.parse_args()

    if args.cmd=="doctor":
        doctor(); return

    if args.file: text=Path(args.file).read_text(encoding="utf-8")
    elif args.text: text=args.text
    else:
        c.print("[bold]Paste full listing, then press Ctrl-D:[/bold]")
        text=sys.stdin.read()

    r,notes=analyze(text)
    c.print(Panel(f"Decision: {r.rule_decision.decision}\n"
                  f"Extraction AI: {r.provider_for_extraction}\n"
                  f"Message AI: {r.provider_for_message or '-'}\n"
                  f"Hard skips: {r.rule_decision.hard_skip_reasons or 'none'}\n"
                  f"Warnings: {r.rule_decision.warnings or 'none'}\n"
                  f"Auto-send safe: {r.auto_send_allowed}"))
    if r.extraction.hidden_questions:
        c.print("[bold]Hidden questions:[/bold]")
        for q in r.extraction.hidden_questions: c.print(f"- {q.question}")
    if r.message:
        c.print(Panel(f"{r.message.subject}\n\n{r.message.body}",title="Application"))
    if r.validation_errors:
        c.print("[bold red]Validation blocks:[/bold red]")
        for e in r.validation_errors: c.print(f"- {e}")
    if notes:
        c.print("[dim]Router notes:[/dim]")
        for n in notes: c.print(f"[dim]- {n}[/dim]")

if __name__=="__main__": main()
