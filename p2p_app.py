"""
p2p_app.py
----------
Merged P2P File Sharing GUI - CMP2204 Term Project Spring 2026

All four modules (Discovery, Announcer, Uploader, Downloader) run inside
this single process.  Each runs in its own daemon thread so the Tkinter
main-loop is never blocked.

Layout:
  - Root window   : main log (Uploader, Downloader output) + Settings
  - Discovery     : opens its own Toplevel window with a dedicated log
  - Announcer     : opens its own Toplevel window with a dedicated log
                    and an "Add File" button to host additional files
"""

import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox
import threading
import socket
import json
import os
import time
import random
import base64

# ── Optional dependency ────────────────────────────────────────────────────────
try:
    import pyDes
    PYDES_AVAILABLE = True
except ImportError:
    PYDES_AVAILABLE = False

# ── Shared configuration ───────────────────────────────────────────────────────
# FIX 4: BROADCAST_IP is now detected automatically from the local interface
#         so it always matches the actual LAN subnet (e.g. 192.168.1.255).
#         Falls back to 192.168.1.255 (spec default) if detection fails.
def _detect_broadcast_ip() -> str:
    """Detect the broadcast address of the active LAN interface."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        # Build subnet broadcast: replace last octet with 255
        parts = local_ip.split(".")
        parts[-1] = "255"
        return ".".join(parts)
    except Exception:
        return "192.168.1.255"   # spec default fallback

BROADCAST_IP      = _detect_broadcast_ip()
BROADCAST_PORT    = 6000
TCP_PORT          = 6001
ANNOUNCE_INTERVAL = 8
WIPE_INTERVAL     = 120   # FIX 2: spec says 2 minutes (120s), was 60

CHUNKS_DIR        = "my_chunks"
SHARED_DIR        = "shared_chunks"
USERNAME_FILE     = "username.txt"
IP_TO_USER_FILE   = "ip_to_username.json"
USER_TO_IP_FILE   = "username_to_ip.json"
CONTENT_DICT_FILE = "content_dict.json"
EXT_REGISTRY_FILE = "ext_registry.json"   # base name -> original extension (e.g. {"forest": ".png"})
UPLOAD_LOG        = "upload_log.txt"
DOWNLOAD_LOG      = "download_log.txt"
HISTORY_LOG       = "history_log.txt"

DH_P = 907
DH_G = 7

# Lock protecting all shared-file reads/writes (Discovery writes while
# Uploader/Downloader read // without this, a read can hit a half-written
# file and return {} silently). (#1 race-condition fix)
_file_lock = threading.RLock()

# ── Shared state flags ─────────────────────────────────────────────────────────
_discovery_running  = False
_uploader_running   = False
_announcer_running  = False
_ann_list           = None   # reference to announcer's own listbox
_dis_list           = None   # reference to discovery's own listbox


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _load_json(path: str) -> dict:
    with _file_lock:
        if os.path.isfile(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}


def _save_json(path: str, data: dict):
    with _file_lock:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


def ip_to_username(ip: str) -> str:
    return _load_json(IP_TO_USER_FILE).get(ip, ip)


def make_des_key(shared_secret_int: int) -> bytes:
    return str(shared_secret_int).zfill(8)[:8].encode("utf-8")


def find_chunk(chunk_name: str) -> str | None:
    for d in (CHUNKS_DIR, SHARED_DIR):
        p = os.path.join(d, chunk_name)
        if os.path.isfile(p):
            return p
    return None


# ── Length-prefixed socket protocol ───────────────────────────────────────────

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        part = sock.recv(n - len(data))
        if not part:
            raise ConnectionError("Socket closed early")
        data += part
    return data


def _recv_msg(sock: socket.socket) -> bytes:
    raw_len = _recv_exact(sock, 4)
    return _recv_exact(sock, int.from_bytes(raw_len, "big"))


def _send_msg(sock: socket.socket, data: bytes):
    sock.sendall(len(data).to_bytes(4, "big") + data)


# ══════════════════════════════════════════════════════════════════════════════
#  GUI LOG HELPERS  (thread-safe)
# ══════════════════════════════════════════════════════════════════════════════

def main_log(text: str):
    """Append a line to the root window's listbox."""
    root.after(0, lambda: (_main_list.insert(tk.END, text), _main_list.see(tk.END)))


def ann_log(text: str):
    """Append a line to the Announcer window's listbox (if open)."""
    if _ann_list is not None:
        _ann_list.after(0, lambda: (_ann_list.insert(tk.END, text), _ann_list.see(tk.END)))


def dis_log(text: str):
    """Append a line to the Discovery window's listbox (if open)."""
    if _dis_list is not None:
        _dis_list.after(0, lambda: (_dis_list.insert(tk.END, text), _dis_list.see(tk.END)))


# ══════════════════════════════════════════════════════════════════════════════
#  CONTENT DISCOVERY  (UDP listener)
# ══════════════════════════════════════════════════════════════════════════════

def _discovery_thread():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", BROADCAST_PORT))
    except Exception as e:
        dis_log(f"[Discovery] Bind error: {type(e).__name__}: {e}")
        return

    dis_log(f"[Discovery] Listening on UDP port {BROADCAST_PORT} ...")
    dis_log(f"[Discovery] Broadcast IP in use: {BROADCAST_IP}")

    # Background wiper
    def wiper():
        while _discovery_running:
            time.sleep(WIPE_INTERVAL)
            _save_json(CONTENT_DICT_FILE, {})
            dis_log("[Discovery] Content dictionary wiped (2-min recency window).")
    threading.Thread(target=wiper, daemon=True).start()

    sock.settimeout(1.0)
    while _discovery_running:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except Exception as e:
            dis_log(f"[Discovery] Error: {type(e).__name__}: {e}")
            continue

        sender_ip = addr[0]
        try:
            payload  = json.loads(data.decode("utf-8"))
            username = payload.get("username", "unknown")
            chunks   = payload.get("chunks", [])

            d = _load_json(IP_TO_USER_FILE); d[sender_ip] = username; _save_json(IP_TO_USER_FILE, d)
            d = _load_json(USER_TO_IP_FILE); d[username] = sender_ip; _save_json(USER_TO_IP_FILE, d)
            d = _load_json(CONTENT_DICT_FILE)
            for c in chunks:
                if c not in d: d[c] = []
                if sender_ip not in d[c]: d[c].append(sender_ip)
            _save_json(CONTENT_DICT_FILE, d)

            chunk_str = ", ".join(chunks) if chunks else "(none)"
            dis_log(f"[Discovery] {username} ({sender_ip}): {chunk_str}")
        except json.JSONDecodeError:
            dis_log(f"[Discovery] Malformed JSON from {sender_ip}")

    sock.close()
    dis_log("[Discovery] Stopped.")


def start_discovery():
    global _discovery_running, _dis_list
    if _discovery_running:
        main_log("[Discovery] Already running.")
        return

    dis_win = tk.Toplevel(root)
    dis_win.title("CONTENT DISCOVERY // SIGNAL MONITOR")
    dis_win.geometry("520x400+820+150")
    dis_win.configure(bg="#000000")

    tk.Label(dis_win, text="// DISCOVERY LOG",
             font=("Times New Roman", 14, "bold"),
             bg="#000000", fg="#ffffff").pack(pady=(12, 4))

    sb = tk.Scrollbar(dis_win)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    _dis_list = tk.Listbox(dis_win, yscrollcommand=sb.set,
                           bg="#000000", fg="#ffffff",
                           selectbackground="#ffffff",
                           selectforeground="#000000",
                           font=("Times New Roman", 12),
                           width=62, height=22,
                           borderwidth=0, highlightthickness=0)
    _dis_list.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
    sb.config(command=_dis_list.yview)

    # FIX 3: close button properly bound (was missing protocol binding before)
    def on_close():
        global _discovery_running, _dis_list
        _discovery_running = False
        _dis_list = None
        dis_win.destroy()

    dis_win.protocol("WM_DELETE_WINDOW", on_close)

    _discovery_running = True
    threading.Thread(target=_discovery_thread, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
#  CHUNK ANNOUNCER  (UDP broadcaster)
# ══════════════════════════════════════════════════════════════════════════════

def _get_all_hosted_chunks() -> list:
    chunks = []
    for d in (CHUNKS_DIR, SHARED_DIR):
        if os.path.isdir(d):
            chunks += [f for f in os.listdir(d) if not f.endswith(".txt")]
    seen, unique = set(), []
    for c in chunks:
        if c not in seen:
            seen.add(c); unique.append(c)
    return unique


def _split_file(filepath: str, base_name: str, n: int = 3) -> list:
    """
    FIX 2 (format check): validates the file is readable and non-empty
    before splitting. Raises ValueError with a clear message if not.
    Also records the original file extension so the downloader can
    reconstruct the correct filename later.
    """
    if not os.path.isfile(filepath):
        raise ValueError(f"File not found: {filepath}")
    if os.path.getsize(filepath) == 0:
        raise ValueError(f"File is empty: {filepath}")

    os.makedirs(CHUNKS_DIR, exist_ok=True)

    # Record original extension: e.g. "forest.png" -> {"forest": ".png"}
    ext = os.path.splitext(os.path.basename(filepath))[1]
    if ext:
        reg = _load_json(EXT_REGISTRY_FILE)
        reg[base_name] = ext
        _save_json(EXT_REGISTRY_FILE, reg)

    with open(filepath, "rb") as f:
        data = f.read()

    size = (len(data) + n - 1) // n
    paths = []
    for i in range(1, n + 1):
        chunk = data[(i-1)*size : i*size]
        name  = f"{base_name}_{i}"
        with open(os.path.join(CHUNKS_DIR, name), "wb") as cf:
            cf.write(chunk)
        paths.append(name)
    return paths


def _announcer_broadcast_loop(username: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    ann_log(f"[Announcer] Broadcasting as '{username}' every {ANNOUNCE_INTERVAL}s ...")
    ann_log(f"[Announcer] Broadcast target: {BROADCAST_IP}:{BROADCAST_PORT}")
    while _announcer_running:
        chunks  = _get_all_hosted_chunks()
        payload = json.dumps({"username": username, "chunks": chunks})
        try:
            sock.sendto(payload.encode("utf-8"), (BROADCAST_IP, BROADCAST_PORT))
            ann_log(f"[Announcer] Sent {len(chunks)} chunk(s): {chunks}")
        except Exception as e:
            ann_log(f"[Announcer] Broadcast error: {type(e).__name__}: {e}")
        for _ in range(ANNOUNCE_INTERVAL * 10):
            if not _announcer_running:
                break
            time.sleep(0.1)
    sock.close()
    ann_log("[Announcer] Stopped.")


def start_announcer():
    global _ann_list, _announcer_running

    if _announcer_running:
        # FIX 1 (multi-file): if already running, just add a file instead
        _announcer_add_file()
        return

    # Ask for username
    username = simpledialog.askstring("Announcer", "Enter your username:", parent=root)
    if not username or not username.strip():
        main_log("[Announcer] Cancelled // no username provided.")
        return
    username = username.strip()
    with open(USERNAME_FILE, "w") as f:
        f.write(username)

    # Ask for first file
    filepath = filedialog.askopenfilename(title="Select file to host")
    if not filepath:
        main_log("[Announcer] Cancelled // no file selected.")
        return

    # FIX 2 (format check): validate before splitting
    try:
        base = os.path.splitext(os.path.basename(filepath))[0]
        os.makedirs(CHUNKS_DIR, exist_ok=True)
        os.makedirs(SHARED_DIR, exist_ok=True)
        chunks = _split_file(filepath, base)
        main_log(f"[Announcer] '{os.path.basename(filepath)}' split into {len(chunks)} chunks: {chunks}")
    except ValueError as e:
        messagebox.showerror("File Error", str(e), parent=root)
        main_log(f"[Announcer] Error: {e}")
        return

    # Build Announcer window
    ann_win = tk.Toplevel(root)
    ann_win.geometry("540x430+820+150")
    ann_win.title(f"CHUNK ANNOUNCER // {username.upper()}")
    ann_win.configure(bg="#000000")

    tk.Label(ann_win, text=f"// ANNOUNCER // {username.upper()}",
             font=("Times New Roman", 14, "bold"),
             bg="#000000", fg="#ffffff").pack(pady=(12, 2))

    # FIX 1 (multi-file): "Add File" button inside the Announcer window
    def _add_file_btn():
        _announcer_add_file(ann_win)

    tk.Button(ann_win, text="+ ADD FILE",
              command=_add_file_btn,
              bg="#ffffff", fg="#000000",
              font=("Times New Roman", 12, "bold"),
              relief=tk.FLAT, padx=10, pady=4,
              cursor="hand2").pack(pady=(2, 6))

    sb = tk.Scrollbar(ann_win)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    _ann_list = tk.Listbox(ann_win, yscrollcommand=sb.set,
                           bg="#000000", fg="#ffffff",
                           selectbackground="#ffffff",
                           selectforeground="#000000",
                           font=("Times New Roman", 12),
                           width=62, height=20,
                           borderwidth=0, highlightthickness=0)
    _ann_list.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
    sb.config(command=_ann_list.yview)

    def on_close():
        global _announcer_running, _ann_list
        _announcer_running = False
        _ann_list = None
        ann_win.destroy()

    ann_win.protocol("WM_DELETE_WINDOW", on_close)

    _announcer_running = True
    threading.Thread(target=_announcer_broadcast_loop, args=(username,), daemon=True).start()


def _announcer_add_file(parent=None):
    """
    FIX 1 (multi-file): split and add another file to my_chunks/ while
    the announcer is already running. The broadcast loop picks it up
    automatically on its next tick since it reads the directory live.
    """
    if parent is None:
        parent = root
    filepath = filedialog.askopenfilename(title="Select additional file to host",
                                          parent=parent)
    if not filepath:
        return

    # FIX 2 (format check): validate before splitting
    try:
        base = os.path.splitext(os.path.basename(filepath))[0]
        chunks = _split_file(filepath, base)
        msg = f"[Announcer] Added '{os.path.basename(filepath)}' → {chunks}"
        ann_log(msg)
        main_log(msg)
    except ValueError as e:
        messagebox.showerror("File Error", str(e), parent=parent)
        main_log(f"[Announcer] Add file error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  CHUNK UPLOADER  (TCP server)
# ══════════════════════════════════════════════════════════════════════════════

def _handle_upload_client(conn: socket.socket, addr: tuple):
    client_ip = addr[0]
    peer_name = ip_to_username(client_ip)
    conn.settimeout(10)
    des_key_bytes = None

    try:
        while True:
            raw = _recv_msg(conn)
            if not raw:
                break
            try:
                msg = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                break

            # DH key exchange
            if "key" in msg:
                their_pub = int(msg["key"])
                my_priv   = random.randint(2, DH_P - 2)
                my_pub    = pow(DH_G, my_priv, DH_P)
                _send_msg(conn, json.dumps({"key": str(my_pub)}).encode("utf-8"))
                shared_secret = pow(their_pub, my_priv, DH_P)
                des_key_bytes = make_des_key(shared_secret)
                raw2 = _recv_msg(conn)
                if not raw2:
                    break
                try:
                    msg = json.loads(raw2.decode("utf-8"))
                except json.JSONDecodeError:
                    break

            if "requested_secured_content" in msg:
                chunk_name = msg["requested_secured_content"]
                main_log(f"[Uploader] {peer_name} → SECURED '{chunk_name}'")
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                path = find_chunk(chunk_name)
                if not path:
                    main_log(f"[Uploader] Chunk not found: {chunk_name}")
                    break
                raw_bytes = open(path, "rb").read()
                if not PYDES_AVAILABLE or des_key_bytes is None:
                    main_log("[Uploader] Cannot encrypt // pyDes unavailable or no DH key.")
                    break
                enc = pyDes.des(des_key_bytes, pyDes.ECB, pad=None,
                                padmode=pyDes.PAD_PKCS5).encrypt(raw_bytes)
                resp = json.dumps({"chunk_name": chunk_name,
                                   "encrypted_chunk": base64.b64encode(enc).decode()})
                _send_msg(conn, resp.encode("utf-8"))
                _log_transfer(ts, chunk_name, client_ip, "SENT", UPLOAD_LOG)
                main_log(f"[Uploader] [{ts}] Sent encrypted '{chunk_name}' → {peer_name}")
                break

            elif "requested_content" in msg:
                chunk_name = msg["requested_content"]
                main_log(f"[Uploader] {peer_name} → PLAIN '{chunk_name}'")
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                path = find_chunk(chunk_name)
                if not path:
                    main_log(f"[Uploader] Chunk not found: {chunk_name}")
                    break
                raw_bytes = open(path, "rb").read()
                resp = json.dumps({"chunk_name": chunk_name,
                                   "data": base64.b64encode(raw_bytes).decode()})
                _send_msg(conn, resp.encode("utf-8"))
                _log_transfer(ts, chunk_name, client_ip, "SENT", UPLOAD_LOG)
                main_log(f"[Uploader] [{ts}] Sent plain '{chunk_name}' → {peer_name}")
                break
            else:
                main_log(f"[Uploader] Unknown message keys: {list(msg.keys())}")
                break

    except Exception as e:
        main_log(f"[Uploader] Error with {peer_name}: {type(e).__name__}: {e}")
    finally:
        conn.close()


def _uploader_thread():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("", TCP_PORT))
    except Exception as e:
        main_log(f"[Uploader] Bind error: {type(e).__name__}: {e}")
        return
    server.listen(10)
    server.settimeout(1.0)
    main_log(f"[Uploader] Listening on TCP port {TCP_PORT} ...")
    while _uploader_running:
        try:
            conn, addr = server.accept()
            threading.Thread(target=_handle_upload_client, args=(conn, addr),
                             daemon=True).start()
        except socket.timeout:
            continue
        except Exception as e:
            if _uploader_running:
                main_log(f"[Uploader] Accept error: {type(e).__name__}: {e}")
    server.close()
    main_log("[Uploader] Stopped.")


def start_uploader():
    global _uploader_running
    if _uploader_running:
        main_log("[Uploader] Already running.")
        return
    os.makedirs(CHUNKS_DIR, exist_ok=True)
    os.makedirs(SHARED_DIR, exist_ok=True)
    _uploader_running = True
    threading.Thread(target=_uploader_thread, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
#  CHUNK DOWNLOADER  (TCP client, interactive via GUI dialogs)
# ══════════════════════════════════════════════════════════════════════════════

def _log_transfer(ts, chunk, ip, status, logfile):
    name = ip_to_username(ip)
    line = f"{ts}\t{chunk}\t{name} ({ip})\t{status}\n"
    with _file_lock:
        with open(logfile, "a") as f:
            f.write(line)
        with open(HISTORY_LOG, "a") as f:
            f.write(line)


def _save_chunk(chunk_name: str, data: bytes):
    os.makedirs(CHUNKS_DIR, exist_ok=True)
    os.makedirs(SHARED_DIR, exist_ok=True)
    for d in (CHUNKS_DIR, SHARED_DIR):
        with open(os.path.join(d, chunk_name), "wb") as f:
            f.write(data)


def _download_chunk(ip: str, chunk_name: str, secure: bool) -> bytes | None:
    ts        = time.strftime("%Y-%m-%d %H:%M:%S")
    peer_name = ip_to_username(ip)
    main_log(f"[Downloader] Requesting '{chunk_name}' from {peer_name} "
             f"[{'secure' if secure else 'plain'}]")
    des_key_bytes = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(None)
        sock.connect((ip, TCP_PORT))

        if secure:
            if not PYDES_AVAILABLE:
                main_log("[Downloader] pyDes unavailable // cannot do secure download.")
                sock.close()
                return None

            my_priv = random.randint(2, DH_P - 2)
            my_pub  = pow(DH_G, my_priv, DH_P)
            _send_msg(sock, json.dumps({"key": str(my_pub)}).encode("utf-8"))

            reply         = json.loads(_recv_msg(sock).decode("utf-8"))
            their_pub     = int(reply["key"])
            shared_sec    = pow(their_pub, my_priv, DH_P)
            des_key_bytes = make_des_key(shared_sec)
            main_log(f"[Downloader] DH complete. shared_secret={shared_sec}, "
                     f"des_key={des_key_bytes}")

            req = json.dumps({"requested_secured_content": chunk_name})
        else:
            req = json.dumps({"requested_content": chunk_name})

        _send_msg(sock, req.encode("utf-8"))
        raw  = _recv_msg(sock)
        sock.close()
        resp = json.loads(raw.decode("utf-8"))

        if secure and "encrypted_chunk" in resp:
            chunk_bytes = pyDes.des(des_key_bytes, pyDes.ECB, pad=None,
                                    padmode=pyDes.PAD_PKCS5
                                    ).decrypt(base64.b64decode(resp["encrypted_chunk"]))
        elif "data" in resp:
            chunk_bytes = base64.b64decode(resp["data"])
        else:
            main_log(f"[Downloader] Unexpected response keys: {list(resp.keys())}")
            return None

        _log_transfer(ts, chunk_name, ip, "RECEIVED", DOWNLOAD_LOG)
        _save_chunk(chunk_name, chunk_bytes)
        main_log(f"[Downloader] ✓ '{chunk_name}' received from {peer_name}.")
        return chunk_bytes

    except Exception as e:
        main_log(f"[Downloader] Failed '{chunk_name}' from {peer_name}: {type(e).__name__}: {e}")
        return None


def _merge_chunks(base_name: str, extension: str, n: int = 3) -> str:
    parts = []
    for i in range(1, n + 1):
        p = os.path.join(CHUNKS_DIR, f"{base_name}_{i}")
        if not os.path.isfile(p):
            raise FileNotFoundError(f"Missing chunk: {p}")
        parts.append(open(p, "rb").read())
    if extension and not extension.startswith("."):
        extension = "." + extension
    out = f"{base_name}{extension}"
    with open(out, "wb") as f:
        f.write(b"".join(parts))
    return out


def _lookup_ext(base_name: str) -> str:
    """Return the registered extension for a base name, or '' if unknown."""
    return _load_json(EXT_REGISTRY_FILE).get(base_name, "")


def _downloader_view_contents():
    cd = _load_json(CONTENT_DICT_FILE)
    if not cd:
        main_log("[Downloader] No content available yet.")
        return
    names = set()
    for k in cd:
        if "_" in k:
            base, idx = k.rsplit("_", 1)
            names.add(base if idx.isdigit() else k)
        else:
            names.add(k)
    main_log("[Downloader] === Available Content ===")
    # Show the real extension when we know it; otherwise show base name only.
    for n in sorted(names):
        ext = _lookup_ext(n)
        if ext:
            main_log(f"  {n}{ext}")
        else:
            main_log(f"  {n}  (type with its extension, e.g. {n}.png)")


def _downloader_do_download(content: str, secure: bool):
    cd = _load_json(CONTENT_DICT_FILE)

    # Determine base name and extension.
    base, ext = os.path.splitext(content)
    if not ext:
        # User typed no extension — try the local registry first.
        ext = _lookup_ext(base)
        if not ext:
            ext = ".png"
            main_log("[Downloader] No extension provided and none on record // "
                     "defaulting to .png. Tip: type the full filename (e.g. forest.png).")
        else:
            main_log(f"[Downloader] Using known extension '{ext}' for '{base}'.")

    if not cd:
        main_log("[Downloader] Content dictionary empty // has Discovery received anything?")
        return
    ok = 0
    for i in range(1, 4):
        chunk = f"{base}_{i}"
        ips   = cd.get(chunk, [])
        if not ips:
            main_log(f"[Downloader] No peers for '{chunk}'.")
            continue
        got = False
        for ip in ips:
            if _download_chunk(ip, chunk, secure) is not None:
                ok += 1; got = True; break
            main_log(f"[Downloader] Could not get '{chunk}' from {ip_to_username(ip)}.")
        if not got:
            main_log(f"[Downloader] '{chunk}' unavailable from any peer.")
    if ok == 3:
        try:
            out = _merge_chunks(base, ext)
            main_log(f"[Downloader] ✓ Merged → '{out}'")
        except FileNotFoundError as e:
            main_log(f"[Downloader] Merge failed: {e}")
    else:
        main_log(f"[Downloader] Incomplete: {ok}/3 chunks retrieved.")


def _downloader_view_history():
    if not os.path.isfile(HISTORY_LOG):
        main_log("[Downloader] No history yet.")
        return
    main_log("[Downloader] === Transfer History ===")
    with open(HISTORY_LOG) as f:
        for line in f.readlines():
            main_log("  " + line.rstrip())


# ── Downloader dialog window ───────────────────────────────────────────────────

def start_downloader():
    dlwin = tk.Toplevel(root)
    dlwin.title("CHUNK DOWNLOADER // TRANSFER INTERFACE")
    dlwin.geometry("340x220+560+320")
    dlwin.configure(bg="#000000")
    dlwin.resizable(False, False)

    tk.Label(dlwin, text="// CHUNK DOWNLOADER",
             font=("Times New Roman", 14, "bold"),
             bg="#000000", fg="#ffffff").pack(pady=(14, 6))

    def do_view():
        threading.Thread(target=_downloader_view_contents, daemon=True).start()
        dlwin.destroy()

    def do_download():
        content = simpledialog.askstring("Download", "Content name (e.g. forest.png):",
                                         parent=dlwin)
        if not content: return
        secure_ans = messagebox.askyesno("Secure Download?",
                                         "Use secure (encrypted) download?",
                                         parent=dlwin)
        secure = secure_ans and PYDES_AVAILABLE
        if secure_ans and not PYDES_AVAILABLE:
            main_log("[Downloader] pyDes not installed; switching to plain mode.")
        threading.Thread(target=_downloader_do_download, args=(content, secure),
                         daemon=True).start()
        dlwin.destroy()

    def do_history():
        threading.Thread(target=_downloader_view_history, daemon=True).start()
        dlwin.destroy()

    btn_style = dict(bg="#ffffff", fg="#000000", font=("Times New Roman", 13, "bold"),
                     relief=tk.FLAT, padx=16, pady=8, cursor="hand2")
    tk.Button(dlwin, text="VIEW AVAILABLE CONTENT", command=do_view,
              **btn_style).pack(pady=5, fill=tk.X, padx=30)
    tk.Button(dlwin, text="INITIATE DOWNLOAD",       command=do_download,
              **btn_style).pack(pady=5, fill=tk.X, padx=30)
    tk.Button(dlwin, text="TRANSFER LOG",           command=do_history,
              **btn_style).pack(pady=5, fill=tk.X, padx=30)


# ══════════════════════════════════════════════════════════════════════════════
#  ROOT WINDOW  (main log + buttons + settings)
# ══════════════════════════════════════════════════════════════════════════════

root = tk.Tk()
root.title("THE RANDOMS // P2P TRANSFER SYSTEM")
root.geometry("860x620+200+100")
root.minsize(860, 620)
root.configure(bg="#000000")
root.resizable(True, True)

# ── Header ────────────────────────────────────────────────────────────────────
tk.Label(root, text="THE RANDOMS // P2P TRANSFER SYSTEM",
         font=("Times New Roman", 17, "bold"),
         bg="#000000", fg="#ffffff").pack(pady=(14, 2))

# FIX 4 (test IP): show detected broadcast IP and allow manual override
ip_frame = tk.Frame(root, bg="#000000")
ip_frame.pack(fill=tk.X, padx=16, pady=(0, 4))

tk.Label(ip_frame, text="BROADCAST TARGET:",
         bg="#000000", fg="#aaaaaa",
         font=("Times New Roman", 12)).pack(side=tk.LEFT)

_ip_var = tk.StringVar(value=BROADCAST_IP)
_ip_entry = tk.Entry(ip_frame, textvariable=_ip_var,
                     bg="#0a0a0a", fg="#ffffff",
                     insertbackground="#ffffff",
                     font=("Times New Roman", 12), width=18,
                     relief=tk.FLAT, highlightthickness=1,
                     highlightbackground="#222222")
_ip_entry.pack(side=tk.LEFT, padx=(6, 4), ipady=2)

def _apply_ip():
    global BROADCAST_IP
    new_ip = _ip_var.get().strip()
    if new_ip:
        BROADCAST_IP = new_ip
        main_log(f"[Settings] Broadcast IP updated to: {BROADCAST_IP}")

tk.Button(ip_frame, text="APPLY",
          command=_apply_ip,
          bg="#ffffff", fg="#000000",
          font=("Times New Roman", 12), relief=tk.FLAT,
          padx=8, pady=2, cursor="hand2").pack(side=tk.LEFT)

tk.Label(ip_frame, text="[ AUTO-DETECTED // CHANGE IF PEERS NOT VISIBLE ]",
         bg="#000000", fg="#666666",
         font=("Times New Roman", 11)).pack(side=tk.LEFT, padx=6)

# ── Main log list ──────────────────────────────────────────────────────────────
frame_list = tk.Frame(root, bg="#000000")
frame_list.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 4))

scrollbar = tk.Scrollbar(frame_list)
scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

_main_list = tk.Listbox(frame_list,
                         yscrollcommand=scrollbar.set,
                         bg="#000000", fg="#ffffff",
                         selectbackground="#ffffff",
                           selectforeground="#000000",
                         font=("Times New Roman", 12),
                         width=72, height=22,
                         borderwidth=0, highlightthickness=0)
_main_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
scrollbar.config(command=_main_list.yview)

# ── Button row ────────────────────────────────────────────────────────────────
btn_row = tk.Frame(root, bg="#000000")
btn_row.pack(pady=(8, 14))

BTN = dict(font=("Times New Roman", 13, "bold"),
           relief=tk.FLAT, padx=14, pady=8,
           cursor="hand2", width=12)

tk.Button(btn_row, text="DISCOVERY",  command=start_discovery,
          bg="#ffffff", fg="#000000", **BTN).pack(side=tk.LEFT, padx=6)
tk.Button(btn_row, text="DOWNLOADER", command=start_downloader,
          bg="#ffffff", fg="#000000", **BTN).pack(side=tk.LEFT, padx=6)
tk.Button(btn_row, text="ANNOUNCER",  command=start_announcer,
          bg="#ffffff", fg="#000000", **BTN).pack(side=tk.LEFT, padx=6)
tk.Button(btn_row, text="UPLOADER",   command=start_uploader,
          bg="#ffffff", fg="#000000", **BTN).pack(side=tk.LEFT, padx=6)

main_log(f"// SYSTEM ONLINE // BROADCAST TARGET: {BROADCAST_IP}")
main_log("// INITIALISE MODULES BELOW. ADJUST BROADCAST IP IF PEERS ARE NOT VISIBLE.")

root.mainloop()