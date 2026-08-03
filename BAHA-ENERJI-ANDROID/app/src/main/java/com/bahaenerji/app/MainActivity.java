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
import android.os.Handler;
import android.os.Looper;
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
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.ArrayDeque;
import java.util.Deque;

public class MainActivity extends Activity {
    private static final int FILE_CHOOSER_REQUEST_CODE = 1107;
    private static final String APP_USER_AGENT = "BahaEnerjiAndroid/3.0";
    private static final String CONFIGURE_EMBEDDED_CHROME_SCRIPT =
            "(function(){"
                    + "var id='baha-native-chrome-hide';"
                    + "var style=document.getElementById(id);"
                    + "if(!style){"
                    + "style=document.createElement('style');"
                    + "style.id=id;"
                    + "style.textContent='"
                    + ".baha-suite-nav,"
                    + ".suite-menu-button,"
                    + ".baha-suite-piyasa .menu-button"
                    + "{display:none!important;visibility:hidden!important}"
                    + ".suite-sidebar,.baha-suite-piyasa .sidebar"
                    + "{display:flex!important}"
                    + "@media(max-width:820px){"
                    + ".suite-sidebar-overlay,.baha-suite-piyasa .sidebar-overlay"
                    + "{display:block!important}"
                    + "}'"
                    + ";(document.head||document.documentElement).appendChild(style);"
                    + "}"
                    + "document.body.classList.remove("
                    + "'sidebar-open','suite-sidebar-open','suite-sidebar-hovered','suite-sidebar-pinned'"
                    + ");"
                    + "var marketSidebar=document.querySelector('.baha-suite-piyasa .sidebar');"
                    + "if(marketSidebar){marketSidebar.classList.remove('open');}"
                    + "return true;"
                    + "})()";
    private static final String TOGGLE_EMBEDDED_SIDEBAR_SCRIPT =
            "(function(){"
                    + "var body=document.body;"
                    + "var market=document.querySelector('.baha-suite-piyasa .sidebar');"
                    + "if(market){"
                    + "var marketOpen=!market.classList.contains('open');"
                    + "market.classList.toggle('open',marketOpen);"
                    + "body.classList.toggle('sidebar-open',marketOpen);"
                    + "return true;"
                    + "}"
                    + "var suite=document.querySelector('.suite-sidebar');"
                    + "if(suite){"
                    + "var suiteOpen=!body.classList.contains('suite-sidebar-open');"
                    + "body.classList.remove('suite-sidebar-collapsed','suite-sidebar-hovered','suite-sidebar-pinned');"
                    + "body.classList.toggle('suite-sidebar-open',suiteOpen);"
                    + "return true;"
                    + "}"
                    + "return false;"
                    + "})()";
    private static final long STARTUP_TIMEOUT_MS = 60000;
    private static final String[] TAB_PATHS = {
            "/piyasa/",
            "/baraj/",
            "/uretim/",
            "/tuketim/",
            "/sistem-yonu-tahmini/"
    };
    private static final String[] BOTTOM_LABELS = {
            "Piyasa",
            "Baraj",
            "UEVM",
            "Tüketim",
            "Tahmin"
    };
    private static final int[] BOTTOM_ICONS = {
            R.drawable.ic_market,
            R.drawable.ic_dam,
            R.drawable.ic_production,
            R.drawable.ic_consumption,
            R.drawable.ic_forecast
    };

    private WebView webView;
    private ProgressBar progressBar;
    private LinearLayout appLayout;
    private LinearLayout appBar;
    private LinearLayout bottomNavigation;
    private LinearLayout splashView;
    private LinearLayout errorView;
    private TextView appSectionTitle;
    private TextView appMenuButton;
    private ImageView appNotificationButton;
    private final LinearLayout[] bottomItems =
            new LinearLayout[BOTTOM_LABELS.length];
    private final ImageView[] bottomIcons =
            new ImageView[BOTTOM_LABELS.length];
    private final TextView[] bottomLabels =
            new TextView[BOTTOM_LABELS.length];
    private TextView splashTitle;
    private TextView splashMessage;
    private TextView errorTitle;
    private TextView errorMessage;
    private Button retryButton;
    private ValueCallback<Uri[]> filePathCallback;
    private String appHost;
    private boolean nativeDarkTheme;
    private int selectedTabIndex = -1;
    private final Deque<Integer> tabHistory = new ArrayDeque<>();
    private String currentPath = "/";
    private long lastBackPressedAt;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Runnable startupTimeout = () -> {
        if (splashView != null && splashView.getVisibility() == View.VISIBLE) {
            showError();
        }
    };
    private int unexpectedStartupPages;
    private boolean startupLoading;

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
                    if (startupLoading && unexpectedStartupPages < 3) {
                        unexpectedStartupPages += 1;
                        view.postDelayed(
                                () -> {
                                    if (startupLoading) {
                                        view.loadUrl(
                                                appUrl(
                                                        "/panel-hazirlaniyor?next=/login"
                                                )
                                        );
                                    }
                                },
                                1800L * unexpectedStartupPages
                        );
                        return;
                    }
                    if (startupLoading) {
                        showError();
                        return;
                    }
                    progressBar.setVisibility(View.VISIBLE);
                    splashView.setVisibility(View.VISIBLE);
                    webView.setVisibility(View.INVISIBLE);
                    return;
                }
                startupLoading = false;
                mainHandler.removeCallbacks(startupTimeout);
                updateNativeChrome(url);
                view.evaluateJavascript(CONFIGURE_EMBEDDED_CHROME_SCRIPT, ignored -> {
                    syncNativeTheme();
                    progressBar.setVisibility(View.GONE);
                    splashView.setVisibility(View.GONE);
                    errorView.setVisibility(View.GONE);
                    webView.setVisibility(View.VISIBLE);
                    CookieManager.getInstance().flush();
                });
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
                dp(82)
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
                dp(78)
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
        appBar.setPadding(dp(14), dp(12), dp(14), dp(8));
        appBar.setBackground(appBarBackground(nativeDarkTheme));
        appBar.setElevation(0);

        appMenuButton = appBarButton("☰", "Uygulama menüsü");
        appMenuButton.setOnClickListener(this::toggleAppSidebar);
        appBar.addView(
                appMenuButton,
                new LinearLayout.LayoutParams(dp(48), dp(48))
        );

        appSectionTitle = new TextView(this);
        appSectionTitle.setText("Baha Enerji");
        appSectionTitle.setTextSize(20);
        appSectionTitle.setGravity(Gravity.CENTER);
        appSectionTitle.setSingleLine(true);
        appSectionTitle.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(
                0,
                ViewGroup.LayoutParams.MATCH_PARENT,
                1
        );
        titleParams.leftMargin = dp(8);
        titleParams.rightMargin = dp(8);
        appBar.addView(appSectionTitle, titleParams);

        appNotificationButton = new ImageView(this);
        appNotificationButton.setImageResource(R.drawable.ic_bell);
        appNotificationButton.setPadding(dp(13), dp(13), dp(13), dp(13));
        appNotificationButton.setContentDescription("Bildirimler");
        appNotificationButton.setElevation(dp(3));
        appNotificationButton.setOnClickListener(
                view -> Toast.makeText(
                        this,
                        "Yeni bildiriminiz bulunmuyor.",
                        Toast.LENGTH_SHORT
                ).show()
        );
        appBar.addView(
                appNotificationButton,
                new LinearLayout.LayoutParams(dp(48), dp(48))
        );
        return appBar;
    }

    private TextView appBarButton(String icon, String description) {
        TextView button = new TextView(this);
        button.setText(icon);
        button.setTextSize(24);
        button.setGravity(Gravity.CENTER);
        button.setContentDescription(description);
        button.setElevation(dp(3));
        return button;
    }

    private LinearLayout buildBottomNavigation() {
        LinearLayout navigation = new LinearLayout(this);
        navigation.setOrientation(LinearLayout.HORIZONTAL);
        navigation.setGravity(Gravity.CENTER);
        navigation.setPadding(dp(4), dp(7), dp(4), dp(5));
        navigation.setElevation(dp(18));

        for (int index = 0; index < BOTTOM_LABELS.length; index++) {
            final int itemIndex = index;
            LinearLayout item = new LinearLayout(this);
            item.setOrientation(LinearLayout.VERTICAL);
            item.setGravity(Gravity.CENTER);
            item.setPadding(dp(1), dp(2), dp(1), 0);
            item.setContentDescription(BOTTOM_LABELS[index]);
            item.setOnClickListener(view -> navigateToTab(itemIndex));

            ImageView icon = new ImageView(this);
            icon.setImageResource(BOTTOM_ICONS[index]);
            icon.setScaleType(ImageView.ScaleType.CENTER_INSIDE);
            icon.setPadding(dp(9), dp(9), dp(9), dp(9));
            item.addView(icon, new LinearLayout.LayoutParams(dp(38), dp(38)));

            TextView label = new TextView(this);
            label.setText(BOTTOM_LABELS[index]);
            label.setTextSize(10);
            label.setGravity(Gravity.CENTER);
            label.setSingleLine(true);
            label.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            item.addView(
                    label,
                    new LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            dp(22)
                    )
            );

            bottomItems[index] = item;
            bottomIcons[index] = icon;
            bottomLabels[index] = label;
            navigation.addView(
                    item,
                    new LinearLayout.LayoutParams(
                            0,
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            1
                    )
            );
        }
        return navigation;
    }

    private void toggleAppSidebar(View ignored) {
        if (webView == null || selectedTabIndex < 0) return;
        webView.evaluateJavascript(TOGGLE_EMBEDDED_SIDEBAR_SCRIPT, null);
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

        if (path.startsWith("/piyasa")) {
            title = "Günlük Piyasa Özeti";
            selectedTabIndex = 0;
        } else if (path.startsWith("/baraj")) {
            title = "Baraj Doluluk Özeti";
            selectedTabIndex = 1;
        } else if (path.startsWith("/uretim")) {
            title = "UEVM & UEÇM";
            selectedTabIndex = 2;
        } else if (path.startsWith("/tuketim")) {
            title = "Gerçek Zamanlı Tüketim";
            selectedTabIndex = 3;
        } else if (path.startsWith("/sistem-yonu-tahmini")) {
            title = "Sistem Yönü Tahmini";
            selectedTabIndex = 4;
        } else if (path.startsWith("/login")) {
            title = "Güvenli Giriş";
            tabHistory.clear();
        } else if (path.startsWith("/oturum-kapatildi")) {
            title = "Oturum Kapatıldı";
            tabHistory.clear();
        } else if (path.startsWith("/tv")) {
            title = "Komuta Merkezi";
        } else if (path.startsWith("/rapor")) {
            title = "Günlük Rapor";
        } else if (path.startsWith("/epias-koruma")) {
            title = "EPİAŞ Koruma";
        }

        appSectionTitle.setText(title);
        if (appMenuButton != null) {
            appMenuButton.setVisibility(
                    selectedTabIndex >= 0 ? View.VISIBLE : View.INVISIBLE
            );
        }
        if (bottomNavigation != null) {
            bottomNavigation.setVisibility(
                    selectedTabIndex >= 0 ? View.VISIBLE : View.GONE
            );
        }
        updateBottomNavigationStyles();
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
        int navigationBackground = dark
                ? Color.rgb(13, 25, 44)
                : Color.rgb(238, 243, 251);
        int primaryText = dark
                ? Color.WHITE
                : Color.rgb(16, 28, 53);
        int secondaryText = dark
                ? Color.rgb(171, 190, 220)
                : Color.rgb(90, 108, 136);

        if (appLayout != null) appLayout.setBackgroundColor(pageBackground);
        if (webView != null) webView.setBackgroundColor(pageBackground);
        if (appBar != null) appBar.setBackground(appBarBackground(dark));
        if (appSectionTitle != null) {
            appSectionTitle.setTextColor(primaryText);
        }
        if (appMenuButton != null) {
            appMenuButton.setTextColor(
                    dark ? Color.rgb(232, 239, 251) : Color.rgb(16, 38, 75)
            );
            appMenuButton.setBackground(
                    outlinedBackground(
                            dark
                                    ? Color.rgb(20, 35, 59)
                                    : Color.WHITE,
                            dark
                                    ? Color.rgb(48, 68, 99)
                                    : Color.rgb(215, 225, 240),
                            14
                    )
            );
        }
        if (appNotificationButton != null) {
            appNotificationButton.setColorFilter(
                    dark ? Color.rgb(232, 239, 251) : Color.rgb(16, 38, 75),
                    PorterDuff.Mode.SRC_IN
            );
            appNotificationButton.setBackground(
                    outlinedBackground(
                            dark
                                    ? Color.rgb(20, 35, 59)
                                    : Color.WHITE,
                            dark
                                    ? Color.rgb(48, 68, 99)
                                    : Color.rgb(215, 225, 240),
                            14
                    )
            );
        }
        if (bottomNavigation != null) {
            bottomNavigation.setBackground(
                    outlinedBackground(
                            dark
                                    ? Color.rgb(14, 27, 47)
                                    : Color.WHITE,
                            dark
                                    ? Color.rgb(43, 62, 92)
                                    : Color.rgb(216, 226, 240),
                            22
                    )
            );
        }
        updateBottomNavigationStyles();
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

        getWindow().setStatusBarColor(
                dark ? Color.rgb(5, 14, 29) : pageBackground
        );
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
    }

    private void updateBottomNavigationStyles() {
        int activeColor = nativeDarkTheme
                ? Color.rgb(102, 153, 255)
                : Color.rgb(35, 104, 242);
        int passiveColor = nativeDarkTheme
                ? Color.rgb(151, 169, 198)
                : Color.rgb(91, 105, 130);
        int activeBackground = nativeDarkTheme
                ? Color.rgb(35, 79, 157)
                : Color.rgb(35, 104, 242);

        for (int index = 0; index < bottomItems.length; index++) {
            if (bottomItems[index] == null) continue;
            boolean active = index == selectedTabIndex;
            bottomIcons[index].setColorFilter(
                    active ? Color.WHITE : passiveColor,
                    PorterDuff.Mode.SRC_IN
            );
            bottomIcons[index].setBackground(
                    active
                            ? roundedBackground(activeBackground, 12)
                            : roundedBackground(Color.TRANSPARENT, 12)
            );
            bottomLabels[index].setTextColor(
                    active ? activeColor : passiveColor
            );
        }
    }

    private GradientDrawable roundedBackground(int color, int radiusDp) {
        GradientDrawable background = new GradientDrawable();
        background.setColor(color);
        background.setCornerRadius(dp(radiusDp));
        return background;
    }

    private GradientDrawable outlinedBackground(
            int fillColor,
            int strokeColor,
            int radiusDp
    ) {
        GradientDrawable background = roundedBackground(fillColor, radiusDp);
        background.setStroke(dp(1), strokeColor);
        return background;
    }

    private GradientDrawable appBarBackground(boolean dark) {
        return roundedBackground(
                dark ? Color.rgb(11, 20, 38) : Color.rgb(244, 247, 251),
                0
        );
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
        startupLoading = true;
        unexpectedStartupPages = 0;
        mainHandler.removeCallbacks(startupTimeout);
        mainHandler.postDelayed(startupTimeout, STARTUP_TIMEOUT_MS);
        progressBar.setVisibility(View.VISIBLE);
        splashView.setVisibility(View.VISIBLE);
        errorView.setVisibility(View.GONE);
        webView.setVisibility(View.INVISIBLE);
        webView.loadUrl(BuildConfig.SITE_URL);
    }

    private void showError() {
        startupLoading = false;
        mainHandler.removeCallbacks(startupTimeout);
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

    @Override
    protected void onDestroy() {
        mainHandler.removeCallbacksAndMessages(null);
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
        }
        super.onDestroy();
    }
}
