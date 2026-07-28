# Baha Enerji Android APK

Bu klasör, Baha Enerji web panelini Android telefonda normal uygulama gibi açan basit bir WebView APK projesidir.

## Site adresi

Varsayılan adres:

```text
https://baha-website.onrender.com
```

Render linkin farklıysa `gradle.properties` içindeki `BAHA_SITE_URL` satırını değiştir.

## APK üretme

GitHub üzerinden:

1. Bu klasörü GitHub'a gönder.
2. GitHub repo sayfasında **Actions** bölümüne gir.
3. **Build Baha Enerji Android APK** workflow'unu aç.
4. Çalışma bittikten sonra **Baha-Enerji-APK** artifact'ini indir.
5. ZIP içindeki `Baha-Enerji.apk` dosyasını Android telefona gönderip kur.

Android Studio ile:

1. Android Studio > Open
2. `BAHA-ENERJI-ANDROID` klasörünü seç
3. Gradle sync bitsin
4. Build > Build Bundle(s) / APK(s) > Build APK(s)

Komut satırıyla, Gradle ve Android SDK kuruluysa:

```powershell
cd BAHA-ENERJI-ANDROID
gradle assembleDebug
```

APK çıktısı:

```text
app/build/outputs/apk/debug/app-debug.apk
```

Telefona yüklerken Android “bilinmeyen kaynaklardan yükleme” izni isteyebilir.
