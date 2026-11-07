# Grade Watch — แจ้งเตือนสถานะการส่งเกรด ED17* ทาง Gmail

ตรวจหน้า [ค้นหาสถานะรายวิชา มข.](https://reg-mirror.kku.ac.th/registrar/class_status.asp?backto=home)
สำหรับทุกรายวิชาที่รหัสขึ้นต้นด้วย `ED17` ถ้า **สถานะเกรด** ของวิชา/ภาค/กลุ่มไหนเปลี่ยน จะส่งอีเมลแจ้งทาง Gmail
โดยรันอัตโนมัติบน GitHub Actions

ขั้นตอนสถานะเกรดตามที่เว็บแสดง: 1. ยังไม่มีนักศึกษาลงทะเบียน → 2. รออาจารย์ส่งเกรด → 3. รอคณะส่งเกรด → 4. ส่งเกรดสมบูรณ์

## การทำงาน
1. ค้นหา `ED17*` (250 แถวต่อหน้า) ถ้าผลเกินหน้าเดียว จะแตกคำค้นเป็น `ED170*` … `ED179*` เอง
   (ไม่ใช้ปุ่ม "หน้าต่อไป" ของเว็บ เพราะทดสอบแล้วว่ามีแถวหายเวลาวิชาคร่อมระหว่างหน้า)
2. เทียบกับ `state/grade_status.json` (ผลครั้งก่อน) โดยแยกแต่ละแถวด้วย วิชา + ปีการศึกษา + ภาค + กลุ่ม
3. ถ้ามีสถานะเปลี่ยน (หรือมีแถวใหม่) → ส่งอีเมลเป็นตาราง สถานะเดิม → สถานะใหม่ พร้อมลิงก์รายวิชา
4. Commit state ใหม่กลับเข้า repo (ดูประวัติการเปลี่ยนสถานะย้อนหลังได้จาก git history)

ครั้งแรกที่รันจะส่งอีเมล "เริ่มติดตามแล้ว" พร้อมรายการที่ยังส่งเกรดไม่สมบูรณ์ เพื่อยืนยันว่าตั้งค่าเมลถูก
ถ้าเว็บล่มหรือดึงข้อมูลได้ผิดปกติ จะไม่อัปเดต state และ workflow จะแสดงเป็น failed (GitHub ส่งเมลแจ้งเจ้าของ repo เอง)

## ติดตั้งบน GitHub
1. **สร้าง Gmail App Password**: เปิด 2-Step Verification ที่ Google Account →
   ไปที่ <https://myaccount.google.com/apppasswords> → สร้างรหัส 16 หลัก
2. **สร้าง repo** (แนะนำให้เป็น private) แล้ว push ไฟล์ทั้งหมดในโฟลเดอร์นี้ขึ้นไป
3. **Settings → Secrets and variables → Actions → New repository secret** เพิ่ม:
   | Secret | ค่า |
   |---|---|
   | `GMAIL_USER` | อีเมล Gmail ที่ใช้ส่ง |
   | `GMAIL_APP_PASSWORD` | App Password 16 หลัก |
   | `MAIL_TO` | ผู้รับ (หลายคนคั่นด้วย `,`) — ไม่ใส่ก็ได้ จะส่งให้ `GMAIL_USER` |
4. (ไม่บังคับ) แท็บ **Variables**: `COURSE_PREFIX` (ค่าเริ่มต้น `ED17`), `NOTIFY_NEW` = `false` ถ้าไม่อยากได้เมลเมื่อมีกลุ่ม/ภาคใหม่โผล่
5. **Settings → Actions → General → Workflow permissions** ให้เป็น *Read and write permissions*
6. แท็บ **Actions → Grade Watch → Run workflow** เพื่อรันครั้งแรก

## ปรับเวลาการตรวจ
แก้บรรทัด `cron` ใน [.github/workflows/grade-watch.yml](.github/workflows/grade-watch.yml) (เวลาเป็น **UTC** = เวลาไทย − 7 ชม.)

| ต้องการ | cron |
|---|---|
| ทุก 15 นาที | `*/15 * * * *` |
| ทุก 30 นาที (ค่าเริ่มต้น) | `*/30 * * * *` |
| ทุกชั่วโมง | `0 * * * *` |
| ทุกชั่วโมง 08:00–20:00 เวลาไทย | `0 1-13 * * *` |
| วันละครั้ง 09:00 เวลาไทย | `0 2 * * *` |

หมายเหตุ: GitHub รันถี่สุดทุก 5 นาที และอาจดีเลย์ได้ช่วงคนใช้เยอะ, workflow มี step heartbeat ที่ commit เดือนละครั้ง
เพื่อไม่ให้ GitHub ปิด schedule อัตโนมัติเมื่อ repo ไม่มี activity 60 วัน

## รันในเครื่อง
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests            # unit tests
.venv/bin/python check_grades.py --dry-run  # ดึงข้อมูลจริง แสดงผล ไม่ส่งเมล ไม่บันทึก state
GMAIL_USER=... GMAIL_APP_PASSWORD=... MAIL_TO=... .venv/bin/python check_grades.py
```
ตัวเลือก: `--prefix ED17` (รหัสวิชาขึ้นต้น), `--state path/to/state.json`
