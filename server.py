from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
import requests, re, sqlite3, json, datetime

APP = Path(__file__).parent
DB = APP / "supermanager.sqlite"
BASE = "https://www.rincondelmanager.com/smgr"
BROKER_URL = f"{BASE}/broker.php"
VALORACION_URL = f"{BASE}/valoracion.php"
CALENDARIO_URL = f"{BASE}/calendario.php"
HEADERS = {"User-Agent": "Mozilla/5.0"}

app = FastAPI(title="SuperManager ACB API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- utilidades de parseo (compartidas) ----------

def clean(s): return re.sub(r"\s+", " ", s or "").strip()

def money(s):
    s = clean(s).replace(".", "").replace("€", "").replace("+", "")
    m = re.search(r"-?\d+", s)
    return int(m.group()) if m else None

def number(s):
    s = clean(s).replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None

def get_soup(url, params=None):
    from bs4 import BeautifulSoup
    r = requests.get(url, params=params, timeout=30, headers=HEADERS)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def find_table(soup, required_terms):
    """Busca la tabla cuyas cabeceras <th> contienen TODOS los required_terms (case-insensitive).
    Igual que hacía el extractor original del Broker: no depende de posiciones fijas."""
    for t in soup.find_all("table"):
        hs = [clean(x.get_text(" ", strip=True)) for x in t.find_all("th")]
        joined = " | ".join(hs).lower()
        if all(term.lower() in joined for term in required_terms):
            return t, hs
    return None, []

def col_index(headers, *terms):
    for i, h in enumerate(headers):
        x = h.lower()
        if all(t.lower() in x for t in terms):
            return i
    return None

# ---------- DB ----------

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS snapshots(
      id INTEGER PRIMARY KEY, fetched_at TEXT, jornada INTEGER, players INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS players(
      snapshot_id INTEGER, name TEXT, position TEXT, team TEXT, price INTEGER,
      cupo TEXT,
      sm_minus15 REAL, sm_zero REAL, sm_plus15 REAL, max_rise INTEGER,
      next_games TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS valoracion(
      snapshot_id INTEGER, name TEXT, team TEXT, media REAL, pj INTEGER, raw_json TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS calendario(
      team TEXT, jornada INTEGER, rival TEXT, home INTEGER,
      UNIQUE(team, jornada))""")
    con.commit(); con.close()

init_db()

# ---------- extractor: Broker + cupo (JFL/COT/EXT) ----------

def parse_broker_rows(soup):
    target, headers = find_table(soup, ["jugador", "precio", "subida"])
    if target is None:
        raise RuntimeError("No se encontró la tabla Broker. La web puede haber cambiado.")
    ip, iprice = col_index(headers, "jugador"), col_index(headers, "precio")
    im15 = col_index(headers, "−15")
    if im15 is None: im15 = col_index(headers, "-15")
    iz = col_index(headers, "0")
    ip15 = col_index(headers, "+15")
    ir = col_index(headers, "subida")
    rows = []
    for tr in target.find_all("tr"):
        c = tr.find_all("td")
        vals = [clean(x.get_text(" ", strip=True)) for x in c]
        if not vals or ip is None or ip >= len(vals) or iprice is None or iprice >= len(vals):
            continue
        first = vals[ip]
        m = re.match(r"^(B|A|P)\b", first)
        if not m:
            continue
        pos = m.group(1); name = clean(first[m.end():])
        team = ""
        for v in vals[:4]:
            if re.fullmatch(r"[A-Z]{2,4}", v):
                team = v; break
        rows.append({
            "name": name, "position": pos, "team": team,
            "price": money(vals[iprice]),
            "sm_minus15": number(vals[im15]) if im15 is not None and im15 < len(vals) else None,
            "sm_zero": number(vals[iz]) if iz is not None and iz < len(vals) else None,
            "sm_plus15": number(vals[ip15]) if ip15 is not None and ip15 < len(vals) else None,
            "max_rise": money(vals[ir]) if ir is not None and ir < len(vals) else None,
            "next_games": vals[-1] if vals else ""
        })
    return rows

def extract_cupo_map():
    """cupo=0 Nacional, cupo=1 Comunitario, cupo=2 Extracom (JFL/COT/EXT).
    Hace 3 peticiones filtradas y etiqueta cada jugador según en qué subconjunto aparece."""
    labels = {0: "JFL", 1: "COT", 2: "EXT"}
    cupo_map = {}
    for code, label in labels.items():
        try:
            soup = get_soup(BROKER_URL, params={"cupo": code})
            rows = parse_broker_rows(soup)
        except Exception:
            continue
        for r in rows:
            cupo_map[(r["name"], r["team"])] = label
    return cupo_map

def extract_broker():
    soup = get_soup(BROKER_URL)
    text = clean(soup.get_text(" ", strip=True))
    jm = re.search(r"Jornada\s+(\d+)", text, re.I)
    jornada = int(jm.group(1)) if jm else None
    rows = parse_broker_rows(soup)
    if len(rows) < 100:
        raise RuntimeError(f"Solo se extrajeron {len(rows)} jugadores. Actualización abortada.")
    cupo_map = extract_cupo_map()
    for r in rows:
        r["cupo"] = cupo_map.get((r["name"], r["team"]))
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("INSERT INTO snapshots VALUES(NULL,?,?,?)",
                (datetime.datetime.now(datetime.timezone.utc).isoformat(), jornada, len(rows)))
    sid = cur.lastrowid
    cur.executemany("INSERT INTO players VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(sid, x["name"], x["position"], x["team"], x["price"], x.get("cupo"),
          x["sm_minus15"], x["sm_zero"], x["sm_plus15"], x["max_rise"], x["next_games"]) for x in rows])
    con.commit(); con.close()
    return {"jornada": jornada, "players": len(rows), "rows": rows}

# ---------- extractor: Valoración ----------

def extract_valoracion(snapshot_id):
    soup = get_soup(VALORACION_URL)
    target, headers = find_table(soup, ["jugador"])
    if target is None:
        raise RuntimeError("No se encontró la tabla de Valoración. La web puede haber cambiado.")
    ip = col_index(headers, "jugador")
    imedia = col_index(headers, "media")
    ipj = col_index(headers, "pj")
    rows = []
    for tr in target.find_all("tr"):
        c = tr.find_all("td")
        vals = [clean(x.get_text(" ", strip=True)) for x in c]
        if not vals or ip is None or ip >= len(vals):
            continue
        first = vals[ip]
        m = re.match(r"^(B|A|P)\b", first)
        name = clean(first[m.end():]) if m else first
        team = ""
        for v in vals[:4]:
            if re.fullmatch(r"[A-Z]{2,4}", v):
                team = v; break
        raw = {headers[i] if i < len(headers) else f"col{i}": v for i, v in enumerate(vals)}
        rows.append({
            "name": name, "team": team,
            "media": number(vals[imedia]) if imedia is not None and imedia < len(vals) else None,
            "pj": int(number(vals[ipj])) if ipj is not None and ipj < len(vals) and number(vals[ipj]) else None,
            "raw": raw
        })
    if not rows:
        raise RuntimeError("Valoración: 0 filas extraídas, revisar cabeceras de la tabla.")
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.executemany("INSERT INTO valoracion VALUES(?,?,?,?,?,?)",
        [(snapshot_id, x["name"], x["team"], x["media"], x["pj"], json.dumps(x["raw"], ensure_ascii=False)) for x in rows])
    con.commit(); con.close()
    return {"players": len(rows)}

# ---------- extractor: Calendario ----------

def parse_matchup_cell(text):
    """'vs RMA' -> (rival='RMA', home=True); '@ BAS' -> (rival='BAS', home=False)"""
    text = clean(text)
    home = text.startswith("vs")
    m = re.search(r"[A-Z]{2,4}", text)
    rival = m.group(0) if m else None
    return rival, home

def extract_calendario():
    soup = get_soup(CALENDARIO_URL)
    target, headers = find_table(soup, ["equipo"])
    if target is None:
        # algunos sitios usan "jornada" como cabecera principal en vez de "equipo"
        target, headers = find_table(soup, ["jornada"])
    if target is None:
        raise RuntimeError("No se encontró la tabla de Calendario. La web puede haber cambiado.")
    jornada_cols = []
    for i, h in enumerate(headers):
        jm = re.search(r"(\d+)", h)
        if jm and ("j" in h.lower() or "jornada" in h.lower()):
            jornada_cols.append((i, int(jm.group(1))))
    rows_out = []
    for tr in target.find_all("tr"):
        c = tr.find_all(["td", "th"])
        vals = [clean(x.get_text(" ", strip=True)) for x in c]
        if not vals:
            continue
        team = None
        for v in vals[:2]:
            if re.fullmatch(r"[A-Z]{2,4}", v):
                team = v; break
        if not team:
            continue
        for i, jornada in jornada_cols:
            if i < len(vals):
                rival, home = parse_matchup_cell(vals[i])
                if rival:
                    rows_out.append({"team": team, "jornada": jornada, "rival": rival, "home": home})
    if not rows_out:
        raise RuntimeError("Calendario: 0 filas extraídas, revisar estructura de columnas por jornada.")
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.executemany("INSERT OR REPLACE INTO calendario VALUES(?,?,?,?)",
        [(x["team"], x["jornada"], x["rival"], 1 if x["home"] else 0) for x in rows_out])
    con.commit(); con.close()
    return {"fixtures": len(rows_out)}

# ---------- endpoints ----------

@app.get("/api/status")
def status():
    con = sqlite3.connect(DB)
    row = con.execute("SELECT jornada,players,fetched_at FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return {"ok": True, "snapshot": row and {"jornada": row[0], "players": row[1], "fetched_at": row[2]}}

@app.post("/api/refresh")
def refresh():
    try:
        return extract_broker()
    except Exception as e:
        raise HTTPException(502, str(e))

@app.post("/api/refresh_all")
def refresh_all():
    result = {}
    try:
        result["broker"] = extract_broker()
    except Exception as e:
        raise HTTPException(502, f"Broker: {e}")
    con = sqlite3.connect(DB)
    sid = con.execute("SELECT id FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.close()
    try:
        result["valoracion"] = extract_valoracion(sid)
    except Exception as e:
        result["valoracion_error"] = str(e)
    try:
        result["calendario"] = extract_calendario()
    except Exception as e:
        result["calendario_error"] = str(e)
    return result

@app.get("/api/players")
def players():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    s = con.execute("SELECT id,jornada FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if not s:
        con.close(); return {"jornada": None, "players": []}
    rows = con.execute("""SELECT p.name,p.position,p.team,p.price,p.cupo,
                                  p.sm_minus15,p.sm_zero,p.sm_plus15,p.max_rise,p.next_games,
                                  v.media as valoracion_media, v.pj as valoracion_pj
                           FROM players p
                           LEFT JOIN valoracion v ON v.snapshot_id=p.snapshot_id AND v.name=p.name AND v.team=p.team
                           WHERE p.snapshot_id=? ORDER BY p.price DESC""", (s["id"],)).fetchall()
    con.close()
    return {"jornada": s["jornada"], "players": [dict(x) for x in rows]}

@app.get("/api/calendario")
def calendario(team: str | None = None):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    if team:
        rows = con.execute("SELECT team,jornada,rival,home FROM calendario WHERE team=? ORDER BY jornada", (team,)).fetchall()
    else:
        rows = con.execute("SELECT team,jornada,rival,home FROM calendario ORDER BY team,jornada").fetchall()
    con.close()
    return {"fixtures": [dict(x) for x in rows]}

@app.get("/")
def index():
    return FileResponse(APP / "index.html")
