"""
구글 시트에 자동화 템플릿을 생성하는 스크립트
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
기존 빈 구글 시트에 접수데이터/설정/사용법 시트를 자동으로 세팅합니다.

사용법:
  python setup_sheet.py --url "https://docs.google.com/spreadsheets/d/..."
"""

import argparse
import sys
from pathlib import Path

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("❌ gspread가 설치되지 않았습니다:")
    print("   pip install gspread google-auth")
    sys.exit(1)

SERVICE_ACCOUNT_FILE = "service_account.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def setup_sheet(spreadsheet_url: str):
    if not Path(SERVICE_ACCOUNT_FILE).exists():
        print(f"❌ {SERVICE_ACCOUNT_FILE} 파일이 없습니다.")
        print("   Google Cloud Console에서 서비스 계정 키를 다운로드하세요.")
        sys.exit(1)

    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(spreadsheet_url)

    print(f"📊 스프레드시트: {sh.title}")

    # ── Sheet 1: 접수데이터 ──
    print("  [1/3] 접수데이터 시트 생성...")
    try:
        old_ws1 = sh.worksheet("접수데이터")
        sh.del_worksheet(old_ws1)
    except gspread.exceptions.WorksheetNotFound:
        pass
    ws1 = sh.add_worksheet(title="접수데이터", rows=110, cols=10)

    # 헤더
    ws1.update("A1", [["🔄 배달의민족 리뷰 게시중단 자동 접수 시트"]])
    ws1.merge_cells("A1:H1")
    ws1.update("A2", [["※ 파란색 영역에 데이터를 입력하세요. G~H열은 자동화 실행 시 자동 기록됩니다."]])
    ws1.merge_cells("A2:H2")

    headers = [["No.", "업체명", "가게번호", "리뷰번호\n(전체 입력)",
                "신청자구분\n(대표자/운영자)", "이메일",
                "처리결과", "처리시간"]]
    ws1.update("A3", headers)

    # 샘플 데이터
    # 샘플 데이터
    samples = [
        [1, "맛있는치킨", "12345678", "RV-2025-001, RV-2025-002", "대표자", "owner@example.com", "", ""],
        [2, "행복한분식", "87654321", "RV-2025-003", "운영자", "manager@example.com", "", ""],
        [3, "정성카페", "11223344", "RV-2025-004, RV-2025-005", "대표자", "cafe@example.com", "", ""],
    ]
    ws1.update("A4", samples)

    # 번호 채우기 (4~103)
    numbers = [[i] for i in range(4, 101)]
    ws1.update("A7", numbers)

    # 드롭다운 (G열 - 신청유형 삭제됨, 필터만 적용)
    ws1.set_basic_filter("A3:H103")

    # 서식
    ws1.format("A1:H1", {"textFormat": {"bold": True, "fontSize": 14}})
    ws1.format("A3:H3", {
        "backgroundColor": {"red": 0.18, "green": 0.46, "blue": 0.71},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontSize": 10},
        "horizontalAlignment": "CENTER",
        "wrapStrategy": "WRAP",
    })
    ws1.format("B4:F103", {
        "backgroundColor": {"red": 1, "green": 0.95, "blue": 0.8},
        "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 1}},
    })

    # 열 너비
    sheet_id = ws1.id
    requests = []
    widths = [50, 120, 120, 200, 130, 200, 120, 160]
    for i, w in enumerate(widths):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i+1},
                "properties": {"pixelSize": w},
                "fields": "pixelSize",
            }
        })
    sh.batch_update({"requests": requests})

    # ── Sheet 2: 설정 ──
    print("  [2/3] 설정 시트 생성...")
    try:
        ws2 = sh.worksheet("설정")
        ws2.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws2 = sh.add_worksheet(title="설정", rows=20, cols=4)

    ws2.update("A1", [["⚙️ 자동화 설정"]])
    ws2.merge_cells("A1:C1")
    ws2.update("A3", [["설정 항목", "값", "설명"]])
    settings = [
        ["챗봇 URL", "https://design.happytalkio.com/chatting?siteId=4000000024&siteName=%EC%9A%B0%EC%95%84%ED%95%9C%ED%98%95%EC%A0%9C%EB%93%A4&categoryId=61602&divisionId=200880", "배달의민족 Happytalk 챗봇 원본 URL (단축 URL 사용 금지)"],
        ["건당 대기시간(초)", "5", "각 접수 건 사이 기본 대기 (실제: 5~10초 랜덤)"],
        ["요소 탐지 타임아웃(초)", "15", "챗봇 버튼/메시지 대기 최대 시간"],
        ["최대 재시도 횟수", "3", "실패 시 재시도 횟수"],
        ["배치 크기(건)", "20", "N건마다 긴 휴식 (0=휴식없음)"],
        ["배치 휴식(초)", "120", "배치 휴식 시간 (실제: 120~150초 랜덤)"],
        ["브라우저 표시", "TRUE", "TRUE=브라우저 보임, FALSE=백그라운드"],
        ["스크린샷 저장", "TRUE", "에러 발생 시 스크린샷 저장"],
        ["기본 신청자구분", "대표자", "시트에 미입력 시 기본값 (대표자/운영자)"],
        ["기본 이메일", "", "시트에 미입력 시 사용할 이메일"],
    ]
    ws2.update("A4", settings)

    ws2.format("A1:C1", {"textFormat": {"bold": True, "fontSize": 14}})
    ws2.format("A3:C3", {
        "backgroundColor": {"red": 0.18, "green": 0.46, "blue": 0.71},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })
    ws2.format("B4:B11", {
        "backgroundColor": {"red": 1, "green": 0.95, "blue": 0.8},
        "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 1}},
    })

    # ── Sheet 3: 사용법 ──
    print("  [3/3] 사용법 시트 생성...")
    try:
        ws3 = sh.worksheet("사용법")
        ws3.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws3 = sh.add_worksheet(title="사용법", rows=20, cols=3)

    ws3.update("A1", [["📖 사용 방법 안내"]])
    ws3.merge_cells("A1:B1")
    guide = [
        ["Step 1", "서버 실행: python server.py (터미널에서 1회만 실행)"],
        ["Step 2", "브라우저에서 http://localhost:8001 접속"],
        ["Step 3", "이 구글 시트 URL을 대시보드에 붙여넣기 → 연결"],
        ["Step 4", "접수데이터 시트에 가게번호, 리뷰번호, 신청자구분, 이메일 입력"],
        ["Step 5", "대시보드에서 '자동화 시작' 클릭"],
        ["Step 6", "실행 완료 후 G~H열에 결과 자동 기록"],
        ["주의", "1건씩 순차 처리, 건당 약 1~2분 소요. 실행 중 시트 수정 금지."],
    ]
    ws3.update("A3", guide)
    ws3.format("A1:B1", {"textFormat": {"bold": True, "fontSize": 14}})

    # 기본 시트(Sheet1) 삭제 시도
    try:
        default = sh.worksheet("Sheet1")
        sh.del_worksheet(default)
    except Exception:
        pass
    try:
        default = sh.worksheet("시트1")
        sh.del_worksheet(default)
    except Exception:
        pass

    print(f"\n✅ 템플릿 설정 완료!")
    print(f"   URL: {spreadsheet_url}")
    print(f"   시트: 접수데이터, 설정, 사용법")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="구글 시트에 자동화 템플릿 생성")
    parser.add_argument("--url", required=True, help="Google Sheets URL")
    args = parser.parse_args()
    setup_sheet(args.url)
