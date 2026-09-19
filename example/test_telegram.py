import requests
import configparser

# 讀取你的 config.ini
config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')

token = config['Telegram']['token']
chat_id = config['Telegram']['chat_id']

print(f"目前讀取到的 Token: {token[:10]}...")
print(f"目前讀取到的 Chat ID: {chat_id}")

def test_send():
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": "🚀 這是來自 Python 的測試訊息！如果你看到這行，代表設定正確。"}
    
    try:
        r = requests.post(url, data=payload)
        result = r.json()
        if result.get("ok"):
            print("✅ 發送成功！請檢查你的 Telegram。")
        else:
            print(f"❌ 發送失敗，錯誤訊息: {result.get('description')}")
    except Exception as e:
        print(f"❌ 發生異常: {e}")

if __name__ == "__main__":
    test_send()