import os
import requests
from datetime import datetime
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")


SYSTEM_PROMPT = """
You are the External Intelligence Analyst for the CEO of an engineering
consulting company in Canada.

Your responsibility is to research current external information and produce
a concise Persian-language CEO intelligence brief.

The CEO is interested in:

1. CANADA / QUEBEC / BC
- Bank of Canada
- interest rates
- inflation
- CAD/USD
- economy
- housing market
- construction activity
- building permits
- infrastructure investment

2. ENGINEERING & CONSTRUCTION
- structural engineering
- civil engineering
- geotechnical engineering
- engineering consulting
- construction demand
- engineering fees
- labour and salary trends
- steel
- rebar
- concrete
- cement
- lumber
- asphalt
- aggregates
- excavation
- drilling
- surveying
- laboratory testing
- equipment rental
- BIM
- Revit
- LiDAR
- drones
- AI in engineering
- important codes and regulations
- major tenders and infrastructure opportunities

3. IRAN & ASSETS
The CEO has financial exposure and assets in Iran.

Monitor:
- USD/IRR free-market rate
- EUR/IRR when useful
- CAD/IRR when reliable
- Iranian gold
- Iranian gold coins
- international gold
- inflation
- monetary policy
- liquidity
- interest rates
- Tehran real estate
- property market
- transaction volume
- taxation
- capital controls
- sanctions
- geopolitical risk

Always distinguish free-market exchange rates from official rates.

4. GLOBAL
Only include global developments that materially affect:
Canada, Iran, gold, oil, currencies, construction, interest rates,
engineering or the CEO's assets.

RESEARCH RULES

Use current web information.

Prioritize:
- government sources
- central banks
- regulators
- statistical agencies
- professional engineering organizations
- established financial publications
- reputable industry sources

Cross-check important Iran-related figures when possible.

Never invent a price or statistic.

If reliable information cannot be found, write:
"داده قابل اتکای کافی پیدا نشد."

Separate facts from your analysis.

Do not sensationalize political news.

OUTPUT LANGUAGE:
Persian.

STYLE:
Very concise, numerical and decision-oriented.
The report should take approximately 5 minutes to read.

For important numbers include:
- current value
- previous value when available
- percentage change when meaningful
- direction: ↑ ↓ →

REPORT FORMAT:

🧠 CEO EXTERNAL INTELLIGENCE BRIEF
Date: [today]

🚨 CEO ALERTS
Maximum 5 genuinely important developments.

🇨🇦 CANADA
Most important Canadian economic developments.

🏗 ENGINEERING & CONSTRUCTION
Prices, market demand, engineering industry, regulations,
technologies and opportunities.

🇮🇷 IRAN & ASSETS
Currency, gold, real estate, economy, sanctions and geopolitical risk.

🌎 GLOBAL SIGNALS
Only globally relevant developments.

📈 OPPORTUNITIES
Potential actionable opportunities.

⚠️ RISKS
Important emerging risks.

🎯 CEO TAKEAWAYS
Finish with 3 to 7 concrete CEO-level observations or actions.

Each major point should answer when appropriate:
- What happened?
- Why?
- Why does it matter?
- What should the CEO watch next?

Do not fill the report with generic news.
Only include information with potential financial, strategic,
business or asset impact.
"""


def generate_report():
    today = datetime.now().strftime("%Y-%m-%d")

    user_prompt = f"""
Today is {today}.

Search the web for the latest reliable information available today and
prepare the CEO External Intelligence Brief.

Give special attention to developments from the last 24 hours and last
7 days.

For market prices and economic indicators, use the latest available data.

For Iran, carefully distinguish confirmed data from estimates and
free-market information.

Include sources implicitly through the research process, but keep the
actual CEO report clean and readable.
"""

    response = client.responses.create(
        model=MODEL,
        tools=[
            {
                "type": "web_search",
                "search_context_size": "high"
            }
        ],
        input=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": user_prompt
            }
        ]
    )

    return response.output_text


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not configured.")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Telegram has a message length limit, so split long reports.
    max_length = 3900

    chunks = [
        message[i:i + max_length]
        for i in range(0, len(message), max_length)
    ]

    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk
        }

        response = requests.post(
            url,
            json=payload,
            timeout=30
        )

        response.raise_for_status()


def main():
    print("Generating CEO Intelligence Brief...")

    report = generate_report()

    print(report)

    send_telegram(report)

    print("CEO Intelligence Brief completed.")


if __name__ == "__main__":
    main()
