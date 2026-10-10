# Setup
1. Daemon-Token: `pass insert personal/AGENT_HUB_TOKEN` (fine-grained PAT, Repo `agent-hub-data`, Contents R/W). Fallback: `gh auth token`.
2. `~/.config/agent-hub/config.json`: `{"data_repo":"rokytnice/agent-hub-data","extra_units":[]}`
3. `systemctl --user enable --now agent-hub-daemon`
4. Webapp: https://rokytnice.github.io/agent-hub/ → eigenes fine-grained PAT (nur `agent-hub-data`, Contents R/W) eintragen.
Sicherheit: erlaubt sind nur start/stop/restart für `claude-agent@<Projektordner>.service`; Kommandos max. 5 min alt, jede ID einmal.
