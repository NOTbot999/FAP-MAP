package si.francmap.app;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;

/**
 * napProxy for Android — same behaviour as nap_proxy.py on the desktop:
 * whitelist of datasets from web/data/nap_datasets.json (codes can be overridden in store/nap_codes.json),
 * OAuth2 token in memory (renewed 60 s before expiry or on HTTP 401, one retry), 60 s cache per dataset,
 * errors returned as {"error", "status"}. Credentials live in the app's private files dir
 * (nap_credentials.json, NOT in store/), are written only from the settings screen and never returned.
 */
class NapProxy {
    final Backend b;
    final File credFile, codesFile;
    JSONObject cfg;
    String accessToken, refreshToken;
    double expiresAt;
    final Map<String, Object[]> cache = new HashMap<>();   // key → {ts, status, bytes, ctype}

    NapProxy(Backend b) {
        this.b = b;
        this.credFile = new File(b.ctx.getFilesDir(), "nap_credentials.json");
        this.codesFile = new File(b.storeDir, "nap_codes.json");
        try (InputStream in = b.assets.open("data/nap_datasets.json")) {
            cfg = new JSONObject(new String(Backend.readAll(in), StandardCharsets.UTF_8));
        } catch (Exception e) { cfg = new JSONObject(); }
    }

    static JSONObject readJson(File f) {
        try (InputStream in = new FileInputStream(f)) { return new JSONObject(new String(Backend.readAll(in), StandardCharsets.UTF_8)); }
        catch (Exception e) { return null; }
    }
    static void writeJson(File f, JSONObject o) throws Exception {
        try (FileOutputStream out = new FileOutputStream(f)) { out.write(o.toString().getBytes(StandardCharsets.UTF_8)); }
    }

    JSONObject datasets() throws Exception {
        JSONObject ds = new JSONObject(cfg.optJSONObject("DATASETS") == null ? "{}" : cfg.getJSONObject("DATASETS").toString());
        JSONObject codes = readJson(codesFile);
        if (codes != null) for (Iterator<String> it = codes.keys(); it.hasNext(); ) {
            String k = it.next();
            if (ds.has(k)) ds.getJSONObject(k).put("code", codes.optString(k, "").trim());
        }
        return ds;
    }

    String[] credentials() {
        JSONObject c = readJson(credFile);
        if (c == null || c.optString("user").isEmpty() || c.optString("pass").isEmpty()) return null;
        return new String[]{c.optString("user"), c.optString("pass")};
    }

    synchronized JSONObject status() throws Exception {
        String[] cr = credentials();
        JSONObject o = new JSONObject();
        o.put("configured", cr != null);
        o.put("source", cr != null ? "file" : JSONObject.NULL);
        o.put("user", cr == null ? JSONObject.NULL : (cr[0].length() > 5 ? cr[0].substring(0, 2) + "…" + cr[0].substring(cr[0].length() - 2) : "…"));
        o.put("attribution", cfg.optString("atribucija"));
        JSONObject ds = datasets(), out = new JSONObject();
        for (Iterator<String> it = ds.keys(); it.hasNext(); ) {
            String k = it.next();
            JSONObject d = ds.getJSONObject(k);
            out.put(k, new JSONObject().put("ime", d.optString("ime")).put("code", d.optString("code"))
                    .put("configured", !d.optString("code").isEmpty()).put("osvezi", d.optInt("osvezi", 300))
                    .put("vklop", d.optBoolean("vklop", false)).put("tip", d.optString("tip", "geojson")).put("opomba", d.optString("opomba")));
        }
        o.put("datasets", out);
        return o;
    }

    synchronized void setCodes(JSONObject codes) throws Exception {
        JSONObject ds = cfg.optJSONObject("DATASETS"), clean = new JSONObject();
        if (codes != null && ds != null) for (Iterator<String> it = codes.keys(); it.hasNext(); ) {
            String k = it.next();
            if (ds.has(k)) clean.put(k, codes.optString(k, "").trim());
        }
        writeJson(codesFile, clean);
        cache.clear();
    }

    synchronized void setCredentials(String user, String pass) throws Exception {
        if (user == null || user.isEmpty() || pass == null || pass.isEmpty()) credFile.delete();
        else writeJson(credFile, new JSONObject().put("user", user).put("pass", pass));
        accessToken = null; refreshToken = null;
        cache.clear();
    }

    // ── OAuth2 ──
    void tokenRequest(String form) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(cfg.optString("tokenUrl")).openConnection();
        c.setConnectTimeout(8000); c.setReadTimeout(30000);
        c.setRequestMethod("POST"); c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");
        c.setRequestProperty("User-Agent", Backend.UA);
        c.getOutputStream().write(form.getBytes(StandardCharsets.UTF_8));
        Backend.Http h = Backend.finish(c);
        if (h.status != 200) throw new SecurityException("prijava v NAP ni uspela (HTTP " + h.status + ")");
        JSONObject j = new JSONObject(new String(h.data, StandardCharsets.UTF_8));
        accessToken = j.getString("access_token");
        refreshToken = j.optString("refresh_token", null);
        expiresAt = System.currentTimeMillis() / 1000.0 + j.optDouble("expires_in", 3600);
    }

    String ensureToken(boolean force) throws Exception {
        if (accessToken != null && !force && System.currentTimeMillis() / 1000.0 < expiresAt - 60) return accessToken;
        if (accessToken != null && refreshToken != null) {
            try { tokenRequest("grant_type=refresh_token&refresh_token=" + URLEncoder.encode(refreshToken, "UTF-8")); return accessToken; }
            catch (Exception e) { accessToken = null; }
        }
        String[] cr = credentials();
        if (cr == null) throw new IllegalStateException("NAP prijava ni nastavljena (Nastavitve → Promet v živo)");
        tokenRequest("grant_type=password&username=" + URLEncoder.encode(cr[0], "UTF-8") + "&password=" + URLEncoder.encode(cr[1], "UTF-8"));
        return accessToken;
    }

    Backend.Http get(String code, String token, String method) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(cfg.optString("dataUrl") + code).openConnection();
        c.setConnectTimeout(8000); c.setReadTimeout(40000);
        c.setRequestMethod(method);
        c.setRequestProperty("Authorization", "bearer " + token);
        c.setRequestProperty("Accept", "application/json");
        c.setRequestProperty("User-Agent", Backend.UA);
        return Backend.finish(c);
    }

    static Backend.Resp err(int status, String msg) {
        try { return Backend.Resp.json(status, new JSONObject().put("error", msg).put("status", status)); }
        catch (Exception e) { return Backend.Resp.json(status, "{\"status\":" + status + "}"); }
    }

    synchronized Backend.Resp fetch(String key) {
        try {
            JSONObject ds = datasets();
            if (key == null || !ds.has(key)) return err(400, "neznan sloj");
            String code = ds.getJSONObject(key).optString("code", "");
            if (code.isEmpty()) return err(400, "ni nastavljeno (manjka koda nabora)");
            Object[] hit = cache.get(key);
            long ttl = cfg.optLong("cacheSekund", 60) * 1000;
            if (hit != null && System.currentTimeMillis() - (long) hit[0] < ttl) return new Backend.Resp((int) hit[1], (String) hit[3], (byte[]) hit[2]);
            Backend.Http h = get(code, ensureToken(false), "GET");
            if (h.status == 405) h = get(code, ensureToken(false), "POST");
            if (h.status == 401) h = get(code, ensureToken(true), "GET");
            if (h.status != 200) {
                String msg = h.status == 401 ? "prijava deluje, dostop do nabora pa še ni odobren (nap.si → profil → Pravice)"
                        : h.status == 403 ? "ni dostopa do nabora ali napačna koda (preveri kodo in Pravice na nap.si)"
                        : h.status == 404 ? "nabor ne obstaja (preveri kodo)" : "NAP HTTP " + h.status;
                return err(h.status, msg);
            }
            String ct = h.ctype.isEmpty() ? "application/json" : h.ctype;
            cache.put(key, new Object[]{System.currentTimeMillis(), 200, h.data, ct});
            return new Backend.Resp(200, ct, h.data);
        } catch (IllegalStateException e) {
            return err(503, e.getMessage());
        } catch (SecurityException e) {
            return err(401, e.getMessage());
        } catch (java.io.IOException e) {
            return err(502, "NAP ni dosegljiv (" + e.getClass().getSimpleName() + ")");
        } catch (Exception e) {
            return err(500, "napaka: " + e.getMessage());
        }
    }
}
