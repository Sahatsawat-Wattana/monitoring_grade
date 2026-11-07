import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import check_grades as cg  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
STATUSES = {"ส่งเกรดสมบูรณ์", "รออาจารย์ส่งเกรด", "รอคณะส่งเกรด", "ยังไม่มีนักศึกษาลงทะเบียน"}


def load(name):
    return (FIX / name).read_bytes().decode(cg.ENCODING)


def test_parse_full_page():
    rows = cg.parse_rows(load("sample_page.html"), "ED17")
    assert len(rows) == 208
    # ไอคอนสีฟ้าในหน้าเป็นแค่คำอธิบาย (legend) ต้องไม่ถูกนับเป็นแถว
    assert {r["status"] for r in rows} == STATUSES - {"ยังไม่มีนักศึกษาลงทะเบียน"}
    assert all(r["code"].startswith("ED17") for r in rows)
    first = rows[0]
    assert first["code"] == "ED171001"
    assert first["name"] == "ADVANCED PERCEPTION SKILLS"
    assert first["credit"] == "3 (3-0-6)"
    assert (first["acadyear"], first["semester"], first["section"]) == ("2560", "2", "1")
    assert (first["capacity"], first["enrolled"], first["remaining"]) == ("30", "27", "3")
    assert "avs" not in first["url"]


def test_keys_unique():
    rows = cg.parse_rows(load("sample_page.html"), "ED17")
    assert len({cg.row_key(r) for r in rows}) == len(rows)


def test_next_page_link():
    page = load("paged_page.html")
    assert len(cg.parse_rows(page, "ED17")) == 50
    url = cg.next_page_url(page, cg.FALLBACK_ACTION)
    assert url and "page=" in url and url.startswith(cg.BASE)
    # หน้าที่ผลลัพธ์ครบในหน้าเดียวไม่มีลิงก์หน้าถัดไป
    assert cg.next_page_url(load("sample_page.html"), cg.FALLBACK_ACTION) is None


def test_fetch_all_splits_truncated_search(monkeypatch):
    full, paged = load("sample_page.html"), load("paged_page.html")
    calls = []

    class FakeResp:
        def __init__(self, text):
            self.content = text.encode(cg.ENCODING)

        def raise_for_status(self):
            pass

    class FakeSession:
        def get(self, url, timeout):
            return FakeResp("<form action='class_info_status.asp?avs1=2&backto=home'></form>")

        def post(self, url, data, timeout):
            calls.append(data["coursecode"])
            # ED17* ถูกตัดหน้า -> ต้องแตกเป็น ED170*..ED179*; ให้ ED171* คืนผลเต็ม ที่เหลือว่าง
            if data["coursecode"] == "ED17*":
                return FakeResp(paged)
            return FakeResp(full if data["coursecode"] == "ED171*" else "<html></html>")

    pages = cg.fetch_all("ED17", session=FakeSession())
    assert calls == ["ED17*"] + [f"ED17{d}*" for d in range(10)]
    assert len(cg.collect(pages, "ED17")) == 208


def _rows():
    return cg.collect([load("sample_page.html")], "ED17")


def test_diff_no_change():
    rows = _rows()
    assert cg.diff(rows, rows) == ([], [])


def test_diff_status_change_and_new_row():
    new = _rows()
    old = json.loads(json.dumps(new))
    key_changed = next(k for k, r in new.items() if r["status"] == "รออาจารย์ส่งเกรด")
    old[key_changed]["status"] = "ยังไม่มีนักศึกษาลงทะเบียน"
    key_new = next(iter(new))
    del old[key_new]

    changed, added = cg.diff(old, new)
    assert [cg.row_key(r) for r in changed] == [key_changed]
    assert changed[0]["old_status"] == "ยังไม่มีนักศึกษาลงทะเบียน"
    assert changed[0]["status"] == "รออาจารย์ส่งเกรด"
    assert [cg.row_key(r) for r in added] == [key_new]

    subject, text, body = cg.build_change_mail("ED17", changed, added)
    assert "สถานะเกรดเปลี่ยน 1 รายการ" in subject and "รายการใหม่ 1 รายการ" in subject
    assert changed[0]["code"] in text and changed[0]["code"] in body


def test_main_flow(tmp_path, monkeypatch):
    pages = [load("sample_page.html")]
    monkeypatch.setattr(cg, "fetch_all", lambda prefix: pages)
    sent = []
    monkeypatch.setattr(cg, "send_mail", lambda s, t, h: sent.append(s))
    state = tmp_path / "state.json"

    # รอบแรก: baseline + เมลยืนยัน
    assert cg.main(["--state", str(state)]) == 0
    assert state.exists() and len(sent) == 1 and "เริ่มติดตาม" in sent[0]

    # รอบสอง: ไม่มีอะไรเปลี่ยน -> ไม่ส่งเมล
    assert cg.main(["--state", str(state)]) == 0
    assert len(sent) == 1

    # จำลองสถานะเปลี่ยน
    data = json.loads(state.read_text(encoding="utf-8"))
    k = next(k for k, r in data["rows"].items() if r["status"] == "รอคณะส่งเกรด")
    data["rows"][k]["status"] = "รออาจารย์ส่งเกรด"
    state.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert cg.main(["--state", str(state)]) == 0
    assert len(sent) == 2 and "สถานะเกรดเปลี่ยน 1 รายการ" in sent[1]
    assert json.loads(state.read_text(encoding="utf-8"))["rows"][k]["status"] == "รอคณะส่งเกรด"


def test_mail_failure_keeps_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, "fetch_all", lambda prefix: [load("sample_page.html")])

    def boom(*a):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(cg, "send_mail", boom)
    state = tmp_path / "state.json"
    try:
        cg.main(["--state", str(state)])
    except RuntimeError:
        pass
    assert not state.exists()
