import json
import requests
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google.protobuf import json_format
from google.protobuf.message import Message
from Crypto.Cipher import AES

# ── Proto compiled imports ────────────────────────────────────────────────────
import Proto.compiled.MajorLogin_pb2 as MajorLogin_pb2
import Proto.compiled.PlayerPersonalShow_pb2 as PlayerPersonalShow_pb2
import Proto.compiled.PlayerStats_pb2 as PlayerStats_pb2
import Proto.compiled.PlayerCSStats_pb2 as PlayerCSStats_pb2
import Proto.compiled.SearchAccountByName_pb2 as SearchAccountByName_pb2

# ── Config ────────────────────────────────────────────────────────────────────
MAIN_KEY       = b'Yg&tc%DEuh6%Zc^8'
MAIN_IV        = b'6oyZDr22E3ychjM%'
RELEASE_VER    = "OB53"
DEBUG          = False

# ── Load accounts from JSON ───────────────────────────────────────────────────
with open("Configuration/AccountConfiguration.json") as f:
    ACCOUNTS: dict = json.load(f)

# ── AES / Protobuf helpers ────────────────────────────────────────────────────
def _pad(data: bytes) -> bytes:
    pad_len = AES.block_size - (len(data) % AES.block_size)
    return data + bytes([pad_len] * pad_len)

def encode_proto(data: dict, proto_msg: Message) -> bytes:
    json_format.ParseDict(data, proto_msg)
    raw = proto_msg.SerializeToString()
    cipher = AES.new(MAIN_KEY, AES.MODE_CBC, MAIN_IV)
    return cipher.encrypt(_pad(raw))

def decode_proto(raw: bytes, msg_type) -> dict:
    instance = msg_type()
    instance.ParseFromString(raw)
    return json.loads(json_format.MessageToJson(instance))

# ── Common headers ────────────────────────────────────────────────────────────
def _base_headers(token: str | None = None) -> dict:
    h = {
        "User-Agent":      "Dalvik/2.1.0 (Linux; U; Android 13; A063 Build/TKQ1.221220.001)",
        "Connection":      "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Content-Type":    "application/x-www-form-urlencoded",
        "Expect":          "100-continue",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA":            "v1 1",
        "ReleaseVersion":  RELEASE_VER,
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

# ── Auth helpers ──────────────────────────────────────────────────────────────
def garena_token(uid: str, password: str) -> dict | None:
    resp = requests.post(
        "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant",
        data={
            "uid": uid, "password": password,
            "response_type": "token", "client_type": "2",
            "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
            "client_id": "100067",
        },
        headers={
            "User-Agent":      "GarenaMSDK/4.0.19P9(A063 ;Android 13;en;IN;)",
            "Connection":      "Keep-Alive",
            "Accept-Encoding": "gzip",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def major_login(access_token: str, open_id: str) -> dict | None:
    payload = encode_proto(
        {"openid": open_id, "logintoken": access_token, "platform": "4"},
        MajorLogin_pb2.request(),
    )
    resp = requests.post(
        "https://loginbp.ggpolarbear.com/MajorLogin",
        data=payload,
        headers=_base_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return decode_proto(resp.content, MajorLogin_pb2.response)

def _auth(server: str) -> tuple[str, str]:
    """Returns (token, serverUrl) or raises."""
    creds = ACCOUNTS[server]
    gt = garena_token(creds["uid"], creds["password"])
    ml = major_login(gt["accessToken"], gt["openId"])
    return ml["token"], ml["serverUrl"]

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="FreeFire API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def err(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)

# ── /getinfo ──────────────────────────────────────────────────────────────────
@app.get("/getinfo")
def getinfo(
    uid:    str = Query(..., description="Player UID"),
    server: str = Query("IND", description="Server region, e.g. IND, BD, SG"),
):
    server = server.upper()

    if not uid.isdigit():
        return err("UID must be numeric")
    if server not in ACCOUNTS:
        return err(f"Unknown server '{server}'. Available: {list(ACCOUNTS)}")

    try:
        token, server_url = _auth(server)
    except Exception as e:
        return err(f"Authentication failed: {e}", 401)

    # Personal show (main profile data)
    try:
        payload = encode_proto(
            {"accountId": int(uid), "callSignSrc": 7,
             "needGalleryInfo": False, "needBlacklist": False, "needSparkInfo": False},
            PlayerPersonalShow_pb2.request(),
        )
        resp = requests.post(
            f"{server_url}/GetPlayerPersonalShow",
            data=payload,
            headers={**_base_headers(token), "Host": "client.ind.freefiremobile.com",
                     "User-Agent": "UnityPlayer/2022.3.47f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
                     "Accept": "*/*", "X-Unity-Version": "2022.3.47f1"},
            timeout=15,
        )
        resp.raise_for_status()
        data = decode_proto(resp.content, PlayerPersonalShow_pb2.response)
    except Exception as e:
        return err(f"Failed to fetch player info: {e}", 502)

    if not data:
        return err(f"No data found for UID {uid}", 404)

    return JSONResponse(data)


# ── Optional extra endpoints (kept minimal) ───────────────────────────────────
@app.get("/getstats")
def getstats(
    uid:       str = Query(...),
    server:    str = Query("IND"),
    gamemode:  str = Query("br",     description="br or cs"),
    matchmode: str = Query("CAREER", description="CAREER, NORMAL or RANKED"),
):
    server    = server.upper()
    gamemode  = gamemode.lower()
    matchmode = matchmode.upper()

    if not uid.isdigit():                           return err("UID must be numeric")
    if server not in ACCOUNTS:                      return err(f"Unknown server '{server}'")
    if gamemode not in ("br", "cs"):                return err("gamemode must be br or cs")
    if matchmode not in ("CAREER","NORMAL","RANKED"):return err("matchmode must be CAREER, NORMAL or RANKED")

    try:
        token, server_url = _auth(server)
    except Exception as e:
        return err(f"Authentication failed: {e}", 401)

    br_map = {"CAREER": 0, "NORMAL": 1, "RANKED": 2}
    cs_map = {"CAREER": 0, "NORMAL": 1, "RANKED": 6}

    if gamemode == "br":
        endpoint    = f"{server_url}/GetPlayerStats"
        proto_mod   = PlayerStats_pb2
        payload_data = {"accountid": int(uid), "matchmode": br_map[matchmode]}
    else:
        endpoint    = f"{server_url}/GetPlayerTCStats"
        proto_mod   = PlayerCSStats_pb2
        payload_data = {"accountid": int(uid), "gamemode": 15, "matchmode": cs_map[matchmode]}

    try:
        payload = encode_proto(payload_data, proto_mod.request())
        resp = requests.post(endpoint, data=payload, headers=_base_headers(token), timeout=30)
        resp.raise_for_status()
        return JSONResponse(decode_proto(resp.content, proto_mod.response))
    except Exception as e:
        return err(f"Stats fetch failed: {e}", 502)


@app.get("/search")
def search(
    keyword: str = Query(..., min_length=3),
    server:  str = Query("IND"),
):
    server = server.upper()
    if server not in ACCOUNTS:
        return err(f"Unknown server '{server}'")

    try:
        token, server_url = _auth(server)
    except Exception as e:
        return err(f"Authentication failed: {e}", 401)

    try:
        payload = encode_proto({"keyword": keyword}, SearchAccountByName_pb2.request())
        resp = requests.post(
            f"{server_url}/FuzzySearchAccountByName",
            data=payload, headers=_base_headers(token), timeout=15,
        )
        resp.raise_for_status()
        return JSONResponse(decode_proto(resp.content, SearchAccountByName_pb2.response))
    except Exception as e:
        return err(f"Search failed: {e}", 502)


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
