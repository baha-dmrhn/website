# Baha Enerji Birleşik Web Sitesi

Bu klasör dört paneli tek alan adı ve tek EPİAŞ oturumu altında birleştirir:

- `/piyasa/`: PTF, SMF, YAL ve YAT
- `/baraj/`: Baraj aktif doluluk
- `/uretim/`: UEVM ve UEÇM
- `/tuketim/`: Gerçek zamanlı tüketim

Birleşik sunucu modüllerin görsel dosyalarını kullanır ve UEVM/UEÇM servis
çekirdeğini ortak oturum katmanı olarak yükler. Eski bağımsız Baraj ve Üretim
sunucuları kaldırılmıştır; çalıştırılacak tek sunucu bu klasördeki `app.py` dosyasıdır.
Site ilk açıldığında ortak giriş ekranına yönlendirir. Başarılı girişten sonra
doğrudan Piyasa panelini açar. Dört modül arasında geçiş yapılırken yeniden
e-posta veya şifre istemez; alt panellerde ayrı giriş ekranı gösterilmez.

Piyasa panelindeki grafik, temel arayüz ve XLSX raporlama bileşenleri yerel
çalışır; ApexCharts, Tabler veya SheetJS CDN erişimine ihtiyaç duymaz.
PTF değerleri EPİAŞ servisinin doğrudan TL, EUR ve USD alanlarıyla gösterilir.
SMF servisi yalnızca TL yayımladığı için SMF değeri her zaman TL/MWh olarak kalır.

## TV modu ve yönetici raporu

- `/tv/`: Beş görünümü 15 saniyede bir otomatik değiştiren tam ekran Enerji
  Komuta Merkezi. Verileri beş dakikada bir yeniler; duraklatma, elle görünüm
  seçme ve tarayıcı tam ekran desteği vardır.
- `/rapor`: Seçilen günün Piyasa, Baraj Aktif, UEVM/UEÇM ve Tüketim özetini
  tek markalı yönetici raporunda birleştirir. Sayfa `PDF / Yazdır` ile PDF'e
  kaydedilebilir ve beş çalışma sayfalı XLSX olarak indirilebilir.

Her iki bölüme de dört panelin üstündeki ortak menüden erişilir ve mevcut ortak
EPİAŞ oturumunu kullanır.

## Baraj Excel arşivi

`Aktif_Doluluk-Delta - Kopya.xlsx` dosyasının `Pivot` sekmesindeki tarih ve
doluluk değerleri Baraj panelinde geçmiş veri olarak kullanılır. Barajların
havza bilgileri aynı dosyanın `Aktif Doluluk` sekmesinden eşleştirilir.
Excel ile EPİAŞ aynı tarihi içeriyorsa Excel kaydı önceliklidir. Tarih
seçiminde kaynak etiketi gösterilmez. Seçilen arşiv tarihi sıralanabilir ve
XLSX olarak yeniden indirilebilir.

Baraj panelindeki Havza Rejimi bölümü, 24 Haziran 2026'dan son yayımlanan
EPİAŞ gününe kadar havza içindeki barajların ortalama aktif doluluğunu çizer.
Rejim ve tükenme tarihi doğrusal eğilim göstergesidir; yağış, havza girişi,
üretim programı ve baraj hacim farklarını içeren hidrolojik tahmin değildir.
Havza Risk Sıralaması; son basit ortalama doluluk, günlük eğim ve `%30` kritik
seviyeye tahmini süreyi birlikte değerlendirerek havzaları yüksek, orta ve düşük
risk şeklinde sıralar. Sıralamadaki havzaya tıklandığında harita, rejim grafiği
ve baraj geçmişi aynı havzaya geçer.

## Tüketim tahmini

Tüketim panelindeki Ertesi Gün Öngörüsü, EPİAŞ'tan alınan son 14 günlük saatlik
gerçek zamanlı tüketimi gün tipi ve tarih yakınlığıyla ağırlıklandırır. Hedef
günün 24 saatlik tahmini gösterilir; gerçekleşen saatler yayımlandıkça tahminle
karşılaştırılır ve ortalama mutlak hata hesaplanır. Bu değer deneysel,
istatistiksel bir operasyon göstergesidir; resmî talep tahmini değildir.

## Sistem yönü tahmini

`/sistem-yonu-tahmini/` ekranı saat 18:00'e kadar bugünün, 18:00 sonrasında
yarının saatlik sistem yönünü gösterir. Tahmin; son 28 gün, aynı hafta gününün
son 12 haftası, son 12 ay ve geçmiş yılların aynı mevsim/hafta/gün örneklerinden
oluşan tarihsel modele dayanır. EPİAŞ KGÜP üretim karışımı, tüketim ve PTF profili;
Open-Meteo'dan alınan sekiz bölge ağırlıklı sıcaklık, nem, rüzgâr, hamle,
bulutluluk, yağış ve güneş radyasyonu sınırlı katsayılarla modele eklenir.

Hava veya ileriye dönük EPİAŞ verisi geçici olarak alınamazsa ekran tarihsel
modele geri döner. Dış girdiler 30 dakika önbellekte tutulur; tahmin önbelleği
aynı veriler için gereksiz EPİAŞ çağrısı yapılmasını engeller. Bu sonuç resmî
EPİAŞ tahmini değildir.

Model ayrıca Türkiye'deki resmî tatil, dini bayram, arife ve köprü gün
etkilerini ayrı takvim özellikleri olarak kullanır. Dini bayramlar takvimde
kaydığı için geçmiş yıl örnekleri aynı ay/gün yerine aynı bayram günüyle
eşleştirilir. Okul tatili dönemi de yaklaşık bir gün-tipi sinyali olarak
değerlendirilir.

Sistem yönü tahmininde kullanıcıya ilk kez yayımlanan her saat, SQLite tahmin
kayıt defterine `INSERT OR IGNORE` mantığıyla yazılır. Sonraki yenilemeler ilk
kaydı değiştirmez; doğrulama ekranı mümkün olduğunda yeniden hesaplanan sonuç
yerine bu kilitli ilk yayını gerçek EPİAŞ yönüyle karşılaştırır. Bu işlem yeni
bir EPİAŞ isteği üretmez.

- `BAHA_FORECAST_LEDGER_ENABLED`: varsayılan `true`
- `BAHA_FORECAST_LEDGER_PATH`: varsayılan
  `BAHA-ENERJI-WEBSITE/.forecast-ledger/system-direction.sqlite3`

Render'ın geçici dosya sistemi servis yeniden oluşturulduğunda silinebilir.
Kayıt defterinin deploy'lar arasında korunması isteniyorsa
`BAHA_FORECAST_LEDGER_PATH` kalıcı bir disk yoluna verilmelidir. Kalıcı disk
tanımlanmadan kayıt defteri çalışan instance boyunca koruma sağlar fakat yeni
deploy sonrasında garanti vermez.

## Yerel çalıştırma

Proje ana klasöründeyken:

```powershell
python .\BAHA-ENERJI-WEBSITE\app.py
```

Ardından `http://127.0.0.1:8000` adresini açın.

Farklı port için:

```powershell
python .\BAHA-ENERJI-WEBSITE\app.py --port 8080
```

EPİAŞ parolası saklanmaz. Başarılı girişten sonra yalnızca geçici TGT, süreli
sunucu belleği oturumunda tutulur.
Hatalı girişler IP bazında sınırlandırılır. Varsayılan olarak 10 dakikalık
pencerede 5 hatalı denemeden sonra giriş 5 dakika bekletilir. Bu değerler
`BAHA_LOGIN_MAX_ATTEMPTS`, `BAHA_LOGIN_WINDOW_SECONDS` ve
`BAHA_LOGIN_BLOCK_SECONDS` ortam değişkenleriyle değiştirilebilir.
Aynı EPİAŞ e-posta adresi için varsayılan olarak 10 saniyede 3 hatalı denemeden
sonra 60 saniye bekleme uygulanır. Bu koruma EPİAŞ'ın 5 hatalı istek / 10 saniye
sınırına ulaşmadan giriş denemelerini uygulama tarafında keser.

## EPİAŞ istek sınırı koruması

EPİAŞ CAS token duyurusundaki kullanıcı bazlı TGT limiti 100 istek/dakika ve
10 istek/saniye burst sınırıdır. IP bazlı TGT limiti 1000 istek/dakika ve
100 istek/saniyedir. TGT token 8 saat geçerlidir. Uygulama bu sınırlara
yaklaşmamak için daha düşük varsayılanlarla çalışır:

- `EPIAS_TGT_REQUESTS_PER_MINUTE`: varsayılan `10`, üst sınır `20`
- `EPIAS_TGT_BURST_PER_SECOND`: varsayılan `2`, üst sınır `5`
- `EPIAS_API_REQUESTS_PER_MINUTE`: varsayılan `24`, üst sınır `30`
- `EPIAS_API_BURST_PER_SECOND`: varsayılan `2`, üst sınır `3`

Tüm EPİAŞ HTTP çağrıları `UEVM-UEÇM/main.py` içindeki ortak rolling-window
sınırlayıcıdan geçer. EPİAŞ 429 döndürürse uygulama `Retry-After` başlığını
okuyup sonraki EPİAŞ isteklerini otomatik olarak bekletir.
Ortak oturum ve TGT kullanımı varsayılan olarak 450 dakika sürer; bu değer
EPİAŞ'ın 8 saatlik TGT ömründen 30 dakika önce kesilir.
Render'da IP bazlı korumanın anlamlı kalması için uygulamayı tek instance ile
çalıştırın. Birden fazla instance/worker kullanılacaksa rate limit sayacını
Redis gibi ortak bir depoya taşımak gerekir.

Arka plan cache yenileyici aktif EPİAŞ oturumu varken Piyasa, Ertesi Gün PTF,
Tüketim, Baraj Aktif ve UEVM/UEÇM cache'lerini güvenli aralıklarla ısıtır.
Şifre saklamaz; aktif TGT yoksa beklemede kalır. Varsayılan aralıklar piyasa için
120 sn, ertesi gün PTF ve tüketim için 180 sn, baraj için 300 sn, üretim için
1800 sn'dir. Durumunu `/epias-koruma` sayfasından izleyebilirsiniz.

## Testler

Python testlerini çalıştırmak için:

```powershell
python -m unittest discover -s .\BAHA-ENERJI-WEBSITE\tests -q
python -m unittest discover -s .\UEVM-UEÇM\tests -q
```

Telefon, iPad ve masaüstü ekran görüntüsü referanslarını ilk kez oluşturmak için:

```powershell
python .\BAHA-ENERJI-WEBSITE\tests\visual_regression.py --update
```

Sonraki görsel karşılaştırmalar için aynı komutu `--update` olmadan çalıştırın.
Test Chrome veya Edge kullanır; gerekirse `CHROME_BINARY` ile tarayıcı yolunu belirtin.

## Render

Depo kökündeki `render.yaml` Blueprint dosyasını kullanın.
Docker bağlamı depo köküdür; böylece birleşik uygulamanın kullandığı dört modül de
imaja alınır.

## Proje düzeni ve deploy temizliği

Çalıştırılacak tek sunucu `BAHA-ENERJI-WEBSITE/app.py` dosyasıdır. Kök dizindeki
`SMF-PTF-YAL-YAT`, `BARAJ AKTİF` ve `UEVM-UEÇM` klasörleri hâlâ birleşik sitenin
kaynak modül dosyaları olarak kullanılır; bu yüzden deploy paketinden rastgele
çıkarılmamalıdır.

Docker build sırasında `.git`, test çıktıları, `__pycache__` ve görsel regresyon
profilleri `.dockerignore` ile dışarıda bırakılır. Bu, Render/Docker context'ini
daha küçük ve build sürecini daha hızlı tutar.
