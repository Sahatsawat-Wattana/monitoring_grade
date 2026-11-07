#!/usr/bin/env python3
"""ตรวจสถานะการส่งเกรดของรายวิชา (เช่น ED17*) บน reg-mirror.kku.ac.th
แล้วแจ้งเตือนทาง Gmail เมื่อสถานะเปลี่ยน

Environment variables:
  GMAIL_USER          บัญชี Gmail ที่ใช้ส่ง
  GMAIL_APP_PASSWORD  App Password 16 หลักของบัญชีนั้น
  MAIL_TO             ผู้รับ (คั่นหลายคนด้วย comma) ถ้าไม่ใส่จะส่งให้ GMAIL_USER
  COURSE_PREFIX       รหัสวิชาขึ้นต้น (ค่าเริ่มต้น ED17)
  NOTIFY_NEW          แจ้งเตือนรายการ (กลุ่ม/ภาค) ใหม่ที่เพิ่งปรากฏด้วยหรือไม่ (true/false, ค่าเริ่มต้น true)
  STATE_FILE          ที่เก็บสถานะล่าสุด (ค่าเริ่มต้น state/grade_status.json)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://reg-mirror.kku.ac.th/registrar/"
SEARCH_PAGE = BASE + "class_status.asp?backto=home"
FALLBACK_ACTION = BASE + "class_info_status.asp?avs909765178=2&backto=home"
ENCODING = "cp874"  # TIS-620 superset
MAX_ROWS = 250  # ค่าสูงสุดที่ฟอร์มค้นหาให้เลือก
MAX_PREFIX_LEN = 8  # รหัสวิชา KKU ยาว 8 ตัว
SPLIT_CHARS = "0123456789"
TH_TZ = timezone(timedelta(hours=7))


class ScrapeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- fetch
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = "Mozilla/5.0 (grade-status-watcher)"
    return s


def decode(resp: requests.Response) -> str:
    resp.raise_for_status()
    return resp.content.decode(ENCODING, errors="replace")


def find_search_action(page_html: str) -> str:
    """หา URL ของฟอร์มค้นหาจากหน้า class_status.asp (พารามิเตอร์ avs... อาจเปลี่ยนได้)"""
    soup = BeautifulSoup(page_html, "html.parser")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if "class_info_status.asp" in action:
            return urljoin(SEARCH_PAGE, action)
    return FALLBACK_ACTION


def next_page_url(page_html: str, current_url: str) -> str | None:
    soup = BeautifulSoup(page_html, "html.parser")
    for a in soup.find_all("a", href=True):
        if "หน้าต่อไป" in a.get_text():
            return urljoin(current_url, a["href"])
    return None


def search(s: requests.Session, action: str, prefix: str, timeout: int = 60) -> str:
    form = {
        "maxrow": str(MAX_ROWS),
        "Acadyear": "",
        "semester": "",
        "coursecode": f"{prefix}*",
        "coursename": "",
        "cmd": "2",
    }
    return decode(s.post(action, data=form, timeout=timeout))


def is_truncated(page_html: str, prefix: str) -> bool:
    return next_page_url(page_html, SEARCH_PAGE) is not None or len(parse_rows(page_html, prefix)) >= MAX_ROWS


def fetch_all(prefix: str, session: requests.Session | None = None, timeout: int = 60) -> list[str]:
    """คืน HTML ของผลค้นหาที่ครอบคลุมทุกแถว

    ระบบแบ่งหน้าของเว็บใช้รหัสวิชาเป็นตัวชี้ตำแหน่ง ทำให้วิชาที่คร่อมระหว่างหน้าหายไปบางแถว
    จึงไม่ใช้ลิงก์ "หน้าต่อไป" แต่ถ้าผลเกิน MAX_ROWS จะแตกคำค้นเป็นรหัสที่ยาวขึ้น
    (ED17* -> ED170* .. ED179*) ไปเรื่อย ๆ จนแต่ละคำค้นอยู่ในหน้าเดียว
    """
    s = session or make_session()
    action = find_search_action(decode(s.get(SEARCH_PAGE, timeout=timeout)))
    pages: list[str] = []
    todo = [prefix]
    while todo:
        p = todo.pop(0)
        page = search(s, action, p, timeout)
        if is_truncated(page, prefix):
            if len(p) >= MAX_PREFIX_LEN:
                raise ScrapeError(f"ผลค้นหา {p}* ยังเกิน {MAX_ROWS} แถว แตกคำค้นต่อไม่ได้")
            todo.extend(p + ch for ch in SPLIT_CHARS)
            continue
        pages.append(page)
    return pages


# --------------------------------------------------------------------------- parse
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def parse_rows(page_html: str, prefix: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    rows = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        code = _clean(a.get_text())
        if "class_info_2.asp" not in href or not code.upper().startswith(prefix.upper()):
            continue
        tr = a.find_parent("tr")
        if tr is None:
            continue
        cells = tr.find_all("td")
        # [0]=เว้นว่าง [1]=รหัสวิชา [2]=ชื่อ [3]=หน่วยกิต [4]=กลุ่ม [5]=รับ [6]=ลง [7]=เหลือ [8]=สถานะ
        idx = next(i for i, td in enumerate(cells) if a in td.descendants)
        c = cells[idx:]
        if len(c) < 8:
            continue
        img = c[7].find("img")
        status = _clean(img.get("title", "")) if img else _clean(c[7].get_text())
        q = {k: v[0] for k, v in parse_qs(urlparse(href).query).items()}
        row = {
            "code": code,
            "name": _clean(c[1].get_text()),
            "credit": _clean(c[2].get_text()),
            "section": _clean(c[3].get_text()),
            "capacity": _clean(c[4].get_text()),
            "enrolled": _clean(c[5].get_text()),
            "remaining": _clean(c[6].get_text()),
            "status": status or "(ไม่ทราบสถานะ)",
            "courseid": q.get("courseid", ""),
            "acadyear": q.get("acadyear", ""),
            "semester": q.get("semester", ""),
        }
        # ตัดพารามิเตอร์ avs... (ตัวนับของ session) ออกจากลิงก์ให้ใช้ซ้ำได้
        row["url"] = (
            f"{BASE}class_info_2.asp?backto=home&option=0&courseid={row['courseid']}"
            f"&acadyear={row['acadyear']}&semester={row['semester']}"
        )
        rows.append(row)
    return rows


def row_key(r: dict) -> str:
    return f"{r['courseid'] or r['code']}|{r['acadyear']}|{r['semester']}|{r['section']}"


def collect(pages: list[str], prefix: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for p in pages:
        for r in parse_rows(p, prefix):
            result[row_key(r)] = r
    return result


# --------------------------------------------------------------------------- diff
def diff(old: dict[str, dict], new: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """คืน (รายการที่สถานะเปลี่ยน, รายการใหม่)"""
    changed, added = [], []
    for key, r in sorted(new.items(), key=lambda kv: sort_key(kv[1])):
        if key not in old:
            added.append(r)
        elif old[key].get("status") != r["status"]:
            changed.append({**r, "old_status": old[key].get("status", "")})
    return changed, added


def sort_key(r: dict):
    return (r["acadyear"], r["semester"], r["code"], r["section"].zfill(4))


# --------------------------------------------------------------------------- state
def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, prefix: str, rows: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"prefix": prefix, "count": len(rows), "rows": dict(sorted(rows.items()))}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- mail
STATUS_COLOR = {
    "ส่งเกรดสมบูรณ์": "#1a7f37",
    "รอคณะส่งเกรด": "#b08800",
    "รออาจารย์ส่งเกรด": "#cf222e",
    "ยังไม่มีนักศึกษาลงทะเบียน": "#0969da",
}


def _badge(status: str) -> str:
    color = STATUS_COLOR.get(status, "#57606a")
    return f'<span style="color:{color};font-weight:bold">{html.escape(status)}</span>'


def _table(rows: list[dict], with_old: bool) -> str:
    head = ["รหัสวิชา", "ชื่อวิชา", "ปี/ภาค", "กลุ่ม", "ลง/รับ"]
    head += ["สถานะเดิม", "สถานะใหม่"] if with_old else ["สถานะ"]
    th = "".join(f'<th style="border:1px solid #d0d7de;padding:6px;background:#f6f8fa">{h}</th>' for h in head)
    body = []
    for r in rows:
        cells = [
            f'<a href="{html.escape(r["url"])}">{html.escape(r["code"])}</a>',
            html.escape(r["name"]),
            f'{html.escape(r["acadyear"])}/{html.escape(r["semester"])}',
            html.escape(r["section"]),
            f'{html.escape(r["enrolled"])}/{html.escape(r["capacity"])}',
        ]
        cells += [_badge(r["old_status"]), _badge(r["status"])] if with_old else [_badge(r["status"])]
        body.append("<tr>" + "".join(f'<td style="border:1px solid #d0d7de;padding:6px">{c}</td>' for c in cells) + "</tr>")
    return f'<table style="border-collapse:collapse;font-size:14px"><tr>{th}</tr>{"".join(body)}</table>'


def _text_lines(rows: list[dict], with_old: bool) -> list[str]:
    out = []
    for r in rows:
        st = f'{r["old_status"]} -> {r["status"]}' if with_old else r["status"]
        out.append(f'- {r["code"]} {r["name"]} ({r["acadyear"]}/{r["semester"]} กลุ่ม {r["section"]}): {st}\n  {r["url"]}')
    return out


def build_change_mail(prefix: str, changed: list[dict], added: list[dict]) -> tuple[str, str, str]:
    parts = []
    if changed:
        parts.append(f"สถานะเกรดเปลี่ยน {len(changed)} รายการ")
    if added:
        parts.append(f"รายการใหม่ {len(added)} รายการ")
    subject = f"[{prefix}] " + ", ".join(parts)
    now = datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M")
    h = [f"<p>ตรวจเมื่อ {now} (เวลาไทย) — รายวิชา <b>{html.escape(prefix)}*</b></p>"]
    t = [f"ตรวจเมื่อ {now} (เวลาไทย) — รายวิชา {prefix}*", ""]
    if changed:
        h += [f"<h3>สถานะเกรดเปลี่ยน ({len(changed)})</h3>", _table(changed, True)]
        t += [f"สถานะเกรดเปลี่ยน ({len(changed)})"] + _text_lines(changed, True) + [""]
    if added:
        h += [f"<h3>รายการใหม่ ({len(added)})</h3>", _table(added, False)]
        t += [f"รายการใหม่ ({len(added)})"] + _text_lines(added, False) + [""]
    h.append(f'<p style="color:#57606a;font-size:12px">ที่มา: <a href="{SEARCH_PAGE}">{SEARCH_PAGE}</a></p>')
    return subject, "\n".join(t), "".join(h)


def build_baseline_mail(prefix: str, rows: dict[str, dict]) -> tuple[str, str, str]:
    counts = Counter(r["status"] for r in rows.values())
    pending = sorted((r for r in rows.values() if r["status"] != "ส่งเกรดสมบูรณ์"), key=sort_key)
    subject = f"[{prefix}] เริ่มติดตามสถานะเกรดแล้ว ({len(rows)} รายการ)"
    summary = "".join(f"<li>{_badge(k)}: {v}</li>" for k, v in counts.most_common())
    h = (
        f"<p>ระบบเริ่มติดตามรายวิชา <b>{html.escape(prefix)}*</b> ทั้งหมด {len(rows)} รายการ "
        f"จะส่งเมลเมื่อสถานะเปลี่ยน</p><ul>{summary}</ul>"
    )
    if pending:
        h += f"<h3>รายการที่ยังส่งเกรดไม่สมบูรณ์ ({len(pending)})</h3>" + _table(pending, False)
    t = "\n".join(
        [f"ระบบเริ่มติดตามรายวิชา {prefix}* ทั้งหมด {len(rows)} รายการ", ""]
        + [f"- {k}: {v}" for k, v in counts.most_common()]
        + ([""] + _text_lines(pending, False) if pending else [])
    )
    return subject, t, h


def send_mail(subject: str, text: str, html_body: str) -> None:
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = [x.strip() for x in (os.environ.get("MAIL_TO") or user).split(",") if x.strip()]
    if not user or not password or not to:
        raise RuntimeError("ยังไม่ได้ตั้งค่า GMAIL_USER / GMAIL_APP_PASSWORD / MAIL_TO")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, to, msg.as_string())


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", default=os.environ.get("COURSE_PREFIX") or "ED17")
    ap.add_argument("--state", default=os.environ.get("STATE_FILE") or "state/grade_status.json")
    ap.add_argument("--dry-run", action="store_true", help="แสดงผลอย่างเดียว ไม่ส่งเมลและไม่บันทึก state")
    args = ap.parse_args(argv)

    prefix = args.prefix.strip().rstrip("*")
    notify_new = os.environ.get("NOTIFY_NEW", "true").strip().lower() not in ("0", "false", "no", "off")
    state_path = Path(args.state)

    pages = fetch_all(prefix)
    current = collect(pages, prefix)
    print(f"fetched {len(pages)} page(s), {len(current)} row(s) for {prefix}*")
    if not current:
        raise ScrapeError("ไม่พบรายวิชาเลย — หน้าเว็บอาจเปลี่ยนรูปแบบหรือล่ม จึงไม่อัปเดต state")

    state = load_state(state_path)
    if state is not None and state.get("prefix") != prefix:
        print(f"prefix changed ({state.get('prefix')} -> {prefix}); starting a new baseline")
        state = None

    if state is None:
        subject, text, body = build_baseline_mail(prefix, current)
        merged = current
    else:
        old = state.get("rows", {})
        if len(current) < len(old) * 0.5:
            raise ScrapeError(f"ได้ข้อมูล {len(current)} แถว น้อยกว่าครั้งก่อน ({len(old)}) มากผิดปกติ — ข้ามรอบนี้")
        changed, added = diff(old, current)
        print(f"status changed: {len(changed)}, new rows: {len(added)}")
        for r in changed:
            print(f"  * {r['code']} {r['acadyear']}/{r['semester']} sec {r['section']}: {r['old_status']} -> {r['status']}")
        for r in added:
            print(f"  + {r['code']} {r['acadyear']}/{r['semester']} sec {r['section']}: {r['status']}")
        merged = {**old, **current}  # เก็บแถวที่หายไปชั่วคราวไว้ด้วย จะได้ไม่ถูกนับเป็น "ใหม่" ซ้ำ
        notify_added = added if notify_new else []
        if changed or notify_added:
            subject, text, body = build_change_mail(prefix, changed, notify_added)
        else:
            subject = None

    if subject:
        if args.dry_run:
            print(f"[dry-run] would send mail: {subject}")
        else:
            send_mail(subject, text, body)
            print(f"mail sent: {subject}")

    # บันทึกหลังส่งเมลสำเร็จ: ถ้าส่งไม่ผ่าน รอบถัดไปจะลองแจ้งใหม่
    if args.dry_run:
        print("[dry-run] state not saved")
    elif state is None or merged != state.get("rows"):
        save_state(state_path, prefix, merged)
        print(f"state saved -> {state_path}")
    else:
        print("no change")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ScrapeError, requests.RequestException, RuntimeError, smtplib.SMTPException) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
