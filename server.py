#!/usr/bin/env python3
"""
XYNEZ — serwis backend.
Serwuje stronę statyczną + API:
  POST /api/reports                    — rejestruje zgłoszenie z formularza
  GET  /api/status?nr=&phone=          — publiczny status (wymaga nr + telefonu)
  POST /api/admin/login                — logowanie właściciela (PIN z config.json)
  GET  /api/admin/reports              — lista zgłoszeń (token)
  POST /api/admin/reports/<nr>/status  — zmiana statusu (token) + SMS przy "gotowe"
"""
import base64
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
REPORTS_FILE = DATA_DIR / "reports.json"
CONFIG_FILE = BASE / "config.json"
PORT = int(os.environ.get("PORT", 8901))
TOKEN_TTL = 12 * 3600  # 12 h

STATUSES = ["przyjęte", "w_trakcie", "gotowe", "odebrane"]
STATUS_LABELS = {
    "przyjęte": "Przyjęte do serwisu",
    "w_trakcie": "W trakcie naprawy",
    "gotowe": "Gotowe do odbioru",
    "odebrane": "Odebrane",
}

_lock = threading.Lock()
_tokens = {}  # token -> expiry timestamp


def load_config():
    """Config z config.json (lokalnie) + zmienne środowiskowe (produkcja).
    Env ma pierwszeństwo — na hostingu config.json nie istnieje."""
    cfg = {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

    if os.environ.get("ADMIN_PIN"):
        cfg["admin_pin"] = os.environ["ADMIN_PIN"]
    if os.environ.get("SMSAPI_TOKEN"):
        cfg["smsapi_token"] = os.environ["SMSAPI_TOKEN"]
    if os.environ.get("SMSAPI_FROM"):
        cfg["smsapi_from"] = os.environ["SMSAPI_FROM"]

    tw = dict(cfg.get("twilio") or {})
    if os.environ.get("TWILIO_SID"):
        tw["sid"] = os.environ["TWILIO_SID"]
    if os.environ.get("TWILIO_TOKEN"):
        tw["token"] = os.environ["TWILIO_TOKEN"]
    if os.environ.get("TWILIO_FROM"):
        tw["from"] = os.environ["TWILIO_FROM"]
    if tw:
        cfg["twilio"] = tw

    if os.environ.get("KVDB_BUCKET"):
        cfg["kvdb_bucket"] = os.environ["KVDB_BUCKET"]
    if os.environ.get("PANEL_KEY"):
        cfg["panel_key"] = os.environ["PANEL_KEY"]

    return cfg


def kvdb_bucket():
    """Zewnętrzna baza (kvdb.io) — przeżywa restarty hostingu.
    Bucket ID to sekret — NIE commitować."""
    return os.environ.get("KVDB_BUCKET") or load_config().get("kvdb_bucket") or ""


def panel_key():
    """Tajny klucz do panelu admina — tylko env/config, nie w repo."""
    return os.environ.get("PANEL_KEY") or load_config().get("panel_key") or ""


# ===== GitHub storage =====
GH_TOKEN = os.environ.get("GH_TOKEN") or load_config().get("gh_token") or ""
PUBLIC_REPO = os.environ.get("GH_PUBLIC_REPO", "xynezserwis/xynezserwis.github.io")
PRIVATE_REPO = os.environ.get("GH_PRIVATE_REPO", "xynezserwis/xynez-api")


def gh_headers():
    return {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "xynez-server",
    }


def gh_get_json(repo, path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/contents/{path}", headers=gh_headers()
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read())
            return j.get("sha", ""), json.loads(base64.b64decode(j["content"]))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "", {}
        raise


def gh_put_json(repo, path, obj, sha):
    body = {
        "message": "status update",
        "content": base64.b64encode(json.dumps(obj, ensure_ascii=False, indent=2).encode()).decode(),
    }
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        data=json.dumps(body).encode(),
        method="PUT",
        headers=gh_headers(),
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def load_reports():
    bucket = kvdb_bucket()
    if bucket:
        try:
            with urllib.request.urlopen(f"https://kvdb.io/{bucket}/reports", timeout=10) as r:
                body = r.read().decode().strip()
            return json.loads(body) if body else []
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            print(f"[KVDB] GET error {e.code} — fallback na plik")
        except Exception as e:
            print(f"[KVDB] GET error {e} — fallback na plik")
    try:
        return json.loads(REPORTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_reports(reports):
    payload = json.dumps(reports, ensure_ascii=False)
    bucket = kvdb_bucket()
    if bucket:
        try:
            req = urllib.request.Request(
                f"https://kvdb.io/{bucket}/reports",
                data=payload.encode("utf-8"),
                method="PUT",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10).read()
            return
        except Exception as e:
            print(f"[KVDB] PUT error {e} — zapisuję lokalnie")
    DATA_DIR.mkdir(exist_ok=True)
    tmp = REPORTS_FILE.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(REPORTS_FILE)


def digits(s):
    return re.sub(r"\D", "", s or "")


def norm_phone(p):
    d = digits(p)
    return d[-9:] if len(d) >= 9 else d


def find_report(reports, nr):
    nr = (nr or "").strip().upper()
    return next((r for r in reports if r.get("nr", "").upper() == nr), None)


def send_sms(to, message):
    """SMS przez SMSAPI.pl (token w config.json) albo Twilio. Bez kluczy — tylko log."""
    cfg = load_config()
    to = "+" + digits(to)

    if cfg.get("smsapi_token"):
        params = {
            "token": cfg["smsapi_token"],
            "from": cfg.get("smsapi_from", "Serwis Xynez"),
            "to": to,
            "message": message,
        }
        url = "https://api.smsapi.pl/sms.do?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                print(f"[SMSAPI] {r.read().decode()[:200]}")
        except Exception as e:
            print(f"[SMSAPI ERROR] {e}")
        return

    tw = cfg.get("twilio") or {}
    if tw.get("sid") and tw.get("token") and tw.get("from"):
        auth = base64.b64encode(f'{tw["sid"]}:{tw["token"]}'.encode()).decode()
        data = urllib.parse.urlencode({"To": to, "From": tw["from"], "Body": message}).encode()
        req = urllib.request.Request(
            f'https://api.twilio.com/2010-04-01/Accounts/{tw["sid"]}/Messages.json',
            data=data,
            headers={"Authorization": f"Basic {auth}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
            print(f"[TWILIO] SMS -> {to}")
        except Exception as e:
            print(f"[TWILIO ERROR] {e}")
        return

    print(f"[SMS DRY-RUN] do {to}: {message}")


PANEL_ROUTE = "/x7-panel"

PANEL_HTML = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex">
<title>Panel</title>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#16140f;color:#f4f1ea;font-family:"Archivo",sans-serif;padding:40px 20px}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:28px;font-weight:800;letter-spacing:-.02em;margin-bottom:6px}
.sub{font-family:"IBM Plex Mono",monospace;font-size:12px;color:#8a8478;letter-spacing:.1em;text-transform:uppercase;margin-bottom:32px}
.card{background:#221f18;border:1px solid #3a352c;padding:24px;margin-bottom:24px}
label{display:block;font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#8a8478;margin-bottom:8px}
input{width:100%;background:#16140f;border:1px solid #3a352c;color:#f4f1ea;font-family:"IBM Plex Mono",monospace;font-size:15px;padding:12px 14px}
input:focus{outline:none;border-color:#d63c14}
button{background:#d63c14;color:#f4f1ea;border:0;font-family:"Archivo",sans-serif;font-weight:700;font-size:14px;padding:12px 22px;cursor:pointer;margin-top:14px}
button:hover{background:#b02f0e}
button.ghost{background:transparent;border:1px solid #3a352c;color:#b8b2a4}
table{width:100%;border-collapse:collapse;font-size:14px}
th{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#8a8478;text-align:left;padding:10px 8px;border-bottom:1px solid #3a352c}
td{padding:12px 8px;border-bottom:1px solid #2a2620;vertical-align:top}
.nr{font-family:"IBM Plex Mono",monospace;font-weight:600;color:#d63c14}
.muted{color:#8a8478;font-size:12px}
select{background:#16140f;border:1px solid #3a352c;color:#f4f1ea;font-family:"IBM Plex Mono",monospace;font-size:13px;padding:8px 10px;width:100%}
.status-przyjęte{color:#d9b24a}.status-w_trakcie{color:#6aa9ff}
.status-gotowe{color:#6fdc8c}.status-odebrane{color:#8a8478}
.hidden{display:none!important}
.msg{font-family:"IBM Plex Mono",monospace;font-size:12px;color:#d63c14;margin-top:10px;min-height:16px}
</style>
</head>
<body>
<div class="wrap">
<h1>Panel serwisu</h1>
<p class="sub">zarządzanie zgłoszeniami</p>
<div id="loginView" class="card">
<label for="pin">PIN</label>
<input type="password" id="pin" placeholder="PIN" autocomplete="off">
<button id="loginBtn">Zaloguj</button>
<p class="msg" id="loginMsg"></p>
</div>
<div id="panelView" class="hidden">
<div class="card">
<table>
<thead><tr><th>Zgłoszenie</th><th>Klient</th><th>Sprzęt</th><th>Problem</th><th>Status</th><th></th></tr></thead>
<tbody id="reportsBody"></tbody>
</table>
<p class="muted" id="emptyMsg" style="margin-top:14px">Brak zgłoszeń.</p>
</div>
<button class="ghost" id="logoutBtn">Wyloguj</button>
</div>
</div>
<script>
var token = localStorage.getItem("xynez_admin_token") || "";
var loginView = document.getElementById("loginView");
var panelView = document.getElementById("panelView");
var STATUSES = [["przyjęte","Przyjęte do serwisu"],["w_trakcie","W trakcie naprawy"],["gotowe","Gotowe do odbioru"],["odebrane","Odebrane"]];

async function api(path, opts) {
  opts = opts || {};
  var res = await fetch(path, Object.assign({}, opts, {headers: Object.assign({"Content-Type":"application/json","Authorization":"Bearer "+token}, opts.headers||{})}));
  if (res.status === 401) { logout(); throw new Error("unauthorized"); }
  return res.json();
}
function logout(){ token=""; localStorage.removeItem("xynez_admin_token"); loginView.classList.remove("hidden"); panelView.classList.add("hidden"); }

document.getElementById("loginBtn").addEventListener("click", async function(){
  var pin = document.getElementById("pin").value;
  var msg = document.getElementById("loginMsg"); msg.textContent="";
  try {
    var res = await fetch("/api/admin/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pin:pin})});
    var data = await res.json();
    if(!data.ok){ msg.textContent=data.error||"Błąd logowania."; return; }
    token=data.token; localStorage.setItem("xynez_admin_token",token); showPanel();
  } catch(e){ msg.textContent="Serwer niedostępny."; }
});
document.getElementById("logoutBtn").addEventListener("click", logout);

async function showPanel(){ loginView.classList.add("hidden"); panelView.classList.remove("hidden"); await loadReports(); }

async function loadReports(){
  var data = await api("/api/admin/reports");
  var tbody = document.getElementById("reportsBody"); tbody.innerHTML="";
  document.getElementById("emptyMsg").style.display = data.reports.length ? "none" : "";
  data.reports.forEach(function(r){
    var tr = document.createElement("tr");
    var sms = r.sms_sent ? ' <span class="muted">(SMS ✓)</span>' : "";
    tr.innerHTML = '<td class="nr">'+r.nr+sms+'<br><span class="muted">'+(r.created_at||"")+'</span></td>'+
      '<td>'+(r.name||"—")+'<br><span class="muted">'+(r.phone||"")+'</span></td>'+
      '<td>'+[r.device,r.brand].filter(Boolean).join(" ")+'</td>'+
      '<td class="muted" style="max-width:220px">'+((r.message||"").slice(0,120))+((r.message||"").length>120?"…":"")+'</td>'+
      '<td class="status-'+r.status+'">'+((STATUSES.find(function(s){return s[0]===r.status})||[])[1]||r.status)+'</td>'+
      '<td><select data-nr="'+r.nr+'">'+STATUSES.map(function(s){return '<option value="'+s[0]+'"'+(s[0]===r.status?" selected":"")+'>'+s[1]+'</option>';}).join("")+'</select></td>';
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll("select").forEach(function(sel){
    sel.addEventListener("change", async function(){
      await api("/api/admin/reports/"+encodeURIComponent(sel.dataset.nr)+"/status",{method:"POST",body:JSON.stringify({status:sel.value})});
      loadReports();
    });
  });
}
if (token) showPanel().catch(logout);
</script>
</body>
</html>
"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(BASE), **kw)

    # --- security: nie serwuj configu i danych ---
    def translate_path(self, path):
        p = super().translate_path(path)
        try:
            rel = os.path.relpath(p, BASE)
        except ValueError:
            return str(BASE / "__blocked__")
        if rel.startswith("data" + os.sep) or rel.split(os.sep)[0] == "__pycache__":
            return str(BASE / "__blocked__")
        if rel in ("config.json", "server.py"):
            return str(BASE / "__blocked__")
        return p

    # --- helpers ---
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _auth(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            return None
        token = h[7:]
        exp = _tokens.get(token)
        if exp and exp > time.time():
            return token
        return None

    # --- routing ---
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Panel admina — tylko z tajnym kluczem z env/config
        if path == PANEL_ROUTE:
            q = urllib.parse.parse_qs(parsed.query)
            key = (q.get("key") or [""])[0]
            expected = panel_key()
            if expected and secrets.compare_digest(key, expected):
                body = PANEL_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
            return

        if path == "/api/health":
            return self._json(200, {"ok": True})

        if path == "/api/status":
            q = urllib.parse.parse_qs(parsed.query)
            nr = (q.get("nr") or [""])[0]
            phone = (q.get("phone") or [""])[0]
            reports = load_reports()
            rep = find_report(reports, nr)
            if not rep or norm_phone(rep.get("phone")) != norm_phone(phone):
                return self._json(404, {"ok": False, "error": "Nie znaleziono zgłoszenia. Sprawdź numer i telefon."})
            return self._json(200, {
                "ok": True,
                "nr": rep["nr"],
                "status": rep["status"],
                "label": STATUS_LABELS.get(rep["status"], rep["status"]),
                "step": STATUSES.index(rep["status"]) if rep["status"] in STATUSES else 0,
                "updated": rep.get("updated_at", ""),
            })

        if path == "/api/admin/reports":
            if not self._auth():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            reports = sorted(load_reports(), key=lambda r: r.get("created_at", ""), reverse=True)
            return self._json(200, {"ok": True, "reports": reports})

        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/reports":
            data = self._body()
            nr = (data.get("zgloszenie") or data.get("nr") or "").strip().upper()
            phone = digits(data.get("phone"))
            email = (data.get("email") or "").strip()
            if not nr or len(phone) < 9:
                return self._json(400, {"ok": False, "error": "Brak numeru zgłoszenia lub telefonu."})

            # Tryb GitHub: commit do status.json (publiczne) + emails.json (prywatne)
            if GH_TOKEN:
                try:
                    for attempt in range(2):
                        try:
                            sha_s, statuses = gh_get_json(PUBLIC_REPO, "status.json")
                            key = nr
                            if key not in statuses:
                                statuses[key] = {
                                    "status": "przyjęte",
                                    "phone4": phone[-4:],
                                    "updated": time.strftime("%Y-%m-%d %H:%M"),
                                }
                                gh_put_json(PUBLIC_REPO, "status.json", statuses, sha_s)
                            if email:
                                sha_e, em = gh_get_json(PRIVATE_REPO, "emails.json")
                                em[key] = email
                                gh_put_json(PRIVATE_REPO, "emails.json", em, sha_e)
                            break
                        except urllib.error.HTTPError as e:
                            if e.code == 409 and attempt == 0:
                                continue
                            raise
                    return self._json(200, {"ok": True, "nr": nr})
                except Exception as e:
                    print(f"[GH] reports error: {e}")
                    return self._json(502, {"ok": False, "error": "backend error"})

            with _lock:
                reports = load_reports()
                rep = find_report(reports, nr)
                if not rep:
                    rep = {
                        "nr": nr,
                        "name": (data.get("name") or "").strip(),
                        "phone": phone,
                        "email": (data.get("email") or "").strip(),
                        "device": (data.get("device") or "").strip(),
                        "brand": (data.get("brand") or "").strip(),
                        "message": (data.get("message") or "").strip(),
                        "status": "przyjęte",
                        "created_at": time.strftime("%Y-%m-%d %H:%M"),
                        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
                        "sms_sent": False,
                    }
                    reports.append(rep)
                    save_reports(reports)
            return self._json(200, {"ok": True, "nr": rep["nr"]})

        if path == "/api/admin/login":
            data = self._body()
            pin = str(data.get("pin") or "")
            cfg = load_config()
            expected = str(cfg.get("admin_pin") or "zmien-mnie-123")
            if not secrets.compare_digest(pin, expected):
                return self._json(401, {"ok": False, "error": "Zły PIN."})
            token = secrets.token_urlsafe(32)
            _tokens[token] = time.time() + TOKEN_TTL
            return self._json(200, {"ok": True, "token": token})

        m = re.match(r"^/api/admin/reports/([A-Za-z0-9\-]+)/status$", path)
        if m:
            if not self._auth():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            status = (self._body().get("status") or "").strip().lower()
            if status not in STATUSES:
                return self._json(400, {"ok": False, "error": "Nieprawidłowy status."})
            nr = m.group(1).upper()
            with _lock:
                reports = load_reports()
                rep = find_report(reports, nr)
                if not rep:
                    return self._json(404, {"ok": False, "error": "Nie znaleziono zgłoszenia."})
                rep["status"] = status
                rep["updated_at"] = time.strftime("%Y-%m-%d %H:%M")
                sms_result = None
                if status == "gotowe" and not rep.get("sms_sent"):
                    msg = (f"Serwis XYNEZ: zgłoszenie {rep['nr']} jest gotowe do odbioru. "
                           f"Skontaktuj sie z serwisem.")
                    send_sms(rep["phone"], msg)
                    rep["sms_sent"] = True
                    sms_result = "sent"
                save_reports(reports)
            return self._json(200, {"ok": True, "status": status, "sms": sms_result})

        return self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")


if __name__ == "__main__":
    print(f"XYNEZ serwis -> http://localhost:{PORT}")
    print(f"Panel -> {PANEL_ROUTE}?key=<PANEL_KEY>")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
