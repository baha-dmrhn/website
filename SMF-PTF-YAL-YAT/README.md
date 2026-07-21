# EPİAŞ Enerji Görünümü

Tabler tabanlı, yerel çalışan SMF–PTF–YAL–YAT paneli. Kullanıcılar EPİAŞ Şeffaflık Platformu hesaplarıyla giriş yapar. EPİAŞ şifresi diske veya tarayıcı depolamasına yazılmaz; alınan TGT yalnızca süreli sunucu belleği oturumunda tutulur.

## Çalıştırma

Python 3 gerekir. Ek paket kurulumu yoktur. Windows'ta `baslat.bat` dosyasına çift tıklayabilir veya terminalden çalıştırabilirsiniz:

```powershell
py app.py
```

Ardından `http://127.0.0.1:3000` adresini açın. İnternet bağlantısı, Tabler/ApexCharts CDN dosyaları ve EPİAŞ servisleri için gereklidir.

## Notlar

- EPİAŞ hesabında Şeffaflık Platformu web servis erişimi bulunmalıdır.
- Tarih seçildiğinde PTF/SMF ve YAL/YAT servisleri yeniden çağrılır.
- YAL/YAT verileri EPİAŞ tarafından yaklaşık dört saat gecikmeli yayımlanabilir.
- Farklı port için: `$env:PORT=8080; py app.py`

## Mobil uygulama (PWA)

Panel; manifest, servis worker ve Baha Enerji uygulama ikonlarıyla kurulabilir bir PWA'dır. HTTPS adresinden açıldığında Android'de ekrandaki **Uygulamayı telefona yükle** düğmesi kullanılabilir. iPhone'da Safari'deki **Paylaş > Ana Ekrana Ekle** yolu kullanılır.

`127.0.0.1` yalnızca bilgisayarın kendisinden erişilebilir. Gerçek telefonda kurulum ve güvenli EPİAŞ girişi için uygulamayı HTTPS kullanan bir alan adında yayımlayın. Uygulama kabuğu önbelleğe alınır; EPİAŞ oturumu ve piyasa verileri güvenlik nedeniyle önbelleğe alınmaz ve internet bağlantısı gerektirir.
