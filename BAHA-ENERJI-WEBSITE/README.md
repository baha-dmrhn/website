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
