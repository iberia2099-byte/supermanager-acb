from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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
    for t in soup.find_all("table"):
        hs = [clean(x.get_text(" ", strip=True)) for x in t.find_all("th")]
        joined = " | ".join(hs).lower()
        if all(term.lower() in joined for term in required_terms):
            return t, hs
    return None, []

TEAM_CODE_RE = re.compile(r"/equipo/([A-Z]{2,4})\b")

def _team_code_from_row(tr):
    """El codigo de equipo casi nunca es texto suelto en la celda: suele ir en el
    href de un enlace a /equipo/CODIGO o en el alt/title de su logo. Probamos eso
    primero (igual que en Calendario) y solo caemos al texto plano como ultimo recurso."""
    for a in tr.find_all("a", href=True):
        m = TEAM_CODE_RE.search(a["href"])
        if m:
            return m.group(1)
    for img in tr.find_all("img"):
        for attr in ("alt", "title"):
            v = img.get(attr, "")
            if re.fullmatch(r"[A-Z]{2,4}", v.strip()):
                return v.strip()
            m = TEAM_CODE_RE.search(v)
            if m:
                return m.group(1)
    return None

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
      snapshot_id INTEGER, name TEXT, team TEXT, media REAL, pj INTEGER,
      forma REAL, rent REAL, reg REAL, raw_json TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS calendario(
      team TEXT, jornada INTEGER, rival TEXT, home INTEGER,
      UNIQUE(team, jornada))""")
    # Bajas marcadas a mano (no depende de snapshot: persiste hasta que la quites)
    con.execute("""CREATE TABLE IF NOT EXISTS bajas_manual(
      name TEXT, team TEXT, baja INTEGER DEFAULT 0, motivo TEXT, updated_at TEXT,
      PRIMARY KEY(name, team))""")
    con.commit(); con.close()

init_db()

# ---------- extractor: Broker + cupo (JFL/COT/EXT) ----------

def parse_broker_rows(soup):
    target, headers = find_table(soup, ["jugador", "precio", "subida"])
    if target is None:
        raise RuntimeError("No se encontro la tabla Broker. La web puede haber cambiado.")
    ip, iprice = col_index(headers, "jugador"), col_index(headers, "precio")
    im15 = col_index(headers, "-15")
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
        team = _team_code_from_row(tr) or ""
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
    if jm is None:
        jm = re.search(r"\bJ(\d+)\b", text)
    jornada = int(jm.group(1)) if jm else None
    rows = parse_broker_rows(soup)
    if len(rows) < 100:
        raise RuntimeError(f"Solo se extrajeron {len(rows)} jugadores. Actualizacion abortada.")
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

# ---------- extractor: Valoracion ----------

def extract_valoracion(snapshot_id):
    soup = get_soup(VALORACION_URL)
    target, headers = find_table(soup, ["jugador"])
    if target is None:
        raise RuntimeError("No se encontro la tabla de Valoracion. La web puede haber cambiado.")
    ip = col_index(headers, "jugador")
    # "Media SM" = media de toda la temporada; "Forma" = rendimiento reciente (lo usamos para "reciente")
    imedia = col_index(headers, "media", "sm")
    if imedia is None:
        imedia = col_index(headers, "media")
    ipj = col_index(headers, "pj")
    iforma = col_index(headers, "forma")
    irent = col_index(headers, "rent")
    ireg = col_index(headers, "reg")
    rows = []
    for tr in target.find_all("tr"):
        c = tr.find_all("td")
        vals = [clean(x.get_text(" ", strip=True)) for x in c]
        if not vals or ip is None or ip >= len(vals):
            continue
        first = vals[ip]
        m = re.match(r"^(B|A|P)\b", first)
        name = clean(first[m.end():]) if m else first
        team = _team_code_from_row(tr) or ""
        raw = {headers[i] if i < len(headers) else f"col{i}": v for i, v in enumerate(vals)}
        def _num(idx):
            return number(vals[idx]) if idx is not None and idx < len(vals) else None
        rows.append({
            "name": name, "team": team,
            "media": _num(imedia), "pj": int(_num(ipj)) if _num(ipj) else None,
            "forma": _num(iforma), "rent": _num(irent), "reg": _num(ireg),
            "raw": raw
        })
    if not rows:
        raise RuntimeError("Valoracion: 0 filas extraidas, revisar cabeceras de la tabla.")
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.executemany("INSERT INTO valoracion VALUES(?,?,?,?,?,?,?,?,?)",
        [(snapshot_id, x["name"], x["team"], x["media"], x["pj"], x["forma"], x["rent"], x["reg"],
          json.dumps(x["raw"], ensure_ascii=False)) for x in rows])
    con.commit(); con.close()
    return {"players": len(rows)}

# ---------- extractor: Calendario ----------

TEAM_HREF_RE = re.compile(r"/equipo/([A-Z]{2,4})\b")

def _team_code_from_link(a):
    href = a.get("href", "")
    m = TEAM_HREF_RE.search(href)
    return m.group(1) if m else None

def extract_calendario():
    """Estructura real (no es una tabla): cada jornada es un bloque con id="j-N"
    (ancla de los botones 1..34), y dentro cada partido tiene exactamente 2 enlaces
    a /smgr/equipo/CODIGO: el primero es el local, el segundo el visitante.
    Buscamos por href, no por texto/clases, para depender lo menos posible del diseno."""
    soup = get_soup(CALENDARIO_URL)
    rows_out = []
    jornada_blocks = [el for el in soup.find_all(id=re.compile(r"^j-\d+$"))]
    if not jornada_blocks:
        # Fallback: buscar cabeceras "Jornada N" y tomar todo hasta la siguiente cabecera
        headers = soup.find_all(string=re.compile(r"^Jornada\s+\d+$"))
        raise RuntimeError("Calendario: no se encontraron bloques por jornada (id=j-N). "
                            f"Cabeceras 'Jornada N' encontradas por texto: {len(headers)}. La web puede haber cambiado.")
    for block in jornada_blocks:
        jm = re.search(r"j-(\d+)", block.get("id", ""))
        if not jm:
            continue
        jornada = int(jm.group(1))
        team_links = [a for a in block.find_all("a", href=TEAM_HREF_RE)]
        codes = [_team_code_from_link(a) for a in team_links]
        codes = [c for c in codes if c]
        # Se agrupan de 2 en 2: (local, visitante) por partido, en el orden en que aparecen
        for i in range(0, len(codes) - 1, 2):
            home_team, away_team = codes[i], codes[i + 1]
            rows_out.append({"team": home_team, "jornada": jornada, "rival": away_team, "home": True})
            rows_out.append({"team": away_team, "jornada": jornada, "rival": home_team, "home": False})
    if not rows_out:
        raise RuntimeError("Calendario: 0 filas extraidas (bloques j-N encontrados pero sin enlaces de equipo dentro).")
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
                                  v.media as valoracion_media, v.pj as valoracion_pj,
                                  v.forma as valoracion_forma, v.rent as valoracion_rent, v.reg as valoracion_reg,
                                  COALESCE(b.baja,0) as de_baja, b.motivo as baja_motivo
                           FROM players p
                           LEFT JOIN valoracion v ON v.snapshot_id=p.snapshot_id AND v.name=p.name AND v.team=p.team
                           LEFT JOIN bajas_manual b ON b.name=p.name AND b.team=p.team
                           WHERE p.snapshot_id=? ORDER BY p.price DESC""", (s["id"],)).fetchall()
    con.close()
    return {"jornada": s["jornada"], "players": [dict(x) for x in rows]}

def _team_strength(snapshot_id):
    """Media de 'media SM' por equipo, como proxy de la fuerza del rival.
    (Cuando aun no se ha jugado ningun partido, todo esto sera None/liga vacia;
    en cuanto haya PJ>0 empezara a rellenarse solo.)"""
    con = sqlite3.connect(DB)
    rows = con.execute("""SELECT team, AVG(media) FROM valoracion
                           WHERE snapshot_id=? AND media IS NOT NULL AND team!='' GROUP BY team""",
                        (snapshot_id,)).fetchall()
    con.close()
    team_avg = {t: m for t, m in rows if m is not None}
    league_avg = sum(team_avg.values()) / len(team_avg) if team_avg else None
    return team_avg, league_avg

def _next_rival_map(jornada):
    if jornada is None:
        return {}
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT team,rival,home FROM calendario WHERE jornada=?", (jornada,)).fetchall()
    con.close()
    return {team: (rival, bool(home)) for team, rival, home in rows}

def compute_proyeccion(snapshot_id, jornada):
    """proyeccion = (forma reciente, o media de temporada si no hay forma aun)
       x factor de dificultad del rival (relativo a la media de la liga)
       x pequeno ajuste local/visitante.
       Bajas manuales -> proyeccion 0 (pero el jugador se sigue devolviendo, visible)."""
    team_avg, league_avg = _team_strength(snapshot_id)
    rival_map = _next_rival_map(jornada)
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT p.name,p.position,p.team,p.price,p.cupo,
                                  v.media as media, v.forma as forma,
                                  COALESCE(b.baja,0) as de_baja
                           FROM players p
                           LEFT JOIN valoracion v ON v.snapshot_id=p.snapshot_id AND v.name=p.name AND v.team=p.team
                           LEFT JOIN bajas_manual b ON b.name=p.name AND b.team=p.team
                           WHERE p.snapshot_id=?""", (snapshot_id,)).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        if d["de_baja"]:
            d["proyeccion"] = 0.0
            d["proyeccion_detalle"] = "de baja"
        else:
            base = d["forma"] if d["forma"] is not None else d["media"]
            if base is None:
                d["proyeccion"] = None
                d["proyeccion_detalle"] = "sin datos todavia (PJ=0)"
            else:
                rival, home = rival_map.get(d["team"], (None, None))
                factor = 1.0
                if rival and league_avg and team_avg.get(rival):
                    factor = league_avg / team_avg[rival]
                    factor = max(0.85, min(1.15, factor))
                home_factor = 1.03 if home is True else (0.97 if home is False else 1.0)
                d["proyeccion"] = round(base * factor * home_factor, 2)
                d["proyeccion_detalle"] = f"base {base} x rival({rival or '-'}) {round(factor,3)} x {'local' if home else 'visitante' if home is False else '-'} {home_factor}"
        out.append(d)
    return out

@app.get("/api/proyeccion")
def proyeccion():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    s = con.execute("SELECT id,jornada FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    if not s:
        return {"jornada": None, "players": []}
    players = compute_proyeccion(s["id"], s["jornada"])
    players.sort(key=lambda p: (p["proyeccion"] is None, -(p["proyeccion"] or 0)))
    return {"jornada": s["jornada"], "players": players}

REGLAS = dict(presupuesto=5_000_000, n_bases=2, n_aleros=4, n_pivots=4, max_ext=2, min_jfl=4)

def _resolver_once(candidates, forbidden_sets, reglas):
    """Un intento de programacion lineal entera: elige exactamente 2B+4A+4P,
    presupuesto <= 5M, EXT<=2, JFL>=4, maximizando la proyeccion total.
    forbidden_sets: soluciones anteriores que no puede repetir exactamente (para dar Equipo 2, 3...)."""
    import pulp
    prob = pulp.LpProblem("supermanager", pulp.LpMaximize)
    x = {i: pulp.LpVariable(f"x_{i}", cat="Binary") for i in range(len(candidates))}
    prob += pulp.lpSum(x[i] * (candidates[i]["proyeccion"] or 0) for i in x)
    prob += pulp.lpSum(x[i] * candidates[i]["price"] for i in x) <= reglas["presupuesto"]
    prob += pulp.lpSum(x[i] for i in x if candidates[i]["position"] == "B") == reglas["n_bases"]
    prob += pulp.lpSum(x[i] for i in x if candidates[i]["position"] == "A") == reglas["n_aleros"]
    prob += pulp.lpSum(x[i] for i in x if candidates[i]["position"] == "P") == reglas["n_pivots"]
    prob += pulp.lpSum(x[i] for i in x if candidates[i]["cupo"] == "EXT") <= reglas["max_ext"]
    prob += pulp.lpSum(x[i] for i in x if candidates[i]["cupo"] == "JFL") >= reglas["min_jfl"]
    for prev in forbidden_sets:
        prob += pulp.lpSum(x[i] for i in prev) <= len(prev) - 1
    status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[status] != "Optimal":
        return None
    chosen = [i for i in x if (x[i].value() or 0) > 0.5]
    return chosen if chosen else None

@app.get("/api/optimizador")
def optimizador(n: int = 3, presupuesto: int = 5_000_000):
    """Optimizador global: sobre TODO el mercado (no depende de tu plantilla actual).
    Devuelve hasta n combinaciones validas y distintas entre si, ordenadas por proyeccion total."""
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    s = con.execute("SELECT id,jornada FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    if not s:
        raise HTTPException(400, "No hay datos de mercado todavia, ejecuta /api/refresh_all primero.")
    try:
        import pulp  # noqa: F401
    except ImportError:
        raise HTTPException(500, "Falta la libreria 'pulp' en requirements.txt (necesaria para el optimizador).")
    players = compute_proyeccion(s["id"], s["jornada"])
    candidates = [p for p in players
                  if not p["de_baja"] and p["position"] in ("B", "A", "P") and p["price"] is not None]
    if len(candidates) < 10:
        raise HTTPException(400, "No hay suficientes jugadores validos para formar un equipo.")
    reglas = dict(REGLAS); reglas["presupuesto"] = presupuesto
    equipos = []
    forbidden = []
    for _ in range(max(1, n)):
        chosen = _resolver_once(candidates, forbidden, reglas)
        if chosen is None:
            break
        forbidden.append(chosen)
        jugadores = [candidates[i] for i in chosen]
        equipos.append({
            "jugadores": jugadores,
            "coste_total": sum(j["price"] for j in jugadores),
            "proyeccion_total": round(sum((j["proyeccion"] or 0) for j in jugadores), 2),
        })
    if not equipos:
        raise HTTPException(400, "No se encontro ninguna combinacion valida con las reglas actuales: 
