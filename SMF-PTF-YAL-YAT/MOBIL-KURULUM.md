# Baha Enerji Android kurulumu

## Gereksinimler

- Node.js LTS
- Android Studio ve Android SDK
- İnternete açık HTTPS Baha Enerji API adresi

`public/config.js` içindeki `apiBaseUrl` alanına panelin HTTPS adresini yazın. Ardından proje klasöründe:

```powershell
npm install
npm run mobile:add:android
npm run mobile:sync
npm run mobile:android
```

Android Studio açıldığında uygulamayı bağlı telefonda çalıştırabilirsiniz. Dağıtım dosyası için **Build > Generate Signed Bundle / APK** menüsünü kullanın. Google Play için AAB, telefona doğrudan kurulum için APK üretin.

Web arayüzündeki her değişiklikten sonra aşağıdaki komutla Android paketini güncelleyin:

```powershell
npm run mobile:sync
```
