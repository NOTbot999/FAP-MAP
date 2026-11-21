package si.francmap.app;

import android.content.Context;
import android.content.res.AssetManager;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.webkit.WebResourceResponse;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorCompletionService;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Android replacement for the desktop app's Python backend (app.py):
 * tile cache + upstream URL building (XYZ / WMS / ArcGIS export / D96 WMS), cached JSON proxy,
 * Overpass with mirror racing, key/value store, offline area downloads and layer health check.
 * The web UI talks to it through the same /tiles/… and /api/… paths as on the desktop.
 */
public class Backend {
    static final String VERSION = "1.5.7";
    static final String UA = "FrancMapAndroid/" + VERSION + " (map viewer for Slovenia)";
    static final long TILE_MAX_AGE = 30L * 86400;
    static final long LIVE_TILE_MAX_AGE = 1800;
    static final int MAX_BULK_TILES = 60000;
    static final String[] ALLOWED_FETCH_HOSTS = {
            "nominatim.openstreetmap.org", "brouter.de", "router.project-osrm.org", "api.adsb.lol",
            "api.open-meteo.com", "ipi.eprostor.gov.si", "gis.arso.gov.si", "rkg.gov.si", "bio.discomap.eea.europa.eu", "api.rainviewer.com"};
    static final String[] OVERPASS_MIRRORS = {
            "https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.openstreetmap.fr/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"};
    static final double HALF = 20037508.342789244;

    final Context ctx;
    final AssetManager assets;
    final String token = UUID.randomUUID().toString().replace("-", "");
    final File storeDir;
    final SQLiteDatabase db;
    final Object dbWrite = new Object();
    final Map<String, JSONObject> layers = new HashMap<>();
    final ExecutorService io = Executors.newFixedThreadPool(8);
    final ExecutorService refreshPool = Executors.newFixedThreadPool(3);
    final Set<String> refreshing = Collections.synchronizedSet(new HashSet<>());

    volatile boolean forcedOffline = false;
    volatile long lastOk = 0, lastFail = 0;
    final Object nominatimLock = new Object();
    long nominatimLast = 0;

    OfflineJob job;
    final Map<String, JSONObject> health = Collections.synchronizedMap(new HashMap<>());
    volatile boolean healthRunning = false;
    volatile double healthChecked = 0;

    Backend(Context c) {
        ctx = c.getApplicationContext();
        assets = ctx.getAssets();
        storeDir = new File(ctx.getFilesDir(), "store");
        storeDir.mkdirs();
        db = SQLiteDatabase.openOrCreateDatabase(new File(ctx.getFilesDir(), "cache.sqlite"), null);
        db.enableWriteAheadLogging();
        db.execSQL("CREATE TABLE IF NOT EXISTS tiles(layer TEXT, z INT, x INT, y INT, data BLOB, ctype TEXT, ts INT, PRIMARY KEY(layer,z,x,y))");
        db.execSQL("CREATE TABLE IF NOT EXISTS http(key TEXT PRIMARY KEY, data BLOB, ctype TEXT, ts INT)");
        try {
            JSONObject d = new JSONObject(new String(readAsset("data/layers.json"), StandardCharsets.UTF_8));
            JSONArray base = d.getJSONArray("base");
            for (int i = 0; i < base.length(); i++) layers.put(base.getJSONObject(i).getString("id"), base.getJSONObject(i));
            JSONArray cats = d.getJSONArray("categories");
            for (int i = 0; i < cats.length(); i++) {
                JSONArray ls = cats.getJSONObject(i).getJSONArray("layers");
                for (int j = 0; j < ls.length(); j++) layers.put(ls.getJSONObject(j).getString("id"), ls.getJSONObject(j));
            }
            JSONArray internal = d.optJSONArray("internal");   // e.g. terrain elevation tiles for the 3D view
            for (int i = 0; internal != null && i < internal.length(); i++) layers.put(internal.getJSONObject(i).getString("id"), internal.getJSONObject(i));
        } catch (Exception e) {
            throw new RuntimeException("layers.json", e);
        }
        // layers whose source changed carry "purgeBefore": drop tiles cached before that
        // (e.g. CARTO's "API KEY REQUIRED" images after the switch to Esri)
        for (Map.Entry<String, JSONObject> e : layers.entrySet()) {
            long ts = e.getValue().optLong("purgeBefore", 0);
            if (ts > 0) {
                try { synchronized (dbWrite) { db.execSQL("DELETE FROM tiles WHERE layer=? AND ts<?", new Object[]{e.getKey(), ts}); } }
                catch (Exception ignored) { }
            }
        }
        loadHealthFile();
        new Thread(() -> {
            try { Thread.sleep(20000); } catch (InterruptedException ignored) { }
            runHealthCheck(false);
        }).start();
    }

    // ───────────────────────── responses ─────────────────────────
    static class Resp {
        int status; String ctype; byte[] data; String source = "";
        Resp(int s, String c, byte[] d) { status = s; ctype = c; data = d; }
        static Resp json(int s, Object o) { return new Resp(s, "application/json", String.valueOf(o).getBytes(StandardCharsets.UTF_8)); }
        static Resp text(int s, String t) { return new Resp(s, "text/plain", t.getBytes(StandardCharsets.UTF_8)); }
        String text() { return new String(data, StandardCharsets.UTF_8); }
    }

    /** GET requests coming straight from the WebView (static files, tiles, legend images). */
    WebResourceResponse intercept(Uri u) {
        Resp r;
        String path = u.getPath() == null ? "/" : u.getPath();
        try {
            if (path.startsWith("/tiles/")) r = tile(path);
            else if (path.startsWith("/api/")) {
                if (!token.equals(u.getQueryParameter("t"))) r = Resp.json(403, "{\"error\":\"bad token\"}");
                else r = api("GET", path + (u.getQuery() != null ? "?" + u.getQuery() : ""), null);
            } else r = staticFile(path);
        } catch (Exception e) {
            r = Resp.text(500, String.valueOf(e));
        }
        Map<String, String> headers = new HashMap<>();
        headers.put("Access-Control-Allow-Origin", "*");
        headers.put("Cache-Control", path.startsWith("/tiles/") && r.status == 200 ? ("cache".equals(r.source) ? "max-age=86400" : "max-age=300") : "no-cache");
        String mime = r.ctype.split(";")[0].trim();
        String reason = r.status == 200 ? "OK" : r.status == 404 ? "Not Found" : "Error";
        return new WebResourceResponse(mime, "UTF-8", r.status, reason, headers, new ByteArrayInputStream(r.data));
    }

    Resp staticFile(String path) throws IOException {
        if (path.equals("/") || path.isEmpty()) path = "/index.html";
        String p = path.substring(1);
        if (p.contains("..")) return Resp.text(404, "not found");
        byte[] d;
        try { d = readAsset(p); } catch (IOException e) { return Resp.text(404, "not found"); }
        return new Resp(200, mime(p), d);
    }

    static String mime(String p) {
        String l = p.toLowerCase(Locale.ROOT);
        if (l.endsWith(".html")) return "text/html";
        if (l.endsWith(".js")) return "application/javascript";
        if (l.endsWith(".css")) return "text/css";
        if (l.endsWith(".json")) return "application/json";
        if (l.endsWith(".png")) return "image/png";
        if (l.endsWith(".svg")) return "image/svg+xml";
        if (l.endsWith(".jpg") || l.endsWith(".jpeg")) return "image/jpeg";
        return "application/octet-stream";
    }

    byte[] readAsset(String p) throws IOException {
        try (InputStream in = assets.open(p)) { return readAll(in); }
    }

    static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
        return bo.toByteArray();
    }

    // ───────────────────────── API ─────────────────────────
    Resp api(String method, String pathAndQuery, String body) {
        try {
            Uri u = Uri.parse("https://x" + pathAndQuery);
            String path = u.getPath();
            if ("GET".equals(method)) {
                switch (path) {
                    case "/api/status": {
                        JSONObject o = new JSONObject();
                        o.put("online", online()); o.put("forcedOffline", forcedOffline); o.put("version", VERSION);
                        o.put("dataDir", ctx.getFilesDir().getAbsolutePath()); o.put("job", job == null ? JSONObject.NULL : job.status());
                        o.put("platform", "android");
                        return Resp.json(200, o);
                    }
                    case "/api/fetch": {
                        String url = u.getQueryParameter("url");
                        if (url == null || !url.startsWith("https://") || !allowedHost(Uri.parse(url).getHost()))
                            return Resp.json(400, "{\"error\":\"host not allowed\"}");
                        long ttl = parseLong(u.getQueryParameter("ttl"), 86400);
                        return cachedGet(url, ttl);
                    }
                    case "/api/health": {
                        JSONObject o = new JSONObject();
                        o.put("running", healthRunning); o.put("checked", healthChecked);
                        synchronized (health) { o.put("results", new JSONObject(health)); }
                        return Resp.json(200, o);
                    }
                    case "/api/cache/stats": return Resp.json(200, stats());
                    case "/api/offline/status": return Resp.json(200, job == null ? "null" : job.status());
                }
                if (path.startsWith("/api/store/")) {
                    String name = path.substring("/api/store/".length());
                    if (!name.matches("[a-z0-9_\\-]{1,64}")) return Resp.json(400, "{\"error\":\"bad name\"}");
                    File f = new File(storeDir, name + ".json");
                    if (!f.exists()) return Resp.json(200, "null");
                    try (InputStream in = new java.io.FileInputStream(f)) { return new Resp(200, "application/json", readAll(in)); }
                }
                return Resp.json(404, "{\"error\":\"unknown endpoint\"}");
            }
            // POST / PUT
            Object parsed = body == null || body.isEmpty() ? JSONObject.NULL : new org.json.JSONTokener(body).nextValue();
            JSONObject p = parsed instanceof JSONObject ? (JSONObject) parsed : new JSONObject();
            if (path.equals("/api/overpass")) return overpass(p.optString("query", ""), p.optLong("ttl", 7 * 86400));
            if (path.startsWith("/api/store/")) {
                String name = path.substring("/api/store/".length());
                if (!name.matches("[a-z0-9_\\-]{1,64}")) return Resp.json(400, "{\"error\":\"bad name\"}");
                File tmp = new File(storeDir, name + ".tmp"), f = new File(storeDir, name + ".json");
                try (FileOutputStream out = new FileOutputStream(tmp)) { out.write(body == null ? "null".getBytes() : body.getBytes(StandardCharsets.UTF_8)); }
                if (!tmp.renameTo(f)) { f.delete(); tmp.renameTo(f); }
                return Resp.json(200, "{\"ok\":true}");
            }
            switch (path) {
                case "/api/network": {
                    forcedOffline = p.optBoolean("forcedOffline");
                    JSONObject o = new JSONObject();
                    o.put("online", online()); o.put("forcedOffline", forcedOffline);
                    return Resp.json(200, o);
                }
                case "/api/offline/start": return startOffline(p);
                case "/api/offline/cancel": if (job != null) job.cancelled.set(true); return Resp.json(200, "{\"ok\":true}");
                case "/api/cache/clear": {
                    synchronized (dbWrite) {
                        if ("http".equals(p.optString("what"))) db.execSQL("DELETE FROM http");
                        else if (p.has("layer")) db.execSQL("DELETE FROM tiles WHERE layer=?", new Object[]{p.getString("layer")});
                        else db.execSQL("DELETE FROM tiles");
                    }
                    db.execSQL("VACUUM");
                    return Resp.json(200, stats());
                }
                case "/api/health/run": runHealthCheck(true); return Resp.json(200, "{\"running\":true}");
                case "/api/collab/hostinfo": return Resp.json(200, "{\"hosting\":false}");
                case "/api/collab/call": return collabCall(p);
            }
            return Resp.json(404, "{\"error\":\"unknown endpoint\"}");
        } catch (Exception e) {
            return Resp.json(500, JSONObject.quote(String.valueOf(e)).replaceFirst("^\"", "{\"error\":\"") + "}");
        }
    }

    /** Skupno delo: proxy a call to a session server (desktop host on the Wi-Fi or an internet URL). */
    Resp collabCall(JSONObject p) {
        String server = p.optString("server", "").replaceAll("/+$", "");
        String path = p.optString("path", "");
        Uri su = Uri.parse(server);
        if (!("http".equals(su.getScheme()) || "https".equals(su.getScheme())) || su.getHost() == null || !path.startsWith("/collab/"))
            return Resp.json(400, "{\"error\":\"Neveljaven naslov strežnika.\"}");
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(server + path).openConnection();
            c.setConnectTimeout(4000);
            c.setReadTimeout(15000);
            c.setRequestProperty("User-Agent", UA);
            if (!"GET".equals(p.optString("method", "GET"))) {
                c.setRequestMethod("POST");
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                JSONObject body = p.optJSONObject("body");
                c.getOutputStream().write((body == null ? "{}" : body.toString()).getBytes(StandardCharsets.UTF_8));
            }
            Http h = finish(c);
            return new Resp(h.status, "application/json", h.data);
        } catch (IOException e) {
            return Resp.json(502, "{\"error\":\"Strežnik seje ni dosegljiv (" + e.getClass().getSimpleName() + ").\"}");
        }
    }

    static long parseLong(String s, long d) { try { return Long.parseLong(s); } catch (Exception e) { return d; } }

    boolean allowedHost(String h) {
        if (h == null) return false;
        for (String a : ALLOWED_FETCH_HOSTS) if (h.equals(a) || h.endsWith("." + a)) return true;
        return false;
    }

    boolean online() {
        if (forcedOffline) return false;
        return lastOk >= lastFail || System.currentTimeMillis() - lastFail > 20000;
    }

    // ───────────────────────── network ─────────────────────────
    static class Http { int status; String ctype; byte[] data; }

    Http httpGet(String url, int connectMs, int readMs) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(connectMs);
        c.setReadTimeout(readMs);
        c.setRequestProperty("User-Agent", UA);
        c.setRequestProperty("Accept-Language", "sl,en;q=0.7");
        return finish(c);
    }

    Http httpPostForm(String url, String key, String value, int readMs) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(6000);
        c.setReadTimeout(readMs);
        c.setRequestProperty("User-Agent", UA);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");
        byte[] b = (key + "=" + URLEncoder.encode(value, "UTF-8")).getBytes(StandardCharsets.UTF_8);
        c.getOutputStream().write(b);
        return finish(c);
    }

    static Http finish(HttpURLConnection c) throws IOException {
        Http h = new Http();
        h.status = c.getResponseCode();
        h.ctype = c.getContentType() == null ? "" : c.getContentType().split(";")[0].trim();
        InputStream in = h.status >= 400 ? c.getErrorStream() : c.getInputStream();
        h.data = in == null ? new byte[0] : readAll(in);
        if (in != null) in.close();
        return h;
    }

    Resp cachedGet(String url, long ttl) {
        String key = "GET " + url;
        Object[] row = getHttp(key);
        long now = System.currentTimeMillis() / 1000;
        if (row != null && (now - (long) row[2] < ttl || forcedOffline)) { Resp r = new Resp(200, (String) row[1], (byte[]) row[0]); r.source = "cache"; return r; }
        if (forcedOffline) return Resp.json(503, "{\"error\":\"offline\"}");
        if (url.contains("nominatim")) {
            synchronized (nominatimLock) {   // usage policy: max 1 request / second
                long wait = 1050 - (System.currentTimeMillis() - nominatimLast);
                if (wait > 0) try { Thread.sleep(wait); } catch (InterruptedException ignored) { }
                nominatimLast = System.currentTimeMillis();
            }
        }
        try {
            Http h = httpGet(url, 6000, 40000);
            lastOk = System.currentTimeMillis();
            String ct = h.ctype.isEmpty() ? "application/octet-stream" : h.ctype;
            if (h.status == 200) { putHttp(key, h.data, ct); return new Resp(200, ct, h.data); }
            if (row != null) return new Resp(200, (String) row[1], (byte[]) row[0]);
            return new Resp(h.status, ct, h.data);
        } catch (IOException e) {
            lastFail = System.currentTimeMillis();
            if (row != null) return new Resp(200, (String) row[1], (byte[]) row[0]);
            return Resp.json(503, "{\"error\":\"Ni povezave\"}");
        }
    }

    Resp overpass(String query, long ttl) throws Exception {
        String key = "OVERPASS " + sha1(query);
        Object[] row = getHttp(key);
        long now = System.currentTimeMillis() / 1000;
        if (row != null && (now - (long) row[2] < ttl || forcedOffline)) return new Resp(200, "application/json", (byte[]) row[0]);
        if (forcedOffline) return Resp.json(503, "{\"error\":\"offline\",\"elements\":[]}");
        // race all mirrors; first valid JSON wins
        ExecutorService pool = Executors.newFixedThreadPool(OVERPASS_MIRRORS.length);
        ExecutorCompletionService<byte[]> ecs = new ExecutorCompletionService<>(pool);
        List<Future<byte[]>> fs = new ArrayList<>();
        for (String m : OVERPASS_MIRRORS) fs.add(ecs.submit(() -> {
            Http h = httpPostForm(m, "data", query, 120000);
            if (h.status == 200 && h.ctype.contains("json")) return h.data;
            throw new IOException("HTTP " + h.status);
        }));
        String last = "?";
        try {
            for (int i = 0; i < fs.size(); i++) {
                Future<byte[]> f = ecs.poll(130, TimeUnit.SECONDS);
                if (f == null) break;
                try {
                    byte[] d = f.get();
                    lastOk = System.currentTimeMillis();
                    putHttp(key, d, "application/json");
                    return new Resp(200, "application/json", d);
                } catch (Exception e) { last = String.valueOf(e.getCause()); }
            }
        } finally { for (Future<byte[]> f : fs) f.cancel(true); pool.shutdownNow(); }
        if (row != null) return new Resp(200, "application/json", (byte[]) row[0]);
        lastFail = System.currentTimeMillis();
        return Resp.json(503, new JSONObject().put("error", "Overpass ni dosegljiv (" + last + ")").put("elements", new JSONArray()));
    }

    static String sha1(String s) throws Exception {
        byte[] d = MessageDigest.getInstance("SHA-1").digest(s.getBytes(StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder();
        for (byte b : d) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    // ───────────────────────── cache ─────────────────────────
    Object[] getTile(String l, int z, int x, int y) {
        try (Cursor c = db.rawQuery("SELECT data, ctype, ts FROM tiles WHERE layer=? AND z=? AND x=? AND y=?",
                new String[]{l, "" + z, "" + x, "" + y})) {
            return c.moveToFirst() ? new Object[]{c.getBlob(0), c.getString(1), c.getLong(2)} : null;
        }
    }

    void putTile(String l, int z, int x, int y, byte[] d, String ct) {
        synchronized (dbWrite) {
            db.execSQL("INSERT OR REPLACE INTO tiles VALUES(?,?,?,?,?,?,?)", new Object[]{l, z, x, y, d, ct, System.currentTimeMillis() / 1000});
        }
    }

    Object[] getHttp(String key) {
        try (Cursor c = db.rawQuery("SELECT data, ctype, ts FROM http WHERE key=?", new String[]{key})) {
            return c.moveToFirst() ? new Object[]{c.getBlob(0), c.getString(1), c.getLong(2)} : null;
        }
    }

    void putHttp(String key, byte[] d, String ct) {
        synchronized (dbWrite) {
            db.execSQL("INSERT OR REPLACE INTO http VALUES(?,?,?,?)", new Object[]{key, d, ct, System.currentTimeMillis() / 1000});
        }
    }

    JSONObject stats() throws JSONException {
        JSONObject o = new JSONObject();
        JSONArray ls = new JSONArray();
        try (Cursor c = db.rawQuery("SELECT layer, COUNT(*), SUM(LENGTH(data)) FROM tiles GROUP BY layer", null)) {
            while (c.moveToNext()) ls.put(new JSONObject().put("layer", c.getString(0)).put("tiles", c.getLong(1)).put("bytes", c.getLong(2)));
        }
        o.put("layers", ls);
        try (Cursor c = db.rawQuery("SELECT COUNT(*), COALESCE(SUM(LENGTH(data)),0) FROM http", null)) {
            c.moveToFirst();
            o.put("http", new JSONObject().put("entries", c.getLong(0)).put("bytes", c.getLong(1)));
        }
        File f = new File(ctx.getFilesDir(), "cache.sqlite"), wal = new File(ctx.getFilesDir(), "cache.sqlite-wal");
        o.put("fileBytes", f.length() + wal.length());
        o.put("path", f.getAbsolutePath());
        return o;
    }

    // ───────────────────────── tiles ─────────────────────────
    Resp tile(String path) {
        String[] s = path.split("/");   // "", "tiles", id, z, x, y
        if (s.length < 6) return Resp.text(404, "");
        String id = s[2];
        int z = Integer.parseInt(s[3]), x = Integer.parseInt(s[4]), y = Integer.parseInt(s[5].replaceAll("\\..*", ""));
        Resp r = fetchTile(id, z, x, y, true);
        return r == null ? Resp.text(404, "") : r;
    }

    Resp fetchTile(String id, int z, int x, int y, boolean allowNet) {
        JSONObject cfg = layers.get(id);
        if (cfg == null) return null;
        Object[] row = getTile(id, z, x, y);
        long maxAge = cfg.optBoolean("live") ? LIVE_TILE_MAX_AGE : TILE_MAX_AGE;
        long now = System.currentTimeMillis() / 1000;
        if (row != null && (now - (long) row[2] < maxAge || !allowNet || forcedOffline)) {
            Resp r = new Resp(200, (String) row[1], (byte[]) row[0]); r.source = "cache"; return r;
        }
        if (!allowNet || forcedOffline) return null;
        if (row != null) {   // stale-while-revalidate
            String k = id + "/" + z + "/" + x + "/" + y;
            if (refreshing.size() < 400 && refreshing.add(k)) refreshPool.submit(() -> {
                try { downloadTile(id, cfg, z, x, y); } finally { refreshing.remove(k); }
            });
            Resp r = new Resp(200, (String) row[1], (byte[]) row[0]); r.source = "stale"; return r;
        }
        return downloadTile(id, cfg, z, x, y);
    }

    Resp downloadTile(String id, JSONObject cfg, int z, int x, int y) {
        String url = buildUrl(cfg, z, x, y);
        if (url == null) return null;
        try {
            Http h = httpGet(url, 6000, 20000);
            lastOk = System.currentTimeMillis();
            if (h.status == 200 && h.ctype.startsWith("image/") && h.data.length > 0) {
                putTile(id, z, x, y, h.data, h.ctype);
                Resp r = new Resp(200, h.ctype, h.data); r.source = "net"; return r;
            }
        } catch (IOException e) {
            lastFail = System.currentTimeMillis();
        }
        return null;
    }

    static double[] bounds3857(int z, int x, int y) {
        double res = 2 * HALF / Math.pow(2, z), minx = -HALF + x * res, maxy = HALF - y * res;
        return new double[]{minx, maxy - res, minx + res, maxy};
    }

    static double[] bounds4326(int z, int x, int y) {   // w, s, e, n
        double n = Math.pow(2, z);
        return new double[]{x / n * 360 - 180, lat(y + 1, n), (x + 1) / n * 360 - 180, lat(y, n)};
    }

    static double lat(double yy, double n) { return Math.toDegrees(Math.atan(Math.sinh(Math.PI * (1 - 2 * yy / n)))); }

    /** WGS84 → D96/TM (EPSG:3794) easting, northing. */
    static double[] toD96(double lat, double lng) {
        double a = 6378137.0, f = 1 / 298.257222101, k0 = 0.9999, lon0 = Math.toRadians(15), fe = 500000, fn = -5000000;
        double e2 = f * (2 - f), ep2 = e2 / (1 - e2), phi = Math.toRadians(lat), lam = Math.toRadians(lng);
        double nn = a / Math.sqrt(1 - e2 * Math.pow(Math.sin(phi), 2));
        double t = Math.pow(Math.tan(phi), 2), c = ep2 * Math.pow(Math.cos(phi), 2), aa = (lam - lon0) * Math.cos(phi);
        double e4 = e2 * e2, e6 = e4 * e2;
        double m = a * ((1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * Math.sin(2 * phi)
                + (15 * e4 / 256 + 45 * e6 / 1024) * Math.sin(4 * phi) - (35 * e6 / 3072) * Math.sin(6 * phi));
        double east = fe + k0 * nn * (aa + (1 - t + c) * Math.pow(aa, 3) / 6 + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * Math.pow(aa, 5) / 120);
        double north = fn + k0 * (m + nn * Math.tan(phi) * (aa * aa / 2 + (5 - t + 9 * c + 4 * c * c) * Math.pow(aa, 4) / 24
                + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * Math.pow(aa, 6) / 720));
        return new double[]{east, north};
    }

    static String enc(String s) { try { return URLEncoder.encode(s, "UTF-8"); } catch (Exception e) { return s; } }

    String buildUrl(JSONObject cfg, int z, int x, int y) {
        String t = cfg.optString("type");
        String url = cfg.optString("url");
        if ("tile".equals(t)) {
            if (url.contains("{s}")) {
                String subs = cfg.optString("subdomains", "abc");
                url = url.replace("{s}", String.valueOf(subs.charAt((x + y) % subs.length())));
            }
            return url.replace("{z}", "" + z).replace("{x}", "" + x).replace("{y}", "" + y).replace("{r}", "");
        }
        if ("wms".equals(t)) {
            String ver = cfg.optString("version", "1.1.1");
            String crsKey = "1.3.0".equals(ver) ? "CRS" : "SRS";
            String crs = "EPSG:3857", bbox;
            if ("EPSG:3794".equals(cfg.optString("crs"))) {
                double[] b = bounds4326(z, x, y);
                double[][] pts = {toD96(b[1], b[0]), toD96(b[1], b[2]), toD96(b[3], b[0]), toD96(b[3], b[2])};
                double minE = 1e12, minN = 1e12, maxE = -1e12, maxN = -1e12;
                for (double[] p : pts) { minE = Math.min(minE, p[0]); maxE = Math.max(maxE, p[0]); minN = Math.min(minN, p[1]); maxN = Math.max(maxN, p[1]); }
                crs = "EPSG:3794";
                bbox = String.format(Locale.ROOT, "%.2f,%.2f,%.2f,%.2f", minE, minN, maxE, maxN);
            } else {
                double[] b = bounds3857(z, x, y);
                bbox = String.format(Locale.ROOT, "%.6f,%.6f,%.6f,%.6f", b[0], b[1], b[2], b[3]);
            }
            return url + (url.contains("?") ? "&" : "?") + "SERVICE=WMS&REQUEST=GetMap&VERSION=" + ver
                    + "&LAYERS=" + enc(cfg.optString("layers")) + "&STYLES=&FORMAT=" + enc(cfg.optString("format", "image/png"))
                    + "&TRANSPARENT=" + (cfg.optBoolean("transparent", true) ? "TRUE" : "FALSE")
                    + "&WIDTH=256&HEIGHT=256&" + crsKey + "=" + enc(crs) + "&BBOX=" + enc(bbox)
                    + (cfg.optString("time").isEmpty() ? "" : "&TIME=" + enc(cfg.optString("time")));   // WMS time dimension
        }
        if ("arcgis_export".equals(t)) {
            int sr = cfg.optInt("bboxSR", 3857);
            boolean transparent = cfg.optBoolean("transparent", true);
            String fmt = cfg.optString("format", transparent ? "png32" : "jpg");
            double[] b = sr == 4326 ? bounds4326(z, x, y) : bounds3857(z, x, y);
            return url + "?bbox=" + enc(String.format(Locale.ROOT, "%.8f,%.8f,%.8f,%.8f", b[0], b[1], b[2], b[3]))
                    + "&bboxSR=" + sr + "&imageSR=" + cfg.optInt("imageSR", sr) + "&size=256%2C256&f=image&format=" + fmt
                    + "&transparent=" + transparent;
        }
        return null;
    }

    static int[] lonLatToTile(double lng, double lat, int z) {
        lat = Math.max(Math.min(lat, 85.0511), -85.0511);
        double n = Math.pow(2, z);
        int x = (int) ((lng + 180) / 360 * n);
        int y = (int) ((1 - Math.log(Math.tan(Math.toRadians(lat)) + 1 / Math.cos(Math.toRadians(lat))) / Math.PI) / 2 * n);
        return new int[]{(int) Math.min(Math.max(x, 0), n - 1), (int) Math.min(Math.max(y, 0), n - 1)};
    }

    // ───────────────────────── offline areas ─────────────────────────
    class OfflineJob {
        final List<String> layerIds; final double[] bbox; final int zmin, zmax; final String name;
        final long total; final long started = System.currentTimeMillis();
        final AtomicInteger done = new AtomicInteger(), failed = new AtomicInteger(), skipped = new AtomicInteger();
        final AtomicBoolean cancelled = new AtomicBoolean();
        volatile boolean finished = false;

        OfflineJob(List<String> l, double[] b, int z0, int z1, String n) {
            layerIds = l; bbox = b; zmin = z0; zmax = z1; name = n;
            total = countTiles(b, z0, z1) * l.size();
        }

        JSONObject status() {
            try {
                return new JSONObject().put("name", name).put("total", total).put("done", done.get()).put("failed", failed.get())
                        .put("skipped", skipped.get()).put("finished", finished).put("cancelled", cancelled.get())
                        .put("elapsed", (System.currentTimeMillis() - started) / 1000.0).put("error", JSONObject.NULL);
            } catch (JSONException e) { return new JSONObject(); }
        }

        void run() {
            ExecutorService pool = Executors.newFixedThreadPool(6);
            outer:
            for (String l : layerIds) {
                JSONObject cfg = layers.get(l);
                long maxAge = cfg.optBoolean("live") ? LIVE_TILE_MAX_AGE : TILE_MAX_AGE;
                for (int z = zmin; z <= zmax; z++) {
                    int[] a = lonLatToTile(bbox[1], bbox[2], z), b = lonLatToTile(bbox[3], bbox[0], z);
                    for (int x = a[0]; x <= b[0]; x++) for (int y = a[1]; y <= b[1]; y++) {
                        if (cancelled.get()) break outer;
                        final int zz = z, xx = x, yy = y;
                        pool.submit(() -> {
                            if (cancelled.get()) return;
                            Object[] row = getTile(l, zz, xx, yy);
                            if (row != null && System.currentTimeMillis() / 1000 - (long) row[2] < maxAge) skipped.incrementAndGet();
                            else if (downloadTile(l, cfg, zz, xx, yy) != null) done.incrementAndGet();
                            else failed.incrementAndGet();
                        });
                    }
                }
            }
            pool.shutdown();
            try { pool.awaitTermination(1, TimeUnit.DAYS); } catch (InterruptedException ignored) { }
            finished = true;
            saveArea(this);
        }
    }

    static long countTiles(double[] b, int z0, int z1) {
        long t = 0;
        for (int z = z0; z <= z1; z++) {
            int[] a = lonLatToTile(b[1], b[2], z), c = lonLatToTile(b[3], b[0], z);
            t += (long) (c[0] - a[0] + 1) * (c[1] - a[1] + 1);
        }
        return t;
    }

    Resp startOffline(JSONObject p) throws JSONException {
        synchronized (this) {
            if (job != null && !job.finished) return Resp.json(409, "{\"error\":\"Prenos že poteka.\"}");
            List<String> ls = new ArrayList<>();
            JSONArray a = p.optJSONArray("layers");
            for (int i = 0; a != null && i < a.length(); i++) {
                JSONObject c = layers.get(a.getString(i));
                if (c != null && !c.optBoolean("noBulk")) ls.add(a.getString(i));
            }
            if (ls.isEmpty()) return Resp.json(400, "{\"error\":\"Ni izbranih slojev, ki dovoljujejo prenos.\"}");
            JSONArray b = p.getJSONArray("bbox");
            double[] bbox = {b.getDouble(0), b.getDouble(1), b.getDouble(2), b.getDouble(3)};
            int z0 = p.optInt("zmin", 8), z1 = p.optInt("zmax", 15);
            if (z0 < 0 || z0 > z1 || z1 > 19) return Resp.json(400, "{\"error\":\"Neveljaven razpon povečav.\"}");
            OfflineJob j = new OfflineJob(ls, bbox, z0, z1, p.optString("name", "Območje"));
            if (j.total > MAX_BULK_TILES)
                return Resp.json(400, new JSONObject().put("error", "Preveč ploščic (" + j.total + "). Največ " + MAX_BULK_TILES + " — zmanjšaj območje ali povečavo."));
            job = j;
            new Thread(j::run).start();
            return Resp.json(200, j.status());
        }
    }

    void saveArea(OfflineJob j) {
        File f = new File(storeDir, "offline_areas.json");
        try {
            JSONArray areas = f.exists() ? new JSONArray(new String(readAll(new java.io.FileInputStream(f)), StandardCharsets.UTF_8)) : new JSONArray();
            areas.put(new JSONObject().put("name", j.name).put("bbox", new JSONArray(j.bbox)).put("zmin", j.zmin).put("zmax", j.zmax)
                    .put("layers", new JSONArray(j.layerIds)).put("tiles", j.done.get() + j.skipped.get()).put("failed", j.failed.get())
                    .put("cancelled", j.cancelled.get())
                    .put("date", new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.ROOT).format(new java.util.Date())));
            try (FileOutputStream out = new FileOutputStream(f)) { out.write(areas.toString().getBytes(StandardCharsets.UTF_8)); }
        } catch (Exception ignored) { }
    }

    // ───────────────────────── layer health ─────────────────────────
    File healthFile() { return new File(storeDir, "layer_health.json"); }

    void loadHealthFile() {
        try {
            JSONObject o = new JSONObject(new String(readAll(new java.io.FileInputStream(healthFile())), StandardCharsets.UTF_8));
            // results from an older app version may describe different layer sources → check again
            healthChecked = VERSION.equals(o.optString("version")) ? o.optDouble("checked", 0) : 0;
            JSONObject r = o.getJSONObject("results");
            for (java.util.Iterator<String> it = r.keys(); it.hasNext(); ) { String k = it.next(); health.put(k, r.getJSONObject(k)); }
        } catch (Exception ignored) { }
    }

    static boolean isBlankImage(byte[] d) {
        Bitmap b = BitmapFactory.decodeByteArray(d, 0, d.length);
        if (b == null || !b.hasAlpha()) return false;
        int w = b.getWidth(), h = b.getHeight();
        int[] px = new int[w];
        for (int y = 0; y < h; y++) {
            b.getPixels(px, 0, w, 0, y, w, 1);
            for (int p : px) if ((p >>> 24) != 0) { b.recycle(); return false; }
        }
        b.recycle();
        return true;
    }

    List<int[]> sampleTiles(JSONObject cfg) {
        int lo = Math.max(cfg.optInt("minZoom", 0), 9);
        Set<Integer> zs = new java.util.TreeSet<>();
        zs.add(lo); zs.add(Math.min(lo + 3, 17)); zs.add(Math.min(Math.max(lo, 15), 17));
        double[][] pts = {{46.0569, 14.5058}, {46.2389, 14.3556}, {46.5547, 15.6459}};
        List<int[]> out = new ArrayList<>();
        for (int z : zs) for (double[] p : pts) { int[] t = lonLatToTile(p[1], p[0], z); out.add(new int[]{z, t[0], t[1]}); }
        return out;
    }

    JSONObject checkLayer(String id, JSONObject cfg) throws JSONException {
        int valid = 0, blank = 0;
        String detail = "";
        for (int[] t : sampleTiles(cfg)) {
            try {
                Http h = httpGet(buildUrl(cfg, t[0], t[1], t[2]), 6000, 25000);
                if (h.status == 200 && h.ctype.startsWith("image/") && h.data.length > 0) {
                    valid++;
                    putTile(id, t[0], t[1], t[2], h.data, h.ctype);
                    if (isBlankImage(h.data)) blank++;
                    else break;
                } else detail = h.ctype.contains("html") ? "Strežnik vrača stran z napako (vir ne deluje)" : ("HTTP " + h.status + " " + h.ctype).trim();
            } catch (IOException e) { detail = "Ni odziva (" + e.getClass().getSimpleName() + ")"; }
        }
        if (valid == 0) return new JSONObject().put("status", "fail").put("detail", detail.isEmpty() ? "Ni slike" : detail);
        if (valid == blank) return new JSONObject().put("status", "empty").put("detail", "Vir vrača samo prazne slike");
        return new JSONObject().put("status", "ok");
    }

    synchronized void runHealthCheck(boolean force) {
        if (healthRunning || forcedOffline) return;
        if (!force && System.currentTimeMillis() / 1000.0 - healthChecked < 6 * 3600 && health.size() >= rasterIds().size()) return;
        healthRunning = true;
        new Thread(() -> {
            ExecutorService pool = Executors.newFixedThreadPool(4);
            List<Future<?>> fs = new ArrayList<>();
            for (String id : rasterIds()) fs.add(pool.submit(() -> {
                try { health.put(id, checkLayer(id, layers.get(id))); } catch (Exception ignored) { }
            }));
            for (Future<?> f : fs) try { f.get(); } catch (Exception ignored) { }
            pool.shutdown();
            healthChecked = System.currentTimeMillis() / 1000.0;
            healthRunning = false;
            try (FileOutputStream out = new FileOutputStream(healthFile())) {
                JSONObject o = new JSONObject().put("checked", healthChecked).put("version", VERSION);
                synchronized (health) { o.put("results", new JSONObject(health)); }
                out.write(o.toString().getBytes(StandardCharsets.UTF_8));
            } catch (Exception ignored) { }
        }).start();
    }

    List<String> rasterIds() {
        List<String> ids = new ArrayList<>();
        for (Map.Entry<String, JSONObject> e : layers.entrySet()) {
            String t = e.getValue().optString("type");
            if (t.equals("tile") || t.equals("wms") || t.equals("arcgis_export")) ids.add(e.getKey());
        }
        return ids;
    }
}
