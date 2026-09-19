import configparser
import html
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib import font_manager
import pandas as pd
import requests
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
STOCK_FILE = os.path.join(BASE_DIR, "stocks.txt")
SMC_DASHBOARD_FILE = os.path.join(BASE_DIR, "smc_dashboard.html")
SMC_DASHBOARD_ASSET_DIR = os.path.join(BASE_DIR, "smc_dashboard_assets")
SMC_PROFILE_CACHE_FILE = os.path.join(BASE_DIR, "smc_profiles_cache.json")

DEFAULT_DAILY_PERIOD = "18mo"
DEFAULT_WEEKLY_PERIOD = "5y"
DEFAULT_ALERT_LOOKBACK_BARS = 3
DEFAULT_MAX_WORKERS = 8
DEFAULT_CHART_BARS = 120


def configure_matplotlib_fonts() -> None:
    font_candidates = [
        r"C:\Windows\Fonts\msjh.ttc",
        r"C:\Windows\Fonts\msjhbd.ttc",
        r"C:\Windows\Fonts\mingliu.ttc",
        r"C:\Windows\Fonts\mingliub.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    for font_path in font_candidates:
        if os.path.exists(font_path):
            font_manager.fontManager.addfont(font_path)
            font_name = font_manager.FontProperties(fname=font_path).get_name()
            matplotlib.rcParams["font.family"] = font_name
            break
    matplotlib.rcParams["axes.unicode_minus"] = False


configure_matplotlib_fonts()


def normalize_chart_direction(value: str) -> str:
    normalized = str(value).strip().lower()
    bullish_values = {"bull", "bullish", "long", "up", "buy", "b", "看漲", "多", "多方"}
    bearish_values = {"bear", "bearish", "short", "down", "sell", "s", "下跌", "看跌", "空", "空方"}
    if normalized in bullish_values:
        return "bullish"
    if normalized in bearish_values:
        return "bearish"
    return "all"


INDUSTRY_MAP = {
    "Semiconductors": "半導體",
    "Semiconductor Equipment & Materials": "半導體設備/材料",
    "Computer Hardware": "硬體/AI伺服器",
    "Consumer Electronics": "消費電子",
    "Electronic Components": "電子零組件",
    "Communication Equipment": "通訊/CPO/網通",
    "Internet Content & Information": "網路/平台",
    "Software - Infrastructure": "軟體基礎建設",
    "Software - Application": "應用軟體",
    "Auto Manufacturers": "汽車/電動車",
    "Utilities - Independent Power Producers": "電力/能源",
}

LOCAL_NAMES = {
    "2330": "台積電",
    "2317": "鴻海",
    "2454": "聯發科",
    "2308": "台達電",
    "2382": "廣達",
    "2303": "聯電",
    "3711": "日月光投控",
    "3231": "緯創",
    "2357": "華碩",
    "2379": "瑞昱",
    "3034": "聯詠",
    "2345": "智邦",
    "6669": "緯穎",
    "3008": "大立光",
    "2395": "研華",
    "4938": "和碩",
    "2408": "南亞科",
    "2327": "國巨",
    "2360": "致茂",
    "3017": "奇鋐",
    "3037": "欣興",
    "8046": "南電",
    "5274": "信驊",
    "3661": "世芯-KY",
    "3529": "力旺",
    "6415": "矽力*-KY",
    "6643": "M31",
    "3653": "健策",
    "3293": "鈊象",
    "3443": "創意",
    "6223": "旺矽",
    "6510": "精測",
    "6683": "雍智科技",
    "7769": "兆聯實業",
    "1590": "亞德客-KY",
    "6409": "旭隼",
}


@dataclass
class Settings:
    token: str
    chat_id: str
    delay: float
    max_workers: int
    alert_lookback_bars: int
    daily_period: str
    weekly_period: str
    pivot_internal: int
    pivot_swing: int
    equal_length: int
    equal_threshold: float
    fvg_min_pct: float
    send_telegram: bool
    send_charts: bool
    chart_bars: int
    chart_timeframe: str
    chart_direction: str
    write_dashboard: bool
    dashboard_charts: bool


@dataclass
class StructureEvent:
    timeframe: str
    scope: str
    direction: str
    tag: str
    level: float
    close: float
    date: pd.Timestamp
    bars_ago: int


@dataclass
class FairValueGap:
    timeframe: str
    direction: str
    top: float
    bottom: float
    date: pd.Timestamp
    bars_ago: int
    mitigated: bool


@dataclass
class OrderBlock:
    timeframe: str
    direction: str
    high: float
    low: float
    date: pd.Timestamp
    bars_ago: int
    source_tag: str


@dataclass
class EqualLevel:
    timeframe: str
    kind: str
    level: float
    first_date: pd.Timestamp
    second_date: pd.Timestamp
    bars_ago: int


def load_settings() -> Settings:
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE, encoding="utf-8")

    try:
        token = config["Telegram"]["token"]
        chat_id = config["Telegram"]["chat_id"]
    except KeyError as exc:
        raise RuntimeError("config.ini 缺少 [Telegram] token 或 chat_id") from exc

    settings = config["Settings"] if "Settings" in config else {}
    return Settings(
        token=token,
        chat_id=chat_id,
        delay=float(settings.get("delay", 1.0)),
        max_workers=int(settings.get("max_workers", DEFAULT_MAX_WORKERS)),
        alert_lookback_bars=int(settings.get("smc_alert_lookback_bars", DEFAULT_ALERT_LOOKBACK_BARS)),
        daily_period=settings.get("smc_daily_period", DEFAULT_DAILY_PERIOD),
        weekly_period=settings.get("smc_weekly_period", DEFAULT_WEEKLY_PERIOD),
        pivot_internal=int(settings.get("smc_pivot_internal", 5)),
        pivot_swing=int(settings.get("smc_pivot_swing", 20)),
        equal_length=int(settings.get("smc_equal_length", 3)),
        equal_threshold=float(settings.get("smc_equal_threshold", 0.001)),
        fvg_min_pct=float(settings.get("smc_fvg_min_pct", 0.0)),
        send_telegram=settings.get("smc_send_telegram", "true").lower() in {"1", "true", "yes", "on"},
        send_charts=settings.get("smc_send_charts", "true").lower() in {"1", "true", "yes", "on"},
        chart_bars=int(settings.get("smc_chart_bars", DEFAULT_CHART_BARS)),
        chart_timeframe=settings.get("smc_chart_timeframe", "auto"),
        chart_direction=normalize_chart_direction(settings.get("smc_chart_direction", "all")),
        write_dashboard=settings.get("smc_write_dashboard", "true").lower() in {"1", "true", "yes", "on"},
        dashboard_charts=settings.get("smc_dashboard_charts", "true").lower() in {"1", "true", "yes", "on"},
    )


def send_tg_msg(settings: Settings, message: str) -> None:
    url = f"https://api.telegram.org/bot{settings.token}/sendMessage"
    payload = {
        "chat_id": settings.chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, data=payload, timeout=15)
        if not response.ok:
            print(f"Telegram 發送失敗: {response.status_code} {response.text[:200]}")
    except Exception as exc:
        print(f"Telegram 發送例外: {exc}")



def send_tg_photo(settings: Settings, image_path: str, caption: str) -> None:
    url = f"https://api.telegram.org/bot{settings.token}/sendPhoto"
    payload = {
        "chat_id": settings.chat_id,
        "caption": caption[:1000],
        "parse_mode": "HTML",
    }
    try:
        with open(image_path, "rb") as image_file:
            response = requests.post(url, data=payload, files={"photo": image_file}, timeout=30)
        if not response.ok:
            print(f"Telegram ??????: {response.status_code} {response.text[:200]}")
    except Exception as exc:
        print(f"Telegram ??????: {exc}")

def chunk_message(text: str, limit: int = 3800) -> List[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks


def load_stocks() -> List[str]:
    if not os.path.exists(STOCK_FILE):
        return []
    with open(STOCK_FILE, "r", encoding="utf-8") as file:
        stocks = []
        for line in file:
            value = line.strip()
            if value and not value.startswith("#"):
                stocks.append(value)
    return list(dict.fromkeys(stocks))


def ticker_candidates(symbol: str) -> List[str]:
    symbol = symbol.strip().upper()
    if "." in symbol:
        return [symbol]
    if symbol.isdigit():
        return [f"{symbol}.TW", f"{symbol}.TWO"]
    return [symbol]


def clean_symbol(symbol: str) -> str:
    return symbol.replace(".TW", "").replace(".TWO", "").upper()


def yahoo_url(ticker: str) -> str:
    return f"https://tw.stock.yahoo.com/quote/{clean_symbol(ticker)}/"


def direction_label(direction: str) -> str:
    return {"Bullish": "多方", "Bearish": "空方", "Neutral": "中性"}.get(direction, direction)


def scope_label(scope: str) -> str:
    return {"Internal": "內部結構", "Swing": "波段結構"}.get(scope, scope)


def equal_level_label(kind: str) -> str:
    return {"Equal High": "等高", "Equal Low": "等低"}.get(kind, kind)


def download_histories(tickers: List[str], period: str, interval: str) -> Dict[str, pd.DataFrame]:
    if not tickers:
        return {}

    try:
        data = yf.download(
            tickers=tickers,
            period=period,
            interval=interval,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception as exc:
        print(f"下載 {interval} K 資料失敗: {exc}")
        return {}

    histories: Dict[str, pd.DataFrame] = {}
    if isinstance(data.columns, pd.MultiIndex):
        for ticker in tickers:
            if ticker not in data.columns.get_level_values(0):
                continue
            df = normalize_history(data[ticker])
            if is_valid_history(df):
                histories[ticker] = df
    else:
        df = normalize_history(data)
        if is_valid_history(df) and tickers:
            histories[tickers[0]] = df
    return histories


def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all").copy()
    if isinstance(df.index, pd.DatetimeIndex):
        df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
    return df


    denom = (high_9 - low_9).replace(0, pd.NA)
    rsv = (df["Close"] - low_9) / denom * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    value = k.iloc[-1]
    return 50.0 if pd.isna(value) else float(value)


def pivot_flags(df: pd.DataFrame, length: int) -> Tuple[List[bool], List[bool]]:
    highs = df["High"].tolist()
    lows = df["Low"].tolist()
    pivot_high = [False] * len(df)
    pivot_low = [False] * len(df)

    for index in range(length, len(df) - length):
        high_window = highs[index - length : index + length + 1]
        low_window = lows[index - length : index + length + 1]
        if highs[index] == max(high_window) and high_window.count(highs[index]) == 1:
            pivot_high[index] = True
        if lows[index] == min(low_window) and low_window.count(lows[index]) == 1:
            pivot_low[index] = True
    return pivot_high, pivot_low


def detect_structure_events(
    df: pd.DataFrame,
    timeframe: str,
    scope: str,
    pivot_length: int,
    lookback_bars: int,
) -> List[StructureEvent]:
    pivot_high, pivot_low = pivot_flags(df, pivot_length)
    last_high_level = None
    last_high_index = None
    last_high_crossed = True
    last_low_level = None
    last_low_index = None
    last_low_crossed = True
    trend_bias = 0
    events: List[StructureEvent] = []

    for index in range(len(df)):
        confirmed_index = index - pivot_length
        if confirmed_index >= 0:
            if pivot_high[confirmed_index]:
                last_high_level = float(df["High"].iloc[confirmed_index])
                last_high_index = confirmed_index
                last_high_crossed = False
            if pivot_low[confirmed_index]:
                last_low_level = float(df["Low"].iloc[confirmed_index])
                last_low_index = confirmed_index
                last_low_crossed = False

        close = float(df["Close"].iloc[index])
        if last_high_level is not None and not last_high_crossed and index > (last_high_index or 0):
            if close > last_high_level:
                tag = "CHoCH" if trend_bias == -1 else "BOS"
                trend_bias = 1
                last_high_crossed = True
                events.append(
                    StructureEvent(timeframe, scope, "Bullish", tag, last_high_level, close, df.index[index], len(df) - 1 - index)
                )

        if last_low_level is not None and not last_low_crossed and index > (last_low_index or 0):
            if close < last_low_level:
                tag = "CHoCH" if trend_bias == 1 else "BOS"
                trend_bias = -1
                last_low_crossed = True
                events.append(
                    StructureEvent(timeframe, scope, "Bearish", tag, last_low_level, close, df.index[index], len(df) - 1 - index)
                )

    return [event for event in events if event.bars_ago <= lookback_bars]


def detect_fair_value_gaps(
    df: pd.DataFrame,
    timeframe: str,
    lookback_bars: int,
    min_pct: float,
) -> List[FairValueGap]:
    gaps: List[FairValueGap] = []

    for index in range(2, len(df)):
        current_low = float(df["Low"].iloc[index])
        current_high = float(df["High"].iloc[index])
        prev_close = float(df["Close"].iloc[index - 1])
        high_two_back = float(df["High"].iloc[index - 2])
        low_two_back = float(df["Low"].iloc[index - 2])

        if current_low > high_two_back and prev_close > high_two_back:
            gap_pct = (current_low - high_two_back) / high_two_back if high_two_back else 0
            if gap_pct >= min_pct:
                mitigated = bool((df["Low"].iloc[index + 1 :] <= high_two_back).any())
                gaps.append(FairValueGap(timeframe, "Bullish", current_low, high_two_back, df.index[index], len(df) - 1 - index, mitigated))

        if current_high < low_two_back and prev_close < low_two_back:
            gap_pct = (low_two_back - current_high) / low_two_back if low_two_back else 0
            if gap_pct >= min_pct:
                mitigated = bool((df["High"].iloc[index + 1 :] >= low_two_back).any())
                gaps.append(FairValueGap(timeframe, "Bearish", low_two_back, current_high, df.index[index], len(df) - 1 - index, mitigated))

    return [gap for gap in gaps if gap.bars_ago <= lookback_bars and not gap.mitigated]


def find_order_block_for_event(df: pd.DataFrame, event: StructureEvent, search_bars: int = 20) -> Optional[OrderBlock]:
    try:
        event_index = df.index.get_loc(event.date)
    except KeyError:
        return None

    start = max(0, event_index - search_bars)
    candidates = range(event_index - 1, start - 1, -1)

    for index in candidates:
        open_price = float(df["Open"].iloc[index])
        close_price = float(df["Close"].iloc[index])
        if event.direction == "Bullish" and close_price < open_price:
            return OrderBlock(
                event.timeframe,
                "Bullish",
                float(df["High"].iloc[index]),
                float(df["Low"].iloc[index]),
                df.index[index],
                len(df) - 1 - index,
                event.tag,
            )
        if event.direction == "Bearish" and close_price > open_price:
            return OrderBlock(
                event.timeframe,
                "Bearish",
                float(df["High"].iloc[index]),
                float(df["Low"].iloc[index]),
                df.index[index],
                len(df) - 1 - index,
                event.tag,
            )
    return None


def detect_order_blocks(df: pd.DataFrame, events: Iterable[StructureEvent], lookback_bars: int) -> List[OrderBlock]:
    blocks = []
    seen = set()
    for event in events:
        if event.bars_ago > lookback_bars:
            continue
        block = find_order_block_for_event(df, event)
        if not block:
            continue
        key = (block.timeframe, block.direction, block.date, round(block.high, 4), round(block.low, 4))
        if key not in seen:
            blocks.append(block)
            seen.add(key)
    return blocks


def detect_equal_levels(df: pd.DataFrame, timeframe: str, length: int, threshold: float, lookback_bars: int) -> List[EqualLevel]:
    pivot_high, pivot_low = pivot_flags(df, length)
    high_points = [(i, float(df["High"].iloc[i])) for i, flag in enumerate(pivot_high) if flag]
    low_points = [(i, float(df["Low"].iloc[i])) for i, flag in enumerate(pivot_low) if flag]
    levels: List[EqualLevel] = []

    for points, kind in ((high_points, "Equal High"), (low_points, "Equal Low")):
        for first, second in zip(points, points[1:]):
            first_index, first_value = first
            second_index, second_value = second
            base = max(abs(first_value), 1e-9)
            if abs(second_value - first_value) / base <= threshold:
                bars_ago = len(df) - 1 - second_index
                if bars_ago <= lookback_bars:
                    levels.append(
                        EqualLevel(
                            timeframe,
                            kind,
                            (first_value + second_value) / 2,
                            df.index[first_index],
                            df.index[second_index],
                            bars_ago,
                        )
                    )
    return levels


def analyze_timeframe(
    df: pd.DataFrame,
    timeframe: str,
    settings: Settings,
) -> Tuple[List[StructureEvent], List[FairValueGap], List[OrderBlock], List[EqualLevel]]:
    internal_events = detect_structure_events(df, timeframe, "Internal", settings.pivot_internal, settings.alert_lookback_bars)
    swing_events = detect_structure_events(df, timeframe, "Swing", settings.pivot_swing, settings.alert_lookback_bars)
    structure_events = internal_events + swing_events
    gaps = detect_fair_value_gaps(df, timeframe, settings.alert_lookback_bars, settings.fvg_min_pct)
    blocks = detect_order_blocks(df, structure_events, settings.alert_lookback_bars)
    equal_levels = detect_equal_levels(df, timeframe, settings.equal_length, settings.equal_threshold, settings.alert_lookback_bars)
    return structure_events, gaps, blocks, equal_levels


def analyze_stock(symbol: str, daily_data: Dict[str, pd.DataFrame], weekly_data: Dict[str, pd.DataFrame], profiles: Dict[str, Dict[str, Optional[str]]], settings: Settings) -> Optional[Dict]:
    real_ticker = next((ticker for ticker in ticker_candidates(symbol) if ticker in daily_data), None)
    if not real_ticker:
        return None

    daily_df = daily_data[real_ticker]
    weekly_df = weekly_data.get(real_ticker, pd.DataFrame())
    profile = profiles.get(real_ticker, {})
    clean = clean_symbol(real_ticker)
    name = LOCAL_NAMES.get(clean) or profile.get("shortName") or symbol
    raw_industry = profile.get("industry") or "未知產業"
    group = INDUSTRY_MAP.get(raw_industry, raw_industry)

    daily_signals = analyze_timeframe(daily_df, "日K", settings)
    weekly_signals = analyze_timeframe(weekly_df, "週K", settings) if is_valid_history(weekly_df) else ([], [], [], [])

    structure_events = daily_signals[0] + weekly_signals[0]
    gaps = daily_signals[1] + weekly_signals[1]
    blocks = daily_signals[2] + weekly_signals[2]
    equal_levels = daily_signals[3] + weekly_signals[3]

    if not any((structure_events, gaps, blocks, equal_levels)):
        return None

    current_price = float(daily_df["Close"].iloc[-1])
    previous_price = float(daily_df["Close"].iloc[-2]) if len(daily_df) > 1 else current_price
    change_pct = ((current_price - previous_price) / previous_price * 100) if previous_price else 0.0

    return {
        "symbol": symbol,
        "ticker": real_ticker,
        "name": name,
        "group": group,
        "price": current_price,
        "change": change_pct,
        "day_k": calculate_k(daily_df),
        "week_k": calculate_k(weekly_df) if is_valid_history(weekly_df) else 50.0,
        "daily_df": daily_df,
        "weekly_df": weekly_df,
        "structure": structure_events,
        "fvg": gaps,
        "ob": blocks,
        "equal": equal_levels,
    }




def chart_timeframe_label(timeframe: str) -> str:
    if timeframe.startswith("\u65e5"):
        return "1D"
    if timeframe.startswith("\u9031") or timeframe.startswith("\u5468"):
        return "1W"
    return timeframe

def signal_count_for_timeframe(info: Dict, timeframe: str) -> int:
    return (
        sum(1 for item in info["structure"] if chart_timeframe_label(item.timeframe) == timeframe)
        + sum(1 for item in info["fvg"] if chart_timeframe_label(item.timeframe) == timeframe)
        + sum(1 for item in info["ob"] if chart_timeframe_label(item.timeframe) == timeframe)
        + sum(1 for item in info["equal"] if chart_timeframe_label(item.timeframe) == timeframe)
    )


def has_breakout_direction(info: Dict, timeframe: str, direction: str) -> bool:
    if direction == "all":
        return any(chart_timeframe_label(item.timeframe) == timeframe for item in info["structure"])
    expected = "Bullish" if direction == "bullish" else "Bearish"
    return any(
        chart_timeframe_label(item.timeframe) == timeframe and item.direction == expected
        for item in info["structure"]
    )


def choose_chart_timeframe(info: Dict, settings: Settings) -> str:
    requested = settings.chart_timeframe.strip().lower()
    if requested in {"daily", "day", "d", "1d", "\u65e5k", "\u65e5"}:
        return "1D"
    if requested in {"weekly", "week", "w", "1w", "\u9031k", "\u5468k", "\u9031", "\u5468"}:
        return "1W"

    if settings.chart_direction != "all":
        if has_breakout_direction(info, "1D", settings.chart_direction):
            return "1D"
        if has_breakout_direction(info, "1W", settings.chart_direction):
            return "1W"

    return "1D" if signal_count_for_timeframe(info, "1D") else "1W"


def chart_caption(info: Dict, timeframe: str) -> str:
    title = html.escape(f"{info['name']} ({info['ticker']})")
    return f"<b>{title}</b> {timeframe} SMC 結構圖\n現價 {fmt_price(info['price'])} | 日 K {info['day_k']:.1f}"


def draw_candles(ax, df: pd.DataFrame) -> None:
    dates = mdates.date2num(df.index.to_pydatetime())
    if len(dates) > 1:
        width = max((dates[-1] - dates[0]) / len(dates) * 0.65, 0.2)
    else:
        width = 0.6

    for date_num, row in zip(dates, df.itertuples()):
        open_price = float(row.Open)
        high_price = float(row.High)
        low_price = float(row.Low)
        close_price = float(row.Close)
        color = "#089981" if close_price >= open_price else "#f23645"
        ax.vlines(date_num, low_price, high_price, color=color, linewidth=1.0, alpha=0.95)
        lower = min(open_price, close_price)
        height = abs(close_price - open_price)
        if height == 0:
            height = max(high_price - low_price, 1e-6) * 0.02
        ax.add_patch(Rectangle((date_num - width / 2, lower), width, height, facecolor=color, edgecolor=color, linewidth=0.8))


def add_right_label(ax, y_value: float, text: str, color: str) -> None:
    x_min, x_max = ax.get_xlim()
    ax.text(
        x_max,
        y_value,
        f" {text}",
        color=color,
        fontsize=8,
        va="center",
        ha="left",
        bbox={"facecolor": "white", "edgecolor": color, "alpha": 0.85, "pad": 1.5},
    )


def plot_smc_chart(info: Dict, settings: Settings, output_dir: Optional[str] = None) -> Optional[str]:
    timeframe = choose_chart_timeframe(info, settings)
    df = info["daily_df"] if timeframe == "1D" else info["weekly_df"]
    if not is_valid_history(df):
        return None

    chart_df = df.tail(settings.chart_bars).copy()
    if chart_df.empty:
        return None

    fig, ax = plt.subplots(figsize=(13, 7), dpi=140)
    draw_candles(ax, chart_df)
    dates = mdates.date2num(chart_df.index.to_pydatetime())
    left = dates[0]
    right = dates[-1]
    span_start = dates[max(0, len(dates) - min(40, len(dates)))]

    for gap in [item for item in info["fvg"] if chart_timeframe_label(item.timeframe) == timeframe]:
        color = "#00a878" if gap.direction == "Bullish" else "#d64550"
        ax.axhspan(gap.bottom, gap.top, xmin=0.0, xmax=1.0, color=color, alpha=0.13)
        add_right_label(ax, (gap.bottom + gap.top) / 2, f"{direction_label(gap.direction)} FVG", color)

    for block in [item for item in info["ob"] if chart_timeframe_label(item.timeframe) == timeframe]:
        color = "#2563eb" if block.direction == "Bullish" else "#b91c1c"
        ax.axhspan(block.low, block.high, xmin=0.68, xmax=1.0, color=color, alpha=0.15)
        add_right_label(ax, (block.low + block.high) / 2, f"{direction_label(block.direction)} OB", color)

    for event in [item for item in info["structure"] if chart_timeframe_label(item.timeframe) == timeframe]:
        color = "#047857" if event.direction == "Bullish" else "#be123c"
        linestyle = "-" if event.tag == "BOS" else "--"
        ax.hlines(event.level, span_start, right, colors=color, linestyles=linestyle, linewidth=1.4)
        ax.scatter(mdates.date2num(event.date.to_pydatetime()), event.close, color=color, s=38, zorder=5)
        add_right_label(ax, event.level, f"{direction_label(event.direction)} {event.tag}", color)

    for level in [item for item in info["equal"] if chart_timeframe_label(item.timeframe) == timeframe]:
        color = "#7c3aed" if level.kind == "Equal High" else "#0f766e"
        ax.hlines(level.level, left, right, colors=color, linestyles=":", linewidth=1.2)
        add_right_label(ax, level.level, "EQH" if level.kind == "Equal High" else "EQL", color)

    ax.set_title(f"{info['name']} ({info['ticker']})－{timeframe} SMC 結構圖", fontsize=13, loc="left")
    ax.set_ylabel("價格")
    ax.grid(True, color="#e5e7eb", linewidth=0.8, alpha=0.8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()

    chart_dir = output_dir or os.path.join(tempfile.gettempdir(), "stock_smc_charts")
    os.makedirs(chart_dir, exist_ok=True)
    safe_ticker = info["ticker"].replace(".", "_").replace("/", "_")
    output_path = os.path.join(chart_dir, f"{safe_ticker}_{timeframe}.png")
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)
    return output_path


def send_signal_charts(settings: Settings, results: List[Dict]) -> None:
    if not settings.send_telegram or not settings.send_charts:
        return

    for info in results:
        timeframe = choose_chart_timeframe(info, settings)
        if not has_breakout_direction(info, timeframe, settings.chart_direction):
            continue
        image_path = plot_smc_chart(info, settings)
        if not image_path:
            continue
        send_tg_photo(settings, image_path, chart_caption(info, timeframe))
        try:
            os.remove(image_path)
        except OSError:
            pass
        time.sleep(0.5)


def dashboard_signal_records(info: Dict) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    for event in info["structure"]:
        records.append({"type": "結構（BOS／CHoCH）", "direction": direction_label(event.direction), "timeframe": event.timeframe, "text": f"{scope_label(event.scope)} {direction_label(event.direction)} {event.tag}：突破 {fmt_price(event.level)}"})
    for gap in info["fvg"]:
        records.append({"type": "公允價差（FVG）", "direction": direction_label(gap.direction), "timeframe": gap.timeframe, "text": f"{direction_label(gap.direction)}公允價差：{fmt_price(gap.bottom)} ~ {fmt_price(gap.top)}"})
    for block in info["ob"]:
        records.append({"type": "訂單塊（OB）", "direction": direction_label(block.direction), "timeframe": block.timeframe, "text": f"{direction_label(block.direction)}訂單塊：{fmt_price(block.low)} ~ {fmt_price(block.high)}"})
    for level in info["equal"]:
        records.append({"type": "等高／等低（EQH／EQL）", "direction": "中性", "timeframe": level.timeframe, "text": f"{equal_level_label(level.kind)}：{fmt_price(level.level)}"})
    return records


def dashboard_records(results: List[Dict], settings: Settings) -> List[Dict]:
    records = []
    if settings.dashboard_charts:
        os.makedirs(SMC_DASHBOARD_ASSET_DIR, exist_ok=True)
    for info in results:
        chart = None
        if settings.dashboard_charts:
            chart_path = plot_smc_chart(info, settings, SMC_DASHBOARD_ASSET_DIR)
            if chart_path:
                chart = os.path.join(os.path.basename(SMC_DASHBOARD_ASSET_DIR), os.path.basename(chart_path)).replace("\\", "/")
        records.append({
            "ticker": info["ticker"], "name": info["name"], "group": info["group"],
            "price": fmt_price(info["price"]), "change": info["change"],
            "dayK": f"{info['day_k']:.1f}", "weekK": f"{info['week_k']:.1f}",
            "chart": chart, "url": yahoo_url(info["ticker"]), "signals": dashboard_signal_records(info),
        })
    return records


def build_smc_dashboard_html(records: List[Dict], total: int, generated_at: Optional[str] = None) -> str:
    payload = json.dumps(records, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    generated_at = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
    return """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>SMC 命中儀表板</title>
<style>:root{color-scheme:light dark;--bg:#f5f7fb;--surface:#fff;--text:#172033;--muted:#64748b;--line:#dbe3ef;--accent:#2563eb;--up:#047857;--down:#be123c;--tag:#e8f0ff}@media(prefers-color-scheme:dark){:root{--bg:#0f172a;--surface:#172033;--text:#e6edf7;--muted:#a6b5ca;--line:#334155;--accent:#7aa2ff;--up:#5eead4;--down:#fda4af;--tag:#263a62}}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:"Microsoft JhengHei",system-ui,sans-serif}.page{max-width:1280px;margin:auto;padding:28px 20px 56px}.top{display:flex;justify-content:space-between;gap:18px;align-items:flex-end;border-bottom:1px solid var(--line);padding-bottom:18px}h1{font-size:26px;margin:0 0 8px}.muted{color:var(--muted);margin:0}.stats{font-weight:600;white-space:nowrap}.controls{display:flex;flex-wrap:wrap;gap:10px;margin:20px 0}.controls input,.controls select{background:var(--surface);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px 12px;font-size:15px}.controls input{min-width:240px;flex:1}.controls select{min-width:140px}.count{color:var(--muted);align-self:center}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px}.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px}.card-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.stock{font-size:19px;font-weight:700;color:var(--text);text-decoration:none}.ticker{color:var(--muted);font-size:14px}.price{font-size:21px;font-weight:700;text-align:right}.up{color:var(--up)}.down{color:var(--down)}.metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:14px 0;color:var(--muted);font-size:14px}.signals{display:flex;flex-direction:column;gap:7px}.signal{border-left:3px solid var(--accent);padding:7px 9px;background:var(--tag);font-size:14px}.signal b{margin-right:6px}.chart{width:100%;margin-top:14px;border-radius:8px;border:1px solid var(--line)}.empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:42px}@media(max-width:600px){.page{padding:20px 14px}.top{align-items:flex-start;flex-direction:column}.stats{white-space:normal}.controls input,.controls select{width:100%;min-width:0}.grid{grid-template-columns:1fr}}</style></head>
<body><main class="page"><header class="top"><div><h1>SMC 命中儀表板</h1><p class="muted">最後掃描：__GENERATED__</p></div><div class="stats">命中 <span id="hit-total">0</span> / __TOTAL__ 檔</div></header><section class="controls" aria-label="篩選條件"><input id="search" type="search" placeholder="搜尋代號、名稱或產業"><select id="signal"><option value="">全部訊號</option><option>結構（BOS／CHoCH）</option><option>公允價差（FVG）</option><option>訂單塊（OB）</option><option>等高／等低（EQH／EQL）</option></select><select id="direction"><option value="">全部方向</option><option value="多方">多方</option><option value="空方">空方</option><option value="中性">中性</option></select><select id="timeframe"><option value="">日K、週K</option><option value="日K">日K</option><option value="週K">週K</option></select><span id="count" class="count" aria-live="polite"></span></section><section id="cards" class="grid" aria-live="polite"></section></main>
<script>const records=__DATA__;const escapeHtml=value=>String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));const els={search:document.querySelector('#search'),signal:document.querySelector('#signal'),direction:document.querySelector('#direction'),timeframe:document.querySelector('#timeframe'),cards:document.querySelector('#cards'),count:document.querySelector('#count'),total:document.querySelector('#hit-total')};els.total.textContent=records.length;function render(){const term=els.search.value.trim().toLowerCase(),type=els.signal.value,direction=els.direction.value,timeframe=els.timeframe.value;const filtered=records.filter(record=>{const searchable=[record.ticker,record.name,record.group].join(' ').toLowerCase();const signals=record.signals.filter(signal=>(!type||signal.type===type)&&(!direction||signal.direction===direction)&&(!timeframe||signal.timeframe===timeframe));return(!term||searchable.includes(term))&&signals.length>0;});els.count.textContent=`顯示 ${filtered.length} / ${records.length} 檔`;els.cards.innerHTML=filtered.length?filtered.map(record=>{const change=Number(record.change),changeClass=change>=0?'up':'down',changeText=`${change>=0?'+':''}${change.toFixed(1)}%`;const signals=record.signals.filter(signal=>(!type||signal.type===type)&&(!direction||signal.direction===direction)&&(!timeframe||signal.timeframe===timeframe)).map(signal=>`<div class="signal"><b>${escapeHtml(signal.type)}</b>${escapeHtml(signal.timeframe)} · ${escapeHtml(signal.text)}</div>`).join('');return `<article class="card"><div class="card-head"><div><a class="stock" href="${escapeHtml(record.url)}" target="_blank" rel="noreferrer">${escapeHtml(record.name)}</a><div class="ticker">${escapeHtml(record.ticker)} · ${escapeHtml(record.group)}</div></div><div class="price">${escapeHtml(record.price)}<div class="${changeClass}">${changeText}</div></div></div><div class="metrics"><span>日 K：${escapeHtml(record.dayK)}</span><span>週 K：${escapeHtml(record.weekK)}</span></div><div class="signals">${signals}</div>${record.chart?`<img class="chart" src="${escapeHtml(record.chart)}" alt="${escapeHtml(record.name)} SMC K 線圖">`:''}</article>`}).join(''):'<p class="empty">沒有符合目前篩選條件的命中股票。</p>';}Object.values(els).slice(0,4).forEach(element=>element.addEventListener('input',render));render();</script></body></html>""".replace("__DATA__", payload).replace("__TOTAL__", str(total)).replace("__GENERATED__", html.escape(generated_at))


def write_smc_dashboard(results: List[Dict], total: int, settings: Settings) -> None:
    if not settings.write_dashboard:
        return
    with open(SMC_DASHBOARD_FILE, "w", encoding="utf-8") as file:
        file.write(build_smc_dashboard_html(dashboard_records(results, settings), total))
    print(f"SMC 儀表板已更新: {SMC_DASHBOARD_FILE}")

def format_date(value: pd.Timestamp) -> str:
    try:
        return value.strftime("%Y-%m-%d")
    except Exception:
        return str(value)[:10]


def fmt_price(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def format_stock_signal(info: Dict) -> str:
    change = f"+{info['change']:.1f}%" if info["change"] >= 0 else f"{info['change']:.1f}%"
    title = html.escape(f"{info['name']} ({info['ticker']})")
    link = f"<a href='{yahoo_url(info['ticker'])}'>{title}</a>"
    lines = [
        f"<b>{link}</b>",
        f"現價: {fmt_price(info['price'])} ({change}) | {html.escape(info['group'])}",
        f"KD: 日K {info['day_k']:.1f} / 週K {info['week_k']:.1f}",
    ]

    if info["structure"]:
        lines.append("結構訊號:")
        for event in sorted(info["structure"], key=lambda item: (item.timeframe, item.bars_ago)):
            lines.append(
                f"  - {event.timeframe} {scope_label(event.scope)} {direction_label(event.direction)} {event.tag}: "
                f"收盤 {fmt_price(event.close)} 突破 {fmt_price(event.level)}（{format_date(event.date)}，{event.bars_ago} 根前）"
            )

    if info["fvg"]:
        lines.append("公允價差（FVG）:")
        for gap in sorted(info["fvg"], key=lambda item: (item.timeframe, item.bars_ago)):
            lines.append(
                f"  - {gap.timeframe} {direction_label(gap.direction)} FVG: {fmt_price(gap.bottom)} ~ {fmt_price(gap.top)} "
                f"（{format_date(gap.date)}，未回補）"
            )

    if info["ob"]:
        lines.append("訂單塊（OB）:")
        for block in sorted(info["ob"], key=lambda item: (item.timeframe, item.bars_ago)):
            lines.append(
                f"  - {block.timeframe} {direction_label(block.direction)} OB: {fmt_price(block.low)} ~ {fmt_price(block.high)} "
                f"（{format_date(block.date)}，來源 {block.source_tag}）"
            )

    if info["equal"]:
        lines.append("等高／等低（EQH／EQL）:")
        for level in sorted(info["equal"], key=lambda item: (item.timeframe, item.bars_ago)):
            lines.append(
                f"  - {level.timeframe} {equal_level_label(level.kind)}: {fmt_price(level.level)} "
                f"（{format_date(level.first_date)} / {format_date(level.second_date)}）"
            )

    return "\n".join(lines)


def build_summary(results: List[Dict], total: int) -> str:
    by_group: Dict[str, int] = {}
    signal_counts = {"結構": 0, "FVG": 0, "OB": 0, "EQH/EQL": 0}

    for item in results:
        by_group[item["group"]] = by_group.get(item["group"], 0) + 1
        signal_counts["結構"] += len(item["structure"])
        signal_counts["FVG"] += len(item["fvg"])
        signal_counts["OB"] += len(item["ob"])
        signal_counts["EQH/EQL"] += len(item["equal"])

    lines = [
        "<b>SMC 篩選摘要</b>",
        f"日期: {time.strftime('%Y-%m-%d')}",
        f"命中股票: {len(results)} / {total}",
        "訊號數: " + " | ".join(f"{key} {value}" for key, value in signal_counts.items()),
    ]

    if by_group:
        lines.append("產業分布:")
        for group, count in sorted(by_group.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"  - {html.escape(group)}: {count}")
    return "\n".join(lines)


def main() -> None:
    settings = load_settings()
    stock_list = load_stocks()
    total = len(stock_list)
    if not stock_list:
        print("stocks.txt 沒有股票清單")
        return

    candidate_map = {symbol: ticker_candidates(symbol) for symbol in stock_list}
    tickers = list(dict.fromkeys(ticker for candidates in candidate_map.values() for ticker in candidates))

    print(f"開始 SMC 篩選，共 {total} 檔，候選 ticker {len(tickers)} 個")
    print("下載日K資料...")
    daily_data = download_histories(tickers, settings.daily_period, "1d")
    print("下載週K資料...")
    weekly_data = download_histories(tickers, settings.weekly_period, "1wk")

    valid_tickers = []
    for symbol in stock_list:
        real_ticker = next((ticker for ticker in candidate_map[symbol] if ticker in daily_data), None)
        if real_ticker:
            valid_tickers.append(real_ticker)

    print(f"讀取公司資料，共 {len(valid_tickers)} 檔")
    with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
        profiles = dict(zip(valid_tickers, executor.map(fetch_profile, valid_tickers)))

    results = []
    for index, symbol in enumerate(stock_list, 1):
        print(f"[{index}/{total}] 分析 {symbol}...", end=" ", flush=True)
        try:
            info = analyze_stock(symbol, daily_data, weekly_data, profiles, settings)
            if info:
                results.append(info)
                print("命中")
            else:
                print("無訊號")
        except Exception as exc:
            print(f"失敗: {exc}")
        if settings.delay > 0:
            time.sleep(settings.delay)

    summary = build_summary(results, total)
    write_smc_dashboard(results, total, settings)
    if settings.send_telegram:
        send_tg_msg(settings, summary)
        if results:
            detail = "<b>SMC 訊號清單</b>\n\n" + "\n\n".join(format_stock_signal(item) for item in results)
            for part in chunk_message(detail):
                send_tg_msg(settings, part)
                time.sleep(0.5)
            send_signal_charts(settings, results)
        else:
            send_tg_msg(settings, "本次沒有符合 SMC 條件的股票。")
    else:
        print("SMC Telegram 通知已關閉，僅更新網頁儀表板。")

    print(f"完成，命中 {len(results)} / {total} 檔")


if __name__ == "__main__":
    main()
