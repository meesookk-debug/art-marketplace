# Art Marketplace — ตัวอย่าง Project

โปรเจกต์ตัวอย่างตามดราฟ Project: ระบบซื้อขายผลงานศิลปะออนไลน์ (Art Marketplace)

## ฟีเจอร์ที่มี
- Register / Login
- Role-based access: `admin`, `staff`, `customer`
- Password hashing
- CRUD ผลงานศิลปะ
- Validation ข้อมูล
- ค้นหา / กรองหมวดหมู่ / เรียงราคาและชื่อ / Pagination
- Dashboard / รายงานยอดขายและจำนวนข้อมูล
- ตะกร้าสินค้า
- สร้างคำสั่งซื้อ
- ขั้นตอน PromptPay: สร้างออเดอร์ → แสดงยอด → ลูกค้ากดยืนยันการชำระ → เจ้าหน้าที่ตรวจสอบและเปลี่ยนสถานะ
- ติดตามสถานะคำสั่งซื้อ
- อนุมัติผลงานก่อนเผยแพร่
- Log การกระทำสำคัญ
- Deploy โครงสร้างสำหรับ Vercel

## บัญชีตัวอย่าง
- admin / admin123
- staff / staff123
- customer / customer123

> เปลี่ยนรหัสผ่านทันทีเมื่อนำไปใช้งานจริง

## วิธีรันในเครื่อง
1. สร้าง PostgreSQL database
2. คัดลอก `.env.example` เป็น `.env` และกำหนด `DATABASE_URL` กับ `SECRET_KEY`
3. ติดตั้งแพ็กเกจ: `pip install -r requirements.txt`
4. ตั้ง environment variables ตาม `.env`
5. รัน: `python app.py`
6. เปิด `http://127.0.0.1:5000`

## Deploy Vercel
- Push โปรเจกต์ขึ้น GitHub
- Import repository เข้า Vercel
- ตั้ง `DATABASE_URL` และ `SECRET_KEY` ใน Vercel Environment Variables
- Deploy

### หมายเหตุเรื่องฐานข้อมูล
โปรเจกต์นี้ใช้ PostgreSQL เพราะ Vercel ไม่ควรใช้ SQLite เป็นฐานข้อมูลถาวรของ production/serverless deployment

## Mapping กับเกณฑ์ Project
1. Login + Role → `login`, `register`, `role_required`
2. CRUD + validation → `/admin/artworks/*`, `validate_artwork`
3. Search/filter/sort/pagination → หน้า Marketplace `/`
4. Dashboard → `/dashboard`
5. Logs → `/admin/logs` + `log_action()`
6. Vercel → `api/index.py`, `vercel.json`

## ฟีเจอร์จากรายละเอียด Art Marketplace ในดราฟที่ทำเป็นตัวอย่าง
- Preview ผลงาน + `tags`
- รองรับช่อง Digital File URL สำหรับไฟล์ความละเอียดสูง (ตัวอย่างยังไม่เปิดดาวน์โหลดก่อนจ่ายเงินจริง)
- Like ผลงาน
- Follow ศิลปิน
- Review / Rating
- Commission Request
- จุดต่อยอดสำหรับ commission และรายงานยอดขาย/ส่วนแบ่งศิลปิน
- ขั้นตอนอนุมัติผลงานก่อนเผยแพร่

## หมายเหตุสำคัญ
QR ในหน้า Payment เป็น placeholder เพื่อสาธิต flow เท่านั้น ไม่ใช่การเชื่อมต่อธนาคารจริง และยังไม่มีการตรวจสลิปอัตโนมัติ
