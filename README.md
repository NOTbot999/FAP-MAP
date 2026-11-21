# Franc Map Desktop

Offline-capable Windows desktop version of the Franc AI Pomočnik map (GIS explorer for Slovenia) — without the AI helper, accounts or Base44 backend.

## Run

- **Built app:** `dist\FrancMap\FrancMap.exe` (portable folder — copy the whole `FrancMap` folder anywhere; needs the Edge WebView2 runtime, which Windows 10/11 already has).
- **From source:** `.venv\Scripts\python app.py` (`--serve` runs only the local server and prints a URL for a browser; `--debug` enables devtools).
- **Rebuild exe:** `build.bat`

All user data lives in `%LOCALAPPDATA%\FrancMap` (settings, drawings, projects, tracks, `cache.sqlite` with map tiles and cached searches/routes/POIs).

## How it works

`app.py` starts a local server on `127.0.0.1` and opens a native WebView2 window. Every map tile, search, route, POI query and elevation lookup goes through that server and is stored in SQLite, so anything you've seen once works without internet. The Offline panel downloads whole areas ahead of time.

## Features

- 8 base maps + 77 overlay layers (GURS, ARSO, OSM, Waymarked Trails, OpenRailwayMap…), drag to reorder, opacity, WMS legends, favourites, filter
- 41 POI categories; 57,000 fire hydrants from the state utility cadastre (GJI, bundled, `tools/import_hydrants.py`) from OpenStreetMap (fast canvas rendering, cached for offline)
- Search: places/addresses, loaded POIs, your drawings, WGS84 / DMS / **D96/TM** coordinates
- Routing (BRouter): walk, bike, road bike, main roads, car; alternatives, draggable waypoints, climb, GPX export
- Draw markers / lines / polygons, measure distance & area, edit vertices, notes, colours, named projects, GeoJSON / GPX / KML import & export
- GPX track library with stats (distance, climb, moving time, speed) and GPS recording
- Elevation profiles for routes, tracks, lines and measurements (hover syncs with the map)
- Swipe comparison of any two layers (e.g. orthophoto 1990 ↔ 2024)
- Right-click: address, elevation, current weather, D96/TM, route from/to, copy coordinates
- Offline area downloader, forced offline mode, cache manager
- 3. razvojna os layer and live flights (adsb.lol)
- Light/dark theme, keyboard shortcuts (Ctrl+K, L, P, N, D, T, C, O, M, A, Esc)

## Notes

- OpenStreetMap's own tile servers forbid bulk downloading, so OSM-hosted layers are cached only while you browse them; use OpenTopoMap, CARTO, Esri or GURS layers for area downloads.
- `tools/convert_config.py` regenerates `web/data/layers.json` from the original React repo's `layerConfig.jsx`.
