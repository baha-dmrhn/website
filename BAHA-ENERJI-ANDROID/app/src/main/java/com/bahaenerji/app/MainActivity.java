package com.bahaenerji.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PorterDuff;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.ArrayDeque;
import java.util.Deque;

public class MainActivity extends Activity {
    private static final int FILE_CHOOSER_REQUEST_CODE = 1107;
    private static final String APP_USER_AGENT = "BahaEnerjiAndroid/2.0";
    private static final String[] TAB_PATHS = {
            "/piyasa/",
            "/baraj/",
            "/uretim/",
            "/tuketim/",
            "/sistem-yonu-tahmini/"
    };
    private static final String[] TAB_ICONS = {"₺", "≈", "↕", "⚡", "⌁"};
    private static final String[] TAB_LABELS = {
            "Piyasa",
            "Baraj",
            "Üretim",
            "Tüketim",
            "Tahmin"
    };

    private WebView webView;
    private ProgressBar progressBar;
    private LinearLayout appLayout;
    private LinearLayout appBar;
    private LinearLayout splashView;
    private LinearLayout errorView;
    private LinearLayout bottomNavigation;
    private TextView appBrandTitle;
    private TextView appSectionTitle;
    private TextView appMenuButton;
    private TextView splashTitle;
    private TextView splashMessage;
    private TextView errorTitle;
    private TextView errorMessage;
    private Button retryButton;
    private TextView[] navigationItems;
    private ValueCallback<Uri[]> filePathCallback;
    private String appHost;
    private boolean nativeDarkTheme;
    private int selectedTabIndex = -1;
    private final Deque<Integer> tabHistory = new ArrayDeque<>();
    private String currentPath = "/";
    private long lastBackPressedAt;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        appHost = hostOf(BuildConfig.SITE_URL);
        nativeDarkTheme = getSharedPreferences(
                "baha_native_preferences",
                MODE_PRIVATE
        ).getBoolean("dark_theme", false);
        buildUi();
        configureWebView();
        loadApp();
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setUserAgentString(
                settings.getUserAgentString() + " " + APP_USER_AGENT
        );

        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);
        cookieManager.setAcceptThirdPartyCookies(webView, true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (isAppUrl(uri)) return false;
                openExternal(uri);
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                String title = view.getTitle();
                if (title == null || !title.toLowerCase().contains("baha")) {
                    progressBar.setVisibility(View.VISIBLE);
                    splashView.setVisibility(View.VISIBLE);
                    webView.setVisibility(View.INVISIBLE);
                    return;
                }
                updateNativeChrome(url);
                syncNativeTheme();
                progressBar.setVisibility(View.GONE);
                splashView.setVisibility(View.GONE);
                errorView.setVisibility(View.GONE);
                webView.setVisibility(View.VISIBLE);
                CookieManager.getInstance().flush();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) showError();
            }

            @Override
            public void onReceivedHttpError(
                    WebView view,
                    WebResourceRequest request,
                    WebResourceResponse errorResponse
            ) {
                if (request.isForMainFrame() && errorResponse.getStatusCode() >= 400) showError();
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progressBar.setProgress(newProgress);
                progressBar.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
            }

            @Override
            public boolean onShowFileChooser(
                    WebView webView,
                    ValueCallback<Uri[]> filePathCallback,
                    FileChooserParams fileChooserParams
            ) {
                if (MainActivity.this.filePathCallback != null) {
                    MainActivity.this.filePathCallback.onReceiveValue(null);
                }
                MainActivity.this.filePathCallback = filePathCallback;
                Intent intent = fileChooserParams.createIntent();
                try {
                    startActivityForResult(intent, FILE_CHOOSER_REQUEST_CODE);
                } catch (ActivityNotFoundException exception) {
                    MainActivity.this.filePathCallback = null;
                    Toast.makeText(MainActivity.this, "Dosya seçici açılamadı.", Toast.LENGTH_SHORT).show();
                    return false;
                }
                return true;
            }
        });

        webView.setDownloadListener(downloadListener());
    }

    private DownloadListener downloadListener() {
        return (url, userAgent, contentDisposition, mimeType, contentLength) -> {
            try {
                String fileName = URLUtil.guessFileName(url, contentDisposition, mimeType);
                DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
                request.addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url));
                request.addRequestHeader("User-Agent", userAgent);
                request.setTitle(fileName);
                request.setDescription("Baha Enerji dosyası indiriliyor");
                request.setMimeType(mimeType);
                request.setNotificationVisibility(
                        DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED
                );
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName);
                DownloadManager manager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                manager.enqueue(request);
                Toast.makeText(this, "İndirme başladı: " + fileName, Toast.LENGTH_SHORT).show();
            } catch (Exception exception) {
                openExternal(Uri.parse(url));
            }
        };
    }

    private void buildUi() {
        FrameLayout root = new FrameLayout(this);

        appLayout = new LinearLayout(this);
        appLayout.setOrientation(LinearLayout.VERTICAL);
        appLayout.setBackgroundColor(Color.rgb(244, 247, 251));
        root.addView(appLayout, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));

        appLayout.addView(buildAppBar(), new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(68)
        ));

        FrameLayout webContainer = new FrameLayout(this);
        LinearLayout.LayoutParams webContainerParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1
        );
        appLayout.addView(webContainer, webContainerParams);

        webView = new WebView(this);
        webView.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));
        webView.setBackgroundColor(Color.rgb(244, 247, 251));
        webView.setVisibility(View.INVISIBLE);
        webContainer.addView(webView);

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        FrameLayout.LayoutParams progressParams = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(3)
        );
        progressParams.gravity = Gravity.TOP;
        webContainer.addView(progressBar, progressParams);

        bottomNavigation = buildBottomNavigation();
        bottomNavigation.setVisibility(View.GONE);
        appLayout.addView(bottomNavigation, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(72)
        ));

        errorView = new LinearLayout(this);
        errorView.setOrientation(LinearLayout.VERTICAL);
        errorView.setGravity(Gravity.CENTER);
        errorView.setPadding(dp(24), dp(24), dp(24), dp(24));
        errorView.setBackgroundColor(Color.rgb(244, 247, 251));
        errorView.setVisibility(View.GONE);

        errorTitle = new TextView(this);
        errorTitle.setText(getString(com.bahaenerji.app.R.string.offline_title));
        errorTitle.setTextSize(24);
        errorTitle.setGravity(Gravity.CENTER);
        errorTitle.setTypeface(null, 1);

        errorMessage = new TextView(this);
        errorMessage.setText(getString(com.bahaenerji.app.R.string.offline_message));
        errorMessage.setTextSize(15);
        errorMessage.setGravity(Gravity.CENTER);
        errorMessage.setPadding(0, dp(10), 0, dp(18));

        retryButton = new Button(this);
        retryButton.setText(getString(com.bahaenerji.app.R.string.retry));
        retryButton.setTextColor(Color.WHITE);
        retryButton.setOnClickListener(view -> loadApp());

        errorView.addView(errorTitle);
        errorView.addView(errorMessage);
        errorView.addView(retryButton, new LinearLayout.LayoutParams(dp(180), dp(48)));
        root.addView(errorView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));

        splashView = new LinearLayout(this);
        splashView.setOrientation(LinearLayout.VERTICAL);
        splashView.setGravity(Gravity.CENTER);
        splashView.setPadding(dp(28), dp(28), dp(28), dp(28));
        splashView.setBackgroundColor(Color.rgb(11, 25, 48));

        ImageView splashLogo = new ImageView(this);
        splashLogo.setImageResource(com.bahaenerji.app.R.drawable.baha_logo);
        splashLogo.setAdjustViewBounds(true);
        splashLogo.setPadding(dp(10), dp(10), dp(10), dp(10));
        splashLogo.setBackgroundColor(Color.WHITE);
        LinearLayout.LayoutParams logoParams = new LinearLayout.LayoutParams(dp(92), dp(92));
        logoParams.bottomMargin = dp(22);

        splashTitle = new TextView(this);
        splashTitle.setText("Baha Enerji");
        splashTitle.setTextColor(Color.WHITE);
        splashTitle.setTextSize(28);
        splashTitle.setGravity(Gravity.CENTER);
        splashTitle.setTypeface(null, 1);

        splashMessage = new TextView(this);
        splashMessage.setText("Panel hazırlanıyor...");
        splashMessage.setTextColor(Color.rgb(171, 190, 220));
        splashMessage.setTextSize(15);
        splashMessage.setGravity(Gravity.CENTER);
        splashMessage.setPadding(0, dp(8), 0, 0);

        splashView.addView(splashLogo, logoParams);
        splashView.addView(splashTitle);
        splashView.addView(splashMessage);
        root.addView(splashView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));

        setContentView(root);
        applyNativeTheme(nativeDarkTheme);
    }

    private View buildAppBar() {
        appBar = new LinearLayout(this);
        appBar.setOrientation(LinearLayout.HORIZONTAL);
        appBar.setGravity(Gravity.CENTER_VERTICAL);
        appBar.setPadding(dp(14), dp(8), dp(10), dp(8));
        appBar.setBackgroundColor(Color.rgb(11, 25, 48));
        appBar.setElevation(dp(8));

        ImageView logo = new ImageView(this);
        logo.setImageResource(com.bahaenerji.app.R.drawable.baha_logo);
        logo.setScaleType(ImageView.ScaleType.CENTER_INSIDE);
        logo.setPadding(dp(5), dp(5), dp(5), dp(5));
        logo.setBackground(roundedBackground(Color.WHITE, 13));
        appBar.addView(logo, new LinearLayout.LayoutParams(dp(44), dp(44)));

        LinearLayout titles = new LinearLayout(this);
        titles.setOrientation(LinearLayout.VERTICAL);
        titles.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(
                0,
                ViewGroup.LayoutParams.MATCH_PARENT,
                1
        );
        titleParams.leftMargin = dp(12);

        appBrandTitle = new TextView(this);
        appBrandTitle.setText("BAHA ENERJİ");
        appBrandTitle.setTextSize(10);
        appBrandTitle.setLetterSpacing(0.14f);
        appBrandTitle.setTypeface(Typeface.DEFAULT, Typeface.BOLD);

        appSectionTitle = new TextView(this);
        appSectionTitle.setText("Güvenli Giriş");
        appSectionTitle.setTextColor(Color.WHITE);
        appSectionTitle.setTextSize(17);
        appSectionTitle.setSingleLine(true);
        appSectionTitle.setTypeface(Typeface.DEFAULT, Typeface.BOLD);

        titles.addView(appBrandTitle);
        titles.addView(appSectionTitle);
        appBar.addView(titles, titleParams);

        appMenuButton = new TextView(this);
        appMenuButton.setText("⋮");
        appMenuButton.setTextSize(26);
        appMenuButton.setGravity(Gravity.CENTER);
        appMenuButton.setContentDescription("Uygulama menüsü");
        appMenuButton.setOnClickListener(this::showAppMenu);
        appBar.addView(appMenuButton, new LinearLayout.LayoutParams(dp(44), dp(44)));
        return appBar;
    }

    private LinearLayout buildBottomNavigation() {
        LinearLayout navigation = new LinearLayout(this);
        navigation.setOrientation(LinearLayout.HORIZONTAL);
        navigation.setGravity(Gravity.CENTER);
        navigation.setPadding(dp(6), dp(6), dp(6), dp(6));
        navigation.setElevation(dp(14));
        navigationItems = new TextView[TAB_PATHS.length];

        for (int index = 0; index < TAB_PATHS.length; index++) {
            final int tabIndex = index;
            TextView item = new TextView(this);
            item.setText(TAB_ICONS[index] + "\n" + TAB_LABELS[index]);
            item.setTextSize(10);
            item.setGravity(Gravity.CENTER);
            item.setLines(2);
            item.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            item.setContentDescription(TAB_LABELS[index] + " bölümünü aç");
            item.setOnClickListener(view -> navigateToTab(tabIndex));
            LinearLayout.LayoutParams itemParams = new LinearLayout.LayoutParams(
                    0,
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    1
            );
            itemParams.setMargins(dp(2), 0, dp(2), 0);
            navigation.addView(item, itemParams);
            navigationItems[index] = item;
        }
        return navigation;
    }

    private void showAppMenu(View anchor) {
        PopupMenu popup = new PopupMenu(this, anchor);
        popup.getMenu().add(0, 1, 0, "Sayfayı yenile");
        popup.getMenu().add(0, 2, 1, "Temayı değiştir");
        popup.getMenu().add(0, 3, 2, "TV modu");
        popup.getMenu().add(0, 4, 3, "Günlük rapor");
        popup.getMenu().add(0, 5, 4, "EPİAŞ koruma");
        popup.getMenu().add(0, 6, 5, "Oturumu kapat");
        popup.setOnMenuItemClickListener(item -> {
            switch (item.getItemId()) {
                case 1:
                    webView.reload();
                    return true;
                case 2:
                    webView.evaluateJavascript(
                            "(function(){var b=document.querySelector('[data-suite-theme-toggle],.theme-toggle');if(b){b.click();return true;}return false;})()",
                            ignored -> webView.postDelayed(this::syncNativeTheme, 180)
                    );
                    return true;
                case 3:
                    webView.loadUrl(appUrl("/tv/"));
                    return true;
                case 4:
                    webView.loadUrl(appUrl("/rapor"));
                    return true;
                case 5:
                    webView.loadUrl(appUrl("/epias-koruma"));
                    return true;
                case 6:
                    webView.evaluateJavascript(
                            "fetch('/api/logout',{method:'POST',credentials:'include'}).finally(function(){location.replace('/oturum-kapatildi');})",
                            null
                    );
                    return true;
                default:
                    return false;
            }
        });
        popup.show();
    }

    private void navigateToTab(int tabIndex) {
        if (tabIndex < 0 || tabIndex >= TAB_PATHS.length) return;
        lastBackPressedAt = 0;
        if (selectedTabIndex == tabIndex) {
            webView.evaluateJavascript(
                    "window.scrollTo({top:0,behavior:'smooth'})",
                    null
            );
            return;
        }
        if (
                selectedTabIndex >= 0
                && (tabHistory.peek() == null
                || tabHistory.peek() != selectedTabIndex)
        ) {
            tabHistory.push(selectedTabIndex);
        }
        webView.loadUrl(appUrl(TAB_PATHS[tabIndex]));
    }

    private void updateNativeChrome(String url) {
        String path = Uri.parse(url).getPath();
        if (path == null) path = "/";
        currentPath = path;
        selectedTabIndex = -1;
        String title = "Baha Enerji";
        boolean showNavigation = false;

        if (path.startsWith("/piyasa")) {
            title = "Piyasa";
            selectedTabIndex = 0;
            showNavigation = true;
        } else if (path.startsWith("/baraj")) {
            title = "Baraj Aktif";
            selectedTabIndex = 1;
            showNavigation = true;
        } else if (path.startsWith("/uretim")) {
            title = "Üretim";
            selectedTabIndex = 2;
            showNavigation = true;
        } else if (path.startsWith("/tuketim")) {
            title = "Tüketim";
            selectedTabIndex = 3;
            showNavigation = true;
        } else if (path.startsWith("/sistem-yonu-tahmini")) {
            title = "Sistem Yönü Tahmini";
            selectedTabIndex = 4;
            showNavigation = true;
        } else if (path.startsWith("/login")) {
            title = "Güvenli Giriş";
            tabHistory.clear();
        } else if (path.startsWith("/oturum-kapatildi")) {
            title = "Oturum Kapatıldı";
            tabHistory.clear();
        } else if (path.startsWith("/tv")) {
            title = "Komuta Merkezi";
            showNavigation = true;
        } else if (path.startsWith("/rapor")) {
            title = "Günlük Rapor";
            showNavigation = true;
        } else if (path.startsWith("/epias-koruma")) {
            title = "EPİAŞ Koruma";
            showNavigation = true;
        }

        appSectionTitle.setText(title);
        bottomNavigation.setVisibility(showNavigation ? View.VISIBLE : View.GONE);
        updateNavigationSelection();
    }

    private void syncNativeTheme() {
        if (webView == null) return;
        webView.evaluateJavascript(
                "document.documentElement.getAttribute('data-theme')||'light'",
                value -> applyNativeTheme(value != null && value.contains("dark"))
        );
    }

    private void applyNativeTheme(boolean dark) {
        nativeDarkTheme = dark;
        getSharedPreferences("baha_native_preferences", MODE_PRIVATE)
                .edit()
                .putBoolean("dark_theme", dark)
                .apply();

        int pageBackground = dark
                ? Color.rgb(11, 20, 38)
                : Color.rgb(244, 247, 251);
        int barBackground = dark
                ? Color.rgb(11, 25, 48)
                : Color.WHITE;
        int navigationBackground = dark
                ? Color.rgb(13, 25, 44)
                : Color.WHITE;
        int primaryText = dark
                ? Color.WHITE
                : Color.rgb(16, 28, 53);
        int secondaryText = dark
                ? Color.rgb(171, 190, 220)
                : Color.rgb(90, 108, 136);

        if (appLayout != null) appLayout.setBackgroundColor(pageBackground);
        if (webView != null) webView.setBackgroundColor(pageBackground);
        if (appBar != null) appBar.setBackgroundColor(barBackground);
        if (appBrandTitle != null) {
            appBrandTitle.setTextColor(
                    dark ? Color.rgb(139, 169, 217) : Color.rgb(47, 112, 238)
            );
        }
        if (appSectionTitle != null) appSectionTitle.setTextColor(primaryText);
        if (appMenuButton != null) {
            appMenuButton.setTextColor(primaryText);
            appMenuButton.setBackground(
                    roundedBackground(
                            dark
                                    ? Color.rgb(24, 49, 84)
                                    : Color.rgb(237, 243, 255),
                            12
                    )
            );
        }
        if (bottomNavigation != null) {
            bottomNavigation.setBackgroundColor(navigationBackground);
        }
        if (errorView != null) errorView.setBackgroundColor(pageBackground);
        if (errorTitle != null) errorTitle.setTextColor(primaryText);
        if (errorMessage != null) errorMessage.setTextColor(secondaryText);
        if (retryButton != null) {
            retryButton.setTextColor(Color.WHITE);
            retryButton.setBackground(
                    roundedBackground(Color.rgb(47, 112, 238), 12)
            );
        }
        if (splashView != null) splashView.setBackgroundColor(pageBackground);
        if (splashTitle != null) splashTitle.setTextColor(primaryText);
        if (splashMessage != null) splashMessage.setTextColor(secondaryText);
        if (progressBar != null) {
            progressBar.getProgressDrawable().setColorFilter(
                    Color.rgb(47, 112, 238),
                    PorterDuff.Mode.SRC_IN
            );
        }

        getWindow().setStatusBarColor(barBackground);
        getWindow().setNavigationBarColor(navigationBackground);
        int systemUi = getWindow().getDecorView().getSystemUiVisibility();
        if (dark) {
            systemUi &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                systemUi &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
            }
        } else {
            systemUi |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                systemUi |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
            }
        }
        getWindow().getDecorView().setSystemUiVisibility(systemUi);
        updateNavigationSelection();
    }

    private void updateNavigationSelection() {
        if (navigationItems == null) return;
        int idleColor = nativeDarkTheme
                ? Color.rgb(157, 176, 205)
                : Color.rgb(104, 122, 150);
        int idleBackground = Color.TRANSPARENT;
        int selectedBackground = nativeDarkTheme
                ? Color.rgb(26, 54, 94)
                : Color.rgb(231, 239, 255);
        for (int index = 0; index < navigationItems.length; index++) {
            boolean selected = index == selectedTabIndex;
            TextView item = navigationItems[index];
            item.setTextColor(selected ? Color.rgb(47, 112, 238) : idleColor);
            item.setBackground(
                    roundedBackground(
                            selected ? selectedBackground : idleBackground,
                            13
                    )
            );
        }
    }

    private GradientDrawable roundedBackground(int color, int radiusDp) {
        GradientDrawable background = new GradientDrawable();
        background.setColor(color);
        background.setCornerRadius(dp(radiusDp));
        return background;
    }

    private String appUrl(String path) {
        String base = BuildConfig.SITE_URL.replaceAll("/+$", "");
        return base + (path.startsWith("/") ? path : "/" + path);
    }

    private void loadApp() {
        if (!hasNetwork()) {
            showError();
            return;
        }
        progressBar.setVisibility(View.VISIBLE);
        splashView.setVisibility(View.VISIBLE);
        errorView.setVisibility(View.GONE);
        webView.setVisibility(View.INVISIBLE);
        webView.loadUrl(BuildConfig.SITE_URL);
    }

    private void showError() {
        progressBar.setVisibility(View.GONE);
        splashView.setVisibility(View.GONE);
        webView.setVisibility(View.GONE);
        errorView.setVisibility(View.VISIBLE);
    }

    private boolean hasNetwork() {
        ConnectivityManager manager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        NetworkInfo info = manager == null ? null : manager.getActiveNetworkInfo();
        return info != null && info.isConnected();
    }

    private boolean isAppUrl(Uri uri) {
        if (uri == null) return false;
        String scheme = uri.getScheme();
        if (!"http".equalsIgnoreCase(scheme) && !"https".equalsIgnoreCase(scheme)) return false;
        String host = uri.getHost();
        return host != null && host.equalsIgnoreCase(appHost);
    }

    private String hostOf(String url) {
        try {
            return new URI(url).getHost();
        } catch (URISyntaxException exception) {
            return "";
        }
    }

    private void openExternal(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException ignored) {
            Toast.makeText(this, "Bağlantı açılamadı.", Toast.LENGTH_SHORT).show();
        }
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    @Override
    public void onBackPressed() {
        if (selectedTabIndex >= 0 && !tabHistory.isEmpty()) {
            int previousTab = tabHistory.pop();
            webView.loadUrl(appUrl(TAB_PATHS[previousTab]));
            return;
        }
        boolean auxiliaryPage = selectedTabIndex < 0
                && !currentPath.startsWith("/login")
                && !currentPath.startsWith("/oturum-kapatildi");
        if (auxiliaryPage && webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        long now = System.currentTimeMillis();
        if (now - lastBackPressedAt <= 2000) {
            finishAndRemoveTask();
            return;
        }
        lastBackPressedAt = now;
        Toast.makeText(
                this,
                "Uygulamadan çıkmak için tekrar geri basın.",
                Toast.LENGTH_SHORT
        ).show();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_CHOOSER_REQUEST_CODE || filePathCallback == null) return;
        Uri[] results = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
        filePathCallback.onReceiveValue(results);
        filePathCallback = null;
    }
}
