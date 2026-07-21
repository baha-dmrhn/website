# Baha Enerji Birleşik Web Sitesi

Bu klasör üç mevcut paneli tek alan adı ve tek EPİAŞ oturumu altında birleştirir:

- `/piyasa/`: PTF, SMF, YAL ve YAT
- `/baraj/`: Baraj aktif doluluk
- `/uretim/`: UEVM ve UEÇM

Birleşik sunucu modüllerin görsel dosyalarını kullanır ve UEVM/UEÇM servis
çekirdeğini ortak oturum katmanı olarak yükler. Eski bağımsız Baraj ve Üretim
sunucuları kaldırılmıştır; çalıştırılacak tek sunucu bu klasördeki `app.py` dosyasıdır.
Site ilk açıldığında ortak giriş ekranına yönlendirir. Başarılı girişten sonra
doğrudan Piyasa panelini açar. Üç modül arasında geçiş yapılırken yeniden
e-posta veya şifre istemez; alt panellerde ayrı giriş ekranı gösterilmez.

Piyasa panelindeki grafik, temel arayüz ve XLSX raporlama bileşenleri yerel
çalışır; ApexCharts, Tabler veya SheetJS CDN erişimine ihtiyaç duymaz.
PTF değerleri EPİAŞ servisinin doğrudan TL, EUR ve USD alanlarıyla gösterilir.
SMF servisi yalnızca TL yayımladığı için SMF değeri her zaman TL/MWh olarak kalır.

## Baraj Excel arşivi

`Aktif_Doluluk-Delta - Kopya.xlsx` dosyasının `Pivot` sekmesindeki tarih ve
doluluk değerleri Baraj panelinde geçmiş veri olarak kullanılır. Barajların
havza bilgileri aynı dosyanın `Aktif Doluluk` sekmesinden eşleştirilir.
Arşiv tarihleri tarih seçiminde `Arşiv`, güncel servis tarihi ise
`EPİAŞ` etiketiyle gösterilir. Seçilen arşiv tarihi sıralanabilir ve XLSX
olarak yeniden indirilebilir.

Baraj panelindeki Havza Rejimi bölümü, 24 Haziran 2026'dan son yayımlanan
EPİAŞ gününe kadar havza içindeki barajların ortalama aktif doluluğunu çizer.
Rejim ve tükenme tarihi doğrusal eğilim göstergesidir; yağış, havza girişi,
üretim programı ve baraj hacim farklarını içeren hidrolojik tahmin değildir.

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

## Render

Depo kökündeki `render.yaml` Blueprint dosyasını kullanın.
Docker bağlamı depo köküdür; böylece birleşik uygulamanın kullandığı üç modül de
imaja alınır.
