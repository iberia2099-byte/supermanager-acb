# SuperManager ACB conectado — v3

Broker + Cupo (JFL/COT/EXT) + Valoración + Calendario, todo en la misma base SQLite.

## Ejecutar
```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

## Endpoints nuevos
- `POST /api/refresh` — solo Broker (igual que v2).
- `POST /api/refresh_all` — Broker + cupo JFL/COT/EXT + Valoración + Calendario, en una sola llamada.
- `GET /api/players` — ahora incluye `cupo` (JFL/COT/EXT) y `valoracion_media`/`valoracion_pj` del último snapshot.
- `GET /api/calendario?team=RMA` — próximos rivales por jornada de un equipo (vacío si no hay `team`, devuelve todo).

## Cómo funciona el cupo (JFL/COT/EXT)
El Broker no marca el cupo en cada fila, pero acepta el filtro `?cupo=0/1/2`
(Nacional / Comunitario / Extracomunitario). `extract_cupo_map()` pide las 3
variantes y etiqueta cada jugador según en qué subconjunto aparece.

## Si Valoración o Calendario fallan
`extract_valoracion()` y `extract_calendario()` buscan la tabla por
cabeceras (igual que ya hacía el Broker), no por posición fija de columna,
así que toleran cambios menores de maquetación. Si la web cambia de forma
más profunda, `/api/refresh_all` sigue devolviendo el resto de módulos y
marca cuál falló en `valoracion_error` / `calendario_error`, en vez de
tirar toda la actualización.

## Próximo módulo
- bajas (fuente pendiente de confirmar: ¿ACB.com oficial o ficha de jugador en El Rincón?)
- proyección (combinar valoración histórica + calendario + cupo + bajas)
- optimizador 5M€ (mochila con restricciones de posición)
- Equipo 1, 2, 3...
