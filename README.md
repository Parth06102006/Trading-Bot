LR21 Trading Bot Strategy and Implementation

1. STRATEGY DESCRIPTION
The bot implements a dual-signal RSI scalping and momentum strategy tailored for the Asset Alpha Exchange. 
- RSI Scalping: Evaluates RSI over a 14-period window. Long signals are generated when RSI indicates an oversold condition (threshold ~48-55), and exit/sell signals occur when RSI indicates overbought territory (~52-45).
- Momentum Filter: Uses a 3-period average price comparison to detect short-term price movements. It confirms signals only when momentum aligns with the RSI trend.
- Restrictions: The bot strictly follows the "No Short Selling" rule, only executing SELL orders if it currently holds a long position.

2. POSITION SIZING
- Capital Allocation: The bot risks 2% of the available wallet balance per trade.
- Leverage: Fixed at 10x to balance return potential with liquidation risk.
- Order Requirements: All quantities are floored to the nearest whole number to comply with the "No Fractional Shares" rule.

3. RISK MANAGEMENT
- Take Profit / Stop Loss: Managed via an autonomous background monitor that calculates ROE-based exits. The target is 1.0% Profit and 0.5% Stop Loss (adjusted for leverage).
- Capital Exposure: A hard limit prevents the bot from deploying more than 60% of total net worth into active positions at any time.
- Daily Limits: The bot monitors daily PnL and will automatically pause trading if a 2.0% daily loss limit is reached.
- Rate Limiting: Adheres to the 10-second tick rule, ensuring only one trade action occurs per market update.

TECHNICAL COMPLIANCE
- Credentials: Reads API_URL and TEAM_API_KEY exclusively from environment variables.
- Stability: Includes exponential backoff retry logic and graceful HTTP 429 (Rate Limit) handling.
- Termination: Handles KeyboardInterrupt for clean shutdown and position closing.
