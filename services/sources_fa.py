"""Persian names of the news outlets (feed names are English or carry suffixes like '(Google News)')."""
import re


SOURCE_FA = [("Bloomberg", "بلومبرگ"), ("Reuters", "رویترز"), ("Financial Times", "فایننشال تایمز"),
             ("Wall Street Journal", "وال‌استریت ژورنال"), ("Asharq Al-Awsat", "الشرق الاوسط"), ("Asharq", "الشرق"),
             ("Foreign Policy", "فارن پالیسی"), ("Al Jazeera", "الجزیره"), ("OilPrice", "اویل‌پرایس"),
             ("The Guardian", "گاردین"), ("BBC Persian", "بی‌بی‌سی فارسی"), ("BBC", "بی‌بی‌سی"),
             ("New York Times", "نیویورک تایمز")]


def source_fa(item: dict) -> str:
    name = item.get("source", "")
    for prefix, fa in SOURCE_FA:
        if name.startswith(prefix):
            return fa
    return re.sub(r"\s*\(.*?\)\s*$", "", name).strip()  # e.g. "دنیای اقتصاد (Google News)" -> "دنیای اقتصاد"
