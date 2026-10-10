#!/usr/bin/env python3
"""agent-hub-daemon — meldet lokale Agenten (systemd-User-Units) an ein privates GitHub-Repo
und führt Start/Stop/Restart-Kommandos aus der Webapp aus.

Nur ausgehende HTTPS-Verbindungen zu api.github.com — kein offener Port.
Ausgeführt werden ausschließlich Aktionen aus ALLOWED_ACTIONS für Units, die ALLOWED_UNIT
matchen und deren Projektordner unter ~/projects existiert. Kommandos sind max. 5 Min. gültig
und jede ID wird nur einmal ausgeführt.
"""
import base64, json, os, re, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

CONFIG = Path(os.environ.get("AGENT_HUB_CONFIG", Path.home() / ".config/agent-hub/config.json"))
STATE = Path.home() / ".local/state/agent-hub/processed.json"
PROJECTS = Path.home() / "projects"
ALLOWED_ACTIONS = {"start", "stop", "restart"}
ALLOWED_UNIT = re.compile(r"^claude-agent@([A-Za-z0-9._-]+)\.service$")
MAX_CMD_AGE = 300
POLL = 10
MIN_WRITE_GAP = 20
HEARTBEAT = 300


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def token():
    t = os.environ.get("AGENT_HUB_TOKEN")
    if t:
        return t.strip()
    for cmd in (["pass", "show", "personal/AGENT_HUB_TOKEN"], ["gh", "auth", "token"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
            if out:
                return out.splitlines()[0]
        except Exception:
            pass
    sys.exit("kein Token: AGENT_HUB_TOKEN setzen oder 'pass insert personal/AGENT_HUB_TOKEN'")


class Gh:
    def __init__(self, repo, tok):
        self.repo, self.tok, self.etags = repo, tok, {}

    def _req(self, method, path, body=None, etag=None):
        req = urllib.request.Request(f"https://api.github.com/repos/{self.repo}/contents/{path}",
                                     method=method, data=json.dumps(body).encode() if body else None)
        req.add_header("Authorization", f"Bearer {self.tok}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "agent-hub-daemon")
        if etag:
            req.add_header("If-None-Match", etag)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.load(r), r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, None, None
            if e.code == 404:
                return 404, None, None
            raise

    def get(self, path):
        st, data, et = self._req("GET", path, etag=self.etags.get(path))
        if st == 304:
            return "unchanged", None
        if st == 404:
            return None, None
        self.etags[path] = et
        return json.loads(base64.b64decode(data["content"])), data["sha"]

    def put(self, path, obj, msg):
        _, cur, _ = self._req("GET", path)
        body = {"message": msg, "content": base64.b64encode(json.dumps(obj, indent=1, ensure_ascii=False).encode()).decode()}
        if cur:
            body["sha"] = cur["sha"]
        self._req("PUT", path, body)
        self.etags.pop(path, None)


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def collect(extra):
    units = {}
    out = sh("systemctl", "--user", "list-units", "claude-agent@*", "--all", "--no-legend", "--plain").stdout
    for line in out.splitlines():
        p = line.split(None, 4)
        if len(p) >= 4:
            units[p[0]] = {"active": p[2], "sub": p[3]}
    for u in extra:
        s = sh("systemctl", "--user", "show", u, "-p", "ActiveState,SubState").stdout
        d = dict(l.split("=", 1) for l in s.splitlines() if "=" in l)
        units[u] = {"active": d.get("ActiveState", "?"), "sub": d.get("SubState", "?")}
    res = []
    for name, v in sorted(units.items()):
        s = sh("systemctl", "--user", "show", name, "-p", "ActiveEnterTimestampMonotonic,MemoryCurrent,NRestarts,MainPID").stdout
        d = dict(l.split("=", 1) for l in s.splitlines() if "=" in l)
        up = None
        try:
            mono = int(d.get("ActiveEnterTimestampMonotonic", "0"))
            if v["active"] == "active" and mono:
                up = int(time.clock_gettime(time.CLOCK_MONOTONIC) - mono / 1e6)
        except Exception:
            pass
        mem = d.get("MemoryCurrent", "")
        res.append({"unit": name, "active": v["active"], "sub": v["sub"], "uptime_s": up,
                    "mem_mb": round(int(mem) / 1048576) if mem.isdigit() else None,
                    "restarts": int(d["NRestarts"]) if d.get("NRestarts", "").isdigit() else None})
    return res


def manual_sessions():
    """Laufende Claude-Sitzungen, die NICHT von einer claude-agent@-Unit gehalten werden (nur Anzeige)."""
    out = []
    for f in (Path.home() / ".claude/sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            pid = int(d.get("pid") or f.stem)
            if not Path(f"/proc/{pid}").exists() or "claude-agent@" in Path(f"/proc/{pid}/cgroup").read_text():
                continue
            out.append({"pid": pid, "project": Path(d.get("cwd", "")).name, "name": d.get("name") or "",
                        "remote": bool(d.get("bridgeSessionId")), "status": d.get("status"),
                        "updated": d.get("updatedAt")})
        except Exception:
            continue
    return sorted(out, key=lambda s: (s["project"], s["pid"]))


def startable():
    """Projektordner, für die eine claude-agent@-Instanz gestartet werden darf."""
    return sorted(p.name for p in PROJECTS.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def run_cmd(c):
    unit, action = str(c.get("unit", "")), str(c.get("action", ""))
    m = ALLOWED_UNIT.match(unit)
    if action not in ALLOWED_ACTIONS or not m:
        return False, "nicht erlaubt"
    if not (PROJECTS / m.group(1)).is_dir():
        return False, "Projektordner fehlt"
    r = sh("systemctl", "--user", action, unit)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[:200] or "ok"


def main():
    cfg = json.loads(CONFIG.read_text())
    gh = Gh(cfg["data_repo"], token())
    extra = cfg.get("extra_units", [])
    STATE.parent.mkdir(parents=True, exist_ok=True)
    done = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    results, last_sig, last_write = {}, None, 0
    host = os.uname().nodename
    log("läuft für", cfg["data_repo"], "auf", host)
    while True:
        try:
            cmds, _ = gh.get("commands.json")
            dirty = False
            if isinstance(cmds, dict):
                for c in cmds.get("commands", []):
                    cid = c.get("id")
                    if not cid or cid in done:
                        continue
                    done.add(cid)
                    age = time.time() - float(c.get("ts", 0)) / 1000
                    ok, msg = (False, "abgelaufen") if age > MAX_CMD_AGE or age < -60 else run_cmd(c)
                    log("cmd", c.get("action"), c.get("unit"), "->", ok, msg)
                    results[cid] = {"ok": ok, "msg": msg, "ts": int(time.time() * 1000)}
                    dirty = True
                STATE.write_text(json.dumps(sorted(done)[-500:]))
            agents = collect(extra)
            sig = json.dumps([agents, [(s['pid'], s['status']) for s in manual_sessions()]], sort_keys=True, default=str)
            sig = re.sub(r'"uptime_s": \d+', '', sig)
            now = time.time()
            if (sig != last_sig or dirty or now - last_write > HEARTBEAT) and now - last_write >= MIN_WRITE_GAP:
                for k in sorted(results)[:-50]:
                    results.pop(k)
                gh.put("status.json", {"host": host, "updated": int(now * 1000), "agents": agents,
                                       "startable": startable(), "sessions": manual_sessions(), "results": results},
                       "status")
                last_sig, last_write = sig, now
        except Exception as e:
            log("Fehler:", repr(e)[:200])
            time.sleep(30)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
