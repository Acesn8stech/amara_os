"""Render one Slack app manifest per seat from slack-app-manifest.yaml -> build/manifests/<agent>.yaml.
Each seat needs its own app because Socket Mode load-balances events across an app's connections.
Only the cos manifest keeps slash commands + interactivity; workers are listeners/posters only."""
import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SEATS = {
    "mausbot-cos": ("MausBot", "Chief of Staff / Orchestrator"),
    "docket-bot": ("DocketBot", "Commission & Docket Machine"),
    "courthouse-bot": ("CourthouseBot", "Digital Clerk & Expiration Pre-Screen"),
    "abstractor-bot": ("AbstractorBot", "Mineral Title Abstracting & Run Sheets"),
    "trace-bot": ("TraceBot", "Owner Skip-Trace, Probate & Cost Ledger"),
    "cfo-bot": ("CFOBot", "Comps, Underwriting & Escrow"),
}

if __name__ == "__main__":
    base = yaml.safe_load((ROOT / "slack-app-manifest.yaml").read_text())
    out = ROOT / "build" / "manifests"
    out.mkdir(parents=True, exist_ok=True)
    for agent, (name, desc) in SEATS.items():
        m = copy.deepcopy(base)
        m["display_information"]["name"] = name
        m["display_information"]["description"] = desc
        m["features"]["bot_user"]["display_name"] = name
        if agent != "mausbot-cos":
            m["features"].pop("slash_commands", None)
            m["settings"]["interactivity"] = {"is_enabled": False}
        (out / f"{agent}.yaml").write_text(yaml.safe_dump(m, sort_keys=False))
        print("wrote", out / f"{agent}.yaml")
