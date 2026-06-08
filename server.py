"""
배달의민족 리뷰 게시중단 자동화 서버
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FastAPI 기반 백엔드 서버
- Google Sheets에서 데이터 읽기/쓰기
- Playwright로 챗봇 자동 접수
- WebSocket으로 실시간 진행상황 전송

실행: uvicorn server:app --reload --port 8001
"""

import asyncio
import json
import os
import sys
import random
import re
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Google Sheets ──
import gspread
from google.oauth2.service_account import Credentials

# ── Playwright ──
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

# ── User-Agent 풀 (랜덤 로테이션) ──
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

app = FastAPI(title="배달의민족 리뷰 게시중단 자동화")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# 전역 상태
# ============================================================
automation_state = {
    "is_running": False,
    "current_item": 0,
    "total_items": 0,
    "success": 0,
    "fail": 0,
    "skip": 0,
    "logs": [],
    "should_stop": False,
}

connected_clients: list[WebSocket] = []

SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)


# ============================================================
# Google Sheets 연동
# ============================================================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# 서비스 계정 키 파일 경로 (환경변수 또는 기본 경로)
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT", "service_account.json")
# Railway 배포 시: 환경변수 GOOGLE_CREDENTIALS_JSON 에 JSON 전체 내용을 붙여넣기
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")


def get_gspread_client():
    """Google Sheets 클라이언트 생성"""
    if GOOGLE_CREDENTIALS_JSON:
        import json as _json
        creds = Credentials.from_service_account_info(
            _json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES
        )
    elif Path(SERVICE_ACCOUNT_FILE).exists():
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    else:
        raise HTTPException(
            status_code=500,
            detail="Google 서비스 계정 인증 정보가 없습니다.\n"
                   "Railway 환경변수 GOOGLE_CREDENTIALS_JSON 을 설정하거나 "
                   "service_account.json 파일을 추가하세요.",
        )
    return gspread.authorize(creds)


def clean_id(s):
    """순수 식별자(가게번호 등) 정규화 — 공백/전각공백/제로폭/BOM 전부 제거"""
    return re.sub(r'[\s　​‌‍﻿]', '', str(s))


def get_sheet_data(spreadsheet_url: str):
    """Google Sheet에서 접수 데이터 읽기"""
    gc = get_gspread_client()
    sh = gc.open_by_url(spreadsheet_url)

    # 접수데이터 시트
    try:
        ws = sh.worksheet("접수데이터")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.sheet1  # 첫 번째 시트 사용

    records = ws.get_all_values()
    if len(records) < 4:
        return [], {}

    # 헤더는 3행 (인덱스 2) — 컬럼 매핑이 예상과 어긋나면 경고 플래그만 (진행은 막지 않음)
    header = records[2] if len(records) > 2 else []
    def _hdr_has(idx, kw):
        return len(header) > idx and kw in str(header[idx])
    header_ok = _hdr_has(2, "가게") and _hdr_has(3, "리뷰") and _hdr_has(5, "이메일")

    items = []
    for i, row in enumerate(records[3:], start=4):  # 4행부터 데이터
        # C열(가게번호)이 비었거나 행이 짧으면 데이터 끝으로 간주
        if len(row) <= 2 or not row[2]:
            break
        # 뒤쪽 셀이 빈 짧은 행도 IndexError 없이 안전하게 (있으면 값, 없으면 빈 문자열)
        items.append({
            "row": i,
            "no": (row[0] if len(row) > 0 and row[0] else str(i - 3)),
            "company_name": str(row[1]).strip() if len(row) > 1 else "",
            "shop_number": clean_id(row[2]),
            "review_numbers": re.sub(r'^[\s　​‌‍﻿]+|[\s　​‌‍﻿]+$', '', str(row[3])) if len(row) > 3 else "",
            "applicant_type": str(row[4]).strip() if len(row) > 4 else "",
            "email": str(row[5]).strip() if len(row) > 5 else "",
            "status": str(row[6]).strip() if len(row) > 6 else "",
            "timestamp": str(row[7]).strip() if len(row) > 7 else "",
        })

    # 설정 시트
    config = {}
    try:
        ws_config = sh.worksheet("설정")
        config_data = ws_config.get_all_values()
        for row in config_data[3:]:  # 4행부터
            if row[0] and row[1]:
                config[row[0].strip()] = row[1].strip()
    except Exception:
        pass

    config["_header_ok"] = header_ok
    return items, config


# 결과 기록용 워크시트 캐시 (URL당 1회만 open → 매 건 open_by_url/worksheet 호출에 의한 읽기 429 차단)
_worksheet_cache = {}


def _get_result_worksheet(spreadsheet_url: str):
    """결과 기록용 워크시트를 캐싱해서 반환 (없으면 1회만 열어서 저장)"""
    ws = _worksheet_cache.get(spreadsheet_url)
    if ws is None:
        gc = get_gspread_client()
        sh = gc.open_by_url(spreadsheet_url)
        try:
            ws = sh.worksheet("접수데이터")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.sheet1
        _worksheet_cache[spreadsheet_url] = ws
    return ws


def update_sheet_result(spreadsheet_url: str, row: int, status: str, timestamp: str):
    """Google Sheet에 결과 기록 (G열=처리결과, H열=처리시간)"""  
    ws = _get_result_worksheet(spreadsheet_url)
    # update_cell 2회 대신 G:H를 한 번에 기록 (읽기·쓰기 요청 모두 절감)
    ws.update(range_name=f"G{row}:H{row}", values=[[status, timestamp]])


# ============================================================
# 챗봇 자동화 엔진 (비동기 버전)
# ============================================================
async def broadcast(event: str, data: dict):
    """연결된 모든 WebSocket 클라이언트에 메시지 전송"""
    msg = json.dumps({"event": event, **data}, ensure_ascii=False)
    disconnected = []
    for ws in connected_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        connected_clients.remove(ws)


async def add_log(message: str, level: str = "info"):
    """로그 추가 + 브로드캐스트"""
    log_entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "message": message,
        "level": level,
    }
    automation_state["logs"].append(log_entry)
    # 최근 200개만 유지
    if len(automation_state["logs"]) > 200:
        automation_state["logs"] = automation_state["logs"][-200:]
    await broadcast("log", log_entry)


async def _save_screenshot(page, item: dict, reason: str):
    """에러 발생 시 현재 페이지 스크린샷 저장"""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shop = item.get("shop_number", "unknown")
        filepath = SCREENSHOT_DIR / f"{ts}_{shop}_{reason}.png"
        await page.screenshot(path=str(filepath), full_page=True)
        await add_log(f"  📸 스크린샷 저장: {filepath.name}", "info")
    except Exception:
        pass


async def process_single_item(page, item: dict, config: dict) -> tuple:
    """단일 건 챗봇 접수 처리 (배달의민족)"""
    timeout = int(config.get("요소 탐지 타임아웃(초)", 15)) * 1000
    # 구글 시트에 단축 URL이 설정되어 있어도 강제로 해피톡 원본 URL 직접 접속
    # (단축 URL buly.kr 리다이렉트 2-5초 절약)
    chatbot_url = "https://design.happytalkio.com/chatting?siteId=4000000024&siteName=%EC%9A%B0%EC%95%84%ED%95%9C%ED%98%95%EC%A0%9C%EB%93%A4&categoryId=61602&divisionId=200880"
    default_applicant = config.get("기본 신청자구분", "대표자")

    async def click_btn(text, wait_after=2, btn_timeout=None, quiet=False):
        """버튼 클릭 헬퍼
        btn_timeout: 개별 타임아웃 ms (기본=설정값)
        quiet=True: 미탐지가 정상인 경우(예: 신규상담 폴백) — 실패 로그·스크린샷 생략
        """
        t = btn_timeout or timeout
        try:
            btn = page.locator(
                f"button:has-text('{text}'), "
                f"div[role='button']:has-text('{text}'), "
                f"a:has-text('{text}'), "
                f"span:has-text('{text}')"
            ).last
            await btn.wait_for(state="visible", timeout=t)
            await asyncio.sleep(random.uniform(0.3, 0.8))
            await btn.click()
            await asyncio.sleep(wait_after)
            return True
        except Exception as e:
            if not quiet:
                await add_log(f"  [디버그] '{text}' 버튼 탐지 실패: {str(e)[:60]}", "warning")
                await _save_screenshot(page, item, f"btn_{text[:10]}")
            return False

    async def type_msg(text, wait_after=2):
        try:
            input_sel = (
                "input[placeholder*='메시지'], "
                "textarea[placeholder*='메시지'], "
                "input[placeholder*='입력'], "
                "textarea[placeholder*='입력'], "
                "textarea[name='textarea'], "
                "div[contenteditable='true']"
            )
            el = page.locator(input_sel).first
            await el.wait_for(state="visible", timeout=timeout)
            await asyncio.sleep(0.5)

            try:
                await el.click(force=True, timeout=timeout)
                await el.fill(text, timeout=timeout)
            except Exception:
                # 값을 JS 코드에 직접 박지 않고 인자로 전달 (작은따옴표·역슬래시·개행 포함 입력도 안전)
                await el.evaluate(
                    "(node, val) => { node.value = val; node.dispatchEvent(new Event('input', { bubbles: true })); }",
                    text,
                )

            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")
            await asyncio.sleep(wait_after)
            return True
        except PlaywrightTimeout:
            return False
        except Exception as e:
            await add_log(f"  [오류] 메시지 입력 실패: {str(e)[:50]}", "warning")
            return False

    try:
        # Step 1: 챗봇 접속
        await add_log(f"  [1/12] 챗봇 접속 중...")
        await page.goto(chatbot_url, wait_until="domcontentloaded", timeout=45000)
        # 고정 sleep 대신 챗봇 UI가 렌더링될 때까지 동적 대기
        try:
            await page.wait_for_selector(
                "button, div[role='button']",
                state="visible", timeout=15000
            )
        except Exception:
            await asyncio.sleep(5)  # 셀렉터 실패 시 fallback 대기

        # Step 2: 신규상담 시작하기
        #  원본 URL 직접 접속 시 챗봇이 이미 대화 시작 상태로 열림 → 이 버튼은 보통 안 뜸.
        #  즉 "미탐지 = 정상"이므로 quiet=True 로 실패 로그·스크린샷을 남기지 않고,
        #  타임아웃도 짧게(부재 확인용) 잡아 불필요한 대기를 줄인다.
        await add_log(f"  [2/12] '신규상담 시작하기' 확인")
        found = await click_btn("신규상담 시작하기", btn_timeout=2500, quiet=True)
        if not found:
            found = await click_btn("신규상담", btn_timeout=1500, quiet=True)
        if not found:
            await add_log(f"  [2/12] 이미 대화 시작 상태 — 정상 진행")

        # Step 3: 리뷰게시중단/리뷰케어 신청
        await add_log(f"  [3/12] '리뷰게시중단/리뷰케어 신청' 선택")
        if not await click_btn("리뷰게시중단/리뷰케어 신청"):
            if not await click_btn("리뷰게시중단"):
                return False, "❌ '리뷰게시중단/리뷰케어 신청' 버튼 탐지 실패"

        # Step 4: 리뷰게시중단 신청
        await add_log(f"  [4/12] '리뷰게시중단 신청' 선택")
        if not await click_btn("리뷰게시중단 신청"):
            return False, "❌ '리뷰게시중단 신청' 버튼 탐지 실패"

        # Step 5: 시작하기
        await add_log(f"  [5/12] '시작하기' 선택")
        if not await click_btn("시작하기"):
            return False, "❌ '시작하기' 버튼 탐지 실패"

        # Step 6: 확인했어요.
        await add_log(f"  [6/12] '확인했어요.' 선택")
        if not await click_btn("확인했어요"):
            return False, "❌ '확인했어요.' 버튼 탐지 실패"

        # Step 7: 가게번호 입력
        await add_log(f"  [7/12] 가게번호 입력: {item['shop_number']}")
        if not await type_msg(item["shop_number"]):
            return False, "❌ 가게번호 입력 실패"

        # Step 8: 리뷰번호 전체 입력
        await add_log(f"  [8/12] 리뷰번호 입력: {item['review_numbers']}")
        if not await type_msg(item["review_numbers"]):
            return False, "❌ 리뷰번호 입력 실패"

        # Step 9: 대표자 또는 운영자 선택
        applicant = item.get("applicant_type") or default_applicant
        await add_log(f"  [9/12] 신청자구분 선택: '{applicant}'")
        if not await click_btn(applicant):
            return False, f"❌ '{applicant}' 버튼 탐지 실패"

        # Step 10: 전자신청서 발송 방법 선택 (이메일/문자메세지)
        #  실측: 발송방식 선택 후 챗봇은 이메일 주소를 받지 않고 곧장 [접수하기]로 이동.
        #  → '이메일 주소 입력' 단계 없음 (값을 넣으면 입력창에 끼어들어 흐름이 깨짐).
        await add_log(f"  [10/12] '이메일' 발송 방식 선택")
        if not await click_btn("이메일"):
            if not await click_btn("문자메세지"):
                return False, "❌ 발송 방식 버튼 탐지 실패"

        # Step 11: 접수하기
        await add_log(f"  [11/12] '접수하기' 클릭")
        if not await click_btn("접수하기"):
            return False, "❌ '접수하기' 버튼 탐지 실패"
        await asyncio.sleep(2)

        # Step 12: 완료/거부 확인
        #  ⭐ 원칙: 멀쩡한 접수는 절대 실패 처리하지 않는다.
        #  1) 완료 신호가 보이면 → 성공 (가장 확실, 거부 검사 건너뜀)
        #  2) 완료 신호 없는데 명백한 거부 신호만 보이면 → 실패 (가게번호 불일치 등)
        #  3) 둘 다 애매하면 → 기존처럼 성공 유지 (정상건 실패 방지가 최우선)
        await add_log(f"  [12/12] 접수 완료 확인...")
        await asyncio.sleep(2)

        page_text = ""
        try:
            page_text = await page.inner_text("body", timeout=5000)
        except Exception:
            pass

        # 1) 완료 신호 우선 (거부 키워드보다 먼저 — 오탐 방지)
        done_kw = ["접수가 완료", "접수되었습니다", "접수 완료", "신청이 완료",
                   "접수번호", "정상적으로 접수"]
        if any(k in page_text for k in done_kw):
            return True, "✅ 접수 완료 (완료 확인됨)"

        # 2) 완료 신호가 없을 때만, 명백한 거부 신호 확인
        #    (정상 안내문에는 안 나오는 표현만 보수적으로 선정)
        reject_kw = ["일치하지 않", "확인되지 않", "유효하지 않", "존재하지 않", "올바르지 않"]
        hit = next((k for k in reject_kw if k in page_text), None)
        if hit:
            await _save_screenshot(page, item, "rejected")
            return False, f"❌ 접수 거부 추정 ('{hit}' 응답 감지)"

        # 3) 완료·거부 신호 둘 다 불명확 → 성공 유지 (실패로 처리하지 않음)
        return True, "✅ 접수 완료"

    except PlaywrightTimeout:
        await _save_screenshot(page, item, "timeout")
        return False, "❌ 타임아웃"
    except Exception as e:
        await _save_screenshot(page, item, "error")
        return False, f"❌ 오류: {str(e)[:80]}"


# ============================================================
# API 엔드포인트
# ============================================================
class SheetRequest(BaseModel):
    spreadsheet_url: str


class RunRequest(BaseModel):
    spreadsheet_url: str
    start_row: int = 1
    end_row: int = 0  # 0 = 전체


@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


@app.get("/api/status")
async def get_status():
    return automation_state


@app.post("/api/sheets/connect")
async def connect_sheet(req: SheetRequest):
    """Google Sheet 연결 & 데이터 읽기"""
    try:
        items, config = get_sheet_data(req.spreadsheet_url)
        return {
            "success": True,
            "total_items": len(items),
            "items": items,
            "config": config,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/automation/start")
async def start_automation(req: RunRequest):
    """자동화 시작"""
    if automation_state["is_running"]:
        raise HTTPException(status_code=409, detail="이미 실행 중입니다")

    # 중복 실행 방지를 위해 바로 상태 변경
    automation_state["is_running"] = True

    # 백그라운드 태스크로 실행
    asyncio.create_task(run_automation(req.spreadsheet_url, req.start_row, req.end_row))
    return {"success": True, "message": "자동화가 시작되었습니다"}


@app.post("/api/automation/stop")
async def stop_automation():
    """자동화 중지"""
    automation_state["should_stop"] = True
    await add_log("⛔ 중지 요청됨. 현재 건 완료 후 중지합니다.", "warn")
    return {"success": True}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """실시간 상태 업데이트용 WebSocket"""
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        # 현재 상태 전송
        await websocket.send_text(json.dumps({
            "event": "state",
            **automation_state,
        }, ensure_ascii=False))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in connected_clients:
            connected_clients.remove(websocket)


# ============================================================
# 자동화 실행 루프
# ============================================================
async def run_automation(spreadsheet_url: str, start_row: int, end_row: int):
    """메인 자동화 루프"""
    global automation_state

    automation_state.update({
        "is_running": True,
        "should_stop": False,
        "success": 0,
        "fail": 0,
        "skip": 0,
        "logs": [],
    })
    _worksheet_cache.clear()  # 이전 실행의 캐시 워크시트 무효화 (시트 구조 변경 대비)

    await add_log("🚀 자동화를 시작합니다...")
    await broadcast("state", automation_state)

    try:
        # 데이터 로드
        items, config = await asyncio.to_thread(get_sheet_data, spreadsheet_url)
        if not items:
            await add_log("⚠️ 처리할 데이터가 없습니다.", "warn")
            return
        if not config.get("_header_ok", True):
            await add_log("⚠️ 시트 헤더가 예상과 달라 컬럼 매핑(C=가게번호/D=리뷰번호/F=이메일)이 "
                          "어긋났을 수 있습니다. 시트 상단 행·컬럼을 임의로 추가했는지 확인하세요. "
                          "(진행은 계속합니다)", "warn")

        # 범위 필터
        start_idx = start_row - 1
        end_idx = end_row if end_row > 0 else len(items)
        items = items[start_idx:end_idx]

        automation_state["total_items"] = len(items)
        delay = int(config.get("건당 대기시간(초)", 5))
        max_retry = int(config.get("최대 재시도 횟수", 3))
        batch_size = int(config.get("배치 크기(건)", 20))
        batch_break = int(config.get("배치 휴식(초)", 120))
        headless_val = str(config.get("브라우저 표시", "FALSE")).upper()
        headless = headless_val not in ("TRUE", "1", "예", "Y")

        # GUI(화면)가 없는 리눅스/서버 환경에서는 강제로 headless=True 적용
        if os.name == "posix" and "DISPLAY" not in os.environ and sys.platform != "darwin":
            headless = True

        await add_log(f"📊 총 {len(items)}건 처리 예정")
        await broadcast("state", automation_state)

        # Playwright 실행
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, slow_mo=100)
            consecutive_relaunch = 0  # 브라우저 연속 재기동 횟수 (폭주 방지용)

            for i, item in enumerate(items):
                if automation_state["should_stop"]:
                    await add_log("⛔ 사용자에 의해 중지되었습니다.", "warn")
                    break

                automation_state["current_item"] = i + 1
                await broadcast("progress", {
                    "current": i + 1,
                    "total": len(items),
                    "item": item,
                })

                await add_log(f"\n━━ [{i+1}/{len(items)}] Row {item['row']} ━━")
                await add_log(f"  업체명: {item.get('company_name', '')}, 가게번호: {item['shop_number']}, 리뷰번호: {item['review_numbers']}")

                # 데이터 검증
                missing = []
                if not item["shop_number"]: missing.append("가게번호")
                if not item["review_numbers"]: missing.append("리뷰번호")

                if missing:
                    msg = f"⏭ 스킵 (누락: {', '.join(missing)})"
                    await add_log(msg, "warn")
                    automation_state["skip"] += 1
                    try:
                        await asyncio.to_thread(
                            update_sheet_result, spreadsheet_url, item["row"], msg,
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    except Exception as e:
                        await add_log(f"  ⚠️ 시트 결과 기록 실패(스킵 건): {e}", "warn")
                    await broadcast("state", automation_state)
                    continue
                
                # 이 항목 처리 — 세션/브라우저 오류가 나도 '이 건만 실패'로 격리하고 다음 건 계속
                context = None
                page = None
                success = False
                result_msg = ""
                try:
                    # 매 항목마다 새 시크릿 세션 (챗봇 쿠키 초기화 목적)
                    ua = random.choice(USER_AGENTS)
                    context = await browser.new_context(
                        viewport={"width": random.randint(1200, 1400), "height": random.randint(750, 900)},
                        locale="ko-KR",
                        user_agent=ua
                    )
                    page = await context.new_page()
                    await Stealth().apply_stealth_async(page)

                    for attempt in range(1, max_retry + 1):
                        if attempt > 1:
                            await add_log(f"  🔁 재시도 {attempt}/{max_retry} (새 세션)...")
                            await asyncio.sleep(delay)
                            # 같은 쿠키로 재시도하면 챗봇이 '진행 중 상담'으로 인식 → context까지 새로 생성
                            try:
                                await page.close()
                                await context.close()
                            except Exception:
                                pass
                            ua = random.choice(USER_AGENTS)
                            context = await browser.new_context(
                                viewport={"width": random.randint(1200, 1400), "height": random.randint(750, 900)},
                                locale="ko-KR",
                                user_agent=ua
                            )
                            page = await context.new_page()
                            await Stealth().apply_stealth_async(page)

                        ok, msg = await process_single_item(page, item, config)
                        if ok:
                            success = True
                            result_msg = msg
                            break
                        else:
                            result_msg = msg
                            await add_log(f"  {msg}", "error")
                            # 데이터 자체 문제(틀린 값으로 거부됨)는 재시도해도 또 거부됨 → 즉시 중단
                            if any(kw in msg for kw in ["입력 실패", "불일치", "거부 추정"]):
                                await add_log(f"  ⚠️ 데이터 문제로 재시도 중단", "warn")
                                break

                    if success:
                        automation_state["success"] += 1
                        await add_log(f"  ✅ 접수 완료!", "success")
                        consecutive_relaunch = 0  # 정상 성공 시 재기동 카운터 리셋
                    else:
                        automation_state["fail"] += 1
                        await add_log(f"  ❌ 최종 실패: {result_msg}", "error")

                    # 시트에 결과 기록
                    try:
                        await asyncio.to_thread(
                            update_sheet_result,
                            spreadsheet_url, item["row"], result_msg,
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        )
                    except Exception as e:
                        await add_log(f"  ⚠️ 시트 결과 기록 실패: {e}", "warn")

                    await broadcast("state", automation_state)

                except Exception as e:
                    # 이 항목만 실패로 격리 — 전체 루프는 멈추지 않고 다음 건으로 진행
                    automation_state["fail"] += 1
                    emsg = str(e)[:120]
                    await add_log(f"  ❌ 브라우저 세션 오류 (이 건만 실패, 다음 진행): {emsg}", "error")
                    try:
                        await asyncio.to_thread(
                            update_sheet_result, spreadsheet_url, item["row"],
                            f"❌ 브라우저 세션 오류: {emsg[:60]}",
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    except Exception:
                        pass
                    await broadcast("state", automation_state)
                    # 브라우저 프로세스 자체가 죽었으면 1회 재기동 (연속 3회 초과 시 자동화 중단)
                    if not browser.is_connected():
                        consecutive_relaunch += 1
                        if consecutive_relaunch > 3:
                            await add_log("💥 브라우저 재기동 3회 초과 — 자동화를 중단합니다.", "error")
                            break
                        await add_log(f"  🔄 브라우저 재기동 ({consecutive_relaunch}/3)...", "warn")
                        try:
                            browser = await p.chromium.launch(headless=headless, slow_mo=100)
                        except Exception as relaunch_err:
                            await add_log(f"💥 브라우저 재기동 실패 — 중단: {str(relaunch_err)[:80]}", "error")
                            break
                
                # 시크릿 창 닫기 (page/context 독립 정리 — 한쪽 실패가 다른쪽 정리를 막지 않게)
                try:
                    if page is not None:
                        await page.close()
                except Exception:
                    pass
                try:
                    if context is not None:
                        await context.close()
                except Exception:
                    pass

                # 건 간 대기
                if i < len(items) - 1 and not automation_state["should_stop"]:
                    # 배치 휴식: N건마다 장시간 대기
                    processed = i + 1
                    if batch_size > 0 and processed % batch_size == 0:
                        rest = batch_break + random.uniform(0, 30)
                        await add_log(f"  ☕ {processed}건 완료 — {rest:.0f}초 배치 휴식 중...")
                        await asyncio.sleep(rest)
                    else:
                        wait = delay + random.uniform(0, delay)
                        await add_log(f"  ⏳ {wait:.1f}초 대기...")
                        await asyncio.sleep(wait)

            await browser.close()

    except Exception as e:
        await add_log(f"💥 치명적 오류: {str(e)}", "error")
    finally:
        automation_state["is_running"] = False
        await add_log("━" * 40)
        await add_log(f"📊 최종 결과: ✅ {automation_state['success']}건 성공, "
                      f"❌ {automation_state['fail']}건 실패, "
                      f"⏭ {automation_state['skip']}건 스킵")
        await broadcast("state", automation_state)
        await broadcast("complete", {
            "success": automation_state["success"],
            "fail": automation_state["fail"],
            "skip": automation_state["skip"],
        })


# ============================================================
# 정적 파일 서빙 (프론트엔드)
# ============================================================
if Path("static").exists():
    app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
