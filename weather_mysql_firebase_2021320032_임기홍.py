import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import pymysql
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json

DAY_NAME_MAP = {
    0: '월요일', 1: '화요일', 2: '수요일',
    3: '목요일', 4: '금요일', 5: '토요일', 6: '일요일'
}

def get_mysql_conn():
    return pymysql.connect(
        host        = os.environ['MYSQL_HOST'],
        port        = int(os.environ['MYSQL_PORT']),
        user        = os.environ['MYSQL_USER'],
        password    = os.environ['MYSQL_PASSWORD'],
        database    = os.environ['MYSQL_DB'],
        charset     = 'utf8mb4',
        cursorclass = pymysql.cursors.DictCursor
    )

def init_firebase():
    cred_env = os.environ.get('FIREBASE_CREDENTIALS', 'serviceAccountKey.json')
    if cred_env.strip().startswith('{'):
        cred = credentials.Certificate(json.loads(cred_env))
    else:
        cred = credentials.Certificate(cred_env)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

def crawl_nate_weather():
    now = datetime.now(timezone(timedelta(hours=9)))
    forecast_date = now.strftime('%Y-%m-%d')
    forecast_time = now.strftime('%H:%M')
    day_of_week   = DAY_NAME_MAP[now.weekday()]

    weather_status = None
    temperature    = None
    min_temp       = None
    max_temp       = None
    humidity       = None
    wind_speed     = None

    try:
        url = (
            "https://weather.nate.com/today.html"
            "?dml=1&lc=l114020&lcn=%EC%B6%A9%EC%B2%AD%EB%82%A8%EB%8F%84%20%EC%95%84%EC%82%B0"
        )
        res  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(res.content, 'html.parser')
        today = soup.find('div', class_='today_wrap')

        weather_status = today.select_one('p.text').text.strip()
        temperature    = today.select_one('p.celsius').contents[0].strip() + '℃'
        max_temp       = today.select_one('span.maximum').text.replace('최고', '').strip()
        min_temp       = today.select_one('span.minimum').text.replace('최저', '').strip()
        humidity       = today.select_one('p.humidity em').text.strip()
        wind_speed     = today.select_one('p.wind em').text.strip()
        precipitation = today.select_one('p.rainfall em').text.strip()

        print(f"[1단계] 크롤링 완료: {forecast_date} {forecast_time} {weather_status} {temperature}")

    except Exception as e:
        print(f"[1단계] 크롤링 실패: {e} → 날씨 데이터 None으로 저장")

    weather_list = [{
        "city"           : "아산시",
        "forecast_date"  : forecast_date,
        "day_of_week"    : day_of_week,
        "weather_status" : weather_status,
        "min_temperature": min_temp,
        "max_temperature": max_temp,
        "humidity"       : humidity,
        "wind_speed"     : wind_speed,
        "precipitation"  : precipitation,
        "source"         : "Nate Weather",
        "forecast_time"  : forecast_time,
        "temperature"    : temperature
    }]

    return weather_list

def print_weather_list(weather_list):
    print(f"\n[2단계] 수집 데이터 구조 확인:")
    for item in weather_list:
        print(f"  {item['forecast_date']} ({item['day_of_week']}) "
              f"{item['forecast_time']} "
              f"{item['weather_status']} "
              f"{item['min_temperature']} ~ {item['max_temperature']}")

def save_to_mysql(weather_list):
    conn = get_mysql_conn()
    try:
        for item in weather_list:
            with conn.cursor() as cursor:
                cursor.callproc('insert_weather_forecast', [
                    item['city'],
                    item['forecast_date'],
                    item['day_of_week'],
                    item['forecast_time'],
                    item['weather_status'],
                    item['temperature'],
                    item['min_temperature'],
                    item['max_temperature'],
                    item['humidity'],
                    item['wind_speed'],
                    item['precipitation'],
                    item['source']
                ])
        conn.commit()
        print(f"\n[3단계] MySQL 저장 완료: {len(weather_list)}건")
    finally:
        conn.close()

def get_unsynced():
    conn = get_mysql_conn()
    try:
        with conn.cursor() as cursor:
            cursor.callproc('get_unsynced_weather')
            rows = cursor.fetchall()
        print(f"\n[4단계] 미업로드 데이터 조회: {len(rows)}건")
        return rows, conn
    except:
        conn.close()
        raise

def upload_to_firestore(rows, conn):
    db = firestore.client()
    try:
        for row in rows:
            doc_id = f"asan_{row['forecast_date']}"
            if row['forecast_time']:
                safe_time = str(row['forecast_time']).replace(":", "")
                doc_id = f"asan_{row['forecast_date']}_{safe_time}"

            db.collection('asan_weather_forecast').document(doc_id).set({
                "mysql_id"       : row['id'],
                "city"           : row['city'],
                "forecast_date"  : str(row['forecast_date']),
                "day_of_week"    : row['day_of_week'],
                "weather_status" : row['weather_status'],
                "temperature"    : row['temperature'],
                "min_temperature": row['min_temperature'],
                "max_temperature": row['max_temperature'],
                "humidity"       : row['humidity'],
                "wind_speed"     : row['wind_speed'],
                "precipitation"  : row['precipitation'],
                "source"         : row['source'],
                "forecast_time"  : row['forecast_time'],
                "uploaded_at"    : datetime.now(timezone(timedelta(hours=9))).isoformat()
            })

            with conn.cursor() as cursor:
                cursor.callproc('update_synced_status', [row['id']])
            conn.commit()

            print(f"  업로드 완료: {doc_id}")

        print(f"\n[5단계] Firestore 업로드 완료: {len(rows)}건")
    finally:
        conn.close()

if __name__ == '__main__':
    init_firebase()

    weather_list = crawl_nate_weather()
    print_weather_list(weather_list)
    save_to_mysql(weather_list)
    rows, conn = get_unsynced()
    upload_to_firestore(rows, conn)
