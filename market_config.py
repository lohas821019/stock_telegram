from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class MarketConfig:
    key: str
    label: str
    stock_file: str
    names: Dict[str, str]
    industry_map: Dict[str, str]

    def ticker_candidates(self, symbol: str) -> List[str]:
        symbol = symbol.strip().upper()
        if "." in symbol:
            return [symbol]
        if self.key == "tw" and symbol.isdigit():
            return [f"{symbol}.TW", f"{symbol}.TWO"]
        return [symbol]

    @staticmethod
    def clean_symbol(symbol: str) -> str:
        return symbol.replace(".TW", "").replace(".TWO", "").upper()


TW_NAMES = {
    "0050": "元大台灣50", "006208": "富邦台50", "0052": "富邦科技",
    "2330": "台積電", "2454": "聯發科", "2317": "鴻海", "2408": "南亞科", "3163": "波若威", "3363": "上詮", "3293": "鈊象", "3008": "大立光", "3017": "奇鋐", "5274": "信驊", "6643": "M31", "6409": "旭隼", "6669": "緯穎", "3661": "世芯-KY", "3529": "譜瑞-KY", "2382": "廣達", "3231": "緯創", "2308": "台達電", "2303": "聯電", "3711": "日月光", "2379": "瑞昱", "3034": "聯詠", "2345": "智邦", "2327": "國巨", "3037": "欣興", "8046": "南電", "4938": "和碩", "2395": "研華", "3443": "創意", "6223": "旺矽", "6510": "精測", "6683": "雍智科技", "7769": "鴻勁", "3653": "健策", "1590": "亞德客-KY", "2049": "上銀", "1476": "儒鴻", "2313": "華通", "2915": "潤泰全", "9945": "潤泰新", "2610": "華航", "2618": "長榮航", "4919": "新唐", "2497": "怡利電", "6531": "愛普", "3167": "大量", "3324": "雙鴻", "6104": "創惟", "3447": "展達", "3036": "文曄", "3665": "貿聯-KY", "2348": "海悅", "2371": "大同", "3189": "景碩", "2383": "台光電", "2368": "金像電", "6147": "頎邦", "8155": "博智", "8299": "群聯", "5289": "宜鼎", "2357": "華碩", "1513": "中興電", "2441": "超豐", "2524": "京城", "6415": "矽力*-KY", "2449": "京元電", "3081": "聯亞", "6715": "嘉基", "6213": "聯茂", "6278": "台表科", "2360": "致茂", "6719": "力智", "2342": "茂矽", "6239": "力成", "3413": "京鼎", "3217": "優群", "6285": "啟碁", "2356": "英業達", "2301": "光寶科", "4768": "晶呈科技", "6217": "中探針", "6863": "永道-KY", "5522": "遠雄",
}
US_NAMES = {
    "NVDA": "輝達", "AMD": "超微", "AVGO": "博通", "SMCI": "美超微", "ARM": "安謀", "TSM": "台積電ADR", "MU": "美光", "ASML": "艾司摩爾", "INTC": "英特爾", "APH": "安費諾", "AMAT": "應材", "QCOM": "高通", "MRVL": "美滿", "PI": "英頻傑", "SNPS": "新思科技", "CLS": "天弘", "SIMO": "慧榮", "AAPL": "蘋果", "MSFT": "微軟", "GOOGL": "谷歌", "AMZN": "亞馬遜", "META": "臉書", "TSLA": "特斯拉", "NFLX": "網飛", "PLTR": "帕蘭泰爾", "OKLO": "Oklo(核能)", "SMR": "NuScale(核能)", "LEU": "Centrus(鈾濃縮)", "UUUU": "Energy Fuels(鈾礦)", "QBTS": "D-Wave(量子)",
}
COMMON_INDUSTRIES = {"Semiconductors": "半導體", "Semiconductor Equipment & Materials": "半導體設備/材料", "Computer Hardware": "電腦硬體/AI伺服器", "Consumer Electronics": "消費電子", "Electronic Components": "電子零組件", "Communication Equipment": "光通訊/CPO/網通", "Internet Content & Information": "軟體/網路服務"}
US_INDUSTRIES = {**COMMON_INDUSTRIES, "Software—Infrastructure": "基礎設施軟體/雲端", "Software—Application": "應用軟體/AI服務", "Auto Manufacturers": "汽車製造/電動車", "Utilities—Independent Power Producers": "核能發電/能源", "Uranium": "鈾礦能源", "Other Industrial Metals & Mining": "工業金屬/採礦", "Scientific & Technical Instruments": "科學技術儀器", "Information Technology Services": "IT資訊服務"}
MARKETS = {"tw": MarketConfig("tw", "台股", "stocks.txt", TW_NAMES, {**COMMON_INDUSTRIES, "Specialty Industrial Machinery": "自動化設備", "Solar": "太陽能/綠能", "Auto Parts": "汽車零件"}), "us": MarketConfig("us", "美股", "stocks_US.txt", US_NAMES, US_INDUSTRIES)}
