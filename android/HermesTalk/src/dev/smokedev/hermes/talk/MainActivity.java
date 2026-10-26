package dev.smokedev.hermes.talk;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.util.Log;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * Minimal installable shell for the Hermes Talk dashboard surface.
 *
 * <p>The Talk surface is a browser WebRTC app served by the Hermes dashboard (the
 * hermes-talk plugin exposes it as the /talk tab). This activity hosts it in an
 * Android WebView, grants the WebView's audio-capture request to the page (the
 * app itself holds RECORD_AUDIO), enables DOM storage for the dashboard login
 * cookie, and puts it on the home screen as "Hermes Talk".
 *
 * <p>Two things can be supplied once via intent extras and are then remembered:
 * <pre>adb shell am start -n dev.smokedev.hermes.talk/.MainActivity \
 *   --es url https://your-dashboard.tailnet.ts.net/talk \
 *   --es talk_token &lt;TALK_DASHBOARD_TOKEN value&gt;</pre>
 *
 * <p>The Talk page keeps its dashboard token in sessionStorage
 * ("hermes-talk-dashboard-token"); when a token is configured here the WebView
 * injects it after the first page load and reloads once, so Talk's remote
 * (non-loopback) dashboard routes authorize without a manual paste.
 */
public final class MainActivity extends Activity {
    private static final String TAG = "HermesTalk";
    static final String DEFAULT_TALK_URL = "https://your-dashboard.tailnet.ts.net/talk";
    private static final String PREFS = "hermes_talk";
    private static final String PREF_URL = "dashboard_url";
    private static final String PREF_TOKEN = "dashboard_token";
    private static final String STORAGE_KEY = "hermes-talk-dashboard-token";
    private static final int MIC_REQUEST = 1;

    private WebView webView;
    private String pendingToken;
    private boolean tokenInjected;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC_REQUEST);
        }

        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        applyIntentExtras(prefs, getIntent());
        pendingToken = prefs.getString(PREF_TOKEN, "");
        if (pendingToken != null && pendingToken.isEmpty()) {
            pendingToken = null;
        }
        Log.i(TAG, "onCreate tokenConfigured=" + (pendingToken != null));

        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                Log.i(TAG, "onPageFinished injected=" + tokenInjected + " url=" + url);
                if (pendingToken == null || tokenInjected) {
                    return;
                }
                tokenInjected = true;
                String js = "try{window.sessionStorage.setItem('" + STORAGE_KEY + "','"
                        + pendingToken.replace("\\", "\\\\").replace("'", "\\'") + "');}catch(e){}";
                view.evaluateJavascript(js, value -> {
                    Log.i(TAG, "token injected into sessionStorage");
                    view.postDelayed(() -> view.reload(), 300);
                });
            }
        });

        // The Talk page asks for microphone access through getUserMedia. A WebView
        // only gets it when the host page is granted the request; the app's own
        // RECORD_AUDIO grant is also required (requested above).
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                Log.i(TAG, "onPermissionRequest granting " + java.util.Arrays.toString(request.getResources()));
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        request.grant(request.getResources());
                    }
                });
            }

            @Override
            public void onPermissionRequestCanceled(final PermissionRequest request) {
                // Nothing to release: no special resources are held here.
            }
        });

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, true);

        setContentView(webView);
        webView.loadUrl(talkUrl(prefs));
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        applyIntentExtras(prefs, intent);
        String token = prefs.getString(PREF_TOKEN, "");
        if (token != null && !token.isEmpty()) {
            pendingToken = token;
            tokenInjected = false;
            if (webView != null) {
                webView.reload();
            }
        }
    }

    private static void applyIntentExtras(SharedPreferences prefs, Intent intent) {
        if (intent == null) {
            return;
        }
        String urlOverride = intent.getStringExtra("url");
        if (urlOverride != null && !urlOverride.isEmpty()) {
            prefs.edit().putString(PREF_URL, urlOverride).apply();
        }
        String tokenOverride = intent.getStringExtra("talk_token");
        if (tokenOverride != null && !tokenOverride.isEmpty()) {
            prefs.edit().putString(PREF_TOKEN, tokenOverride).apply();
        }
    }

    private static String talkUrl(SharedPreferences prefs) {
        String url = prefs.getString(PREF_URL, DEFAULT_TALK_URL);
        return url == null || url.isEmpty() ? DEFAULT_TALK_URL : url;
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.loadUrl("about:blank");
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
