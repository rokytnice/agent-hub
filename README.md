# Agent Hub
Webapp (GitHub Pages) zeigt die lokalen Claude-Agenten (`claude-agent@*`) und startet/stoppt sie.
- `web/index.html` → Pages (öffentlich, enthält keine Daten)
- `daemon/agent_hub_daemon.py` → läuft lokal als systemd-User-Service, spricht nur ausgehend mit GitHub
- Daten: privates Repo `agent-hub-data` (`status.json` vom Daemon, `commands.json` von der Webapp)
Setup: `docs/SETUP.md`
