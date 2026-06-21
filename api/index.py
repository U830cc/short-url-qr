"""Short URL & QR Code — Flask app (Neon Postgres + Vercel serverless).

ธีม terminal/dark ให้เข้ากับบล็อก HubRoute+ (https://hubrouteplus.blogspot.com/)

Environment variables:
- DATABASE_URL   : connection string ของ Neon Postgres (จำเป็น)
- BASE_URL       : โดเมนที่ใช้สร้างลิงก์สั้น (ไม่ใส่ก็ใช้โดเมนของ request)
- CRON_SECRET    : โทเคนป้องกัน endpoint /api/cleanup (Vercel ใส่ให้อัตโนมัติเมื่อใช้ Cron)
- ADMIN_PASSWORD : รหัสผ่านเข้าหน้า /dashboard (ถ้าไม่ตั้ง หน้า dashboard จะถูกปิด)
"""

import base64
import io
import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg
import qrcode
from flask import (
    Flask,
    abort,
    redirect,
    render_template_string,
    request,
    Response,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
BASE_URL = os.environ.get("BASE_URL", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
INACTIVE_DAYS = 7
CODE_LENGTH = 6
TH_TZ = timezone(timedelta(hours=7))  # Asia/Bangkok

app = Flask(__name__)


# ---------- database ----------

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("ยังไม่ได้ตั้งค่า DATABASE_URL (Neon connection string)")
    return psycopg.connect(DATABASE_URL, autocommit=True)


_schema_ready = False


def ensure_schema():
    """สร้างตาราง/คอลัมน์ถ้ายังไม่มี (เรียกครั้งเดียวต่อ cold start)."""
    global _schema_ready
    if _schema_ready:
        return
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id          BIGSERIAL PRIMARY KEY,
                code        TEXT        UNIQUE NOT NULL,
                original    TEXT        NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_used   TIMESTAMPTZ NOT NULL DEFAULT now(),
                clicks      BIGINT      NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("ALTER TABLE links ADD COLUMN IF NOT EXISTS clicks BIGINT NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_links_last_used ON links (last_used)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
        )
    _schema_ready = True


# ---------- settings (key/value) ----------

def get_ads():
    """คืนค่า (เปิดแสดงโฆษณาหรือไม่, โค้ดโฆษณา)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN ('ads_enabled', 'ad_code')"
        ).fetchall()
    d = {k: v for k, v in rows}
    return d.get("ads_enabled") == "1", d.get("ad_code", "")


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


# ---------- helpers ----------

def generate_code(conn):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(CODE_LENGTH))
        exists = conn.execute("SELECT 1 FROM links WHERE code = %s", (code,)).fetchone()
        if not exists:
            return code


def create_link(original):
    with get_conn() as conn:
        code = generate_code(conn)
        conn.execute(
            "INSERT INTO links (code, original) VALUES (%s, %s)",
            (code, original),
        )
    return code


def make_qr_data_url(target):
    img = qrcode.make(target, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def base_url():
    if BASE_URL:
        return BASE_URL.rstrip("/")
    return request.host_url.rstrip("/")


def run_cleanup():
    cutoff = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM links WHERE last_used < %s", (cutoff,))
        return cur.rowcount


def fmt_dt(dt):
    if dt is None:
        return "-"
    try:
        return dt.astimezone(TH_TZ).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(dt)


# ---------- routes ----------

@app.route("/", methods=["GET", "POST"])
def index():
    ensure_schema()
    result = None
    error = None
    if request.method == "POST":
        original = (request.form.get("url") or "").strip()
        if not original:
            error = "กรุณากรอก URL"
        elif not original.startswith(("http://", "https://")):
            error = "URL ต้องขึ้นต้นด้วย http:// หรือ https://"
        else:
            code = create_link(original)
            short = f"{base_url()}/{code}"
            result = {
                "short": short,
                "original": original,
                "qr": make_qr_data_url(short),
            }
    ads_enabled, ad_code = get_ads()
    ad_html = ad_code if (ads_enabled and ad_code.strip()) else None
    return render_template_string(
        PAGE_HTML,
        result=result,
        error=error,
        inactive_days=INACTIVE_DAYS,
        year=datetime.now(TH_TZ).year,
        ad_html=ad_html,
    )


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    # ป้องกันด้วย HTTP Basic Auth (รหัสผ่าน = ADMIN_PASSWORD)
    if not ADMIN_PASSWORD:
        return Response(
            "หน้า dashboard ยังไม่เปิดใช้งาน — ตั้งค่า environment variable ADMIN_PASSWORD ก่อน",
            status=503,
            mimetype="text/plain; charset=utf-8",
        )
    auth = request.authorization
    if not auth or not secrets.compare_digest(auth.password or "", ADMIN_PASSWORD):
        return Response(
            "ต้องยืนยันตัวตน",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="HubRoute+ Dashboard"'},
        )

    ensure_schema()

    # บันทึกการตั้งค่าโฆษณา
    saved = False
    if request.method == "POST":
        set_setting("ad_code", request.form.get("ad_code", "").strip())
        set_setting("ads_enabled", "1" if request.form.get("ads_enabled") == "1" else "0")
        saved = True

    ads_enabled, ad_code = get_ads()
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        total_links = conn.execute("SELECT count(*) FROM links").fetchone()[0]
        total_clicks = conn.execute("SELECT coalesce(sum(clicks), 0) FROM links").fetchone()[0]
        rows = conn.execute(
            """
            SELECT code, original, clicks, created_at, last_used
            FROM links
            ORDER BY clicks DESC, created_at DESC
            LIMIT 300
            """
        ).fetchall()

    links = []
    expiring_soon = 0
    for code, original, clicks, created_at, last_used in rows:
        days_idle = (now - last_used).days
        days_left = max(0, INACTIVE_DAYS - days_idle)
        if days_left <= 2:
            expiring_soon += 1
        links.append(
            {
                "code": code,
                "short": f"{base_url()}/{code}",
                "original": original,
                "original_short": (original[:60] + "…") if len(original) > 60 else original,
                "clicks": clicks,
                "created": fmt_dt(created_at),
                "last_used": fmt_dt(last_used),
                "days_left": days_left,
            }
        )

    return render_template_string(
        DASHBOARD_HTML,
        total_links=total_links,
        total_clicks=total_clicks,
        expiring_soon=expiring_soon,
        links=links,
        inactive_days=INACTIVE_DAYS,
        year=datetime.now(TH_TZ).year,
        ads_enabled=ads_enabled,
        ad_code=ad_code,
        saved=saved,
    )


@app.route("/api/cleanup")
def cleanup_endpoint():
    if CRON_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {CRON_SECRET}":
            abort(401)
    ensure_schema()
    removed = run_cleanup()
    return {"removed": removed}


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/<code>")
def resolve(code):
    ensure_schema()
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE links SET last_used = now(), clicks = clicks + 1 "
            "WHERE code = %s RETURNING original",
            (code,),
        ).fetchone()
    if row is None:
        abort(404)
    return redirect(row[0])


# ---------- shared theme ----------

THEME_CSS = r"""
    :root {
      --bg:#1a1a2e; --panel:#16213e; --term:#0d1117; --term-head:#0a0e16;
      --line:rgba(255,255,255,.08); --text:#e0e0e0; --muted:#7a8ba3;
      --teal:#00d4aa; --green:#28c840; --red:#ff5f57; --yellow:#ffbd2e; --blue:#4fc3f7;
    }
    * { box-sizing:border-box; margin:0; padding:0; }
    body {
      font-family:"JetBrains Mono","Fira Code","Cascadia Code","SF Mono",Consolas,monospace;
      background:var(--bg); color:var(--text);
      min-height:100vh; padding:20px;
      display:flex; align-items:flex-start; justify-content:center;
    }
    .term {
      width:100%; max-width:760px; background:var(--term);
      border:1px solid var(--line); border-radius:12px; overflow:hidden;
      box-shadow:0 20px 50px rgba(0,0,0,.45);
    }
    .term-head {
      background:var(--term-head); padding:11px 14px;
      display:flex; align-items:center; gap:8px; border-bottom:1px solid var(--line);
    }
    .dot { width:12px; height:12px; border-radius:50%; }
    .dot.r{background:var(--red)} .dot.y{background:var(--yellow)} .dot.g{background:var(--green)}
    .term-title { color:var(--muted); font-size:13px; margin-left:10px; letter-spacing:.3px; }
    .term-body { background:var(--panel); padding:30px; }
    .prompt { color:var(--green); }
    .prompt .path { color:var(--blue); }
    a { color:var(--teal); }
    .credit {
      margin-top:22px; padding-top:16px; border-top:1px solid var(--line);
      text-align:center; font-size:12px; color:var(--muted); letter-spacing:.3px;
    }
    .credit .brand { color:var(--teal); font-weight:700; }
    .credit .sep { color:var(--line); margin:0 8px; }
    .credit .heart { color:var(--red); }
"""


# ---------- index template ----------

PAGE_HTML = r"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Short URL & QR Code</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
""" + THEME_CSS + r"""
    .heading { font-size:13px; color:var(--muted); margin-bottom:18px; }
    h1 { font-size:24px; color:#fff; margin-bottom:6px; }
    h1 .arrow { color:var(--teal); }
    .subtitle { color:var(--muted); font-size:13px; margin-bottom:26px; }
    form { display:flex; gap:10px; }
    .input-wrap { flex:1; display:flex; align-items:center; gap:8px;
      background:var(--term); border:1px solid var(--line); border-radius:8px;
      padding:0 14px; transition:border-color .15s; }
    .input-wrap:focus-within { border-color:var(--teal); }
    .input-wrap .sigil { color:var(--green); font-weight:700; }
    input[type="url"] {
      flex:1; padding:13px 0; font-size:15px; background:transparent;
      border:none; outline:none; color:var(--text);
      font-family:inherit;
    }
    input[type="url"]::placeholder { color:#55657d; }
    button {
      padding:13px 24px; font-size:15px; font-weight:700; color:#062b25;
      background:var(--teal); border:none; border-radius:8px; cursor:pointer;
      font-family:inherit; transition:filter .15s, transform .1s;
    }
    button:hover { filter:brightness(1.08); }
    button:active { transform:translateY(1px); }
    .error { color:#ffb4ae; background:rgba(255,95,87,.12);
      border:1px solid rgba(255,95,87,.3); padding:10px 14px;
      border-radius:8px; margin-top:16px; font-size:13px; }
    .result { margin-top:30px; display:grid; grid-template-columns:1fr auto;
      gap:26px; align-items:center; }
    .result-info label { display:block; color:var(--muted); font-size:11px;
      text-transform:uppercase; letter-spacing:1px; margin-bottom:8px; }
    .short-box { display:flex; align-items:center; gap:8px; }
    .short-link { font-size:17px; font-weight:700; color:var(--teal); word-break:break-all; text-decoration:none; }
    .copy-btn { padding:6px 12px; font-size:12px; background:rgba(0,212,170,.12);
      color:var(--teal); border:1px solid rgba(0,212,170,.3); border-radius:6px;
      cursor:pointer; white-space:nowrap; font-family:inherit; }
    .copy-btn:hover { background:rgba(0,212,170,.2); }
    .original-url { color:var(--muted); font-size:12px; margin-top:12px; word-break:break-all; }
    .original-url .arrow { color:var(--green); }
    .qr-wrap { text-align:center; }
    .qr-wrap .qr-card { background:#fff; padding:10px; border-radius:10px; display:inline-block; }
    .qr-wrap img { width:160px; height:160px; display:block; }
    .download-btn { display:inline-block; margin-top:10px; font-size:12px;
      color:var(--teal); text-decoration:none; }
    .ad-slot { margin-top:30px; text-align:center; overflow:hidden; }
    .ad-slot img, .ad-slot iframe { max-width:100%; }
    .note { margin-top:30px; padding-top:18px; border-top:1px solid var(--line);
      color:var(--muted); font-size:12px; }
    .note .green { color:var(--green); }
    @media (max-width:540px) {
      .term-body { padding:22px; }
      form { flex-direction:column; }
      .result { grid-template-columns:1fr; justify-items:start; }
    }
  </style>
</head>
<body>
  <div class="term">
    <div class="term-head">
      <span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
      <span class="term-title">shorturl — qr-generator</span>
    </div>
    <div class="term-body">
      <div class="heading"><span class="prompt"><span class="path">~/shorturl</span>$</span> ./generate</div>
      <h1><span class="arrow">&#9656;</span> Short URL &amp; QR Code</h1>
      <p class="subtitle">วาง URL ยาว ๆ แล้วรับทั้งลิงก์สั้นและ QR Code ทันที</p>

      <form method="post">
        <div class="input-wrap">
          <span class="sigil">&gt;</span>
          <input type="url" name="url" placeholder="https://example.com/very/long/url/..."
                 value="{{ result.original if result else '' }}" required autofocus>
        </div>
        <button type="submit">&#9656; ย่อ URL</button>
      </form>

      {% if error %}
        <div class="error"># {{ error }}</div>
      {% endif %}

      {% if result %}
        <div class="result">
          <div class="result-info">
            <label>ลิงก์สั้น</label>
            <div class="short-box">
              <a class="short-link" href="{{ result.short }}" target="_blank" rel="noopener">{{ result.short }}</a>
              <button class="copy-btn" type="button" onclick="copyLink(this, '{{ result.short }}')">copy</button>
            </div>
            <div class="original-url"><span class="arrow">&rarr;</span> {{ result.original }}</div>
          </div>
          <div class="qr-wrap">
            <div class="qr-card"><img src="{{ result.qr }}" alt="QR Code"></div>
            <br>
            <a class="download-btn" href="{{ result.qr }}" download="qr-{{ result.short.split('/')[-1] }}.png">&#x2B07; download QR</a>
          </div>
        </div>
      {% endif %}

      {% if ad_html %}
      <div class="ad-slot">{{ ad_html|safe }}</div>
      {% endif %}

      <div class="note">
        <span class="green">#</span> ลิงก์ที่ไม่ได้ถูกใช้งานเกิน {{ inactive_days }} วัน จะถูกระบบลบอัตโนมัติ
      </div>

      <div class="credit">
        <span class="brand">MAPOHJI License</span><span class="sep">·</span>&copy; {{ year }}<span class="sep">·</span>Short URL &amp; QR Code
      </div>
    </div>
  </div>

  <script>
    function copyLink(btn, url) {
      navigator.clipboard.writeText(url).then(function () {
        var old = btn.textContent;
        btn.textContent = 'copied ✓';
        setTimeout(function () { btn.textContent = old; }, 1500);
      });
    }
  </script>
</body>
</html>"""


# ---------- dashboard template ----------

DASHBOARD_HTML = r"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dashboard — Short URL</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
""" + THEME_CSS + r"""
    body { align-items:flex-start; }
    .term { max-width:1000px; }
    .heading { font-size:13px; color:var(--muted); margin-bottom:20px; }
    h1 { font-size:22px; color:#fff; margin-bottom:22px; }
    h1 .arrow { color:var(--teal); }
    .stats { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-bottom:26px; }
    .stat { background:var(--term); border:1px solid var(--line); border-radius:10px; padding:18px 20px; }
    .stat .num { font-size:30px; font-weight:700; color:var(--teal); }
    .stat.green .num { color:var(--green); }
    .stat.warn .num { color:var(--yellow); }
    .stat .lbl { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; margin-top:4px; }
    .table-wrap { overflow-x:auto; border:1px solid var(--line); border-radius:10px; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th, td { text-align:left; padding:11px 14px; border-bottom:1px solid var(--line); white-space:nowrap; }
    th { color:var(--muted); font-weight:500; text-transform:uppercase; font-size:11px; letter-spacing:.6px; background:var(--term); }
    tr:last-child td { border-bottom:none; }
    td.code a { color:var(--teal); text-decoration:none; font-weight:700; }
    td.orig { max-width:340px; overflow:hidden; text-overflow:ellipsis; color:var(--muted); }
    td.clicks { color:var(--green); font-weight:700; }
    .pill { font-size:11px; padding:2px 8px; border-radius:20px; }
    .pill.ok { color:var(--green); background:rgba(40,200,64,.12); }
    .pill.warn { color:var(--yellow); background:rgba(255,189,46,.14); }
    .empty { padding:30px; text-align:center; color:var(--muted); }
    .note { margin-top:18px; color:var(--muted); font-size:12px; }
    .ads-panel { margin-top:30px; padding-top:22px; border-top:1px solid var(--line); }
    .ads-panel h2 { font-size:16px; color:#fff; margin-bottom:6px; }
    .ads-panel h2 .arrow { color:var(--teal); }
    .ads-panel .hint { color:var(--muted); font-size:12px; margin-bottom:16px; }
    .chk { display:flex; align-items:center; gap:9px; color:var(--text);
      font-size:14px; margin-bottom:14px; cursor:pointer; }
    .chk input { width:16px; height:16px; accent-color:var(--teal); cursor:pointer; }
    .ads-panel textarea {
      width:100%; min-height:140px; resize:vertical; padding:14px;
      background:var(--term); color:var(--text); border:1px solid var(--line);
      border-radius:8px; font-family:inherit; font-size:13px; line-height:1.5;
      outline:none; transition:border-color .15s;
    }
    .ads-panel textarea:focus { border-color:var(--teal); }
    .ads-panel textarea::placeholder { color:#55657d; }
    .ads-actions { display:flex; align-items:center; gap:14px; margin-top:14px; }
    .ads-panel button {
      padding:11px 22px; font-size:14px; font-weight:700; color:#062b25;
      background:var(--teal); border:none; border-radius:8px; cursor:pointer;
      font-family:inherit; transition:filter .15s, transform .1s;
    }
    .ads-panel button:hover { filter:brightness(1.08); }
    .ads-panel button:active { transform:translateY(1px); }
    .saved { color:var(--green); font-size:13px; }
    @media (max-width:600px){ .stats{grid-template-columns:1fr;} .term-body{padding:20px;} }
  </style>
</head>
<body>
  <div class="term">
    <div class="term-head">
      <span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
      <span class="term-title">shorturl — dashboard</span>
    </div>
    <div class="term-body">
      <div class="heading"><span class="prompt"><span class="path">~/shorturl</span>$</span> ./stats --all</div>
      <h1><span class="arrow">&#9656;</span> สถิติการใช้งาน</h1>

      <div class="stats">
        <div class="stat"><div class="num">{{ total_links }}</div><div class="lbl">ลิงก์ทั้งหมด</div></div>
        <div class="stat green"><div class="num">{{ total_clicks }}</div><div class="lbl">คลิกรวม</div></div>
        <div class="stat warn"><div class="num">{{ expiring_soon }}</div><div class="lbl">ใกล้หมดอายุ (&le;2 วัน)</div></div>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>code</th><th>ปลายทาง</th><th>คลิก</th><th>สร้างเมื่อ</th><th>ใช้ล่าสุด</th><th>เหลือ</th>
            </tr>
          </thead>
          <tbody>
            {% for l in links %}
            <tr>
              <td class="code"><a href="{{ l.short }}" target="_blank" rel="noopener">/{{ l.code }}</a></td>
              <td class="orig" title="{{ l.original }}">{{ l.original_short }}</td>
              <td class="clicks">{{ l.clicks }}</td>
              <td>{{ l.created }}</td>
              <td>{{ l.last_used }}</td>
              <td>
                {% if l.days_left <= 2 %}
                  <span class="pill warn">{{ l.days_left }} วัน</span>
                {% else %}
                  <span class="pill ok">{{ l.days_left }} วัน</span>
                {% endif %}
              </td>
            </tr>
            {% endfor %}
            {% if not links %}
            <tr><td colspan="6" class="empty"># ยังไม่มีลิงก์ในระบบ</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>

      <div class="note"># เรียงตามจำนวนคลิกมากสุด · แสดงสูงสุด 300 รายการ · เวลาเป็น GMT+7</div>

      <div class="ads-panel">
        <h2><span class="arrow">&#9656;</span> โฆษณา (Ads)</h2>
        <p class="hint">วางโค้ด Google AdSense หรือ HTML/JS แล้วเปิดสวิตช์ — โฆษณาจะแสดงบนหน้าแรกก่อนบรรทัดแจ้งเตือนการลบลิงก์ (ถ้าปิดหรือไม่มีโค้ด หน้าแรกจะแสดงปกติ)</p>
        <form method="post">
          <label class="chk">
            <input type="checkbox" name="ads_enabled" value="1" {% if ads_enabled %}checked{% endif %}>
            เปิดแสดงโฆษณาบนหน้าแรก
          </label>
          <textarea name="ad_code" placeholder="&lt;!-- วางโค้ด Google AdSense หรือ HTML/JS ที่นี่ --&gt;">{{ ad_code }}</textarea>
          <div class="ads-actions">
            <button type="submit">&#9656; บันทึก</button>
            {% if saved %}<span class="saved">&#10003; บันทึกแล้ว</span>{% endif %}
          </div>
        </form>
      </div>

      <div class="credit">
        <span class="brand">MAPOHJI License</span><span class="sep">·</span>&copy; {{ year }}<span class="sep">·</span>Short URL &amp; QR Code
      </div>
    </div>
  </div>
</body>
</html>"""


if __name__ == "__main__":
    ensure_schema()
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
