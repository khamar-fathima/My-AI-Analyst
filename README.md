# Daily AI Intelligence Brief Automation (Telegram Edition)

This repository contains an automated pipeline that fetches AI news from strict designated sources, generates a briefing using the Gemini API, and sends it directly to your Telegram chat.

## Setup Instructions

1. **API Keys required**:
   * Gemini API Key (from Google AI Studio)
   * Telegram Bot Token (from `@BotFather` on Telegram)
   * Telegram Chat ID (your personal user ID from a bot like `@userinfobot`)

2. **GitHub Secrets Configuration**:
   Go to your repository settings -> Secrets and variables -> Actions.
   Add the following secrets:
   * `GEMINI_API_KEY`
   * `TELEGRAM_BOT_TOKEN`
   * `TELEGRAM_CHAT_ID`

3. **Running the Pipeline**:
   The action is scheduled to run daily at 07:00 AM IST (01:30 AM UTC). You can also run it manually from the 'Actions' tab in GitHub.
