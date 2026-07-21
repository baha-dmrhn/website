$Host.UI.RawUI.WindowTitle = "Baha Enerji - Mobil Erisim"
$env:HOST = "0.0.0.0"
$env:PORT = "3000"
$lanIp = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
    Where-Object { $_.AddressFamily -eq 'InterNetwork' } |
    Select-Object -First 1 -ExpandProperty IPAddressToString

Write-Host ""
Write-Host "Baha Enerji paneli yerel aga aciliyor..."
Write-Host "Telefonda su adresi acin:"
Write-Host ""
Write-Host "    http://${lanIp}:3000" -ForegroundColor Cyan
Write-Host ""
Write-Host "Telefon ve bilgisayar ayni Wi-Fi aginda olmalidir."
Write-Host "Windows guvenlik duvari sorarsa Ozel aglar icin erisim izni verin."
Write-Host "Sunucuyu kapatmak icin Ctrl+C tuslarina basin."
Write-Host ""

py app.py
