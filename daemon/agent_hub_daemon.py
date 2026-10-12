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


def _short(s, n=160):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:n]


def collect_board():
    """Kanban-Karten (todo/doing/done) aus Hintergrund-Jobs, personal_agent und Sitzungs-Todo-Listen."""
    cards, now = [], time.time()
    # 1) Claude-Hintergrund-Jobs
    for f in (Path.home() / ".claude/jobs").glob("*/state.json"):
        try:
            d = json.loads(f.read_text())
            st = d.get("state")
            col = {"working": "doing", "running": "doing", "busy": "doing", "blocked": "doing",
                   "queued": "todo", "pending": "todo", "todo": "todo"}.get(st, "done")
            cards.append({"id": "job-" + f.parent.name, "col": col, "src": "Claude-Job",
                          "title": _short(d.get("name") or d.get("intent") or f.parent.name, 90),
                          "detail": _short(d.get("detail")), "badge": "wartet auf dich" if st == "blocked" else st,
                          "needs": _short(d.get("needs"), 200) if st == "blocked" else "",
                          "ts": int(f.stat().st_mtime * 1000)})
        except Exception:
            continue
    # 2) personal_agent: laufende/wartende Jobs und letzte Scheduler-Läufe (7 Tage)
    pa = PROJECTS / "personal_agent" / "data"
    try:
        cj = json.loads((pa / "current_jobs.json").read_text())
        for k, col in (("running", "doing"), ("queued", "todo")):
            for j in cj.get(k, []):
                cards.append({"id": f"pa-{k}-{j.get('id') or j.get('title')}", "col": col, "src": "personal_agent",
                              "title": _short(j.get("title") or j.get("task_id") or "Job", 90),
                              "detail": _short(j.get("agent") or ""), "badge": k, "ts": int(now * 1000)})
    except Exception:
        pass
    try:
        for tid, v in json.loads((pa / "scheduler_state.json").read_text()).items():
            lr = v.get("last_run")
            if not lr:
                continue
            ts = time.mktime(time.strptime(lr[:19], "%Y-%m-%dT%H:%M:%S"))
            if now - ts < 7 * 86400:
                cards.append({"id": "sched-" + tid, "col": "done", "src": "Scheduler", "title": tid,
                              "detail": f"{v.get('last_status')} · {v.get('last_duration_seconds')} s",
                              "badge": v.get("last_status"), "ts": int(ts * 1000)})
    except Exception:
        pass
    # 3) Todo-Listen aus Sitzungs-Transkripten (nur Sitzungen der letzten 24 h)
    try:
        for f in (Path.home() / ".claude/projects").glob("*/*.jsonl"):
            if now - f.stat().st_mtime > 86400:
                continue
            with open(f, "rb") as fh:
                fh.seek(max(0, f.stat().st_size - 3_000_000))
                tail = fh.read().decode("utf-8", "ignore")
            idx = tail.rfind('"name":"TodoWrite"')
            if idx < 0:
                continue
            m = re.search(r'"todos":(\[.*?\])\s*[,}]', tail[idx:idx + 20000], re.S)
            if not m:
                continue
            proj = f.parent.name.split("-projects-")[-1]
            for i, td in enumerate(json.loads(m.group(1))):
                col = {"completed": "done", "in_progress": "doing"}.get(td.get("status"), "todo")
                cards.append({"id": f"todo-{f.stem[:8]}-{i}", "col": col, "src": "Todo · " + proj,
                              "title": _short(td.get("content") or td.get("activeForm"), 120), "detail": "",
                              "badge": "", "ts": int(f.stat().st_mtime * 1000)})
    except Exception:
        pass
    # 4) Sitzungen, die gerade arbeiten
    for s in manual_sessions():
        if s.get("status") == "busy":
            cards.append({"id": f"sess-{s['pid']}", "col": "doing", "src": "Sitzung", "title": s["project"] or "?",
                          "detail": "arbeitet gerade", "badge": "busy", "ts": int(now * 1000)})
    order = {"doing": 0, "todo": 1, "done": 2}
    cards.sort(key=lambda c: (order[c["col"]], -c["ts"]))
    out = {"todo": [], "doing": [], "done": []}
    for c in cards:
        if len(out[c["col"]]) < 40:
            out[c["col"]].append(c)
    return out


def collect_crons():
    jobs = []
    try:
        for line in sh("crontab", "-l").stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("@"):
                sched, _, cmd = line.partition(" ")
            else:
                parts = line.split(None, 5)
                sched, cmd = " ".join(parts[:5]), parts[5] if len(parts) > 5 else ""
            scripts = re.findall(r"[\w./-]+\.(?:sh|py)\b", cmd)
            jobs.append({"kind": "cron", "name": _short(Path(scripts[0]).name if scripts else (Path(cmd.split()[0]).name if cmd else "?"), 60),
                         "when": sched, "detail": _short(cmd, 120), "enabled": True})
    except Exception:
        pass
    try:
        out = sh("systemctl", "--user", "list-timers", "--all", "--output=json").stdout
        for t in json.loads(out):
            nxt = t.get("next") or 0
            jobs.append({"kind": "timer", "name": t.get("unit", "?"), "when": "",
                         "next": int(nxt / 1e6) if nxt else None,
                         "last": int((t.get("last") or 0) / 1e6) or None,
                         "detail": _short(t.get("activates"), 80), "enabled": True})
    except Exception:
        pass
    try:
        cfg = json.loads((PROJECTS / "personal_agent/config/agents.json").read_text())
        for aid, a in cfg.get("agents", {}).items():
            for tk in a.get("scheduled_tasks", []):
                jobs.append({"kind": "scheduler", "name": f"{aid}/{tk.get('id')}", "when": tk.get("cron", ""),
                             "detail": "personal_agent", "enabled": bool(tk.get("enabled"))})
    except Exception:
        pass
    return jobs[:150]


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
            sig = json.dumps([agents, [(s['pid'], s['status']) for s in manual_sessions()], collect_board(), collect_crons()], sort_keys=True, default=str)
            sig = re.sub(r'"uptime_s": \d+', '', sig)
            now = time.time()
            if (sig != last_sig or dirty or now - last_write > HEARTBEAT) and now - last_write >= MIN_WRITE_GAP:
                for k in sorted(results)[:-50]:
                    results.pop(k)
                gh.put("status.json", {"host": host, "updated": int(now * 1000), "agents": agents,
                                       "startable": startable(), "sessions": manual_sessions(), "board": collect_board(), "crons": collect_crons(), "results": results},
                       "status")
                last_sig, last_write = sig, now
        except Exception as e:
            log("Fehler:", repr(e)[:200])
            time.sleep(30)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
