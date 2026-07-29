# Baha Enerji Android APK

Bu klasör, Baha Enerji web panelini Android telefonda uygulama olarak açan APK projesidir.
Web sitesiyle aynı veri ve ekran içeriklerini kullanır; APK içinde web menüleri yerine
yerel Android üst çubuğu, uygulama menüsü ve beş bölümlü alt gezinme çubuğu gösterilir.

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
gradle assembleRelease
```

APK çıktısı:

```text
app/build/outputs/apk/release/app-release.apk
```

Telefona yüklerken Android “bilinmeyen kaynaklardan yükleme” izni isteyebilir.

## Kalıcı release imzası

Uygulamanın yeni APK sürümlerinin eskisinin üzerine kurulabilmesi için bütün
derlemeler aynı release anahtarıyla imzalanır. Anahtar dosyası repoya yüklenmez.

Bir defaya mahsus bir release anahtarı oluşturun:

```powershell
keytool -genkeypair -v -keystore baha-release.jks -alias baha-release -keyalg RSA -keysize 2048 -validity 10000
```

`baha-release.jks` dosyasını güvenli ve yedekli bir yerde saklayın. Ardından
dosyanın Base64 değerini oluşturun:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("baha-release.jks"))
```

GitHub deposunda `Settings > Secrets and variables > Actions` bölümüne şu
Repository Secrets değerlerini ekleyin:

- `BAHA_ANDROID_KEYSTORE_BASE64`: Yukarıdaki Base64 değeri
- `BAHA_ANDROID_KEYSTORE_PASSWORD`: Keystore parolası
- `BAHA_ANDROID_KEY_ALIAS`: `baha-release`
- `BAHA_ANDROID_KEY_PASSWORD`: Anahtar parolası

Bu dört secret olmadan workflow bilinçli olarak durur; geçici bir debug
anahtarıyla güncellenemeyen APK üretmez.
