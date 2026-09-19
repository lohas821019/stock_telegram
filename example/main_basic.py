import yfinance as yf
import pandas as pd
import requests
import time

# --- 配置區 ---
TG_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TG_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"

# 這裡你可以直接寫代碼，或是美股代號
STOCK_LIST = ["3163", "3363", "2330", "NVDA", "6488"] 

def send_tg_msg(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Telegram 發送失敗: {e}")

def get_valid_df(symbol, period, interval):
    """自動嘗試 .TW 與 .TWO 後綴"""
    # 如果是美股 (純英文)，直接回傳
    if symbol.isalpha():
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        return df, symbol

    # 嘗試上市 (.TW)
    ticker_tw = f"{symbol}.TW"
    df = yf.Ticker(ticker_tw).history(period=period, interval=interval)
    if not df.empty:
        return df, ticker_tw

    # 嘗試上櫃 (.TWO)
    ticker_two = f"{symbol}.TWO"
    df = yf.Ticker(ticker_two).history(period=period, interval=interval)
    if not df.empty:
        return df, ticker_two

    return pd.DataFrame(), symbol

def add_indicators(df):
    """指標計算邏輯 (維持不變)"""
    if len(df) < 30: return df
    # MACD
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['DIF'] = ema12 - ema26
    df['DEA'] = df['DIF'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist'] = (df['DIF'] - df['DEA']) * 2
    # KDJ
    low_9 = df['Low'].rolling(window=9).min()
    high_9 = df['High'].rolling(window=9).max()
    rsv = (df['Close'] - low_9) / (high_9 - low_9) * 100
    df['K'] = rsv.ewm(com=2).mean()
    df['D'] = df['K'].ewm(com=2).mean()
    return df

def check_stock(symbol):
    print(f"正在搜尋並分析: {symbol}...")
    
    # 自動辨識上市櫃並抓取日線
    df_d, real_ticker = get_valid_df(symbol, "1y", "1d")
    # 抓取週線
    df_w, _ = get_valid_df(symbol, "2y", "1wk")

    if df_d.empty or df_w.empty:
        print(f"無法找到 {symbol} 的資料，請檢查代碼是否正確。")
        return None

    # 計算指標
    df_d = add_indicators(df_d)
    df_w = add_indicators(df_w)
    df_w['MA20'] = df_w['Close'].rolling(window=20).mean()
    
    last_d = df_d.iloc[-1]
    last_w = df_w.iloc[-1]
    curr_price = last_d['Close']
    ma20_w = last_w['MA20']
    
    # 判斷是否碰觸週線 (誤差 1.5% 內)
    touch_ma20w = abs(curr_price - ma20_w) / ma20_w < 0.015
    
    msg = f"<b>📊 標的分析: {real_ticker}</b>\n"
    msg += f"現價: {curr_price:.2f} (週線 MA20: {ma20_w:.2f})\n"
    
    if touch_ma20w:
        msg += "🎯 <b>[提醒] 接近/回測週線支撐！</b>\n"
    
    # 加上 KDJ 與 MACD 簡報
    msg += f"\n<b>[週線]</b> K: {last_w['K']:.1f} / {'多頭' if last_w['MACD_Hist']>0 else '空頭'}\n"
    msg += f"<b>[日線]</b> K: {last_d['K']:.1f} / {'偏多' if last_d['MACD_Hist']>0 else '偏空'}"

    return msg

def main():
    for s in STOCK_LIST:
        try:
            report = check_stock(s)
            if report:
                send_tg_msg(report)
                print(f"-> {s} 處理完畢")
            time.sleep(1)
        except Exception as e:
            print(f"{s} 執行錯誤: {e}")

if __name__ == "__main__":
    main()