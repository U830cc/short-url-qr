"""Short URL & QR Code — Flask app (Neon Postgres + Vercel serverless).

ทำงานได้ทั้งบนเครื่อง local และบน Vercel โดยอ่านการตั้งค่าจาก environment variable
- DATABASE_URL : connection string ของ Neon Postgres (จำเป็น)
- BASE_URL     : โดเมนที่ใช้สร้างลิงก์สั้น (ไม่ใส่ก็ได้ จะใช้โดเมนของ request เอง)
- CRON_SECRET  : โทเคนป้องกัน endpoint /api/cleanup (Vercel ใส่ให้อัตโนมัติเมื่อใช้ Cron)
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
INACTIVE_DAYS = 7
CODE_LENGTH = 6

app = Flask(__name__)


# ---------- database ----------

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("ยังไม่ได้ตั้งค่า DATABASE_URL (Neon connection string)")
    return psycopg.connect(DATABASE_URL, autocommit=True)


_schema_ready = False


def ensure_schema():
    """สร้างตารางถ้ายังไม่มี (เรียกครั้งเดียวต่อ cold start)."""
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
                last_used   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_links_last_used ON links (last_used)"
        )
    _schema_ready = True


# ---------- helpers ----------

def generate_code(conn):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(CODE_LENGTH))
        exists = conn.execute(
            "SELECT 1 FROM links WHERE code = %s", (code,)
        ).fetchone()
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
    """โดเมนสำหรับสร้างลิงก์สั้น — ใช้ค่า BASE_URL ถ้ามี ไม่งั้นเดาจาก request."""
    if BASE_URL:
        return BASE_URL.rstrip("/")
    return request.host_url.rstrip("/")


def run_cleanup():
    """ลบลิงก์ที่ไม่ถูกใช้งานเกิน INACTIVE_DAYS วัน คืนค่าจำนวนที่ลบ."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM links WHERE last_used < %s", (cutoff,))
        return cur.rowcount


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
    return render_template_string(
        PAGE_HTML, result=result, error=error, inactive_days=INACTIVE_DAYS
    )


@app.route("/api/cleanup")
def cleanup_endpoint():
    """ถูกเรียกโดย Vercel Cron ทุกวัน — ป้องกันด้วย CRON_SECRET ถ้าตั้งค่าไว้."""
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
            "UPDATE links SET last_used = now() WHERE code = %s RETURNING original",
            (code,),
        ).fetchone()
    if row is None:
        abort(404)
    return redirect(row[0])


# ---------- inline template ----------

PAGE_HTML = r"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Short URL & QR Code</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Sarabun", sans-serif;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      background: #fff;
      border-radius: 18px;
      box-shadow: 0 20px 50px rgba(0,0,0,.2);
      max-width: 720px;
      width: 100%;
      padding: 40px;
    }
    h1 { color: #2d2d44; font-size: 28px; text-align: center; margin-bottom: 8px; }
    .subtitle { color: #888; text-align: center; margin-bottom: 28px; font-size: 14px; }
    form { display: flex; gap: 10px; }
    input[type="url"] {
      flex: 1; padding: 14px 16px; font-size: 16px;
      border: 2px solid #e0e0e0; border-radius: 10px; outline: none;
      transition: border-color .2s;
    }
    input[type="url"]:focus { border-color: #667eea; }
    button {
      padding: 14px 28px; font-size: 16px; font-weight: 600; color: #fff;
      background: linear-gradient(135deg, #667eea, #764ba2);
      border: none; border-radius: 10px; cursor: pointer; transition: transform .1s;
    }
    button:hover { transform: translateY(-2px); }
    button:active { transform: translateY(0); }
    .error {
      color: #d32f2f; background: #ffebee; padding: 10px 14px;
      border-radius: 8px; margin-top: 16px; font-size: 14px;
    }
    .result {
      margin-top: 32px; display: grid; grid-template-columns: 1fr auto;
      gap: 28px; align-items: center;
    }
    .result-info label {
      display: block; color: #999; font-size: 12px; text-transform: uppercase;
      letter-spacing: .5px; margin-bottom: 6px;
    }
    .short-box { display: flex; align-items: center; gap: 8px; }
    .short-link { font-size: 18px; font-weight: 600; color: #667eea; word-break: break-all; }
    .copy-btn {
      padding: 6px 12px; font-size: 13px; background: #f0f0f5; color: #2d2d44;
      border: none; border-radius: 6px; cursor: pointer; white-space: nowrap;
    }
    .copy-btn:hover { background: #e0e0eb; }
    .original-url { color: #777; font-size: 13px; margin-top: 10px; word-break: break-all; }
    .qr-wrap { text-align: center; }
    .qr-wrap img { width: 180px; height: 180px; border: 1px solid #eee; border-radius: 10px; }
    .download-btn {
      display: inline-block; margin-top: 10px; font-size: 13px;
      color: #667eea; text-decoration: none;
    }
    .note {
      margin-top: 32px; padding-top: 20px; border-top: 1px solid #f0f0f0;
      color: #aaa; font-size: 12px; text-align: center;
    }
    @media (max-width: 540px) {
      .card { padding: 24px; }
      form { flex-direction: column; }
      .result { grid-template-columns: 1fr; justify-items: center; text-align: center; }
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>Short URL & QR Code</h1>
    <p class="subtitle">วาง URL ยาว ๆ แล้วรับทั้งลิงก์สั้นและ QR Code ทันที</p>

    <form method="post">
      <input type="url" name="url" placeholder="https://example.com/very/long/url/..."
             value="{{ result.original if result else '' }}" required autofocus>
      <button type="submit">ย่อ URL</button>
    </form>

    {% if error %}
      <div class="error">{{ error }}</div>
    {% endif %}

    {% if result %}
      <div class="result">
        <div class="result-info">
          <label>ลิงก์สั้น</label>
          <div class="short-box">
            <a class="short-link" href="{{ result.short }}" target="_blank" rel="noopener">{{ result.short }}</a>
            <button class="copy-btn" type="button" onclick="copyLink(this, '{{ result.short }}')">คัดลอก</button>
          </div>
          <div class="original-url">&rarr; {{ result.original }}</div>
        </div>
        <div class="qr-wrap">
          <img src="{{ result.qr }}" alt="QR Code">
          <br>
          <a class="download-btn" href="{{ result.qr }}" download="qr-{{ result.short.split('/')[-1] }}.png">&#x2B07; ดาวน์โหลด QR</a>
        </div>
      </div>
    {% endif %}

    <div class="note">
      ลิงก์ที่ไม่ได้ถูกใช้งานเกิน {{ inactive_days }} วัน จะถูกระบบลบอัตโนมัติ
    </div>
  </div>

  <script>
    function copyLink(btn, url) {
      navigator.clipboard.writeText(url).then(function () {
        var old = btn.textContent;
        btn.textContent = 'คัดลอกแล้ว ✓';
        setTimeout(function () { btn.textContent = old; }, 1500);
      });
    }
  </script>
</body>
</html>"""


if __name__ == "__main__":
    ensure_schema()
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
