// Source of the Talk surface patched into the official Hermes Agent Android app.
// Built with the portable Android SDK (build-tools 37), dexed with d8, converted
// back to smali with baksmali, and merged into the official decompiled apktool
// project. Kept here so the same source can be offered upstream as a PR.
package ai.nousresearch.hermes.mobile;

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
 * Native entry point for the Hermes Talk dashboard surface inside the official app.
 *
 * <p>Hosts the dashboard's /talk page in a WebView, grants its getUserMedia
 * request (the app already holds RECORD_AUDIO), persists the dashboard login
 * cookie, and injects the optional remote-dashboard token into the page's
 * sessionStorage. Launched from its own "Hermes Talk" home-screen icon.
 *
 * <p>Optional one-time extras (remembered after first use):
 * <pre>adb shell am start -n ai.nousresearch.hermes.mobile/.TalkActivity \
 *   --es url https://your-dashboard.tailnet.ts.net/talk \
 *   --es talk_token &lt;TALK_DASHBOARD_TOKEN&gt;</pre>
 */
public final class TalkActivity extends Activity {
    private static final String TAG = "HmTalk";
    private static final String DEFAULT_TALK_URL = "https://your-dashboard.tailnet.ts.net/talk";
    private static final String PREFS = "hermes_talk_official";
    private static final String PREF_URL = "dashboard_url";
    private static final String PREF_TOKEN = "dashboard_token";
    private static final String STORAGE_KEY = "hermes-talk-dashboard-token";
    private static final int MIC_REQUEST = 513;

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

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                Log.i(TAG, "onPageFinished injected=" + tokenInjected);
                if (pendingToken == null || tokenInjected) {
                    return;
                }
                tokenInjected = true;
                String js = "try{window.sessionStorage.setItem('" + STORAGE_KEY + "','"
                        + pendingToken.replace("\\", "\\\\").replace("'", "\\'") + "');}catch(e){}";
                view.evaluateJavascript(js, value -> view.postDelayed(() -> view.reload(), 300));
            }
        });

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
