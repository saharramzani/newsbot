"""Manage the bots on the server (run as root):

  python3 deploy/bots.py main avayenaft          the existing bot (service "newsbot") becomes the Avaye Naft bot
  python3 deploy/bots.py add aghaye aghaye_eghtesad   a new bot for one outlet (asks for its token)
  python3 deploy/bots.py add newsroom hub        the mother bot (asks for its token, finds the outlet bots itself)
  python3 deploy/bots.py hub                     refresh the mother bot's menu after adding an outlet bot
  python3 deploy/bots.py status                  list the bots

Tokens are typed hidden and stored only in /opt/newsbot/bots/<name>.env (readable by the bot user only).
"""
import getpass
import json
import os
import pwd
import subprocess
import sys
import urllib.request
from pathlib import Path

APP = Path("/opt/newsbot")
BOTS = APP / "bots"
MAIN_ENV = APP / ".env"


def read_env(p: Path) -> dict:
    out = {}
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def set_env(p: Path, values: dict):
    """Update or append KEY=value lines, keeping everything else in the file."""
    lines = p.read_text().splitlines() if p.exists() else []
    done = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if k in values and not line.lstrip().startswith("#"):
            lines[i] = f"{k}={values[k]}"
            done.add(k)
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    p.write_text("\n".join(lines) + "\n")
    u = pwd.getpwnam("newsbot")
    os.chown(p, u.pw_uid, u.pw_gid)
    os.chmod(p, 0o600)


def username(token: str) -> str:
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=20) as r:
            return json.load(r)["result"]["username"]
    except Exception as e:
        sys.exit(f"Telegram did not accept this token ({e}). Copy it again from BotFather.")


def brands() -> list[str]:
    return sorted(p.parent.name for p in (APP / "brands").glob("*/brand.json"))


def outlet_bots() -> dict:
    """outlet key -> token, for every single-outlet bot (the main one and bots/*.env)."""
    found = {}
    main = read_env(MAIN_ENV)
    if main.get("BOT_MODE") == "brand" and main.get("BOT_BRAND"):
        found[main["BOT_BRAND"]] = main["TELEGRAM_BOT_TOKEN"]
    for f in sorted(BOTS.glob("*.env")):
        e = read_env(f)
        if e.get("BOT_MODE") == "brand" and e.get("BOT_BRAND"):
            found[e["BOT_BRAND"]] = e["TELEGRAM_BOT_TOKEN"]
    return found


def refresh_hub():
    hubs = [f for f in BOTS.glob("*.env") if read_env(f).get("BOT_MODE") == "hub"]
    links = {f"BOT_USERNAME_{k.upper()}": username(t) for k, t in outlet_bots().items()}
    for f in hubs:
        set_env(f, links)
        subprocess.run(["systemctl", "restart", f"newsbot@{f.stem}"])
    for k, v in links.items():
        print(f"  {k} = @{v}")
    return hubs


def systemctl(*a):
    subprocess.run(["systemctl", *a], check=True)


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return
    if os.geteuid() != 0:
        sys.exit("Run as root.")
    BOTS.mkdir(exist_ok=True)
    if a[0] == "main" and len(a) == 2:
        if a[1] not in brands():
            sys.exit(f"Unknown outlet {a[1]!r}. Outlets: {', '.join(brands())}")
        set_env(MAIN_ENV, {"BOT_MODE": "brand", "BOT_BRAND": a[1]})
        systemctl("restart", "newsbot")
        print(f"The main bot (@{username(read_env(MAIN_ENV)['TELEGRAM_BOT_TOKEN'])}) now works only for {a[1]}.")
        refresh_hub()
    elif a[0] == "add" and len(a) == 3:
        name, what = a[1], a[2]
        if what != "hub" and what not in brands():
            sys.exit(f"Unknown outlet {what!r}. Outlets: {', '.join(brands())} (or 'hub')")
        token = getpass.getpass(f"Paste the token of the new bot '{name}' (hidden) and press Enter: ").strip()
        user = username(token)
        values = {"TELEGRAM_BOT_TOKEN": token, "WORK_DIR": f"work/{name}",
                  "BOT_MODE": "hub" if what == "hub" else "brand"}
        if what != "hub":
            values["BOT_BRAND"] = what
        set_env(BOTS / f"{name}.env", values)
        systemctl("daemon-reload")
        systemctl("enable", "--now", f"newsbot@{name}")
        print(f"Bot @{user} is running as service newsbot@{name}.")
        print("Hub menu:")
        refresh_hub()
    elif a[0] == "hub":
        if not refresh_hub():
            print("No mother bot yet: python3 deploy/bots.py add newsroom hub")
    elif a[0] == "status":
        main_env = read_env(MAIN_ENV)
        print(f"newsbot (main): mode={main_env.get('BOT_MODE', 'multi')} outlet={main_env.get('BOT_BRAND', '-')}")
        for f in sorted(BOTS.glob("*.env")):
            e = read_env(f)
            state = subprocess.run(["systemctl", "is-active", f"newsbot@{f.stem}"], capture_output=True, text=True)
            print(f"newsbot@{f.stem}: mode={e.get('BOT_MODE')} outlet={e.get('BOT_BRAND', '-')} [{state.stdout.strip()}]")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
