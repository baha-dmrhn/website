import json, os, re, secrets, socket, threading, time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).parent.resolve()
PUBLIC = ROOT / "public"
HOST, PORT = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "3000"))
ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.getenv("ALLOWED_ORIGINS", "https://localhost").split(",")
    if origin.strip()
}
CAS_URL = "https://giris.epias.com.tr/cas/v1/tickets"
ELECTRICITY_ROOT = "https://seffaflik.epias.com.tr/electricity-service/v1"
PTF_URL = f"{ELECTRICITY_ROOT}/markets/dam/data/mcp"
SMF_URL = f"{ELECTRICITY_ROOT}/markets/bpm/data/system-marginal-price"
DIRECTION_URL = f"{ELECTRICITY_ROOT}/markets/bpm/data/system-direction"
ELECTRICITY_URL = f"{ELECTRICITY_ROOT}/markets/bpm/data"
SESSION_SECONDS = 90 * 60
sessions = {}
data_cache = {}
epias_request_lock = threading.Lock()
direct_opener = build_opener(ProxyHandler({}))

def epias_post(url, tgt=None, body=None, form=None):
    if form is not None:
        data = urlencode(form).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/plain"}
    else:
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json", "TGT": tgt}
    for attempt in range(3):
        req = Request(url, data=data, headers=headers, method="POST")
        try:
            with direct_opener.open(req, timeout=25) as response:
                raw = response.read().decode("utf-8", "replace")
                return raw if form is not None else json.loads(raw)
        except HTTPError as error:
            if error.code == 429 and form is None and attempt < 2:
                retry_after = error.headers.get("Retry-After", "")
                try: wait = min(max(float(retry_after), 1), 8)
                except ValueError: wait = 1.5 * (attempt + 1)
                error.read(); time.sleep(wait); continue
            detail = error.read().decode("utf-8", "replace")[:400]
            if form is not None and error.code in (401, 403):
                message = "EPİAŞ giriş bilgilerini reddetti. E-posta/şifreyi ve hesabın Şeffaflık web servis erişimini kontrol edin."
            elif error.code in (401, 403):
                message = "EPİAŞ oturumu geçersiz veya süresi dolmuş."
            elif error.code == 429:
                message = "EPİAŞ istek sınırına ulaşıldı. Kısa bir süre sonra yeniden deneyin."
            else:
                message = f"EPİAŞ servisi {error.code} hatası döndürdü."
            raise EpiasError(message, error.code, detail)
        except URLError as error:
            reason = str(error.reason)
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                message = "EPİAŞ SSL sertifikası doğrulanamadı. Bilgisayarın tarihini ve sertifika güncellemelerini kontrol edin."
            elif "timed out" in reason.lower():
                message = "EPİAŞ bağlantısı zaman aşımına uğradı. İnternet veya güvenlik duvarını kontrol edin."
            elif "name or service" in reason.lower() or "getaddrinfo" in reason.lower():
                message = "EPİAŞ adresi çözümlenemedi. DNS veya internet bağlantısını kontrol edin."
            else:
                message = f"EPİAŞ servisine bağlanılamadı: {reason}"
            raise EpiasError(message, 502, reason)
        except json.JSONDecodeError:
            raise EpiasError("EPİAŞ servisinden geçersiz yanıt alındı.", 502)

class EpiasError(Exception):
    def __init__(self, message, status=502, detail=None):
        super().__init__(message); self.status = status; self.detail = detail

class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

def get_items(payload):
    if isinstance(payload, list): return payload
    if not isinstance(payload, dict): return []
    for candidate in (payload.get("items"), (payload.get("body") or {}).get("items"), (payload.get("data") or {}).get("items")):
        if isinstance(candidate, list): return candidate
    return []

def response_section(payload, *names):
    containers = (payload, payload.get("body") or {}, payload.get("data") or {}) if isinstance(payload, dict) else ({},)
    for container in containers:
        for name in names:
            value = container.get(name)
            if isinstance(value, dict): return value
            if isinstance(value, list) and value and isinstance(value[0], dict): return value[0]
    return {}

def number_from(row, names):
    for name in names:
        try:
            if row.get(name) is not None: return float(row[name])
        except (TypeError, ValueError): pass
    return None

def hour_key(row, index):
    raw_hour = row.get("hour") if row.get("hour") is not None else row.get("saat")
    if raw_hour is not None:
        hour_text = str(raw_hour).strip()
        match = re.search(r"T(\d{2}):", hour_text) or re.match(r"^(\d{1,2})(?::|\D|$)", hour_text)
        if match:
            hour = int(match.group(1))
            return 23 if hour == 24 else hour
    source = str(row.get("date") or row.get("tarih") or row.get("effectiveDate") or row.get("time") or "")
    match = re.search(r"T(\d{2}):", source)
    return int(match.group(1)) if match else index

def quantities(payload, coded_fields):
    result = {}
    for index, row in enumerate(get_items(payload)):
        coded = [number_from(row, (field,)) for field in coded_fields]
        coded = [value for value in coded if value is not None]
        result[hour_key(row, index)] = sum(coded) if coded else None
    return result

class Handler(SimpleHTTPRequestHandler):
    server_version = "EnergyDashboard/1.0"
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json"}
    def log_message(self, fmt, *args): print(f"[{self.log_date_time_string()}] {fmt % args}")
    def end_headers(self):
        path = urlparse(self.path).path
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        if path in ("/sw.js", "/manifest.webmanifest"):
            self.send_header("Cache-Control", "no-cache")
        if path == "/sw.js":
            self.send_header("Service-Worker-Allowed", "/")
        super().end_headers()
    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin not in ALLOWED_ORIGINS:
            return self.send_error(403)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()
    def send_json(self, status, body, cookie=None):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store")
        if cookie: self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def read_json(self):
        size = min(int(self.headers.get("Content-Length", 0)), 100_000)
        return json.loads(self.rfile.read(size) or b"{}")
    def current_session(self):
        cookies = SimpleCookie(self.headers.get("Cookie", "")); morsel = cookies.get("epias_sid")
        data = sessions.get(morsel.value) if morsel else None
        if not data or data["expires"] < time.time():
            if morsel: sessions.pop(morsel.value, None)
            return None
        data["expires"] = time.time() + SESSION_SECONDS
        return morsel.value, data
    def session_cookie(self, value, max_age):
        forwarded_https = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        cross_origin = self.headers.get("Origin", "").rstrip("/") in ALLOWED_ORIGINS
        secure_enabled = forwarded_https or os.getenv("PRODUCTION") == "1"
        secure = "; Secure" if secure_enabled else ""
        same_site = "None" if cross_origin and secure_enabled else "Strict"
        return f"epias_sid={value}; HttpOnly; SameSite={same_site}; Path=/; Max-Age={max_age}{secure}"
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                body = self.read_json(); email, password = str(body.get("email") or "").strip(), str(body.get("password") or "")
                if not email or not password: return self.send_json(400, {"error":"E-posta ve şifre zorunludur."})
                raw = epias_post(CAS_URL, form={"username":email, "password":password})
                match = re.search(r"TGT-[A-Za-z0-9_-]+", raw)
                if not match: return self.send_json(401, {"error":"EPİAŞ girişi başarısız. Bilgilerinizi ve web servis erişiminizi kontrol edin."})
                sid = secrets.token_hex(32); sessions[sid] = {"tgt":match.group(0), "email":email, "name":email, "expires":time.time()+SESSION_SECONDS}
                return self.send_json(200, {"ok":True,"email":email,"name":email}, self.session_cookie(sid, SESSION_SECONDS))
            if path == "/api/logout":
                current = self.current_session()
                if current: sessions.pop(current[0], None)
                return self.send_json(200, {"ok":True}, self.session_cookie("", 0))
            self.send_json(404, {"error":"Bulunamadı."})
        except (EpiasError, ValueError, json.JSONDecodeError) as error:
            self.send_json(getattr(error, "status", 400), {"error":str(error)})
        except Exception: self.send_json(500, {"error":"Beklenmeyen bir hata oluştu."})
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            return self.send_json(200, {"ok":True})
        if parsed.path == "/api/session":
            current = self.current_session()
            return self.send_json(200, {"authenticated":True,"email":current[1]["email"],"name":current[1]["name"]}) if current else self.send_json(401, {"authenticated":False})
        if parsed.path == "/api/data": return self.handle_data(parse_qs(parsed.query).get("date", [""])[0])
        requested = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        target = (PUBLIC / requested).resolve()
        if PUBLIC not in target.parents and target != PUBLIC: return self.send_error(403)
        if not target.is_file(): return self.send_error(404)
        self.path = "/" + requested; return SimpleHTTPRequestHandler.do_GET(self)
    def translate_path(self, path): return str(PUBLIC / urlparse(path).path.lstrip("/"))
    def handle_data(self, date):
        current = self.current_session()
        if not current: return self.send_json(401, {"error":"Oturum açmanız gerekiyor."})
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date): return self.send_json(400, {"error":"Geçerli bir tarih seçin."})
        cached = data_cache.get(date)
        if cached and cached["expires"] > time.time():
            payload = {**cached["payload"], "cached":True}
            return self.send_json(200, payload)
        with epias_request_lock:
            cached = data_cache.get(date)
            if cached and cached["expires"] > time.time():
                payload = {**cached["payload"], "cached":True}
                return self.send_json(200, payload)
            return self.fetch_data(date, current)

    def fetch_data(self, date, current):
        _, session = current
        tgt = session["tgt"]
        body = {"startDate":f"{date}T00:00:00+03:00", "endDate":f"{date}T00:00:00+03:00", "page":{"number":1,"size":100}}
        warnings, price_payloads = [], {}
        for label, endpoint in (("PTF", PTF_URL), ("SMF", SMF_URL)):
            try:
                price_payloads[label] = epias_post(endpoint, tgt, body)
            except EpiasError as error:
                if error.status in (401, 403):
                    message = f"{label} servisi hesabın web servis yetkisini kabul etmedi."
                elif error.status == 400:
                    message = f"{label} verisi bu tarih için henüz yayınlanmamış veya kullanılamıyor."
                else:
                    message = f"{label} verisi alınamadı ({error.status})."
                warnings.append(message)

        yal, yat, quantity_totals, quantity_total_sources = {}, {}, {}, {}
        quantity_sources = {
            "YAL": ("order-summary-up", ("upRegulationZeroCoded", "upRegulationOneCoded", "upRegulationTwoCoded")),
            "YAT": ("order-summary-down", ("downRegulationZeroCoded", "downRegulationOneCoded", "downRegulationTwoCoded")),
        }
        for label, (endpoint, coded_fields) in quantity_sources.items():
            try:
                quantity_payload = epias_post(f"{ELECTRICITY_URL}/{endpoint}", tgt, body)
                data = quantities(quantity_payload, coded_fields)
                prefix = "upRegulation" if label == "YAL" else "downRegulation"
                stats = response_section(quantity_payload, "statistics", "statistic")
                official_parts = [number_from(stats, (f"{prefix}{code}CodedTotal",)) for code in ("Zero", "One", "Two")]
                official_parts = [value for value in official_parts if value is not None]
                if official_parts:
                    quantity_totals[label] = sum(official_parts)
                    quantity_total_sources[label] = "EPİAŞ statistics resmî toplamı"
                else:
                    coded_values = [value for value in data.values() if value is not None]
                    quantity_totals[label] = sum(coded_values) if coded_values else None
                    quantity_total_sources[label] = "EPİAŞ 0+1+2 kodlu resmî alanları"
                if label == "YAL": yal = data
                else: yat = data
            except EpiasError as error:
                warnings.append(f"{label} verisi alınamadı ({error.status}).")

        direction_by_hour = {}
        try:
            direction_payload = epias_post(DIRECTION_URL, tgt, body)
            direction_by_hour = {hour_key(row, i): row.get("systemDirection") for i, row in enumerate(get_items(direction_payload))}
        except EpiasError as error:
            warnings.append(f"Sistem yönü alınamadı ({error.status}).")

        ptf_by_hour = {hour_key(row, i): row for i, row in enumerate(get_items(price_payloads.get("PTF", {})))}
        smf_by_hour = {hour_key(row, i): row for i, row in enumerate(get_items(price_payloads.get("SMF", {})))}
        rows = []
        for hour in sorted(set(ptf_by_hour) | set(smf_by_hour)):
            ptf_row, smf_row = ptf_by_hour.get(hour, {}), smf_by_hour.get(hour, {})
            rows.append({"hour":hour,"time":f"{hour:02}:00","ptf":number_from(ptf_row,("price",)),"smf":number_from(smf_row,("systemMarginalPrice",)),"yal":yal.get(hour),"yat":yat.get(hour),"direction":direction_by_hour.get(hour)})

        def calculated_average(key):
            values = [row[key] for row in rows if row[key] is not None]
            return sum(values) / len(values) if values else None
        ptf_stats = response_section(price_payloads.get("PTF", {}), "statistic", "statistics")
        smf_stats = response_section(price_payloads.get("SMF", {}), "statistics", "statistic")
        calculated_ptf = calculated_average("ptf")
        calculated_smf = calculated_average("smf")
        epias_ptf = number_from(ptf_stats,("priceAvg",))
        epias_smf = number_from(smf_stats,("smpArithmeticalAverage",))
        validation = {
            "ptf": {"field":"price", "items":len(ptf_by_hour), "calculatedAverage":calculated_ptf, "epiasAverage":epias_ptf},
            "smf": {"field":"systemMarginalPrice", "items":len(smf_by_hour), "calculatedAverage":calculated_smf, "epiasAverage":epias_smf},
            "yal": {"field":"upRegulationZeroCoded + upRegulationOneCoded + upRegulationTwoCoded", "items":len(yal)},
            "yat": {"field":"downRegulationZeroCoded + downRegulationOneCoded + downRegulationTwoCoded", "items":len(yat)},
            "direction": {"field":"systemDirection", "items":len(direction_by_hour)},
        }
        if date < time.strftime("%Y-%m-%d", time.localtime()):
            for label in ("yal", "yat", "direction"):
                if 0 < validation[label]["items"] < 23:
                    warnings.append(f"{label.upper()} için yalnızca {validation[label]['items']} saatlik kayıt eşleştirildi; veri eksik olabilir.")
        for label in ("ptf", "smf"):
            check = validation[label]
            if check["calculatedAverage"] is not None and check["epiasAverage"] is not None and abs(check["calculatedAverage"] - check["epiasAverage"]) > .02:
                warnings.append(f"{label.upper()} ortalaması EPİAŞ istatistiğiyle uyuşmuyor; veri kontrol edilmeli.")
        summary = {
            "ptfAverage": epias_ptf if epias_ptf is not None else calculated_ptf,
            "smfAverage": epias_smf if epias_smf is not None else calculated_smf,
            "yalTotal": quantity_totals.get("YAL"),
            "yatTotal": abs(quantity_totals["YAT"]) if quantity_totals.get("YAT") is not None else None,
            "ptfAverageSource": "EPİAŞ statistic.priceAvg" if epias_ptf is not None else "Saatlik veriler",
            "smfAverageSource": "EPİAŞ statistics.smpArithmeticalAverage" if epias_smf is not None else "Saatlik veriler",
            "yalTotalSource": quantity_total_sources.get("YAL"),
            "yatTotalSource": quantity_total_sources.get("YAT"),
        }
        payload = {"date":date,"rows":rows,"summary":summary,"warnings":warnings,"validation":validation,"updatedAt":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"cached":False}
        if rows and not warnings:
            today = time.strftime("%Y-%m-%d", time.localtime())
            data_cache[date] = {"payload":payload, "expires":time.time() + (300 if date == today else 21600)}
        return self.send_json(200, payload)

if __name__ == "__main__":
    try:
        server = ExclusiveThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as error:
        if getattr(error, "winerror", None) == 10048:
            print(f"Baha Enerji paneli zaten açık: http://127.0.0.1:{PORT}")
            print("Yeni bir sunucu başlatılmadı. Mevcut tarayıcı sekmesini kullanabilirsiniz.")
            raise SystemExit(1)
        raise
    print(f"EPİAŞ Paneli: http://{HOST}:{PORT}")
    print("Sunucuyu durdurmak için Ctrl+C tuşlarına basın.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSunucu durduruluyor...")
    finally:
        server.server_close()
        print("Sunucu durduruldu. Port serbest bırakıldı.")
