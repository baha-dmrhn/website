# Baha Enerji Android APK

Bu klasör, Baha Enerji web panelini Android telefonda uygulama olarak açan APK projesidir.
Web sitesiyle aynı veri ve ekran içeriklerini kullanır. APK içinde yerel bir üst başlık
çubuğu bulunmaz; sol üstteki bağımsız menü düğmesi web panelinin çekmecesini açar.
Ana bölümler arasında ekranın altındaki uygulama gezinmesiyle geçiş yapılır.

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

Bilgisayara JDK veya Android Studio kurmak zorunlu değildir. Derleme GitHub
Actions sunucusunda yapılır. Release imza secret'ları eklenmemişse workflow
otomatik olarak kurulabilir bir debug APK üretir ve işlem hata vermez.

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

Bu dört secret isteğe bağlıdır. Eklendiğinde kalıcı release anahtarıyla imzalı
APK üretilir. Eklenmediğinde GitHub Actions, geliştirme anahtarını önbellekte
tutarak debug APK üretir. GitHub önbelleği silinirse yeni APK'nın kurulabilmesi
için telefondaki eski debug sürümünün bir kez kaldırılması gerekebilir.
