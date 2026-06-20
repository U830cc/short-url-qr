# Short URL & QR Code

เว็บแอปย่อ URL พร้อมสร้าง QR Code — Python + Flask, ฐานข้อมูล Neon Postgres, deploy บน Vercel

## คุณสมบัติ

- วาง URL ยาว ๆ แล้วได้ลิงก์สั้นทันที
- สร้าง QR Code ให้อัตโนมัติ พร้อมปุ่มดาวน์โหลด
- คลิกลิงก์สั้นแล้ว redirect ไป URL เดิม (พร้อมอัปเดตเวลาใช้งานล่าสุด)
- ลิงก์ที่ไม่ได้ถูกใช้งานเกิน **7 วัน** จะถูกลบอัตโนมัติ ผ่าน Vercel Cron (รันทุกวันตี 3 UTC)
- ฝังในหน้าเว็บ Blogger ได้ด้วย iframe (ดู `BLOGGER_EMBED.html`)
- โดเมนหลัก: **https://hongnii.com** (สำรอง: https://short-url-qr.vercel.app)
- ธีม terminal/dark เข้ากับบล็อก HubRoute+ (ฟอนต์ JetBrains Mono, accent teal/green)
- หน้า **`/dashboard`** ดูสถิติ (ลิงก์ทั้งหมด, คลิกรวม, ใกล้หมดอายุ) ป้องกันด้วย Basic Auth (`ADMIN_PASSWORD`)

## สถาปัตยกรรม

| ส่วน             | เทคโนโลยี                          |
| ---------------- | --------------------------------- |
| เว็บเซิร์ฟเวอร์   | Flask (WSGI) บน Vercel Python      |
| ฐานข้อมูล         | Neon Postgres (serverless)         |
| ลบลิงก์อัตโนมัติ  | Vercel Cron → `/api/cleanup`       |

## ตั้งค่า Environment Variables

| ตัวแปร         | จำเป็น | คำอธิบาย                                                        |
| -------------- | ------ | -------------------------------------------------------------- |
| `DATABASE_URL` | ✅     | Neon Postgres connection string (แนะนำ endpoint แบบ pooled)     |
| `BASE_URL`     | —      | โดเมนสำหรับสร้างลิงก์สั้น เช่น `https://hongnii.com` (ไม่ใส่ก็เดาจาก request) |
| `CRON_SECRET`  | —      | โทเคนป้องกัน `/api/cleanup` (Vercel ใส่ให้อัตโนมัติเมื่อตั้ง Cron) |
| `ADMIN_PASSWORD` | —    | รหัสผ่านเข้าหน้า `/dashboard` (ถ้าไม่ตั้ง หน้า dashboard จะปิด)   |

## รัน local

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL="postgresql://...neon.tech/neondb?sslmode=require"
python3 api/index.py          # เปิด http://localhost:5000
```

## Deploy บน Vercel

```bash
vercel                # preview
vercel --prod         # production
```

ตั้งค่า `DATABASE_URL` ใน Vercel Project → Settings → Environment Variables
(Vercel Cron จะสร้าง `CRON_SECRET` ให้อัตโนมัติเมื่อ deploy ครั้งแรก)

## โครงสร้างไฟล์

```
.
├── api/
│   └── index.py          # โค้ดหลักทั้งหมด (Flask app + template + cleanup)
├── requirements.txt      # dependencies
├── vercel.json           # rewrites + cron
├── BLOGGER_EMBED.html    # โค้ด iframe สำหรับแปะใน Blogger
└── README.md
```
