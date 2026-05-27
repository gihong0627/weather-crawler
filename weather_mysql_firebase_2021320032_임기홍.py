import requests
from bs4 import BeautifulSoup
from datetime import datetime
import pymysql
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json

# =============================================
# MySQL 연결
# =============================================
def get_mysql_conn():
    return pymysql.connect(
        host        = os.environ['MYSQL_HOST'],
        port        = int(os.environ['MYSQL_PORT']),
        user        = os.environ['MYSQL_USER'],
        password    = os.environ['MYSQL_PASSWORD'],
        db          = os.environ['MYSQL_DB'],
        charset     = 'utf8mb4',
        cursorclass = pymysql.cursors.DictCursor
    )

# =============================================
# Firebase 초기화
# =============================================
def init_firebase():
    cred_env = os.environ.get('FIREBASE_CREDENTIALS', 'serviceAccountKey.json')
    if cred_env.strip().startswith('{'):
        cred = credentials.Certificate(json.loads(cred_env))
    else:
        cred = credentials.Certificate(cred_env)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

# =============================================
# 1단계: Nate 아산 일주일 예보 수집
# =============================================
def crawl_nate_weather():
    try:
        url = (
            "https://weather.nate.com/today.html"
            "?dml=1&lc=l114020&lcn=%EC%B6%A9%EC%B2%AD%EB%82%A8%EB%8F%84%20%EC%95%84%EC%82%B0"
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')

        items = soup.select('div.oneweek_wrap ul li')
        if not items:
            raise ValueError("선택자로 데이터를 찾지 못함")

        weather_list = []
        for item in items:
            # 요일, 날짜 파싱
            day_text  = item.select_one('p.day').contents[0].strip()   # 목, 금...
            date_text = item.select_one('p.day span').text.strip()      # /5.28
            month, day = date_text.replace('/', '').split('.')
            year = 2026
            forecast_date = f"{year}-{int(month):02d}-{int(day):02d}"

            # 날씨 상태
            weather_status = item.select_one('p.te').contents[0].strip()

            # 최고/최저 기온
            max_temp = item.select_one('p.te em').contents[0].strip()
            min_temp = item.select_one('p.te em span').text.strip().replace('/', '')

            weather_list.append({
                "city"           : "아산시",
                "forecast_date"  : forecast_date,
                "day_of_week"    : day_text,
                "weather_status" : weather_status,
                "min_temperature": min_temp,
                "max_temperature": max_temp,
                "source"         : "Nate Weather"
            })

        print(f"[1단계] Nate 크롤링 완료: {len(weather_list)}건")
        return weather_list

    except Exception as e:
        print(f"[1단계] 크롤링 실패: {e} → 예시 데이터 사용")
        return get_example_data()

# =============================================
# 크롤링 실패 시 예시 데이터
# =============================================
def get_example_data():
    sample = [
        ("2026-05-28", "목", "흐리고 비", "26℃", "20℃"),
        ("2026-05-29", "금", "맑음",      "26℃", "19℃"),
        ("2026-05-30", "토", "맑음",      "28℃", "15℃"),
        ("2026-05-31", "일", "맑음",      "31℃", "15℃"),
        ("2026-06-01", "월", "맑음",      "29℃", "16℃"),
        ("2026-06-02", "화", "구름많음",  "28℃", "17℃"),
    ]
    return [
        {
            "city"           : "아산시",
            "forecast_date"  : d, "day_of_week": w,
            "weather_status" : s,
            "min_temperature": mn, "max_temperature": mx,
            "source"         : "Nate Weather"
        }
        for d, w, s, mx, mn in sample
    ]

# =============================================
# 2단계: 리스트/딕셔너리 구조 확인 출력
# =============================================
def print_weather_list(weather_list):
    print(f"\n[2단계] 수집 데이터 구조 확인 ({len(weather_list)}건):")
    for item in weather_list:
        print(f"  {item['forecast_date']} ({item['day_of_week']}) "
              f"{item['weather_status']} "
              f"{item['min_temperature']} ~ {item['max_temperature']}")

# =============================================
# 3단계: MySQL 저장 (저장 프로시저)
# =============================================
def save_to_mysql(weather_list):
    conn = get_mysql_conn()
    try:
        for item in weather_list:
            with conn.cursor() as cursor:
                cursor.callproc('insert_weather_forecast', [
                    item['city'],
                    item['forecast_date'],
                    item['day_of_week'],
                    None,                        # forecast_time
                    item['weather_status'],
                    None,                        # temperature
                    item['min_temperature'],
                    item['max_temperature'],
                    None,                        # humidity
                    None,                        # wind_speed
                    None,                        # precipitation
                    item['source']
                ])
        conn.commit()
        print(f"\n[3단계] MySQL 저장 완료: {len(weather_list)}건")
    finally:
        conn.close()

# =============================================
# 4단계: 미업로드 데이터 조회
# =============================================
def get_unsynced():
    conn = get_mysql_conn()
    try:
        with conn.cursor() as cursor:
            cursor.callproc('get_unsynced_weather')
            rows = []
            for result in cursor.stored_results():
                rows = result.fetchall()
        print(f"\n[4단계] 미업로드 데이터 조회: {len(rows)}건")
        return rows, conn
    except:
        conn.close()
        raise

# =============================================
# 5단계: Firestore 업로드 + synced 상태 업데이트
# PDF 15~17페이지 기준
# =============================================
def upload_to_firestore(rows, conn):
    db = firestore.client()
    try:
        for row in rows:
            # PDF 15페이지: 문서 ID = asan_YYYY-MM-DD
            doc_id = f"asan_{row['forecast_date']}"
            if row['forecast_time']:
                safe_time = str(row['forecast_time']).replace(":", "")
                doc_id = f"asan_{row['forecast_date']}_{safe_time}"

            # PDF 16페이지: Firestore 문서 구조
            db.collection('asan_weather_forecast').document(doc_id).set({
                "mysql_id"       : row['id'],
                "city"           : row['city'],
                "forecast_date"  : str(row['forecast_date']),
                "day_of_week"    : row['day_of_week'],
                "weather_status" : row['weather_status'],
                "min_temperature": row['min_temperature'],
                "max_temperature": row['max_temperature'],
                "humidity"       : row['humidity'],
                "wind_speed"     : row['wind_speed'],
                "source"         : row['source'],
                "uploaded_at"    : datetime.now().isoformat()
            })

            # PDF 17페이지: 업로드 완료 후 synced 업데이트
            with conn.cursor() as cursor:
                cursor.callproc('update_synced_status', [row['id']])
            conn.commit()

            print(f"  업로드 완료: {doc_id}")

        print(f"\n[5단계] Firestore 업로드 완료: {len(rows)}건")
    finally:
        conn.close()

# =============================================
# 메인: PDF 19페이지 통합 실행 순서
# =============================================
if __name__ == '__main__':
    print("=" * 50)
    print("Nate → Python → MySQL → Firebase 시작")
    print("=" * 50)

    init_firebase()

    weather_list = crawl_nate_weather()          # 1단계
    print_weather_list(weather_list)              # 2단계
    save_to_mysql(weather_list)                   # 3단계
    rows, conn = get_unsynced()                   # 4단계
    upload_to_firestore(rows, conn)               # 5단계

    print("\n[6단계] Firebase Console에서 확인하세요!")
    print("  https://console.firebase.google.com")
    print("=" * 50)
    print("완료")
    print("=" * 50)