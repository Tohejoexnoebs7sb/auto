from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

def add_custom_query_to_url(url, custom_query, protocol):
    if not custom_query or protocol.lower() == 'vmess':
        return url
    url = clean_config_url(url)
    if '#' in url:
        base, fragment = url.split('#', 1)
    else:
        base = url
        fragment = None
    parsed = urlparse(base)
    existing_params = parse_qs(parsed.query)
    custom_params = parse_qs(custom_query)
    new_query_dict = {}
    for k, v in custom_params.items():
        new_query_dict[k] = v[-1] if v else ""
    for k, v in existing_params.items():
        if k not in new_query_dict:
            new_query_dict[k] = v[-1] if v else ""
    new_query = urlencode(new_query_dict, doseq=True)
    new_base = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ''))
    if fragment:
        new_base += '#' + fragment
    return new_base

async def _get_ping_client():
    global _PING_CLIENT
    if _PING_CLIENT is None or _PING_CLIENT.is_closed:
        async with _PING_CLIENT_LOCK:
            if _PING_CLIENT is None or _PING_CLIENT.is_closed:
                _PING_CLIENT = httpx.AsyncClient(
                    timeout=httpx.Timeout(2.5, connect=1.5),
                    limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
    return _PING_CLIENT

async def _close_ping_client():
    global _PING_CLIENT
    client = _PING_CLIENT
    _PING_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()

async def host_to_ip(host):
    host = (host or "").strip().lower()
    if not host:
        return None
    cached = _DNS_CACHE.get(host)
    now = time.monotonic()
    if cached and now - cached[0] < _DNS_CACHE_TTL:
        return cached[1]
    try:
        ip = await asyncio.to_thread(socket.gethostbyname, host)
        _DNS_CACHE[host] = (now, ip)
        return ip
    except Exception as e:
        log.debug(f"DNS resolution failed for {host}: {e}")
        return None

async def _check_host_submit(client, target):
    """Submit one normal Check-Host Ping request with global rate limiting."""
    global _CHECK_HOST_LAST_SUBMIT
    last_status = None
    for attempt in range(_CHECK_HOST_MAX_429_RETRIES + 1):
        async with _CHECK_HOST_SUBMIT_LOCK:
            now = time.monotonic()
            wait_for = _CHECK_HOST_MIN_SUBMIT_INTERVAL - (now - _CHECK_HOST_LAST_SUBMIT)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            try:
                # IMPORTANT: this is intentionally the exact Check-Host URL requested:
                # https://check-host.net/check-ping?host=<HOST>
                # No node=, max_nodes=, DNS resolution, TCP fallback, or other
                # target transformation is used. Accept: application/json only
                # selects the API representation of the same check.
                check_url = f"https://check-host.net/check-ping?host={quote(target, safe="")}"
                log.debug("🔎 Check-Host URL: %s", check_url)
                response = await client.get(
                    check_url,
                    headers={"Accept": "application/json"},
                )
            finally:
                _CHECK_HOST_LAST_SUBMIT = time.monotonic()

        last_status = response.status_code
        if response.status_code != 429:
            return response

        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = max(2.0, min(float(retry_after), 15.0))
        except (TypeError, ValueError):
            delay = 3.0 * (attempt + 1)
        log.warning(
            "⏳ Check-Host rate limited (429) for %s; retry %d/%d in %.1fs",
            target, attempt + 1, _CHECK_HOST_MAX_429_RETRIES, delay
        )
        await asyncio.sleep(delay)

    log.warning("❌ Check-Host submit failed after 429 retries for %s (status=%s)", target, last_status)
    return None

def _parse_check_host_packets(node_result):
    """Parse one Check-Host Ping node and return (ok_count, avg_ms, ip).

    Official Check-Host Ping results are normally:
        [[ [packet1, packet2, packet3, packet4] ]]
    but this parser also tolerates the equivalent one-level/list variants so
    a formatting difference can never turn a non-OK packet into a success.
    """
    packets = None
    if isinstance(node_result, list):
        # Normal API shape: [[...four packets...]]
        if len(node_result) == 1 and isinstance(node_result[0], list):
            candidate = node_result[0]
            if candidate and all(isinstance(x, list) for x in candidate):
                packets = candidate
            elif candidate and candidate[0] is None:
                packets = []
        # Defensive support for [...four packets...]
        elif node_result and all(isinstance(x, list) for x in node_result):
            packets = node_result

    if not isinstance(packets, list):
        return 0, 0, None, False

    ok_times = []
    resolved_ip = None
    examined = 0
    for packet in packets[:4]:
        examined += 1
        if not isinstance(packet, list) or not packet:
            continue
        status = str(packet[0] or "").strip().upper()
        if status != "OK":
            continue
        try:
            seconds = float(packet[1])
            if seconds >= 0:
                ok_times.append(seconds)
        except (IndexError, TypeError, ValueError):
            pass
        if len(packet) >= 3 and packet[2]:
            resolved_ip = str(packet[2]).strip()

    complete = examined >= 4
    ok_count = len(ok_times)
    avg_ms = int(round(sum(ok_times) / ok_count * 1000)) if ok_times else 0
    return ok_count, avg_ms, resolved_ip, complete

def get_iran_ping_min_ok():
    """Global minimum successful probes required for every Iranian Check-Host node (0..4)."""
    try:
        row = c.execute("SELECT v FROM cfg WHERE k='iran_ping_min_ok'").fetchone()
        value = int(row[0]) if row and row[0] is not None else 2
    except Exception:
        value = 2
    return max(0, min(4, value))

def set_iran_ping_min_ok(value, apply_all_profiles=True):
    """Persist the Iran Ping threshold and optionally synchronize every profile."""
    value = max(0, min(4, int(value)))
    c.execute("INSERT OR REPLACE INTO cfg (k, v) VALUES ('iran_ping_min_ok', ?)", (str(value),))
    if apply_all_profiles:
        # Keep every profile consistent with the admin-wide default.
        c.execute("UPDATE profiles SET ping_mode='iran', config_ping_mode='iran', proxy_ping_mode='iran'")
    conn.commit()
    return value

async def _check_host_ping(host, mode):
    """Run one exact Check-Host Ping and apply the selected mode's rule."""
    target = str(host or "").strip()
    if not target:
        return 0, False, 0

    mode = _normalize_ping_mode(mode)
    try:
        cl = await _get_ping_client()
        r = await _check_host_submit(cl, target)
        if r is None or r.status_code != 200:
            log.warning("❌ Check-Host %s submit failed for %s", mode, target)
            return 0, False, 0
        try:
            created = r.json()
        except (json.JSONDecodeError, ValueError):
            log.warning("❌ Check-Host %s returned non-JSON for %s", mode, target)
            return 0, False, 0

        if not isinstance(created, dict) or not created.get("ok") or not created.get("request_id"):
            log.warning("❌ Check-Host %s invalid submit response for %s: %s", mode, target, created)
            return 0, False, 0

        node_meta = created.get("nodes") or {}
        if not isinstance(node_meta, dict):
            return 0, False, 0

        # Use ONLY the country code supplied by Check-Host. Never infer Iran
        # from a node name. In global mode every node is eligible.
        if mode == "iran":
            relevant_nodes = [
                str(name) for name, info in node_meta.items()
                if isinstance(info, list) and info
                and str(info[0] or "").strip().lower() == "ir"
            ]
            required = get_iran_ping_min_ok()
        else:
            relevant_nodes = [str(name) for name in node_meta.keys()]
            required = 4

        if not relevant_nodes:
            log.warning("❌ Check-Host %s has no relevant nodes for %s", mode, target)
            return 0, False, 0

        request_id = str(created["request_id"])
        result_url = f"https://check-host.net/check-result/{quote(request_id, safe='')}"
        deadline = time.monotonic() + 15.0
        best_ok = 0
        best_avg = 0
        best_ip = None
        concrete_nodes = set()

        while time.monotonic() < deadline:
            try:
                rr = await cl.get(result_url, headers={"Accept": "application/json"})
            except Exception as exc:
                log.debug("Check-Host result poll failed for %s: %s", target, exc)
                await asyncio.sleep(0.75)
                continue

            if rr.status_code != 200:
                await asyncio.sleep(0.75)
                continue
            try:
                result = rr.json()
            except (json.JSONDecodeError, ValueError):
                await asyncio.sleep(0.75)
                continue
            if not isinstance(result, dict):
                await asyncio.sleep(0.75)
                continue

            all_relevant_concrete = True
            for node_name in relevant_nodes:
                raw = result.get(node_name)
                if raw is None:
                    all_relevant_concrete = False
                    continue
                concrete_nodes.add(node_name)
                ok_count, avg_ms, resolved_ip, complete = _parse_check_host_packets(raw)
                if not complete:
                    all_relevant_concrete = False
                if ok_count > best_ok:
                    best_ok, best_avg, best_ip = ok_count, avg_ms, resolved_ip
                elif ok_count == best_ok and ok_count > 0 and avg_ms and (not best_avg or avg_ms < best_avg):
                    best_avg, best_ip = avg_ms, resolved_ip

                # STRICT acceptance:
                # Iran: EVERY Iranian Check-Host location shown in the response
                # must finish all 4 probes and meet the configured minimum successful probes
                # (default 2/4). One missing/failed Iranian location rejects the host.
                # Global: retain the existing rule of at least one location at 4/4.

            # Iran is intentionally ALL-or-NOTHING: we do not accept a host just
            # because one Iranian location passed. Every IR node returned by
            # Check-Host must be present, complete (4 packets), and >= the configured threshold.
            if mode == "iran" and all_relevant_concrete and len(concrete_nodes) == len(relevant_nodes):
                iran_results = []
                iran_ok = True
                for node_name in relevant_nodes:
                    raw = result.get(node_name)
                    ok_count, avg_ms, resolved_ip, complete = _parse_check_host_packets(raw)
                    iran_results.append((node_name, ok_count, avg_ms, complete))
                    if not complete or ok_count < required:
                        iran_ok = False

                if iran_ok and iran_results:
                    avg_values = [x[2] for x in iran_results if x[2] > 0]
                    final_avg = int(round(sum(avg_values) / len(avg_values))) if avg_values else 0
                    min_ok = min(x[1] for x in iran_results)
                    # Cache an IP only when the complete Iran-wide check passed.
                    for node_name, ok_count, avg_ms, complete in iran_results:
                        raw = result.get(node_name)
                        _oc, _avg, resolved_ip, _complete = _parse_check_host_packets(raw)
                        if resolved_ip:
                            _PING_TARGET_IP_CACHE[target.lower()] = (time.monotonic(), resolved_ip)
                            break
                    log.info(
                        "✅ Check-Host IRAN ALL PASS for %s: %d Iranian locations, minimum %d/4, avg %sms",
                        target, len(iran_results), min_ok, final_avg
                    )
                    return final_avg, True, min_ok

                failed = [f"{n}={ok}/4" for n, ok, _avg, complete in iran_results if (not complete or ok < required)]
                log.info(
                    "❌ Check-Host IRAN FAIL for %s: every IR location must be >=2/4; failed=%s",
                    target, ", ".join(failed) or "unknown"
                )
                return 0, False, 0

            # Global: if every relevant node is concrete and no node satisfied
            # the existing 4/4 rule, this check is definitively failed.
            if mode != "iran" and all_relevant_concrete and len(concrete_nodes) == len(relevant_nodes):
                log.info("❌ Check-Host GLOBAL FAIL for %s: best result %d/4", target, best_ok)
                return 0, False, 0

            await asyncio.sleep(0.75)

        log.warning(
            "⏳ Check-Host %s incomplete for %s: best=%d/4, concrete=%d/%d",
            mode.upper(), target, best_ok, len(concrete_nodes), len(relevant_nodes)
        )
        return 0, False, 0
    except Exception as e:
        log.warning("❌ Check-Host %s error for %s: %s", mode, target, e)
        return 0, False, 0

async def ping_from_iran_only(host, port=None, allow_tcp_fallback=False):
    # Kept as a compatibility wrapper; no TCP fallback is ever used.
    return await _check_host_ping(host, "iran")

async def ping_from_global(host):
    return await _check_host_ping(host, "global")

def _find_core(kind):
    env_names = ["XRAY_BIN", "XRAY_PATH"] if kind == "xray" else ["SING_BOX_BIN", "SING_BOX_PATH", "SINGBOX_BIN"]
    for name in env_names:
        value = os.getenv(name, "").strip()
        if value and os.path.isfile(value) and os.access(value, os.X_OK):
            return value
    candidates = [kind, "sing-box"] if kind == "singbox" else ["xray", "xray-core"]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    for path in (f"/usr/local/bin/{candidates[0]}", f"/usr/bin/{candidates[0]}", f"/app/{candidates[0]}"):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None

def _q(url):
    try:
        return urlparse(url)
    except Exception:
        return None

def _first(qs, key, default=""):
    vals = qs.get(key) or []
    return str(vals[-1] if vals else default)

def _ss_parts(url):
    p = urlparse(url)
    if p.hostname and p.port and p.username:
        # Modern ss://BASE64(method:password)@host:port form.
        try:
            user = unquote(p.username)
            if ":" in user:
                return p.hostname, int(p.port), user.split(":",1)[0], user.split(":",1)[1]
        except Exception:
            pass
    raw = str(url).split("ss://",1)[-1].split("#",1)[0]
    if "@" in raw:
        left, right = raw.rsplit("@",1)
        hostport = right
        try:
            if hostport.startswith("["):
                host = hostport[1:hostport.find("]")]
                port = int(hostport.split("]:",1)[1].split("?",1)[0])
            else:
                host, port = hostport.rsplit(":",1); port=int(port.split("?",1)[0])
            decoded = _decode_b64(left).decode("utf-8", errors="strict")
            method, password = decoded.split(":",1)
            return host, port, method, password
        except Exception:
            return None
    try:
        decoded = _decode_b64(raw).decode("utf-8", errors="strict")
        auth, hostport = decoded.rsplit("@",1)
        method,password=auth.split(":",1)
        host,port=hostport.rsplit(":",1)
        return host,int(port),method,password
    except Exception:
        return None

def _build_xray_outbound(url):
    p = _q(url)
    if not p or not p.hostname or not p.port:
        raise ValueError("invalid URI")
    scheme=p.scheme.lower(); q=parse_qs(p.query); host=p.hostname; port=int(p.port)
    out={"protocol":None,"settings":{},"streamSettings":{}}
    ss=out["streamSettings"]
    network=_first(q,"type", "tcp").lower()
    if network not in ("tcp","ws","grpc","http","httpupgrade","xhttp"):
        network="tcp"
    ss["network"]=network
    if scheme=="vless":
        out["protocol"]="vless"
        user=str(unquote(p.username or ""))
        if not user: raise ValueError("VLESS UUID missing")
        vu={"id":user,"encryption":_first(q,"encryption","none")}
        flow=_first(q,"flow","")
        if flow: vu["flow"]=flow
        out["settings"]={"vnext":[{"address":host,"port":port,"users":[vu]}]}
    elif scheme=="vmess":
        raw=p.netloc.split("@",1)[-1]
        obj=parse_vmess(url).get("metadata") or {}
        host=str(obj.get("add") or host); port=int(obj.get("port") or port)
        out["protocol"]="vmess"
        out["settings"]={"vnext":[{"address":host,"port":port,"users":[{"id":str(obj.get("id") or ""),"alterId":int(obj.get("aid") or obj.get("alterId") or 0),"security":str(obj.get("scy") or obj.get("security") or "auto")}]}]}
        network=str(obj.get("net") or _first(q,"type","tcp")).lower()
        ss["network"]=network if network in ("tcp","ws","grpc","http") else "tcp"
        # Keep the original URI query and overlay VMess JSON transport fields.
        for _k in ("host","path","sni","security","fp"):
            if obj.get(_k) not in (None, ""):
                q[_k]=[str(obj.get(_k))]
    elif scheme=="trojan":
        out["protocol"]="trojan"
        out["settings"]={"servers":[{"address":host,"port":port,"password":unquote(p.username or "")} ]}
    elif scheme in ("ss","shadowsocks"):
        parts=_ss_parts(url)
        if not parts: raise ValueError("Shadowsocks URI unsupported")
        host,port,method,password=parts
        out["protocol"]="shadowsocks"; out["settings"]={"servers":[{"address":host,"port":port,"method":method,"password":password}]}
    elif scheme in ("socks","socks5","http"):
        out["protocol"]="socks" if scheme.startswith("socks") else "http"
        user=p.username; pw=p.password
        server={"address":host,"port":port}
        if user: server["users"]=[{"user":unquote(user),"pass":unquote(pw or "")}]
        out["settings"]={"servers":[server]}
    else:
        raise ValueError(f"Xray does not support URI scheme {scheme}")

    security=_first(q,"security", "none").lower()
    if scheme in ("vless","vmess","trojan") and security in ("tls","reality"):
        ss["security"]=security
        if security=="tls":
            tls={"serverName":_first(q,"sni",_first(q,"host",host)),"allowInsecure":_first(q,"allowInsecure","0") in ("1","true","yes")}
            fp=_first(q,"fp","")
            if fp: tls["fingerprint"]=fp
            ss["tlsSettings"]=tls
        else:
            rs={"serverName":_first(q,"sni",host),"publicKey":_first(q,"pbk",_first(q,"publicKey","")),"shortId":_first(q,"sid",_first(q,"shortId",""))}
            fp=_first(q,"fp","")
            if fp: rs["fingerprint"]=fp
            spx=_first(q,"spx","")
            if spx: rs["spiderX"]=unquote(spx)
            if not rs["publicKey"]: raise ValueError("Reality public key missing")
            ss["realitySettings"]=rs
    elif scheme in ("vless","vmess","trojan"):
        ss["security"]="none"

    if ss["network"]=="ws":
        ss["wsSettings"]={"path":unquote(_first(q,"path","/")),"headers":{}}
        wh=_first(q,"host","")
        if wh: ss["wsSettings"]["headers"]["Host"]=wh
    elif ss["network"]=="grpc":
        ss["grpcSettings"]={"serviceName":_first(q,"serviceName",_first(q,"path",""))}
    elif ss["network"]=="http":
        ss["httpSettings"]={"path":unquote(_first(q,"path","/")),"host":[_first(q,"host",host)]}
    return out

def _build_singbox_outbound(url):
    p=_q(url)
    if not p or not p.hostname:
        raise ValueError("invalid URI")
    scheme=p.scheme.lower(); q=parse_qs(p.query); host=p.hostname; port=int(p.port or 0)
    network=_first(q,"type","tcp").lower()
    if scheme=="vless":
        o={"type":"vless","server":host,"server_port":port,"uuid":unquote(p.username or ""),"packet_encoding":"xudp"}
    elif scheme=="vmess":
        obj=parse_vmess(url).get("metadata") or {}; host=str(obj.get("add") or host); port=int(obj.get("port") or port)
        o={"type":"vmess","server":host,"server_port":port,"uuid":str(obj.get("id") or ""),"security":str(obj.get("scy") or obj.get("security") or "auto"),"alter_id":int(obj.get("aid") or obj.get("alterId") or 0)}
        network=str(obj.get("net") or network).lower()
        for _k in ("host","path","sni","security","fp"):
            if obj.get(_k) not in (None, ""):
                q[_k]=[str(obj.get(_k))]
    elif scheme=="trojan":
        o={"type":"trojan","server":host,"server_port":port,"password":unquote(p.username or "")}
    elif scheme in ("ss","shadowsocks"):
        parts=_ss_parts(url)
        if not parts: raise ValueError("Shadowsocks URI unsupported")
        host,port,method,password=parts; o={"type":"shadowsocks","server":host,"server_port":port,"method":method,"password":password}
    elif scheme in ("socks","socks5"):
        o={"type":"socks","server":host,"server_port":port}
        if p.username: o.update({"username":unquote(p.username),"password":unquote(p.password or "")})
    elif scheme=="http":
        o={"type":"http","server":host,"server_port":port}
        if p.username: o.update({"username":unquote(p.username),"password":unquote(p.password or "")})
    elif scheme in ("hy2","hysteria","hysteria2"):
        o={"type":"hysteria2","server":host,"server_port":port,"password":unquote(p.username or _first(q,"password",""))}
    elif scheme in ("wg","wireguard"):
        private_key=_first(q,"private_key",_first(q,"privateKey",_first(q,"private-key","")))
        peer_key=_first(q,"public_key",_first(q,"publicKey",_first(q,"peer_public_key","")))
        if not private_key or not peer_key or not host or not port:
            raise ValueError("WireGuard URI needs private_key, public_key, host and port")
        o={"type":"wireguard","server":host,"server_port":port,"private_key":private_key,"peers":[{"public_key":peer_key,"allowed_ips":["0.0.0.0/0","::/0"]}]}
        mtu=_first(q,"mtu","")
        if mtu.isdigit(): o["mtu"]=int(mtu)
    else:
        raise ValueError(f"sing-box unsupported URI scheme {scheme}")
    security=_first(q,"security", "none").lower()
    if scheme in ("vless","vmess","trojan","hy2","hysteria","hysteria2") and security in ("tls","reality"):
        tls={"enabled":True,"server_name":_first(q,"sni",_first(q,"host",host))}
        if _first(q,"allowInsecure","0") in ("1","true","yes"): tls["insecure"]=True
        fp=_first(q,"fp","")
        if fp: tls["utls"]={"enabled":True,"fingerprint":fp}
        if security=="reality":
            tls["reality"]={"enabled":True,"public_key":_first(q,"pbk",_first(q,"publicKey","")),"short_id":_first(q,"sid",_first(q,"shortId",""))}
        o["tls"]=tls
    elif scheme in ("vless","vmess","trojan"):
        # For VLESS/Trojan URI without TLS, sing-box still accepts the outbound.
        pass
    if network=="ws":
        o["transport"]={"type":"ws","path":unquote(_first(q,"path","/")),"headers":{}}
        wh=_first(q,"host","")
        if wh: o["transport"]["headers"]["Host"]=wh
    elif network=="grpc":
        o["transport"]={"type":"grpc","service_name":_first(q,"serviceName",_first(q,"path",""))}
    return o

async def _run_full_config_core(url, core_path, kind):
    work=tempfile.mkdtemp(prefix="fullcfg_")
    proc=None
    try:
        # Pick a free localhost port without binding the port permanently.
        sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM); sock.bind(("127.0.0.1",0)); local_port=sock.getsockname()[1]; sock.close()
        if kind=="xray":
            outbound=_build_xray_outbound(url)
            cfg={"log":{"loglevel":"error"},"inbounds":[{"listen":"127.0.0.1","port":local_port,"protocol":"http","settings":{"timeout":10}}],"outbounds":[dict(outbound,tag="proxy"),{"protocol":"freedom","tag":"direct"}]}
            cfg_path=os.path.join(work,"config.json"); open(cfg_path,"w",encoding="utf-8").write(json.dumps(cfg,ensure_ascii=False))
            argv=[core_path,"run","-c",cfg_path] if os.path.basename(core_path).startswith("xray") else [core_path,"-c",cfg_path]
        else:
            outbound=_build_singbox_outbound(url)
            cfg={"log":{"level":"error"},"inbounds":[{"type":"mixed","tag":"in","listen":"127.0.0.1","listen_port":local_port}],"outbounds":[dict(outbound,tag="proxy"),{"type":"direct","tag":"direct"}],"route":{"final":"proxy"}}
            cfg_path=os.path.join(work,"config.json"); open(cfg_path,"w",encoding="utf-8").write(json.dumps(cfg,ensure_ascii=False))
            argv=[core_path,"run","-c",cfg_path]
        proc=await asyncio.create_subprocess_exec(*argv,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        started=time.monotonic(); ready=False
        while time.monotonic()-started<5.0:
            if proc.returncode is not None: break
            try:
                test=socket.socket(socket.AF_INET,socket.SOCK_STREAM); test.settimeout(.25); test.connect(("127.0.0.1",local_port)); test.close(); ready=True; break
            except Exception: await asyncio.sleep(.08)
        if not ready: return 0,False,"core_not_ready"
        t0=time.monotonic()
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{local_port}",timeout=httpx.Timeout(7.0,connect=3.0),follow_redirects=False) as hc:
            rr=await hc.get(_FULL_TEST_URL,headers={"User-Agent":"Mozilla/5.0"})
        elapsed=int(round((time.monotonic()-t0)*1000))
        ok=200 <= rr.status_code < 500
        return elapsed,ok,f"http={rr.status_code}"
    except Exception as exc:
        return 0,False,str(exc)[:180]
    finally:
        if proc is not None:
            try:
                proc.terminate(); await asyncio.wait_for(proc.wait(),timeout=1.5)
            except Exception:
                try: proc.kill()
                except Exception: pass
        shutil.rmtree(work,ignore_errors=True)

async def full_config_ping(url):
    """End-to-end config test. Prefer sing-box for broad protocol coverage, then Xray."""
    last="no_core"
    for kind in ("singbox","xray"):
        core=_find_core(kind)
        if not core: continue
        try:
            return await asyncio.wait_for(_run_full_config_core(url,core,kind),timeout=_FULL_TEST_TIMEOUT)
        except Exception as exc:
            last=str(exc)[:180]
    return 0,False,last

async def check_full_link_ping(url, ping_mode="global", perform_ping=True):
    """Legacy Host/Check-Host test only."""
    if not perform_ping:
        return 0, True, 0
    host, _port = extract_host(url)
    if not host:
        return 0, False, 0
    return await asyncio.wait_for(
        _check_host_ping(host, _normalize_ping_mode(ping_mode)),
        timeout=18.0,
    )
