package si.francmap.app;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.provider.OpenableColumns;
import android.webkit.GeolocationPermissions;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/**
 * Hosts the shared Franc Map web UI in a WebView. Every request to *.francmap.local is answered
 * locally by {@link Backend} (static assets from the APK, tiles from the offline cache, API calls).
 * POST bodies can't be intercepted by a WebView, so the UI sends API calls through the async
 * JavaScript bridge `window.FrancAndroid` instead; results come back via `window.__fmResolve`.
 */
public class MainActivity extends Activity {
    static final String HOST = "app.francmap.local";
    static final int REQ_SAVE = 11, REQ_OPEN = 12, REQ_LOCATION = 13;

    WebView web;
    Backend backend;

    // pending native-dialog state
    String saveId, saveContent, openId;
    String geoOrigin;
    GeolocationPermissions.Callback geoCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        backend = new Backend(this);
        web = new WebView(this);
        web.setBackgroundColor(0xFF0B1220);   // app navy instead of black while the page loads
        setContentView(web);

        if ((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0) WebView.setWebContentsDebuggingEnabled(true);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setGeolocationEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportZoom(false);
        s.setMediaPlaybackRequiresUserGesture(false);   // AR camera preview autoplays
        s.setTextZoom(100);
        s.setUserAgentString(s.getUserAgentString() + " FrancMapAndroid/" + Backend.VERSION);

        web.addJavascriptInterface(new Bridge(), "FrancAndroid");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                String h = u.getHost();
                if (h != null && h.endsWith("francmap.local") && "GET".equals(req.getMethod())) return backend.intercept(u);
                return null;
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                android.util.Log.i("FrancMap", "page started " + url);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                android.util.Log.i("FrancMap", "page finished " + url);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, android.webkit.WebResourceError err) {
                android.util.Log.w("FrancMap", "load error " + req.getUrl() + " : " + err.getErrorCode() + " " + err.getDescription());
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest req, WebResourceResponse resp) {
                if (!req.getUrl().getPath().startsWith("/tiles/"))
                    android.util.Log.w("FrancMap", "http " + resp.getStatusCode() + " " + req.getUrl());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if (u.getHost() != null && u.getHost().endsWith("francmap.local")) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
                return true;   // external links (Wikipedia, websites, attributions) open in the browser
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(android.webkit.ConsoleMessage m) {
                android.util.Log.i("FrancMapJS", m.messageLevel() + " " + m.message() + " @" + m.sourceId() + ":" + m.lineNumber());
                return true;
            }

            // AR pogled is disabled: the app never needs the camera or microphone
            @Override
            public void onPermissionRequest(android.webkit.PermissionRequest request) {
                runOnUiThread(request::deny);
            }

            @Override
            public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
                if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
                    callback.invoke(origin, true, false);
                } else {
                    geoOrigin = origin;
                    geoCallback = callback;
                    requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_LOCATION);
                }
            }
        });

        if (savedInstanceState != null) web.restoreState(savedInstanceState);
        else web.loadUrl("https://" + HOST + "/index.html?t=" + backend.token);
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    public void onBackPressed() {
        // let the UI close panels / popups / tools first
        web.evaluateJavascript("window.fmBack ? fmBack() : false", v -> {
            if (!"true".equals(v)) MainActivity.super.onBackPressed();
        });
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (code == REQ_LOCATION && geoCallback != null) {
            boolean ok = results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED;
            geoCallback.invoke(geoOrigin, ok, false);
            geoCallback = null;
        }
    }

    void resolve(String id, int status, String text) {
        String js = "window.__fmResolve && window.__fmResolve(" + JSONObject.quote(id) + "," + status + "," + JSONObject.quote(text) + ")";
        runOnUiThread(() -> web.evaluateJavascript(js, null));
    }

    class Bridge {
        @JavascriptInterface
        public String token() { return backend.token; }

        @JavascriptInterface
        public void request(String id, String method, String path, String body) {
            backend.io.submit(() -> {
                Backend.Resp r = backend.api(method, path, body);
                resolve(id, r.status, r.text());
            });
        }

        @JavascriptInterface
        public void saveText(String id, String filename, String content) {
            runOnUiThread(() -> {
                saveId = id;
                saveContent = content;
                Intent i = new Intent(Intent.ACTION_CREATE_DOCUMENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType(mimeFor(filename));
                i.putExtra(Intent.EXTRA_TITLE, filename);
                startActivityForResult(i, REQ_SAVE);
            });
        }

        @JavascriptInterface
        public void openText(String id) {
            runOnUiThread(() -> {
                openId = id;
                Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                startActivityForResult(i, REQ_OPEN);
            });
        }

        @JavascriptInterface
        public void copy(String text) {
            runOnUiThread(() -> {
                android.content.ClipboardManager cm = (android.content.ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
                cm.setPrimaryClip(android.content.ClipData.newPlainText("Franc Map", text));
            });
        }

        @JavascriptInterface
        public void toast(String msg) {
            runOnUiThread(() -> Toast.makeText(MainActivity.this, msg, Toast.LENGTH_SHORT).show());
        }
    }

    static String mimeFor(String name) {
        String n = name.toLowerCase();
        if (n.endsWith(".gpx")) return "application/gpx+xml";
        if (n.endsWith(".kml")) return "application/vnd.google-earth.kml+xml";
        if (n.endsWith(".geojson") || n.endsWith(".json")) return "application/json";
        return "text/plain";
    }

    @Override
    protected void onActivityResult(int code, int result, Intent data) {
        super.onActivityResult(code, result, data);
        if (code == REQ_SAVE) {
            String id = saveId, content = saveContent;
            saveId = null; saveContent = null;
            if (result != RESULT_OK || data == null || data.getData() == null) { resolve(id, 200, "null"); return; }
            Uri uri = data.getData();
            backend.io.submit(() -> {
                try (OutputStream out = getContentResolver().openOutputStream(uri, "wt")) {
                    out.write(content.getBytes(StandardCharsets.UTF_8));
                    resolve(id, 200, JSONObject.quote(displayName(uri)));
                } catch (Exception e) { resolve(id, 500, JSONObject.quote(String.valueOf(e))); }
            });
        } else if (code == REQ_OPEN) {
            String id = openId;
            openId = null;
            if (result != RESULT_OK || data == null) { resolve(id, 200, "[]"); return; }
            backend.io.submit(() -> {
                JSONArray arr = new JSONArray();
                try {
                    if (data.getClipData() != null) {
                        for (int i = 0; i < data.getClipData().getItemCount(); i++) arr.put(readDoc(data.getClipData().getItemAt(i).getUri()));
                    } else if (data.getData() != null) arr.put(readDoc(data.getData()));
                    resolve(id, 200, arr.toString());
                } catch (Exception e) { resolve(id, 500, JSONObject.quote(String.valueOf(e))); }
            });
        }
    }

    JSONObject readDoc(Uri uri) throws Exception {
        try (InputStream in = getContentResolver().openInputStream(uri)) {
            return new JSONObject().put("name", displayName(uri)).put("content", new String(Backend.readAll(in), StandardCharsets.UTF_8));
        }
    }

    String displayName(Uri uri) {
        try (Cursor c = getContentResolver().query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (c != null && c.moveToFirst()) return c.getString(0);
        } catch (Exception ignored) { }
        return uri.getLastPathSegment();
    }
}
