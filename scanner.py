import configparser
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd
import requests
import yfinance as yf

from market_config import MARKETS, MarketConfig

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "rsi_alert_state.json"
TW_NAME_CACHE_FILE = BASE_DIR / "tw_stock_names_cache.json"
TWSE_ISIN_URLS = (
    "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",
    "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",
)
ETF_INDICATOR_TICKERS = ("0050", "0052", "006208")
REPORT_TYPES = ("rsi", "kd", "etf", "group", "strong", "pattern", "smc", "completion")


@dataclass(frozen=True)
class Settings:
    token: str
    chat_id: str
    max_workers: int = 8
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    enabled_reports: tuple[str, ...] = REPORT_TYPES
    market_reports: Dict[str, tuple[str, ...]] = None


@dataclass(frozen=True)
class Signal:
    timeframe: str
    direction: str
    value: float
    message: str


def load_settings(config_path: Path = BASE_DIR / "config.ini") -> Settings:
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8")
    telegram = config["Telegram"] if config.has_section("Telegram") else {}
    values = config["Settings"] if config.has_section("Settings") else {}
    reports = config["Reports"] if config.has_section("Reports") else {}
    token = os.getenv("TELEGRAM_BOT_TOKEN", telegram.get("token", ""))
    chat_id = os.getenv("TELEGRAM_CHAT_ID", telegram.get("chat_id", ""))
    if not token or not chat_id:
        raise RuntimeError("請設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，或 config.ini 的 [Telegram]。")
    def parse_reports(value: str) -> tuple[str, ...]:
        selected = tuple(item.strip().lower() for item in value.split(",") if item.strip())
        invalid = set(selected) - set(REPORT_TYPES)
        if invalid:
            raise RuntimeError(f"[Reports] 有不支援的報告類別: {', '.join(sorted(invalid))}。可用類別: {', '.join(REPORT_TYPES)}")
        return selected

    enabled_reports = parse_reports(reports.get("enabled", ",".join(REPORT_TYPES)))
    market_reports = {
        market: parse_reports(reports[f"{market}_enabled"])
        for market in ("tw", "us")
        if f"{market}_enabled" in reports
    }
    return Settings(token, chat_id, int(values.get("max_workers", 8)), int(values.get("rsi_period", 14)), float(values.get("rsi_oversold", 30)), float(values.get("rsi_overbought", 70)), enabled_reports, market_reports)


def send_tg_msg(settings: Settings, message: str) -> None:
    try:
        response = requests.post(f"https://api.telegram.org/bot{settings.token}/sendMessage", data={"chat_id": settings.chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"TG 連線失敗: {error}")


def enabled_reports_for_market(market: MarketConfig, settings: Settings) -> tuple[str, ...]:
    """Return the configured Telegram report categories for one market."""
    reports = settings.market_reports.get(market.key, settings.enabled_reports) if settings.market_reports else settings.enabled_reports
    return tuple(report for report in reports if report != "smc" or market.key == "tw")


def run_smc_report() -> None:
    """Run the standalone Taiwan SMC scanner only when its report category is selected."""
    from main_smc import main as smc_main
    smc_main()


def load_stocks(path: Path) -> List[str]:
    if not path.exists():
        return []
    return list(dict.fromkeys(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")))


def download_histories(tickers: List[str], period: str, interval: str) -> Dict[str, pd.DataFrame]:
    if not tickers:
        return {}
    try:
        data = yf.download(tickers=tickers, period=period, interval=interval, auto_adjust=True, group_by="ticker", threads=True, progress=False)
    except Exception as error:
        print(f"批次下載失敗 ({interval}): {error}")
        return {}
    if data.empty:
        return {}
    if not isinstance(data.columns, pd.MultiIndex):
        return {tickers[0]: data.dropna(how="all")} if "Close" in data else {}
    return {ticker: data[ticker].dropna(how="all") for ticker in tickers if ticker in data.columns.get_level_values(0) and not data[ticker].dropna(how="all").empty and "Close" in data[ticker]}


def fetch_profile(ticker: str) -> Dict[str, Optional[str]]:
    try:
        info = yf.Ticker(ticker).info
    except Exception:
        info = {}
    return {"shortName": info.get("shortName"), "industry": info.get("industry", "其他")}


def calculate_k(df: pd.DataFrame) -> Optional[float]:
    if "Close" not in df or "Low" not in df or "High" not in df or len(df) < 15:
        return None
    low, high = df["Low"].rolling(9).min(), df["High"].rolling(9).max()
    value = ((df["Close"] - low) / (high - low).replace(0, pd.NA) * 100).ewm(com=2).mean().iloc[-1]
    return None if pd.isna(value) else float(value)


def calculate_rsi(df: pd.DataFrame, period: int) -> Optional[float]:
    if "Close" not in df or len(df) < period + 1:
        return None
    delta = df["Close"].diff()
    gains, losses = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    if avg_loss.iloc[-1] == 0 and avg_gain.iloc[-1] > 0:
        return 100.0
    value = 100 - 100 / (1 + avg_gain / avg_loss.replace(0, pd.NA))
    return None if pd.isna(value.iloc[-1]) else float(value.iloc[-1])


def check_n_pattern(df: pd.DataFrame) -> bool:
    if "Close" not in df or len(df) < 30:
        return False
    close, window = df["Close"], df["Close"].iloc[-25:-5]
    peak, index = window.max(), window.idxmax()
    try:
        pos = close.index.get_loc(index)
        if close.iloc[pos - 2] > peak or close.iloc[pos + 2] > peak:
            return False
    except (IndexError, KeyError):
        return False
    return close.iloc[pos:-1].min() > close.iloc[-30:pos].min() and 0.98 <= close.iloc[-1] / peak <= 1.03


def check_w_pattern(df: pd.DataFrame) -> bool:
    if "Close" not in df or len(df) < 40:
        return False
    close, left, right = df["Close"], df["Close"].iloc[-40:-20], df["Close"].iloc[-20:-2]
    low1, low2 = left.min(), right.min()
    if not low1 or abs(low1 - low2) / low1 > 0.05:
        return False
    neckline = close.loc[left.idxmin():right.idxmin()]
    return len(neckline) >= 3 and 0.98 <= close.iloc[-1] / neckline.max() <= 1.04


def rsi_zone(value: Optional[float], oversold: float, overbought: float) -> str:
    if value is None:
        return "unavailable"
    return "oversold" if value < oversold else "overbought" if value > overbought else "normal"


def load_state() -> Dict[str, str]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_tw_name_cache() -> Dict[str, str]:
    try:
        cache = json.loads(TW_NAME_CACHE_FILE.read_text(encoding="utf-8"))
        return {str(code): str(name) for code, name in cache.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def parse_twse_names(page: str) -> Dict[str, str]:
    """Parse the official ISIN list; its first cell is '<code> <Chinese name>'."""
    names: Dict[str, str] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.IGNORECASE | re.DOTALL)
        if not cells:
            continue
        text = html.unescape(re.sub(r"<[^>]+>", " ", cells[0])).replace("\xa0", " ")
        match = re.match(r"\s*(\d{4,6})\s+(.+?)\s*$", " ".join(text.split()))
        if match:
            names[match.group(1)] = match.group(2)
    return names


def resolve_tw_official_names(symbols: Iterable[str]) -> Dict[str, str]:
    """Return cached official names and fetch only codes that the cache lacks."""
    requested = {MarketConfig.clean_symbol(symbol) for symbol in symbols if MarketConfig.clean_symbol(symbol).isdigit()}
    cache = load_tw_name_cache()
    missing = requested - cache.keys()
    if not missing:
        return {code: cache[code] for code in requested}

    fetched: Dict[str, str] = {}
    for url in TWSE_ISIN_URLS:
        try:
            response = requests.get(url, timeout=20, headers={"User-Agent": "stock-telegram-name-resolver/1.0"})
            response.raise_for_status()
            fetched.update(parse_twse_names(response.text))
        except requests.RequestException as error:
            print(f"台股正式名稱查詢失敗: {error}")
    if fetched:
        cache.update(fetched)
        TW_NAME_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    unresolved = missing - fetched.keys()
    if unresolved:
        print(f"⚠️ 無法取得正式中文名稱: {', '.join(sorted(unresolved))}")
    return {code: cache[code] for code in requested if code in cache}


def rsi_transition_signals(market: str, ticker: str, values: Dict[str, Optional[float]], settings: Settings, state: Dict[str, str]) -> List[Signal]:
    signals, labels = [], {"day": "日", "week": "週"}
    for timeframe, value in values.items():
        zone = rsi_zone(value, settings.rsi_oversold, settings.rsi_overbought)
        if zone == "unavailable":
            continue
        key, old = f"{market}:{ticker}:rsi:{timeframe}", state.get(f"{market}:{ticker}:rsi:{timeframe}", "normal")
        state[key] = zone
        if zone == old:
            continue
        if zone in {"oversold", "overbought"}:
            signals.append(Signal(timeframe, zone, value, f"{labels[timeframe]} RSI: {value:.1f}（進入{'超賣' if zone == 'oversold' else '超買'}）"))
        elif old in {"oversold", "overbought"}:
            signals.append(Signal(timeframe, "normal", value, f"{labels[timeframe]} RSI: {value:.1f}（離開{'超賣' if old == 'oversold' else '超買'}區）"))
    return signals


def analyze_stock(symbol: str, market: MarketConfig, daily: Dict[str, pd.DataFrame], weekly: Dict[str, pd.DataFrame], profiles: Dict[str, Dict[str, Optional[str]]], settings: Settings, state: Dict[str, str], official_names: Optional[Dict[str, str]] = None) -> Optional[Dict]:
    ticker = next((item for item in market.ticker_candidates(symbol) if item in daily), None)
    if not ticker or daily[ticker].empty:
        return None
    day, week = daily[ticker], weekly.get(ticker, pd.DataFrame())
    profile = profiles.get(ticker, {})
    price, previous = float(day["Close"].iloc[-1]), float(day["Close"].iloc[-2]) if len(day) > 1 else float(day["Close"].iloc[-1])
    rsi = {"day": calculate_rsi(day, settings.rsi_period), "week": calculate_rsi(week, settings.rsi_period)}
    names = official_names or {}
    return {"name": names.get(market.clean_symbol(symbol)) or market.names.get(market.clean_symbol(symbol)) or profile.get("shortName") or symbol, "ticker": ticker, "price": price, "change": (price - previous) / previous * 100 if previous else 0, "group": market.industry_map.get(profile.get("industry", "其他"), profile.get("industry", "其他")), "kd": {"day": calculate_k(day), "week": calculate_k(week)}, "rsi": rsi, "rsi_signals": rsi_transition_signals(market.key, ticker, rsi, settings, state), "patterns": {"day_n": check_n_pattern(day), "day_w": check_w_pattern(day), "week_n": check_n_pattern(week), "week_w": check_w_pattern(week)}}


def stock_link(info: Dict) -> str:
    return f"<a href='https://tw.stock.yahoo.com/quote/{html.escape(info['ticker'])}'>{html.escape(info['name'])} ({html.escape(info['ticker'])})</a>"


def build_rsi_alerts(results: Iterable[Dict]) -> List[str]:
    return [f"⚠️ {stock_link(info)}\n    " + "\n    ".join(signal.message for signal in info["rsi_signals"]) + f"\n    現價: {info['price']:.1f}\n" for info in results if info["rsi_signals"]]


def build_kd_alerts(results: Iterable[Dict]) -> List[str]:
    labels, output = {"day": "日", "week": "週"}, []
    for info in results:
        lows = [(frame, value) for frame, value in info["kd"].items() if value is not None and value < 20]
        if lows:
            output.append(f"⚠️ {stock_link(info)}\n" + "".join(f"    {'🔴' if frame == 'week' else '🟡'} {labels[frame]} K 值: {value:.1f}\n" for frame, value in lows) + f"    現價: {info['price']:.1f}\n")
    return output


def build_etf_indicator_summary(results: Iterable[Dict]) -> Optional[str]:
    """Build the always-on KD/RSI snapshot for the three tracked Taiwan ETFs."""
    indicators = {MarketConfig.clean_symbol(info["ticker"]): info for info in results}
    lines = ["<b>📌 ETF KD／RSI 指標摘要</b>", "━━━━━━━━━━━━━━━"]
    for symbol in ETF_INDICATOR_TICKERS:
        info = indicators.get(symbol)
        if not info:
            continue
        value = lambda source, timeframe: "--" if source[timeframe] is None else f"{source[timeframe]:.1f}"
        lines.extend((
            f"<b>{html.escape(symbol)} {html.escape(info['name'])}</b> | 現價: {info['price']:.1f}",
            f"    日 KD: {value(info['kd'], 'day')} | RSI: {value(info['rsi'], 'day')}",
            f"    週 KD: {value(info['kd'], 'week')} | RSI: {value(info['rsi'], 'week')}",
        ))
    return "\n".join(lines) if len(lines) > 2 else None


def build_pattern_alerts(results: Iterable[Dict]) -> List[str]:
    labels = {"day_n": "日N型觀察(近前高)", "day_w": "日W型觀察(近頸線)", "week_n": "週N大波段預備", "week_w": "週W長線大底預備"}
    output = []
    for info in results:
        matches = [label for key, label in labels.items() if info["patterns"][key]]
        if matches:
            change = f"+{info['change']:.1f}%" if info["change"] >= 0 else f"{info['change']:.1f}%"
            output.append(f"🔍 {stock_link(info)}\n    現價: {info['price']:.1f} ({change})\n    狀態: {' | '.join(matches)}\n")
    return output


def build_group_summary(results: Iterable[Dict]) -> Optional[str]:
    groups: Dict[str, List[float]] = {}
    for info in results:
        if info["change"] >= 5:
            groups.setdefault(info["group"], []).append(info["change"])
    if not groups:
        return None
    lines = ["<b>📊 今日族群熱度排行榜</b>", "━━━━━━━━━━━━━━━"]
    for group, changes in sorted(groups.items(), key=lambda item: len(item[1]), reverse=True):
        lines.append(f"🏷️ <b>{html.escape(group)}</b> ({len(changes)}檔) | 平均 +{sum(changes) / len(changes):.1f}%")
    return "\n".join(lines)


def build_report_messages(market: MarketConfig, results: Iterable[Dict], stocks: Iterable[str], settings: Settings) -> Dict[str, str]:
    """Build every available Telegram report; sending is selected by config."""
    results, stocks = list(results), list(stocks)
    messages: Dict[str, str] = {}
    rsi_alerts = build_rsi_alerts(results)
    if rsi_alerts:
        messages["rsi"] = f"<b>📊 {market.label} RSI 狀態提醒</b>\n━━━━━━━━━━━━━━━\n" + "\n".join(rsi_alerts)
    kd_alerts = build_kd_alerts(results)
    if kd_alerts:
        messages["kd"] = f"<b>📉 {market.label} KD 指標低檔提醒</b>\n━━━━━━━━━━━━━━━\n" + "\n".join(kd_alerts)
    if market.key == "tw":
        etf_indicator_summary = build_etf_indicator_summary(results)
        if etf_indicator_summary:
            messages["etf"] = etf_indicator_summary
    group_summary = build_group_summary(results)
    if group_summary:
        messages["group"] = group_summary
    strong = [item for item in results if item["change"] >= 5]
    if strong:
        messages["strong"] = f"<b>🔥 {market.label} 強勢標兵</b>\n━━━━━━━━━━━━━━━\n" + "\n".join(f"🚀 {stock_link(item)}\n    現價: {item['price']:.1f} | 漲幅: +{item['change']:.1f}% | #{html.escape(item['group'])}\n" for item in strong)
    pattern_alerts = build_pattern_alerts(results)
    if pattern_alerts:
        messages["pattern"] = f"<b>🔍 {market.label}形態學篩選報告</b>\n━━━━━━━━━━━━━━━\n" + "\n".join(pattern_alerts)
    messages["completion"] = f"<b>✅ {market.label}掃描完成</b>\n━━━━━━━━━━━━━━━\n📅 日期: {time.strftime('%Y-%m-%d')}\n📊 成功監控: {len(results)} / {len(stocks)} 檔"
    return messages


def run_market(market_key: str) -> None:
    market, settings = MARKETS[market_key], load_settings()
    stocks, state = load_stocks(BASE_DIR / market.stock_file), load_state()
    official_names = resolve_tw_official_names(stocks) if market.key == "tw" else {}
    candidates = {symbol: market.ticker_candidates(symbol) for symbol in stocks}
    tickers = list(dict.fromkeys(ticker for group in candidates.values() for ticker in group))
    print(f"🚀 開始{market.label}掃描（共 {len(stocks)} 檔）")
    daily, weekly = download_histories(tickers, "1y", "1d"), download_histories(tickers, "3y", "1wk")
    valid = [ticker for ticker in tickers if ticker in daily]
    with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
        profiles = dict(zip(valid, executor.map(fetch_profile, valid)))
    results = [info for index, symbol in enumerate(stocks, 1) if (info := analyze_stock(symbol, market, daily, weekly, profiles, settings, state, official_names))]
    for info in results:
        print(f"✅ {info['ticker']}")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    messages = build_report_messages(market, results, stocks, settings)
    enabled_reports = enabled_reports_for_market(market, settings)
    for report_type in enabled_reports:
        if message := messages.get(report_type):
            send_tg_msg(settings, message)
    if market.key == "tw" and "smc" in enabled_reports:
        print("🚀 啟用 SMC 篩選報告")
        run_smc_report()
