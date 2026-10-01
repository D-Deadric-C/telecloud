package com.suryansh.telecloud;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.os.Environment;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.URLUtil;
import android.widget.ProgressBar;
import android.widget.Toast;

public final class MainActivity extends Activity {
    private static final String HOME_URL = "https://telecloud.web.app";
    private static final int FILE_CHOOSER_REQUEST = 10;

    private WebView webView;
    private ProgressBar progress;
    private View errorPanel;
    private ValueCallback<Uri[]> fileChooserCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.web_view);
        progress = findViewById(R.id.progress);
        errorPanel = findViewById(R.id.error_panel);
        findViewById(R.id.retry_button).setOnClickListener(view -> loadHome());

        configureWebView();
        if (savedInstanceState == null) {
            loadHome();
        } else {
            webView.restoreState(savedInstanceState);
        }
    }

    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(true);

        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                errorPanel.setVisibility(View.GONE);
                webView.setVisibility(View.VISIBLE);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("https".equals(uri.getScheme()) && "telecloud.web.app".equals(uri.getHost())) {
                    return false;
                }
                openExternal(uri);
                return true;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) showOffline();
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel();
                showOffline();
            }
        });

        webView.addJavascriptInterface(new TeleCloudBridge(), "TeleCloudAndroid");
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progress.setProgress(newProgress);
                progress.setVisibility(newProgress < 100 ? View.VISIBLE : View.GONE);
            }

            @Override
            public boolean onShowFileChooser(
                    WebView view,
                    ValueCallback<Uri[]> callback,
                    FileChooserParams params
            ) {
                if (fileChooserCallback != null) fileChooserCallback.onReceiveValue(null);
                fileChooserCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), FILE_CHOOSER_REQUEST);
                    return true;
                } catch (Exception exception) {
                    fileChooserCallback = null;
                    Toast.makeText(MainActivity.this, "No file picker is available.", Toast.LENGTH_SHORT).show();
                    return false;
                }
            }
        });

        webView.setDownloadListener(new TeleCloudDownloadListener());
    }

    private void loadHome() {
        errorPanel.setVisibility(View.GONE);
        webView.setVisibility(View.VISIBLE);
        if (hasNetwork()) {
            webView.loadUrl(HOME_URL);
        } else {
            showOffline();
        }
    }

    private boolean hasNetwork() {
        ConnectivityManager manager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        NetworkCapabilities capabilities = manager.getNetworkCapabilities(manager.getActiveNetwork());
        return capabilities != null && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
    }

    private void showOffline() {
        progress.setVisibility(View.GONE);
        webView.setVisibility(View.GONE);
        errorPanel.setVisibility(View.VISIBLE);
    }

    private void openExternal(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (Exception exception) {
            Toast.makeText(this, "No app can open this link.", Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_CHOOSER_REQUEST || fileChooserCallback == null) return;
        Uri[] result = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
        fileChooserCallback.onReceiveValue(result);
        fileChooserCallback = null;
    }

    @Override
    protected void onSaveInstanceState(Bundle state) {
        webView.saveState(state);
        super.onSaveInstanceState(state);
    }

    @Override
    protected void onDestroy() {
        if (fileChooserCallback != null) fileChooserCallback.onReceiveValue(null);
        webView.stopLoading();
        webView.setWebChromeClient(null);
        webView.setWebViewClient(null);
        webView.destroy();
        super.onDestroy();
    }

    private final class TeleCloudDownloadListener implements DownloadListener {
        @Override
        public void onDownloadStart(
                String url,
                String userAgent,
                String contentDisposition,
                String mimeType,
                long contentLength
        ) {
            if (!url.startsWith("https://")) return;
            try {
                DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
                request.setMimeType(mimeType);
                request.addRequestHeader("User-Agent", userAgent);
                String cookies = CookieManager.getInstance().getCookie(url);
                if (cookies != null) request.addRequestHeader("Cookie", cookies);
                request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                String filename = URLUtil.guessFileName(url, contentDisposition, mimeType);
                request.setTitle(filename);
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename);
                DownloadManager manager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                manager.enqueue(request);
                Toast.makeText(MainActivity.this, "Download started.", Toast.LENGTH_SHORT).show();
            } catch (Exception exception) {
                openExternal(Uri.parse(url));
            }
        }
    }

    private final class TeleCloudBridge {
        @JavascriptInterface
        public void download(String url, String accessToken, String requestedName) {
            Uri uri = Uri.parse(url);
            if (!"https".equals(uri.getScheme())
                    || !"telecloud-api-787502128394.asia-southeast1.run.app".equals(uri.getHost())
                    || !uri.getPath().startsWith("/files/")
                    || accessToken == null
                    || accessToken.isEmpty()
                    || accessToken.length() > 10000) {
                return;
            }

            String safeName = requestedName == null
                    ? "TeleCloud-file"
                    : requestedName.replaceAll("[\\\\/:*?\"<>|]", "_").trim();
            if (safeName.isEmpty()) safeName = "TeleCloud-file";

            final String filename = safeName;
            runOnUiThread(() -> {
                try {
                    DownloadManager.Request request = new DownloadManager.Request(uri);
                    request.addRequestHeader("Authorization", "Bearer " + accessToken);
                    request.setTitle(filename);
                    request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename);
                    DownloadManager manager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                    manager.enqueue(request);
                    Toast.makeText(MainActivity.this, "Download started.", Toast.LENGTH_SHORT).show();
                } catch (Exception exception) {
                    Toast.makeText(MainActivity.this, "Download could not start.", Toast.LENGTH_SHORT).show();
                }
            });
        }
    }
}
