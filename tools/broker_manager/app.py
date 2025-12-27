
import os, pathlib
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from dotenv import load_dotenv
from .db import q, exec_sql
from .crypto import encrypt_value, decrypt_value

load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parents[1]/".env")

app = FastAPI(title="Broker DB Editor")
app.mount("/static", StaticFiles(directory=str(pathlib.Path(__file__).parent/"static")), name="static")

templates = Environment(
    loader=FileSystemLoader(str(pathlib.Path(__file__).parent/"templates")),
    autoescape=select_autoescape(["html","xml"])
)
def render(name, **ctx): return HTMLResponse(templates.get_template(name).render(**ctx))

@app.get("/")
def home():
    brokers = q("SELECT * FROM brokers ORDER BY broker_id")
    accounts = q("SELECT * FROM broker_accounts ORDER BY account_id")
    return render("home.html", brokers=brokers, accounts=accounts)

@app.post("/initdb")
def initdb():
    sql = (pathlib.Path(__file__).resolve().parents[1]/"db"/"schema.sql").read_text()
    exec_sql(sql); return RedirectResponse("/", 302)

@app.get("/brokers/new")
def brokers_new(): return render("broker_new.html")

@app.post("/brokers/create")
def brokers_create(broker_id: str = Form(...), display_name: str = Form(...), api_base: str = Form(""),
                   kind: str = Form("retail"), characteristics: str = Form("{}"), flags: str = Form("{}"), notes: str = Form("")):
    q("""INSERT INTO brokers (broker_id, display_name, api_base, kind, characteristics, flags, notes)
        VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)""", (broker_id, display_name, api_base, kind, characteristics, flags, notes), fetch=None)
    return RedirectResponse("/", 302)

@app.get("/brokers/{broker_id}")
def brokers_show(broker_id: str):
    b = q("SELECT * FROM brokers WHERE broker_id=%s", (broker_id,), fetch="one")
    if not b: raise HTTPException(404)
    accounts = q("SELECT * FROM broker_accounts WHERE broker_id=%s ORDER BY account_id", (broker_id,))
    creds = q("SELECT cred_id, key, created_at, updated_at FROM credentials WHERE scope_type='broker' AND scope_id=%s ORDER BY cred_id DESC", (broker_id,))
    return render("broker_show.html", b=b, accounts=accounts, creds=creds)

@app.post("/brokers/{broker_id}/delete")
def brokers_delete(broker_id: str):
    q("DELETE FROM brokers WHERE broker_id=%s", (broker_id,), fetch=None)
    return RedirectResponse("/", 302)

@app.get("/accounts/new")
def accounts_new():
    brokers = q("SELECT broker_id, display_name FROM brokers ORDER BY broker_id")
    return render("account_new.html", brokers=brokers)

@app.post("/accounts/create")
def accounts_create(account_id: str = Form(...), broker_id: str = Form(...), account_code: str = Form(""),
                    label: str = Form(""), klass: str = Form("margin"), pdt_applies: str = Form("true"),
                    pdt_restricted: str = Form("false"), allow_short: str = Form("true"),
                    margin_rate_bps: str = Form(""), commissions: str = Form("{}"), flags: str = Form("{}"), notes: str = Form("")):
    q("""INSERT INTO broker_accounts (account_id, broker_id, account_code, label, class, pdt_applies, pdt_restricted, allow_short, margin_rate_bps, commissions, flags, notes)
        VALUES (%s,%s,%s,%s,%s,%s::boolean,%s::boolean,%s::boolean,NULLIF(%s,'')::int,%s::jsonb,%s::jsonb,%s)""",
      (account_id, broker_id, account_code, label, klass, pdt_applies, pdt_restricted, allow_short, margin_rate_bps, commissions, flags, notes), fetch=None)
    return RedirectResponse("/", 302)

@app.get("/accounts/{account_id}")
def accounts_show(account_id: str):
    a = q("SELECT * FROM broker_accounts WHERE account_id=%s", (account_id,), fetch="one")
    if not a: raise HTTPException(404)
    flags = q("SELECT * FROM account_symbol_flags WHERE account_id=%s ORDER BY symbol", (account_id,))
    creds = q("SELECT cred_id, key, created_at, updated_at FROM credentials WHERE scope_type='account' AND scope_id=%s ORDER BY cred_id DESC", (account_id,))
    notes = q("SELECT * FROM journal WHERE scope_type='account' AND scope_id=%s ORDER BY created_at DESC", (account_id,))
    return render("account_show.html", a=a, flags=flags, creds=creds, notes=notes)

@app.post("/accounts/{account_id}/delete")
def accounts_delete(account_id: str):
    q("DELETE FROM broker_accounts WHERE account_id=%s", (account_id,), fetch=None)
    return RedirectResponse("/", 302)

@app.get("/creds/new")
def creds_new(scope_type: str, scope_id: str):
    return render("creds_new.html", scope_type=scope_type, scope_id=scope_id)

@app.post("/creds/create")
def creds_create(scope_type: str = Form(...), scope_id: str = Form(...), key: str = Form(...), value: str = Form(...)):
    cipher = encrypt_value(value)
    q("INSERT INTO credentials (scope_type, scope_id, key, value_cipher) VALUES (%s,%s,%s,%s)", (scope_type, scope_id, key, cipher), fetch=None)
    return RedirectResponse("/accounts/"+scope_id if scope_type=="account" else "/brokers/"+scope_id, 302)

@app.get("/creds/{cred_id}/show")
def creds_show(cred_id: int):
    row = q("SELECT * FROM credentials WHERE cred_id=%s", (cred_id,), fetch="one")
    if not row: raise HTTPException(404)
    try:
        val = decrypt_value(bytes(row["value_cipher"]))
    except Exception:
        val = "[UNABLE TO DECRYPT - check SECRET_KEY]"
    return PlainTextResponse(f"{row['key']} = {val}")
