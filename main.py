import os
import json
from flask import Flask, request, jsonify

# 구글 API 및 데이터베이스 관련 라이브러리
from google.cloud import firestore
import google.generativeai as genai
from google.apps import gmail_v1
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# 1. Flask 웹 서버 초기화
app = Flask(__name__)

# 인증 파일 및 환경 변수 경로 설정
# (Cloud Shell에 업로드한 인증 파일 이름과 정확히 일치해야 합니다)
FIREBASE_KEY_PATH = "firebase_key.json"
GMAIL_TOKEN_PATH = "token.json"

# Gemini API 키 설정 (Google AI Studio에서 발급받은 키 입력)
# 보안을 위해 시스템 환경변수에 등록해 쓰거나 직접 문자열로 대입합니다.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6I3pyda8PXNhcuVJA5AsnJRrnqq6ZbBTnQvEsM3G1ud-Q")
genai.configure(api_key=GEMINI_API_KEY)


def fetch_latest_receipt_emails():
    """지메일 API를 통해 읽지 않은 영수증 관련 메일을 파싱하는 함수"""
    if not os.path.exists(GMAIL_TOKEN_PATH):
        raise FileNotFoundError(f"⚠️ 지메일 인증 토큰({GMAIL_TOKEN_PATH}) 파일이 없습니다.")
        
    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, ['https://www.googleapis.com/auth/gmail.readonly'])
    
    # 토큰 만료 시 자동 갱신
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, 'w') as token_file:
            token_file.write(creds.to_json())

    service = gmail_v1.Gmail(credentials=creds)
    
    # '영수증' 또는 '주문내역' 키워드가 들어간 읽지 않은(is:unread) 메일 검색
    query = "영수증 OR 주문내역 is:unread"
    results = service.users().messages().list(userId='me', q=query, maxResults=5).execute()
    messages = results.get('messages', [])
    
    email_contents = []
    for msg in messages:
        full_msg = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        
        # 메일 본문 추출 (HTML 또는 Plain Text)
        payload = full_msg.get('payload', {})
        body = ""
        
        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/html' or part['mimeType'] == 'text/plain':
                    body = part['body'].get('data', '')
                    break
        else:
            body = payload.get('body', {}).get('data', '')
            
        if body:
            import base64
            # base64 디코딩하여 실제 텍스트 문자열로 변환
            decoded_body = base64.urlsafe_b64decode(body).decode('utf-8', errors='ignore')
            email_contents.append(decoded_body)
            
            # (옵션) 읽은 메일은 다시 읽지 않도록 'UNREAD' 라벨 제거 처리 가능
            # service.users().messages().batchModify(userId='me', body={'ids': [msg['id']], 'removeLabelIds': ['UNREAD']}).execute()
            
    return email_contents


def analyze_receipt_with_gemini(html_content):
    """gemini-2.5-flash 모델을 사용하여 HTML 영수증에서 식재료 추출"""
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    prompt = f"""
    너는 영수증 메일 분석 전문가이자 냉장고 재고 관리 시스템의 데이터 정제기야.
    입력되는 영수증 HTML을 분석하여 주소가 부산인 경우만 분석해줘.
    입력되는 영수증 HTML을 분석하여 식품만 골라내고 아래 규칙을 따라줘.
    결과는 반드시 아무런 설명 없이 순수한 JSON 양식만 반환해야 해.
    
    [출력 JSON 예시]
    [
      {{"item_name": "친환경 흙대파", "quantity": 1, "category": "채소", "expiry_days": 7, "storage" : "냉장"}},
      {{"item_name": "신선란 30구", "quantity": 1, "category": "아이스크림", "expiry_days": 20, "storage" : "냉동"}},
      {{"item_name": "신선란 30구", "quantity": 1, "category": "새우깡", "expiry_days": 20, "storage" : "실온"}}
    ]
    
    [영수증 데이터]
    {html_content}
    """
    
    response = model.generate_content(prompt)
    
    # 자칫 Gemini가 마크다운(```json ... ```)을 붙여서 응답할 경우를 대비해 껍데기 제거
    response_text = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(response_text)


def save_to_firestore(items):
    """정제된 식재료 데이터를 Firestore DB의 'fridge_items' 컬렉션에 보관"""
    if not os.path.exists(FIREBASE_KEY_PATH):
        raise FileNotFoundError(f"⚠️ Firestore 키({FIREBASE_KEY_PATH}) 파일이 없습니다.")
        
    db = firestore.Client.from_service_account_json(FIREBASE_KEY_PATH)
    collection_ref = db.collection("fridge_items")
    
    saved_count = 0
    for item in items:
        # DB에 하나씩 문서 생성 (자동 ID 생성)
        doc_ref = collection_ref.document()
        doc_ref.set({
            "item_name": item.get("item_name"),
            "quantity": item.get("quantity", 1),
            "category": item.get("category", "기타"),
            "expiry_days": item.get("expiry_days", 7),
            "created_at": firestore.SERVER_TIMESTAMP  # 저장 시간 기록
        })
        saved_count += 1
        
    return saved_count


# =====================================================================
# 3. 구글 클라우드 전용 웹 에디터 웹훅 진입점 (HTTP / PubSub 공용)
# =====================================================================
@app.route('/', methods=['POST', 'GET'])
def main_handler():
    try:
        print("📥 Cloud Run 서빙 함수가 정상적으로 깨어났습니다.")
        
        # 1단계: 지메일에서 읽지 않은 영수증 메일들 가져오기
        emails = fetch_latest_receipt_emails()
        if not emails:
            print("📭 처리할 새로운 읽지 않은 영수증 메일이 없습니다.")
            return jsonify({"status": "success", "message": "새로운 영수증이 없습니다."}), 200
            
        print(f"📧 총 {len(emails)}개의 영수증 메일을 발견했습니다. 분석을 시작합니다.")
        
        total_saved = 0
        # 2단계: 수신된 메일들을 하나씩 순회하며 처리
        for html_receipt in emails:
            # Gemini를 이용해 식재료 JSON 리스트 추출
            parsed_items = analyze_receipt_with_gemini(html_receipt)
            print(f"🤖 Gemini 추출 결과: {parsed_items}")
            
            # Firestore 데이터베이스에 일괄 저장
            saved_count = save_to_firestore(parsed_items)
            total_saved += saved_count
            
        print(f"🎉 성공적으로 총 {total_saved}개의 식재료를 냉장고 데이터베이스에 저장 완료!")
        return jsonify({
            "status": "success",
            "message": f"성공적으로 {total_saved}개의 식재료 데이터가 저장되었습니다."
        }), 200

    except Exception as e:
        error_msg = f"❌ 실행 중 치명적 에러 발생: {str(e)}"
        print(error_msg)
        return jsonify({"status": "error", "message": error_msg}), 500


# 구글 클라우드가 내부적으로 컨테이너를 가동할 때 지정한 PORT 변수를 읽어서 대기 상태 전환
if __name__ == "__main__":
    server_port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=server_port)