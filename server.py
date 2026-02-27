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

    # 헤더는 3행 (인덱스 2)
    items = []
    for i, row in enumerate(records[3:], start=4):  # 4행부터 데이터
        if not row[2]:  # C열(가게번호)이 비어있으면 중단
            break
        items.append({
            "row": i,
            "no": row[0] if row[0] else str(i - 3),
            "company_name": str(row[1]).strip(),
            "shop_number": str(row[2]).strip(),
            "review_numbers": str(row[3]).strip(),
            "applicant_type": str(row[4]).strip(),
            "email": str(row[5]).strip(),
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

    return items, config


def update_sheet_result(spreadsheet_url: str, row: int, status: str, timestamp: str):
    """Google Sheet에 결과 기록 (H열, I열)"""  
    gc = get_gspread_client()
    sh = gc.open_by_url(spreadsheet_url)
    try:
        ws = sh.worksheet("접수데이터")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.sheet1
    ws.update_cell(row, 7, status)     # G열
    ws.update_cell(row, 8, timestamp)  # H열


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


async def process_single_item(page, item: dict, config: dict) -> tuple:
    """단일 건 챗봇 접수 처리 (배달의민족)"""
    timeout = int(config.get("요소 탐지 타임아웃(초)", 10)) * 1000
    # URL 단축(buly.kr) 바이패스 — 해피톡 원본 URL 직접 접속
    chatbot_url = config.get("챗봇 URL", "https://buly.kr/GZz78WH")
    default_applicant = config.get("기본 신청자구분", "대표자")
    default_email = config.get("기본 이메일", "")

    async def click_btn(text, wait_after=2):
        """버튼 클릭 헬퍼"""
        try:
            btn = page.locator(
                f"button:has-text('{text}'), "
                f"div[role='button']:has-text('{text}'), "
                f"a:has-text('{text}'), "
                f"span:has-text('{text}')"
            ).last
            await btn.wait_for(state="visible", timeout=timeout)
            await asyncio.sleep(random.uniform(0.3, 0.8))
            await btn.click()
            await asyncio.sleep(wait_after)
            return True
        except Exception:
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
                await el.evaluate(f"node => {{ node.value = '{text}'; node.dispatchEvent(new Event('input', {{ bubbles: true }})); }}")

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
        await page.goto(chatbot_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Step 2: 신규상담 시작하기
        await add_log(f"  [2/12] '신규상담 시작하기' 선택")
        if not await click_btn("신규상담 시작하기"):
            if not await click_btn("신규상담"):
                return False, "❌ '신규상담 시작하기' 버튼 탐지 실패"

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

        # Step 10: 이메일 입력
        email = item.get("email") or default_email
        await add_log(f"  [10/12] 이메일 입력: {email}")
        if not await type_msg(email):
            return False, "❌ 이메일 입력 실패"

        # Step 11: 접수하기
        await add_log(f"  [11/12] '접수하기' 클릭")
        if not await click_btn("접수하기"):
            return False, "❌ '접수하기' 버튼 탐지 실패"
        await asyncio.sleep(2)

        # Step 12: 완료 확인
        await add_log(f"  [12/12] 접수 완료 확인...")
        await asyncio.sleep(2)

        return True, "✅ 접수 완료"

    except PlaywrightTimeout:
        return False, "❌ 타임아웃"
    except Exception as e:
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

    await add_log("🚀 자동화를 시작합니다...")
    await broadcast("state", automation_state)

    try:
        # 데이터 로드
        items, config = get_sheet_data(spreadsheet_url)
        if not items:
            await add_log("⚠️ 처리할 데이터가 없습니다.", "warn")
            return

        # 범위 필터
        start_idx = start_row - 1
        end_idx = end_row if end_row > 0 else len(items)
        items = items[start_idx:end_idx]

        automation_state["total_items"] = len(items)
        delay = int(config.get("건당 대기시간(초)", 3))
        max_retry = int(config.get("최대 재시도 횟수", 3))
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
            
            # 봇 탐지 회피를 위한 User-Agent 설정
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            
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
                    update_sheet_result(spreadsheet_url, item["row"], msg,
                                       datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    await broadcast("state", automation_state)
                    continue
                
                # 매 항목마다 새로운 브라우저 컨텍스트/시크릿 창 생성 (챗봇 세션, 쿠키 초기화 목적)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800}, 
                    locale="ko-KR",
                    user_agent=user_agent
                )
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)

                # 접수 시도
                success = False
                result_msg = ""

                for attempt in range(1, max_retry + 1):
                    if attempt > 1:
                        await add_log(f"  🔁 재시도 {attempt}/{max_retry}...")
                        await asyncio.sleep(delay)

                    ok, msg = await process_single_item(page, item, config)
                    if ok:
                        success = True
                        result_msg = msg
                        break
                    else:
                        result_msg = msg
                        await add_log(f"  {msg}", "error")
                        pass

                if success:
                    automation_state["success"] += 1
                    await add_log(f"  ✅ 접수 완료!", "success")
                else:
                    automation_state["fail"] += 1
                    await add_log(f"  ❌ 최종 실패: {result_msg}", "error")

                # 시트에 결과 기록
                try:
                    update_sheet_result(
                        spreadsheet_url, item["row"], result_msg,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                except Exception as e:
                    await add_log(f"  ⚠️ 시트 결과 기록 실패: {e}", "warn")

                await broadcast("state", automation_state)
                
                # 시크릿 창 닫기
                try:
                    await page.close()
                    await context.close()
                except Exception:
                    pass

                # 건 간 대기
                if i < len(items) - 1 and not automation_state["should_stop"]:
                    wait = delay + random.uniform(0, 2)
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
