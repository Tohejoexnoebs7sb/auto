from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

# Module-local state used by the dedup cache. The original monolith kept these
# in its global namespace; after modularization they must exist in this module.
_SEEN_CONFIG_KEYS = set()
_SEEN_PROXY_KEYS = set()
_DEDUP_CACHE_READY = False
_ARCHIVED_CONFIG_KEYS = set()
_ARCHIVED_PROXY_KEYS = set()
_ARCHIVE_LOCK = threading.Lock()

def _archive_path(kind):
    return CONFIG_ARCHIVE_FILE if kind == "config" else PROXY_ARCHIVE_FILE

def _load_archive(kind):
    """Load compact gzip archive entries. Config rows may include backup_num."""
    result = []
    path = _archive_path(kind)
    if not os.path.isfile(path):
        return result
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" not in line:
                    continue
                parts = line.split("\t", 2)
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                if kind == "config" and len(parts) == 3:
                    try: backup_num = int(parts[1] or 0)
                    except ValueError: backup_num = 0
                    value = parts[2]
                    if value: result.append((pid, value, backup_num))
                elif len(parts) >= 2 and parts[1]:
                    result.append((pid, parts[1]))
    except Exception:
        log.exception("archive load failed: %s", path)
    return result

def _append_archive(kind, rows):
    if not rows:
        return 0
    path = _archive_path(kind)
    seen = _ARCHIVED_CONFIG_KEYS if kind == "config" else _ARCHIVED_PROXY_KEYS
    written = 0
    with _ARCHIVE_LOCK:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        try:
            with gzip.open(path, "at", encoding="utf-8") as f:
                for row in rows:
                    pid = row[0]
                    value = row[1] if len(row) > 1 else ""
                    extra = row[2] if len(row) > 2 else None
                    if not value:
                        continue
                    key = (int(pid), _config_identity_hash(value) if kind == "config" else _proxy_identity_hash(value))
                    if key in seen:
                        continue
                    if kind == "config" and extra is not None:
                        f.write(f"{int(pid)}\t{int(extra or 0)}\t{value}\n")
                    else:
                        f.write(f"{int(pid)}\t{value}\n")
                    seen.add(key)
                    written += 1
        except Exception:
            log.exception("archive write failed: %s", path)
    return written

def archive_current_posted_data():
    """Move short-lived URL copies into compact gzip archives before DB cleanup."""
    counts = {"configs": 0, "proxies": 0, "ok": True}
    db = None
    try:
        # This function is executed from the maintenance thread. Never use the
        # event-loop's shared cursor here; a private connection prevents thread
        # races with Telegram callbacks and posting workers.
        db = get_conn()
        cur = db.cursor()
        cfg_rows = cur.execute("SELECT profile_id,full_url,backup_num FROM seen WHERE full_url IS NOT NULL AND full_url!=''").fetchall()
        prx_rows = cur.execute("SELECT profile_id,proxy_url FROM proxies_seen WHERE proxy_url IS NOT NULL AND proxy_url!=''").fetchall()
        counts["configs"] = _append_archive("config", cfg_rows)
        counts["proxies"] = _append_archive("proxy", prx_rows)
    except sqlite3.Error:
        counts["ok"] = False
        log.exception("archive current posted data failed")
    finally:
        if db is not None:
            db.close()
    return counts

def load_archive_dedup_cache():
    global _ARCHIVED_CONFIG_KEYS, _ARCHIVED_PROXY_KEYS
    for row in _load_archive("config"):
        pid, url = row[0], row[1]
        _ARCHIVED_CONFIG_KEYS.add((pid, _config_identity_hash(url)))
    for row in _load_archive("proxy"):
        pid, url = row[0], row[1]
        _ARCHIVED_PROXY_KEYS.add((pid, _proxy_identity_hash(url)))
    _SEEN_CONFIG_KEYS.update(_ARCHIVED_CONFIG_KEYS)
    _SEEN_PROXY_KEYS.update(_ARCHIVED_PROXY_KEYS)
    log.info("[ARCHIVE] dedup archive loaded: configs=%d proxies=%d", len(_ARCHIVED_CONFIG_KEYS), len(_ARCHIVED_PROXY_KEYS))

def backup_archive_file(kind, destination):
    source = _archive_path(kind)
    if not os.path.isfile(source):
        return False
    shutil.copy2(source, destination)
    return True

def replace_archive_file(kind, source_path):
    """Validate and atomically replace one compact archive, then refresh memory dedup."""
    path = _archive_path(kind)
    tmp = path + ".tmp"
    # Parse first so a corrupt upload can never replace the active archive.
    rows = []
    try:
        with gzip.open(source_path, "rt", encoding="utf-8", errors="strict") as f:
            for line in f:
                line=line.rstrip("\n")
                if "\t" not in line: continue
                parts=line.split("\t",2)
                if kind == "config" and len(parts) == 3:
                    rows.append((int(parts[0]), parts[2], int(parts[1] or 0)))
                elif len(parts) >= 2:
                    rows.append((int(parts[0]), parts[1]))
    except Exception as exc:
        return False, f"آرشیو معتبر نیست: {str(exc)[:180]}"
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            seen_local=set()
            for row in rows:
                pid,value=row[0],row[1]
                extra=row[2] if len(row)>2 else None
                if not value: continue
                key=(pid, _config_identity_hash(value) if kind=="config" else _proxy_identity_hash(value))
                if key in seen_local: continue
                seen_local.add(key)
                if kind=="config" and extra is not None:
                    f.write(f"{pid}\t{int(extra or 0)}\t{value}\n")
                else:
                    f.write(f"{pid}\t{value}\n")
        os.replace(tmp, path)
        _ARCHIVED_CONFIG_KEYS.clear()
        _ARCHIVED_PROXY_KEYS.clear()
        _SEEN_CONFIG_KEYS.clear()
        _SEEN_PROXY_KEYS.clear()
        global _DEDUP_CACHE_READY
        _DEDUP_CACHE_READY = False
        load_dedup_cache()
        return True, f"{len(rows)} رکورد بررسی شد."
    except Exception as exc:
        try: os.remove(tmp)
        except OSError: pass
        return False, f"جایگزینی انجام نشد: {str(exc)[:180]}"

def export_archive_rows(kind, profile_id=None):
    rows = _load_archive(kind)
    if profile_id is not None:
        rows = [row for row in rows if int(row[0])==int(profile_id)]
    return rows

def get_country_info(code):
    """Return (flag, english_name, persian_name) for a country code."""
    if not code or len(code) != 2:
        return "🌐", "Unknown", "ناشناخته"
    flag = country_to_flag(code)
    en = COUNTRY_NAMES_EN.get(code, code)
    fa = COUNTRY_NAMES_FA.get(code, code)
    return flag, en, fa

def country_to_flag(code):
    if not code or len(code) != 2 or not code.isalpha():
        return "🌐"
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)

async def get_flag_for_ip(ip):
    cached = c.execute(
        "SELECT country, flag FROM country_cache WHERE ip=?", (ip,)
    ).fetchone()
    if cached and len(cached[1]) > 1:
        return cached[1], cached[0]  # flag, country_code

    try:
        async with httpx.AsyncClient(timeout=3) as cl:
            r = await cl.get(
                f"http://ip-api.com/json/{ip}?fields=countryCode",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                data = r.json()
                country = data.get("countryCode", "").upper()
                if country:
                    flag = country_to_flag(country)
                    c.execute(
                        "INSERT OR REPLACE INTO country_cache VALUES (?,?,?)",
                        (ip, country, flag))
                    conn.commit()
                    return flag, country
    except Exception as e:
        log.warning(f"flag API fail for {ip}: {e}")

    return "🌐", ""

def clean_proxy_link(url):
    if not url:
        return url
    url = url.strip()
    while url and url[-1] in ".,;:!؟\"'`(){}[]<>":
        url = url[:-1]
    return url

def normalize_telegram_proxy(url):
    if not url:
        return url
    url = clean_proxy_link(url)
    url = url.replace('&amp;', '&')
    url = re.sub(r'&amp;', '&', url, flags=re.IGNORECASE)
    try:
        url = unquote(url)
    except:
        pass
    return url

def clean_config_url(url: str) -> str:
    """Normalize a config URI without decoding reserved characters globally.

    Trojan passwords are frequently percent-encoded (for example ``%23`` for
    ``#``). Decoding the complete URI before parsing would turn an encoded
    password character into a URL delimiter and corrupt the netloc. Only HTML
    entities and surrounding whitespace are normalized here; percent-encoding
    is intentionally preserved.
    """
    if not url:
        return url
    url = html.unescape(str(url)).strip()
    url = url.replace("&amp;", "&")
    return url

def validate_vless(url):
    """Validate VLESS URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "vless":
            return False, "not vless"
        # Check for userinfo (UUID)
        if not parsed.username or len(parsed.username) < 8:
            return False, "missing or invalid UUID"
        # Check host
        if not parsed.hostname:
            return False, "missing host"
        # Check port
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def normalize_vmess_url(url, name=""):
    """Normalize VMess to standard JSON-base64 form. Name is stored only in ps."""
    try:
        raw = url.split("vmess://", 1)[1].strip()
        raw = raw.split("#", 1)[0]
        raw = raw.strip()
        raw += "=" * (-len(raw) % 4)
        obj = json.loads(base64.b64decode(raw).decode("utf-8", errors="ignore"))
        if name:
            obj["ps"] = str(name).strip()
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        return "vmess://" + base64.b64encode(payload).decode()
    except Exception as e:
        log.warning(f"VMESS normalize failed: {e}")
        return None

def apply_display_name_to_config(url, name):
    """
    Change display name safely.
    VMess has no URI fragment name in many clients; the name must be written
    into JSON field ps and the vmess payload must be rebuilt.
    Other protocols keep their normal #fragment behaviour.
    """
    if not name:
        return url
    try:
        if (url or "").lower().startswith("vmess://"):
            fixed = normalize_vmess_url(url, name)
            return fixed or url
    except Exception:
        pass
    return url

def validate_vmess(url):
    """Validate VMESS URL structure (base64 encoded JSON)."""
    try:
        if not url.startswith("vmess://"):
            return False, "not vmess"
        b64 = url[8:]
        # Add padding if needed
        b64 += "=" * (-len(b64) % 4)
        data = base64.b64decode(b64, validate=True).decode('utf-8', errors='ignore')
        # Parse JSON
        obj = json.loads(data)
        required = ['add', 'port', 'id', 'aid', 'net', 'type', 'host', 'path', 'tls']
        for key in ['add', 'port', 'id']:
            if key not in obj or not obj[key]:
                return False, f"missing {key}"
        if not str(obj['port']).isdigit() or int(obj['port']) < 1 or int(obj['port']) > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception as e:
        return False, f"decode error: {str(e)}"

def validate_trojan(url):
    """Validate standard Trojan URIs, including percent-encoded passwords.

    Supported common forms include:
      trojan://PASSWORD@HOST:PORT
      trojan://PASSWORD@HOST:PORT?security=tls&sni=example.com
      trojan://user:PASSWORD@HOST:PORT?...   (accepted for compatibility)

    The credential is opaque data; it must not be URL-decoded before parsing
    because encoded delimiters such as %23 and %40 are valid password bytes.
    """
    try:
        value = clean_config_url(url)
        parsed = urlparse(value)
        if parsed.scheme.lower() != "trojan":
            return False, "not trojan"

        if not parsed.hostname:
            return False, "missing host"

        try:
            port = parsed.port
        except ValueError:
            return False, "invalid port"
        if not port or not (1 <= port <= 65535):
            return False, "invalid port"

        # In the normal Trojan form (PASSWORD@HOST), urllib exposes the
        # password as ``username`` because there is no separate user field.
        credential = parsed.password if parsed.password is not None else parsed.username
        if credential is None or credential == "":
            return False, "missing password"

        # Validate percent escapes in the credential without changing the URI.
        # urllib.parse.unquote tolerates malformed escapes, so explicitly
        # reject a stray '%' followed by a non-hex pair.
        if re.search(r"%(?![0-9A-Fa-f]{2})", credential):
            return False, "invalid percent-encoding in password"

        # A raw '@' in userinfo would be parsed as a delimiter. Encoded %40
        # remains part of the credential and is therefore fully supported.
        return True, "valid"
    except Exception as e:
        return False, f"parse error: {e}"

def validate_ss(url):
    """Validate Shadowsocks URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "ss":
            return False, "not ss"
        # Check for valid base64 userinfo
        if parsed.username and parsed.password:
            return True, "valid"
        else:
            # May be in format ss://base64
            b64 = url[5:]
            b64 += "=" * (-len(b64) % 4)
            decoded = base64.b64decode(b64, validate=True).decode('utf-8', errors='ignore')
            if '@' in decoded:
                return True, "valid"
            else:
                return False, "invalid format"
    except Exception:
        return False, "parse error"

def validate_socks(url):
    """Validate SOCKS URL structure (simple)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != 'socks':
            return False, "not socks"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_hy2(url):
    """Validate Hysteria2 URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "hy2":
            return False, "not hy2"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_tuic(url):
    """Validate TUIC URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "tuic":
            return False, "not tuic"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_wireguard(url):
    """Validate WireGuard URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "wireguard":
            return False, "not wireguard"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_http_proxy(url):
    """Legacy validator only; HTTP/HTTPS is not a supported bot proxy type."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "not http"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or not (1 <= parsed.port <= 65535):
            return False, "invalid port"
        # HTTP proxy URLs normally have credentials or an explicit proxy-like port.
        # Accept explicit host:port URLs because source channels commonly publish them.
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_hysteria(url):
    """Validate Hysteria v1 URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "hysteria":
            return False, "not hysteria"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or not (1 <= parsed.port <= 65535):
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def is_telegram_proxy_url(url):
    """Strictly accept Telegram proxies: MTProto links or SOCKS5 links."""
    if not url:
        return False
    u = html.unescape(str(url)).strip()
    if u.lower().startswith(("tg://proxy?", "https://t.me/proxy?")):
        return validate_telegram_proxy_url(u)[0]
    if u.lower().startswith(("socks5://", "socks5h://")):
        return validate_telegram_proxy_url(u)[0]
    return False

def detect_protocol_name(url):
    """Return a human-readable protocol name for ONLY the supported config/proxy schemes."""
    u = (url or "").strip().lower()
    if u.startswith("vless://"): return "VLESS"
    if u.startswith("vmess://"): return "VMESS"
    if u.startswith("trojan://"): return "TROJAN"
    if u.startswith(("wireguard://", "wg://")): return "WIREGUARD"
    if u.startswith(("hysteria://", "hysteria2://", "hy2://")): return "HYSTERIA2"
    if u.startswith(("wireguard://", "wg://")): return "WIREGUARD"
    if u.startswith(("shadowsocks://", "ss://")): return "SHADOWSOCKS"
    if u.startswith("socks://"): return "SOCKS"
    if is_telegram_proxy_url(u):
        return "MTPROTO" if u.startswith(("tg://proxy", "https://t.me/proxy")) else "SOCKS5"
    return "Unknown"

def validate_config_link(url):
    """Strict validation for the exact config protocols requested by the bot owner."""
    if not url:
        return False, "empty"
    url = clean_config_url(url.strip())
    if is_telegram_proxy_url(url):
        return True, "telegram proxy config"
    scheme = urlparse(url).scheme.lower()
    if scheme == "vless": return validate_vless(url)
    if scheme == "vmess": return validate_vmess(url)
    if scheme == "trojan": return validate_trojan(url)
    if scheme in ("wireguard", "wg"): return validate_wireguard(url.replace("wg://", "wireguard://", 1))
    if scheme in ("shadowsocks", "ss"): return validate_ss(url.replace("shadowsocks://", "ss://", 1))
    if scheme == "socks": return validate_socks(url)
    if scheme in ("hysteria2", "hy2"): return validate_hy2(url.replace("hysteria2://", "hy2://", 1))
    return False, "unsupported protocol"

def _link_name(url):
    try:
        return unquote(urlparse(url).fragment or "").strip()
    except Exception:
        return ""

def _decode_b64(s):
    try:
        s = s.strip().replace("-", "+").replace("_", "/")
        return base64.b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        return b""

def parse_vmess(url):
    try:
        raw=url.split("vmess://",1)[1].split("#",1)[0].strip()
        obj=json.loads(_decode_b64(raw).decode("utf-8"))
        if not obj.get("add") or not obj.get("port") or not obj.get("id"):
            return {"protocol":"VMESS","url":url,"name":"","valid":False,"metadata":{}}
        obj["ps"]=obj.get("ps") or _link_name(url)
        payload=base64.b64encode(json.dumps(obj,ensure_ascii=False,separators=(",",":")).encode()).decode()
        return {"protocol":"VMESS","url":"vmess://"+payload,"name":obj.get("ps",""),"valid":True,"metadata":obj}
    except Exception:
        return {"protocol":"VMESS","url":url,"name":"","valid":False,"metadata":{}}

def parse_vless(url):
    try:
        p=urlparse(url)
        return {"protocol":"VLESS","url":url,"name":_link_name(url),
                "valid":bool(p.username and p.hostname and p.port),
                "metadata":parse_qs(p.query)}
    except Exception:
        return {"protocol":"VLESS","url":url,"name":"","valid":False,"metadata":{}}

def parse_trojan(url):
    try:
        p=urlparse(url)
        return {"protocol":"TROJAN","url":url,"name":_link_name(url),
                "valid":bool(p.username and p.hostname and p.port),
                "metadata":parse_qs(p.query)}
    except Exception:
        return {"protocol":"TROJAN","url":url,"name":"","valid":False,"metadata":{}}

def parse_ss(url):
    try:
        p=urlparse(url)
        return {"protocol":"SHADOWSOCKS","url":url,"name":_link_name(url),
                "valid":bool(p.hostname and p.port),"metadata":{}}
    except Exception:
        return {"protocol":"SHADOWSOCKS","url":url,"name":"","valid":False,"metadata":{}}

def parse_socks(url):
    try:
        raw = str(url).strip()
        # Telegram SOCKS links commonly contain a human name after #. Keep it as
        # part of the display name and never let it break the actual URL parser.
        p=urlparse(raw)
        return {"protocol":"SOCKS","url":raw,"name":_link_name(raw),
                "valid":bool(p.hostname and p.port),
                "metadata":{"user":p.username or "","password":p.password or ""}}
    except Exception:
        return {"protocol":"SOCKS","url":url,"name":"","valid":False,"metadata":{}}

def parse_hysteria(url):
    try:
        p=urlparse(url)
        return {"protocol":"HYSTERIA","url":url,"name":_link_name(url),
                "valid":bool(p.hostname and p.port),"metadata":parse_qs(p.query)}
    except Exception:
        return {"protocol":"HYSTERIA","url":url,"name":"","valid":False,"metadata":{}}

def parse_wireguard(url):
    try:
        p=urlparse(url)
        return {"protocol":"WIREGUARD","url":url,"name":_link_name(url),
                "valid":bool(p.hostname),"metadata":parse_qs(p.query)}
    except Exception:
        return {"protocol":"WIREGUARD","url":url,"name":"","valid":False,"metadata":{}}

def parse_telegram_proxy(url):
    try:
        p=urlparse(url)
        q=parse_qs(p.query)
        if p.scheme=="tg" and p.netloc=="proxy" or p.path.lower()=="/proxy":
            ok=bool(q.get("server") and q.get("port") and q.get("secret"))
            return {"protocol":"MTPROTO","url":url,"name":"","valid":ok,"metadata":q}
        if p.scheme=="tg" and p.netloc=="socks":
            ok=bool(q.get("server") and q.get("port"))
            return {"protocol":"SOCKS5","url":url,"name":"","valid":ok,"metadata":q}
    except Exception:
        pass
    return {"protocol":"TELEGRAM_PROXY","url":url,"name":"","valid":False,"metadata":{}}

def parse_config_url(url):
    s=url.lower()
    if s.startswith("vmess://"): return parse_vmess(url)
    if s.startswith("vless://"): return parse_vless(url)
    if s.startswith("trojan://"): return parse_trojan(url)
    if s.startswith(("ss://","ssr://")): return parse_ss(url)
    if s.startswith("socks://"): return parse_socks(url)
    if s.startswith(("hy2://","hysteria://","hysteria2://")): return parse_hysteria(url)
    if s.startswith(("wg://","wireguard://", "https://t.me/proxy?", "tg://proxy?")): return parse_wireguard(url)
    return {"protocol":"","url":url,"name":"","valid":False,"metadata":{}}

def extract_links_from_text(text):
    """Extract published configuration links without confusing them with bot proxies."""
    if not text:
        return []
    source = html.unescape(text)
    patterns = [
        r'(?:vless|vmess|trojan|ss|ssr|shadowsocks|socks|socks5|socks5h|hy2|hysteria|hysteria2|wg|wireguard)://[^\s<>"\']+'
    ]
    out=[]; seen=set()
    for pat in patterns:
        for m in re.finditer(pat, source, re.I):
            u=m.group(0).rstrip('.,;:!؟)]}')
            # Telegram proxy links are handled only by extract_proxy_links_from_text()
            # Never count MTProto proxies as VPN configs.
            if u.lower().startswith(('http://t.me/proxy?', 'https://t.me/proxy?')):
                continue
            item=parse_config_url(u)
            if item.get('valid'):
                u=item.get('url',u)
                key=hashlib.sha256(u.encode()).hexdigest()
                if key not in seen:
                    seen.add(key)
                    out.append(u)
    return out

def validate_telegram_proxy_url(url):
    """Strict Telegram proxy validation: MTProto or SOCKS5 only."""
    if not url:
        return False, "empty"
    u = html.unescape(clean_proxy_link(str(url))).strip()
    try:
        p = urlparse(u)
        scheme = p.scheme.lower()
        if scheme == "https" and p.hostname and p.hostname.lower() == "t.me" and p.path.lower() == "/proxy":
            q = parse_qs(p.query, keep_blank_values=True)
            server = (q.get("server") or [""])[0].strip()
            port_raw = (q.get("port") or [""])[0].strip()
            secret = (q.get("secret") or [""])[0].strip()
            if not server or not secret or not port_raw.isdigit():
                return False, "missing server/port/secret"
            port = int(port_raw)
            if not (1 <= port <= 65535):
                return False, "invalid port"
            return True, "MTPROTO"
        if scheme == "tg" and p.netloc.lower() == "proxy":
            q = parse_qs(p.query, keep_blank_values=True)
            server = (q.get("server") or [""])[0].strip()
            port_raw = (q.get("port") or [""])[0].strip()
            secret = (q.get("secret") or [""])[0].strip()
            if not server or not secret or not port_raw.isdigit():
                return False, "missing server/port/secret"
            if not (1 <= int(port_raw) <= 65535):
                return False, "invalid port"
            return True, "MTPROTO"
        if scheme in ("socks", "socks5"):
            if not p.hostname:
                return False, "missing host"
            if p.port is None or not (1 <= p.port <= 65535):
                return False, "invalid port"
            return True, "SOCKS5"
        if scheme == "tg" and p.netloc.lower() == "socks":
            q = parse_qs(p.query, keep_blank_values=True)
            if (q.get("server") or [""])[0] and (q.get("port") or [""])[0].isdigit():
                return True, "SOCKS5"
    except Exception as e:
        return False, f"parse error: {e}"
    return False, "not a Telegram proxy"

def canonical_telegram_proxy_url(url):
    """Canonical Telegram proxy URL. MTProto is normalized to t.me/proxy; SOCKS5 is preserved."""
    norm = normalize_telegram_proxy(url or "")
    if not norm:
        return None
    norm = html.unescape(norm).strip()
    ok, kind = validate_telegram_proxy_url(norm)
    if not ok:
        return None
    if kind == "MTPROTO":
        if norm.lower().startswith("tg://proxy?"):
            norm = "https://t.me/proxy?" + norm.split("?", 1)[1]
        p = urlparse(norm)
        q = parse_qs(p.query, keep_blank_values=True)
        query = urlencode(sorted((k.lower(), v) for k, vals in q.items() for v in vals), doseq=True)
        return "https://t.me/proxy?" + query
    p = urlparse(norm)
    q = urlencode(sorted((k.lower(), v) for k, vals in parse_qs(p.query, keep_blank_values=True).items() for v in vals), doseq=True)
    return urlunparse(("socks", p.netloc, p.path, p.params, q, ""))

def normalize_proxy_url(url):
    return canonical_telegram_proxy_url(url)

def extract_proxy_links_from_text(text):
    """Extract ONLY Telegram MTProto and Telegram SOCKS5 links, including raw href attributes."""
    if not text:
        return []
    source = html.unescape(text)
    results = []
    seen = set()
    patterns = [
        r'https?://t\.me/proxy\?[^\s<>"\']+',
        r'tg://proxy\?[^\s<>"\']+',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, source, re.IGNORECASE):
            norm = canonical_telegram_proxy_url(clean_proxy_link(m.group(0)).rstrip('.,;:!?)]}\'"'))
            if norm:
                ident = canonical_proxy_identity(norm)
                if ident not in seen:
                    seen.add(ident)
                    results.append(norm)
    return results

def extract_uuid_and_address(url):
    try:
        clean = url.split("#")[0] if "#" in url else url
        clean = clean.split("?")[0] if "?" in clean else clean
        after = clean.split("://", 1)[1] if "://" in clean else clean
        if url.startswith("vmess://"):
            try:
                b64 = after + "=" * (-len(after) % 4)
                data = json.loads(
                    base64.b64decode(b64).decode('utf-8', errors='ignore'))
                uid = data.get("id", "") or data.get("uuid", "")
                host = f"{data.get('add','')}:{data.get('port','')}"
                return uid, host
            except Exception:
                return ("vmess_" +
                        hashlib.md5(after.encode()).hexdigest()[:16]), after
        else:
            if "@" in after:
                uid, host = after.split("@", 1)
            else:
                uid, host = after, ""
            if "/" in host:
                host = host.split("/")[0]
            return (uid.split("?")[0].split("#")[0],
                    host.split("?")[0].split("#")[0])
    except Exception:
        return "", ""

def _legacy_canonical_config_identity(url):
    """Compatibility identity used only for hashes created by <=4.1.3."""
    try:
        url = clean_config_url(url or "").strip()
        p = urlparse(url)
        scheme = p.scheme.lower()
        if scheme == "vmess":
            raw = unquote(p.netloc + p.path)
            raw += "=" * (-len(raw) % 4)
            try:
                obj = json.loads(base64.b64decode(raw).decode("utf-8", errors="ignore"))
                keys = ["v","add","port","id","aid","scy","net","type","host","path","tls","sni","alpn","fp","allowInsecure"]
                obj = {k: str(obj.get(k, "")) for k in keys if k in obj}
                return "vmess|" + json.dumps(obj, sort_keys=True, separators=(",", ":"))
            except Exception:
                pass
        pairs = parse_qs(p.query, keep_blank_values=True)
        query = urlencode(sorted((k, v) for k, vals in pairs.items() for v in vals), doseq=True)
        return urlunparse((scheme, p.netloc.lower(), p.path or "", p.params, query, ""))
    except Exception:
        return clean_config_url(url or "").split("#", 1)[0].strip()

def _q_first(q, *names):
    for name in names:
        for key in (name, name.lower(), name.upper()):
            if key in q:
                vals = q.get(key) or [""]
                return str(vals[0] or "").strip()
    return ""

def canonical_config_identity(url):
    """Strict connection identity.

    Display-only fragment and the bot's Telegram tagging query are ignored.
    The identity contains protocol, credential/UUID, ADDRESS, PORT, NETWORK and
    the remaining connection parameters. Therefore two links with the same real
    connection details are duplicates even if their Telegram/name tag differs.
    """
    try:
        u = clean_config_url(url or "").strip()
        p = urlparse(u)
        scheme = p.scheme.lower()
        if scheme == "vmess":
            raw = unquote(p.netloc + p.path)
            raw += "=" * (-len(raw) % 4)
            try:
                obj = json.loads(base64.b64decode(raw).decode("utf-8", errors="ignore"))
                # ps/name is display-only. Keep all transport/auth fields that
                # can affect the actual connection.
                ignored = {"ps", "name", "remarks", "remark"}
                fields = {}
                for k, v in obj.items():
                    lk = str(k).strip().lower()
                    if lk in ignored:
                        continue
                    if v is None:
                        v = ""
                    fields[lk] = str(v).strip()
                fields["protocol"] = "vmess"
                fields["address"] = str(obj.get("add", "") or "").strip().lower()
                fields["port"] = str(obj.get("port", "") or "").strip()
                fields["uuid"] = str(obj.get("id", obj.get("uuid", "")) or "").strip().lower()
                fields["network"] = str(obj.get("net", obj.get("type", "")) or "").strip().lower()
                return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            except Exception:
                # Fall through to a normalized URI identity; still ignore fragment.
                pass
        q = parse_qs(p.query, keep_blank_values=True)
        # Telegram channel/tag parameters are display metadata, not connection identity.
        ignored_query = {"telegram", "name", "remark", "remarks"}
        clean_q = []
        for k, vals in q.items():
            if str(k).strip().lower() in ignored_query:
                continue
            for v in vals:
                clean_q.append((str(k).strip().lower(), str(v).strip()))
        clean_q.sort()
        fields = {
            "protocol": scheme,
            "uuid": unquote(str(p.username or "")).strip().lower(),
            "password": unquote(str(p.password or "")).strip(),
            "address": str(p.hostname or "").strip().lower(),
            "port": str(p.port or "").strip(),
            "network": _q_first(q, "type", "net").lower(),
            "query": clean_q,
        }
        # For SOCKS/SS, credentials are part of the connection and already live
        # in username/password. For every protocol, the normalized query carries
        # path/host/security/reality/etc. exactly as configured.
        return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return clean_config_url(url or "").split("#", 1)[0].strip().lower()

def _config_identity_hash(url):
    return hashlib.sha256(canonical_config_identity(url).encode("utf-8", errors="ignore")).hexdigest()

def _legacy_config_identity_hash(url):
    return hashlib.sha256(_legacy_canonical_config_identity(url).encode("utf-8", errors="ignore")).hexdigest()

def _proxy_identity_hash(url):
    return hashlib.sha256(canonical_proxy_identity(url).encode("utf-8", errors="ignore")).hexdigest()

def load_dedup_cache():
    """Warm compact permanent dedup hashes in bounded batches."""
    global _DEDUP_CACHE_READY
    if _DEDUP_CACHE_READY:
        return
    try:
        load_archive_dedup_cache()
        cur = conn.cursor()
        cur.execute("SELECT profile_id,full_url,first_seen,source FROM seen WHERE full_url IS NOT NULL AND full_url!=''")
        while True:
            rows = cur.fetchmany(2000)
            if not rows: break
            cur.executemany(
                "INSERT OR IGNORE INTO posted_config_keys(profile_id,identity_hash,first_posted,source) VALUES (?,?,?,?)",
                [(int(pid), _config_identity_hash(url), fs, src or "") for pid,url,fs,src in rows]
            )
            conn.commit()
        cur.execute("SELECT profile_id,proxy_url,first_seen FROM proxies_seen WHERE proxy_url IS NOT NULL AND proxy_url!=''")
        while True:
            rows = cur.fetchmany(2000)
            if not rows: break
            cur.executemany(
                "INSERT OR IGNORE INTO posted_proxy_keys(profile_id,identity_hash,first_posted) VALUES (?,?,?)",
                [(int(pid), _proxy_identity_hash(url), fs) for pid,url,fs in rows]
            )
            conn.commit()
        cur.execute("SELECT profile_id,identity_hash FROM posted_config_keys")
        while True:
            rows = cur.fetchmany(5000)
            if not rows: break
            _SEEN_CONFIG_KEYS.update((int(pid), h) for pid,h in rows)
        cur.execute("SELECT profile_id,identity_hash FROM posted_proxy_keys")
        while True:
            rows = cur.fetchmany(5000)
            if not rows: break
            _SEEN_PROXY_KEYS.update((int(pid), h) for pid,h in rows)
        _DEDUP_CACHE_READY = True
        log.info("[DB] compact dedup cache ready: configs=%d proxies=%d", len(_SEEN_CONFIG_KEYS), len(_SEEN_PROXY_KEYS))
    except sqlite3.Error:
        log.exception("[DB] compact dedup cache load failed")

def is_already_posted(profile_id, url):
    try:
        pid = int(profile_id)
        new_hash = _config_identity_hash(url)
        legacy_hash = _legacy_config_identity_hash(url)
        if _DEDUP_CACHE_READY:
            return ((pid, new_hash) in _SEEN_CONFIG_KEYS or
                    (pid, legacy_hash) in _SEEN_CONFIG_KEYS)
        return c.execute(
            "SELECT 1 FROM posted_config_keys WHERE profile_id=? AND identity_hash IN (?,?) LIMIT 1",
            (pid, new_hash, legacy_hash)
        ).fetchone() is not None
    except sqlite3.Error as exc:
        log.warning("config duplicate check failed: %s", exc)
        return False

def canonical_proxy_identity(url):
    try:
        norm = canonical_telegram_proxy_url(url or "") or (url or "").strip()
        p = urlparse(norm)
        pairs = parse_qs(p.query, keep_blank_values=True)
        query = urlencode(sorted((k, v) for k, vals in pairs.items() for v in vals), doseq=True)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, p.params, query, ""))
    except Exception:
        return (url or "").strip().split("#", 1)[0]

def is_proxy_posted(profile_id, proxy_url):
    try:
        key = (int(profile_id), _proxy_identity_hash(proxy_url))
        if _DEDUP_CACHE_READY:
            return key in _SEEN_PROXY_KEYS
        return c.execute("SELECT 1 FROM posted_proxy_keys WHERE profile_id=? AND identity_hash=? LIMIT 1", key).fetchone() is not None
    except sqlite3.Error as exc:
        log.warning("proxy duplicate check failed: %s", exc)
        return False

def mark_proxy_posted(profile_id, proxy_url):
    norm = canonical_telegram_proxy_url(proxy_url or "") or (proxy_url or "").strip()
    identity_hash = _proxy_identity_hash(norm)
    now = get_tehran_time()
    c.execute("INSERT OR IGNORE INTO posted_proxy_keys(profile_id,identity_hash,first_posted) VALUES (?,?,?)",
              (profile_id, identity_hash, now))
    c.execute("INSERT OR IGNORE INTO proxies_seen (proxy_url,first_seen,last_posted,profile_id) VALUES (?,?,?,?)",
              (norm, now, now, profile_id))
    conn.commit()
    _SEEN_PROXY_KEYS.add((int(profile_id), identity_hash))

def mark_as_posted(profile_id, url, source, full_url=""):
    clean = clean_config_url(url or "")
    identity_hash = _config_identity_hash(clean)
    now = get_tehran_time()
    c.execute("INSERT OR IGNORE INTO posted_config_keys(profile_id,identity_hash,first_posted,source) VALUES (?,?,?,?)",
              (profile_id, identity_hash, now, source or ""))
    # Keep only a short-lived URL copy for backup/export; the hash above is the
    # permanent once-only record.
    uid, host = extract_uuid_and_address(clean)
    if not uid and not host:
        uid = clean[:200]; host = ""
    c.execute("INSERT OR IGNORE INTO seen(uuid,address,source,first_seen,last_posted,profile_id,full_url,backup_num) VALUES (?,?,?,?,?,?,?,0)",
              (uid, host, source or "", now, now, profile_id, clean))
    conn.commit()
    _SEEN_CONFIG_KEYS.add((int(profile_id), identity_hash))

def mark_configs_posted_batch(profile_id, entries):
    if not entries:
        return 0
    now = get_tehran_time()
    cfg_rows = []
    seen_rows = []
    for entry in entries:
        url = entry[0] if len(entry) > 0 else ""
        source = entry[1] if len(entry) > 1 else ""
        # In Quote/normal rendering the display URL can receive a custom query
        # or name fragment. Dedup must use the ORIGINAL scraped URL so changing
        # a display template/query can never make the same config look new.
        identity_url = entry[2] if len(entry) > 2 and entry[2] else url
        clean = clean_config_url(url or "")
        identity_clean = clean_config_url(identity_url or "")
        h = _config_identity_hash(identity_clean)
        cfg_rows.append((profile_id, h, now, source or ""))
        uid, host = extract_uuid_and_address(clean)
        if not uid and not host:
            uid = clean[:200]; host = ""
        seen_rows.append((uid, host, source or "", now, now, profile_id, clean))
    c.executemany("INSERT OR IGNORE INTO posted_config_keys(profile_id,identity_hash,first_posted,source) VALUES (?,?,?,?)", cfg_rows)
    c.executemany("INSERT OR IGNORE INTO seen(uuid,address,source,first_seen,last_posted,profile_id,full_url,backup_num) VALUES (?,?,?,?,?,?,?,0)", seen_rows)
    conn.commit()
    for pid,h,_a,_b in cfg_rows:
        _SEEN_CONFIG_KEYS.add((int(pid), h))
    return len(entries)

def mark_proxies_posted_batch(profile_id, urls):
    if not urls:
        return 0
    now = get_tehran_time()
    key_rows = [(profile_id, _proxy_identity_hash(u), now) for u in urls]
    seen_rows = [(canonical_telegram_proxy_url(u or "") or (u or "").strip(), now, now, profile_id) for u in urls]
    c.executemany("INSERT OR IGNORE INTO posted_proxy_keys(profile_id,identity_hash,first_posted) VALUES (?,?,?)", key_rows)
    c.executemany("INSERT OR IGNORE INTO proxies_seen(proxy_url,first_seen,last_posted,profile_id) VALUES (?,?,?,?)", seen_rows)
    conn.commit()
    for pid,h,_t in key_rows:
        _SEEN_PROXY_KEYS.add((int(pid), h))
    return len(urls)

def is_message_processed(profile_id, source, message_id):
    r = c.execute("SELECT 1 FROM processed_messages WHERE source=? AND message_id=? AND profile_id=?", (source, message_id, profile_id)).fetchone()
    return r is not None

def mark_message_processed(profile_id, source, message_id):
    c.execute("INSERT OR REPLACE INTO processed_messages (source, message_id, profile_id) VALUES (?,?,?)",
              (source, message_id, profile_id))
    conn.commit()

def get_last_scrape_time(profile_id, source):
    r = c.execute("SELECT last_scrape_time FROM last_scrape WHERE source=? AND profile_id=?", (source, profile_id)).fetchone()
    return r[0] if r else None

def update_last_scrape_time(profile_id, source, time_str, last_message_id=""):
    c.execute("INSERT OR REPLACE INTO last_scrape (source, last_scrape_time, profile_id, last_message_id) VALUES (?,?,?,?)",
              (source, time_str, profile_id, last_message_id))
    conn.commit()

def get_last_message_id(profile_id, source):
    r = c.execute("SELECT last_message_id FROM last_scrape WHERE source=? AND profile_id=?", (source, profile_id)).fetchone()
    return r[0] if r else ""

def update_last_message_id(profile_id, source, msg_id):
    c.execute("UPDATE last_scrape SET last_message_id=? WHERE source=? AND profile_id=?", (msg_id, source, profile_id))
    conn.commit()

def get_stream_last_message_id(profile_id, source, stream):
    r = c.execute(
        "SELECT last_message_id FROM source_stream_state WHERE profile_id=? AND source=? AND stream=?",
        (profile_id, source, stream)
    ).fetchone()
    return r[0] if r else ""

def set_stream_last_message_ids_batch(profile_id, stream, updates):
    rows = [(int(profile_id), str(src), str(stream), str(msg_id or ""), get_tehran_time())
            for src, msg_id in updates if msg_id]
    if not rows:
        return
    c.executemany("""INSERT INTO source_stream_state
        (profile_id,source,stream,last_message_id,updated_at) VALUES (?,?,?,?,?)
        ON CONFLICT(profile_id,source,stream) DO UPDATE SET
        last_message_id=excluded.last_message_id,
        updated_at=excluded.updated_at
        WHERE source_stream_state.last_message_id IS NOT excluded.last_message_id""", rows)
    conn.commit()

def set_stream_last_message_id(profile_id, source, stream, msg_id):
    c.execute(
        """INSERT INTO source_stream_state
           (profile_id, source, stream, last_message_id, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(profile_id, source, stream) DO UPDATE SET
             last_message_id=excluded.last_message_id,
             updated_at=excluded.updated_at""",
        (profile_id, source, stream, str(msg_id or ""), get_tehran_time())
    )
    conn.commit()

def strip_url_fragment(url):
    if '#' in url:
        return url.split('#')[0]
    return url

def extract_host(url):
    """
    Extract ONLY the actual target host used by Check-Host.
    VMess is special: its payload is Base64(JSON), so it must be decoded first
    and the JSON `add` field is used as the host. Other supported URI schemes
    use their URI hostname (or the Telegram proxy `server` parameter).
    """
    try:
        raw_url = html.unescape(str(url or "")).strip()
        if not raw_url:
            return None, None

        # VMess: decode the payload first; never try to parse the Base64 text as
        # a URI hostname.
        if raw_url.lower().startswith("vmess://"):
            payload = raw_url.split("://", 1)[1].split("#", 1)[0].strip()
            try:
                decoded = _decode_b64(payload).decode("utf-8", errors="strict")
                obj = json.loads(decoded)
                host = str(obj.get("add") or obj.get("address") or "").strip()
                port_raw = obj.get("port")
                try:
                    port = int(port_raw) if port_raw not in (None, "") else None
                except (TypeError, ValueError):
                    port = None
                if host:
                    return host, port
            except Exception as e:
                log.debug("VMESS host extraction failed: %s", e)
                return None, None

        parsed = urlparse(raw_url)
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            port = None

        # Telegram MTProto/SOCKS proxy links keep the actual server in
        # ?server=... rather than in the URI hostname.
        if parsed.scheme.lower() in ("https", "http", "tg") and (
            parsed.path.lower().startswith("/proxy") or parsed.netloc.lower() == "proxy"
        ):
            query = parse_qs(parsed.query)
            server = (query.get("server") or [None])[0]
            if server:
                server = str(server).strip()
                if server.startswith("[") and "]" in server:
                    close = server.find("]")
                    host = server[1:close]
                    remainder = server[close + 1:]
                    if remainder.startswith(":") and remainder[1:].isdigit():
                        port = int(remainder[1:])
                elif server.count(":") == 1:
                    maybe_host, maybe_port = server.rsplit(":", 1)
                    if maybe_port.isdigit():
                        host, port = maybe_host, int(maybe_port)
                    else:
                        host = server
                else:
                    host = server
                if port is None:
                    port_raw = (query.get("port") or [None])[0]
                    if str(port_raw or "").isdigit():
                        port = int(port_raw)
                return host, port
            return host, port

        if host:
            return host, port

        # Conservative fallback for legacy/non-standard forms.
        candidate = raw_url
        if "://" in candidate:
            candidate = candidate.split("://", 1)[1]
        candidate = candidate.split("?", 1)[0].split("#", 1)[0]
        if "@" in candidate:
            candidate = candidate.rsplit("@", 1)[-1]
        candidate = candidate.strip()

        if candidate.startswith("[") and "]" in candidate:
            close = candidate.find("]")
            host = candidate[1:close]
            rest = candidate[close + 1:]
            port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
            return host, port

        if ":" in candidate:
            maybe_host, maybe_port = candidate.rsplit(":", 1)
            if maybe_port.isdigit():
                return maybe_host.strip(), int(maybe_port)

        return candidate or None, None
    except Exception as e:
        log.warning(f"extract_host error for {url}: {e}")
        return None, None
